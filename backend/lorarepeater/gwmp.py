"""Semtech UDP packet-forwarder protocol (GWMP v2) multiplexer.

The local `lora_pkt_fwd` (sx1302_hal) talks to us on one UDP port. We

* acknowledge its PUSH_DATA / PULL_DATA,
* log every rxpk / stat into the database,
* forward PUSH_DATA to every enabled upstream (ChirpStack, TTN, Helium, ...),
* keep a PULL_DATA keep-alive open to each upstream so they can send PULL_RESP (downlinks),
* relay the first PULL_RESP for a given uplink back to the forwarder and route the TX_ACK
  back to the upstream that sent it.

Protocol reference: https://github.com/Lora-net/packet_forwarder/blob/master/PROTOCOL.TXT

Packet layout: [version=2][token hi][token lo][identifier][gateway EUI (8 bytes, PUSH/PULL_DATA/TX_ACK)][JSON]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import GwmpConfig, UpstreamConfig
from .db import Database
from .lorawan import NO_VENDORS, Vendors, decode_phy, lora_airtime_ms

log = logging.getLogger(__name__)

RxHook = Callable[[dict[str, Any]], bool]   # returns True to claim an rxpk (not LoRaWAN)

PUSH_DATA, PUSH_ACK, PULL_DATA, PULL_RESP, PULL_ACK, TX_ACK = range(6)
RESOLVE_S = 300


@dataclass
class Upstream:
    cfg: UpstreamConfig
    transport: asyncio.DatagramTransport | None = None
    addr: tuple[str, int] | None = None   # resolved; network servers rotate their addresses
    last_push_ack: float = 0.0
    last_pull_ack: float = 0.0
    # token -> (time, kind) for outstanding requests
    pending: dict[int, tuple[float, int]] = field(default_factory=dict)
    # tokens of PULL_RESP we relayed to the forwarder, so TX_ACK can be routed back
    downlink_tokens: dict[int, float] = field(default_factory=dict)
    was_alive: bool = False

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def alive(self) -> bool:
        return (time.time() - self.last_pull_ack) < 60


class _UpstreamProtocol(asyncio.DatagramProtocol):
    def __init__(self, mux: GwmpMux, up: Upstream):
        self.mux, self.up = mux, up

    def connection_made(self, transport):  # type: ignore[override]
        self.up.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        self.mux.loop.create_task(self.mux.on_upstream_datagram(self.up, data))

    def error_received(self, exc):  # type: ignore[override]
        log.warning("upstream %s socket error: %s", self.up.name, exc)


class _ForwarderProtocol(asyncio.DatagramProtocol):
    def __init__(self, mux: GwmpMux):
        self.mux = mux

    def connection_made(self, transport):  # type: ignore[override]
        self.mux.fwd_transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        self.mux.loop.create_task(self.mux.on_forwarder_datagram(data, addr))


class GwmpMux:
    def __init__(self, cfg: GwmpConfig, db: Database, vendors: Vendors = NO_VENDORS):
        self.vendors = vendors
        self.cfg = cfg
        self.db = db
        self.loop = asyncio.get_event_loop()
        self.fwd_transport: asyncio.DatagramTransport | None = None
        self.pull_addr: tuple[str, int] | None = None   # where PULL_DATA came from (downlink target)
        self.gateway_eui: bytes = bytes.fromhex(cfg.gateway_eui) if cfg.gateway_eui else b""
        self.upstreams: list[Upstream] = [Upstream(u) for u in cfg.upstreams if u.enabled]
        self.counters = {"rxpk": 0, "txpk": 0, "stat": 0, "push": 0, "pull": 0}
        self.last_forwarder_seen = 0.0
        self._tasks: list[asyncio.Task] = []
        self._recent_tx: dict[int, str] = {}   # tmst -> upstream that won the downlink slot
        self.rx_hooks: list[RxHook] = []       # e.g. the Meshtastic engine claiming its channel
        self._local_tokens: dict[int, float] = {}   # tokens of txpk we originated ourselves

    def add_rx_hook(self, fn: RxHook) -> None:
        self.rx_hooks.append(fn)

    # -- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.loop.create_datagram_endpoint(
            lambda: _ForwarderProtocol(self), local_addr=(self.cfg.listen_host, self.cfg.listen_port)
        )
        log.info("GWMP listening on %s:%d", self.cfg.listen_host, self.cfg.listen_port)
        for up in self.upstreams:
            await self.db.upsert_upstream_status(up.name, host=up.cfg.host, port=up.cfg.port)
            await self._connect_upstream(up)
        self._tasks.append(asyncio.create_task(self._pull_loop()))
        await self.db.add_event("gwmp", "info", f"mux started on udp/{self.cfg.listen_port} with {len(self.upstreams)} upstream(s)")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.fwd_transport:
            self.fwd_transport.close()
        for up in self.upstreams:
            if up.transport:
                up.transport.close()

    # -- forwarder side ---------------------------------------------------------------

    async def on_forwarder_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 4 or data[0] != 2:
            return
        token, ident = data[1:3], data[3]
        self.last_forwarder_seen = time.time()
        if ident == PUSH_DATA and len(data) >= 12:
            eui = data[4:12]
            if not self.gateway_eui:
                self.gateway_eui = eui
            self.fwd_transport.sendto(bytes([2, token[0], token[1], PUSH_ACK]), addr)  # type: ignore[union-attr]
            self.counters["push"] += 1
            forward = await self._handle_push(eui, data[12:])
            if forward is not None:
                await self._fanout(data, forward)
        elif ident == PULL_DATA and len(data) >= 12:
            self.pull_addr = addr
            self.counters["pull"] += 1
            self.fwd_transport.sendto(bytes([2, token[0], token[1], PULL_ACK]), addr)  # type: ignore[union-attr]
        elif ident == TX_ACK:
            tok = int.from_bytes(token, "big")
            if tok in self._local_tokens:
                self._local_tokens.pop(tok, None)
                err = json.loads(data[12:] or b"{}").get("txpk_ack", {}).get("error")
                if err and err != "NONE":
                    self.counters["tx_errors"] = self.counters.get("tx_errors", 0) + 1
                    await self.db.add_event("mesh", "warn", f"own transmission rejected by forwarder: {err}")
                return
            for up in self.upstreams:
                if tok in up.downlink_tokens and up.transport:
                    up.downlink_tokens.pop(tok, None)
                    up.transport.sendto(data)
                    break
            err = json.loads(data[12:] or b"{}").get("txpk_ack", {}).get("error")
            if err and err != "NONE":
                log.warning("upstream downlink rejected by the forwarder: %s", err)
            await self.db.add_event("gwmp", "warn", f"TX_ACK error: {err}")

    async def _handle_push(self, eui: bytes, payload: bytes) -> dict[str, Any] | None:
        """Log the forwarder's message. Returns the message to fan out to upstreams (with
        rxpk entries claimed by hooks removed), or None if nothing is left to forward."""
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("bad PUSH_DATA json")
            return None
        kept: list[dict[str, Any]] = []
        for rx in msg.get("rxpk", []):
            self.counters["rxpk"] += 1
            if any(hook(rx) for hook in self.rx_hooks):
                self.counters["claimed"] = self.counters.get("claimed", 0) + 1
                continue
            kept.append(rx)
            row: dict[str, Any] = {
                "direction": "up",
                "gateway_eui": eui.hex().upper(),
                "tmst": rx.get("tmst"), "time": rx.get("time"), "freq": rx.get("freq"),
                "chan": rx.get("chan"), "rfch": rx.get("rfch"), "stat": rx.get("stat"),
                "modu": rx.get("modu"), "datr": rx.get("datr"), "codr": rx.get("codr"),
                "rssi": rx.get("rssi"), "lsnr": rx.get("lsnr"),
                "size": rx.get("size"), "data": rx.get("data"),
                "forwarded": [u.name for u in self.upstreams if u.transport],
            }
            row["airtime_ms"] = lora_airtime_ms(row["datr"], row["codr"], row["size"])
            phy = decode_phy(rx.get("data", ""), self.vendors) if rx.get("data") else None
            if phy:
                row.update(phy.as_row())
            await self.db.add_lorawan_packet(row)
        if "stat" in msg:
            self.counters["stat"] += 1
            st = msg["stat"]
            await self.db.add_gateway_stats({
                "gateway_eui": eui.hex().upper(),
                "time": st.get("time"), "lati": st.get("lati"), "long": st.get("long"), "alti": st.get("alti"),
                "rxnb": st.get("rxnb"), "rxok": st.get("rxok"), "rxfw": st.get("rxfw"), "ackr": st.get("ackr"),
                "dwnb": st.get("dwnb"), "txnb": st.get("txnb"), "temp": st.get("temp"),
            })
        if "rxpk" in msg:
            if kept:
                msg["rxpk"] = kept
            else:
                del msg["rxpk"]
        return msg if msg else None

    async def _fanout(self, data: bytes, msg: dict[str, Any]) -> None:
        # Rewrite EUI if configured, keep the forwarder's token (upstream ACKs are per upstream socket)
        eui = self.gateway_eui if self.cfg.gateway_eui else data[4:12]
        if self.rx_hooks or self.cfg.gateway_eui:
            data = data[:4] + eui + json.dumps(msg, separators=(",", ":")).encode()
        for up in self.upstreams:
            if up.transport:
                tok = int.from_bytes(data[1:3], "big")
                up.pending[tok] = (time.time(), PUSH_DATA)
                up.transport.sendto(data)
                await self.db.bump_upstream(up.name, "push_sent")

    # -- upstream side ----------------------------------------------------------------

    async def _connect_upstream(self, up: Upstream) -> None:
        """(Re)open the upstream socket when the host resolves to a new address."""
        try:
            infos = await self.loop.getaddrinfo(up.cfg.host, up.cfg.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("cannot resolve upstream %s (%s): %s", up.name, up.cfg.host, e)
            return
        addrs = [i[4][:2] for i in infos]
        if up.transport and up.addr in addrs:      # DNS round-robin: keep a still-valid address
            return
        addr = addrs[0]
        if up.transport:
            up.transport.close()
            await self.db.add_event("gwmp", "info", f"upstream {up.name}: {up.cfg.host} moved {up.addr[0]} -> {addr[0]}")
        await self.loop.create_datagram_endpoint(lambda: _UpstreamProtocol(self, up), remote_addr=addr)
        up.addr = addr
        log.info("upstream %s -> %s (%s:%d)", up.name, up.cfg.host, *addr)

    async def _pull_loop(self) -> None:
        """Keep a PULL_DATA keep-alive with each upstream so they can push downlinks; re-resolve
        the upstream hosts every RESOLVE_S."""
        last_resolve = time.monotonic()
        while True:
            if time.monotonic() - last_resolve > RESOLVE_S:
                last_resolve = time.monotonic()
                for up in self.upstreams:
                    await self._connect_upstream(up)
            eui = self.gateway_eui or bytes(8)
            for up in self.upstreams:
                if up.alive != up.was_alive:
                    up.was_alive = up.alive
                    what = "connected (acknowledging)" if up.alive else "lost: no acknowledgement for 60 s"
                    log.info("upstream %s %s", up.name, what)
                    await self.db.add_event("gwmp", "info" if up.alive else "warn", f"upstream {up.name} {what}")
                if up.transport and eui != bytes(8):
                    tok = os.urandom(2)
                    up.pending[int.from_bytes(tok, "big")] = (time.time(), PULL_DATA)
                    up.transport.sendto(bytes([2, tok[0], tok[1], PULL_DATA]) + eui)
                # drop stale tokens
                cutoff = time.time() - 120
                up.pending = {t: v for t, v in up.pending.items() if v[0] > cutoff}
                up.downlink_tokens = {t: v for t, v in up.downlink_tokens.items() if v > cutoff}
            await asyncio.sleep(self.cfg.pull_interval_s)

    async def on_upstream_datagram(self, up: Upstream, data: bytes) -> None:
        if len(data) < 4 or data[0] != 2:
            return
        token, ident = data[1:3], data[3]
        tok = int.from_bytes(token, "big")
        if ident == PUSH_ACK:
            up.last_push_ack = time.time()
            up.pending.pop(tok, None)
            await self.db.upsert_upstream_status(up.name, last_push_ack=up.last_push_ack)
            await self.db.bump_upstream(up.name, "push_acked")
        elif ident == PULL_ACK:
            up.last_pull_ack = time.time()
            up.pending.pop(tok, None)
            await self.db.upsert_upstream_status(up.name, last_pull_ack=up.last_pull_ack)
        elif ident == PULL_RESP:
            await self.db.bump_upstream(up.name, "pull_resp")
            await self._handle_pull_resp(up, token, data[4:])

    async def _handle_pull_resp(self, up: Upstream, token: bytes, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            return
        tx = msg.get("txpk", {})
        self.counters["txpk"] += 1
        row: dict[str, Any] = {
            "direction": "down", "upstream": up.name,
            "gateway_eui": self.gateway_eui.hex().upper() if self.gateway_eui else None,
            "tmst": tx.get("tmst"), "time": tx.get("time"), "freq": tx.get("freq"), "rfch": tx.get("rfch"),
            "modu": tx.get("modu"), "datr": tx.get("datr"), "codr": tx.get("codr"),
            "size": tx.get("size"), "data": tx.get("data"), "forwarded": [],
        }
        row["airtime_ms"] = lora_airtime_ms(row["datr"], row["codr"], row["size"])
        phy = decode_phy(tx.get("data", ""), self.vendors) if tx.get("data") else None
        if phy:
            row.update(phy.as_row())
        log.info("upstream %s downlink %s %s %s MHz %s, %s bytes", up.name, row.get("mtype") or "?",
                 row.get("devaddr") or "", row["freq"], row["datr"], row["size"])
        if up.cfg.rx_only:
            row["forwarded"] = ["dropped:rx_only"]
            await self.db.add_lorawan_packet(row)
            return
        if not self.pull_addr or not self.fwd_transport:
            row["forwarded"] = ["dropped:no_forwarder"]
            await self.db.add_lorawan_packet(row)
            await self.db.add_event("gwmp", "warn", f"downlink from {up.name} dropped: forwarder not pulling")
            return
        # Simple collision rule: first upstream wins; a later PULL_RESP with the same tmst is dropped.
        tmst = tx.get("tmst")
        if tmst is not None and tmst in self._recent_tx:
            row["forwarded"] = ["dropped:collision"]
            await self.db.add_lorawan_packet(row)
            await self.db.add_event("gwmp", "warn", f"downlink collision: {up.name} lost to {self._recent_tx[tmst]}")
            return
        if tmst is not None:
            self._recent_tx[tmst] = up.name
            if len(self._recent_tx) > 64:
                for k in list(self._recent_tx)[:-32]:
                    self._recent_tx.pop(k, None)
        up.downlink_tokens[int.from_bytes(token, "big")] = time.time()
        self.fwd_transport.sendto(bytes([2, token[0], token[1], PULL_RESP]) + payload, self.pull_addr)
        row["forwarded"] = ["radio"]
        await self.db.add_lorawan_packet(row)

    # -- own transmissions (Meshtastic engine) ----------------------------------------

    async def send_txpk(self, txpk: dict[str, Any]) -> bool:
        """Hand a txpk to the packet forwarder as if we were a network server. Returns False
        when the forwarder has not opened its downlink channel (no PULL_DATA seen yet)."""
        if not self.pull_addr or not self.fwd_transport:
            return False
        tok = os.urandom(2)
        self._local_tokens[int.from_bytes(tok, "big")] = time.time()
        if len(self._local_tokens) > 256:
            cutoff = time.time() - 120
            self._local_tokens = {t: v for t, v in self._local_tokens.items() if v > cutoff}
        payload = json.dumps({"txpk": txpk}, separators=(",", ":")).encode()
        self.fwd_transport.sendto(bytes([2, tok[0], tok[1], PULL_RESP]) + payload, self.pull_addr)
        self.counters["own_tx"] = self.counters.get("own_tx", 0) + 1
        return True

    # -- status ----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "listen": f"{self.cfg.listen_host}:{self.cfg.listen_port}",
            "gateway_eui": self.gateway_eui.hex().upper() if self.gateway_eui else None,
            "forwarder_seen_s_ago": round(time.time() - self.last_forwarder_seen, 1) if self.last_forwarder_seen else None,
            "forwarder_pull_addr": f"{self.pull_addr[0]}:{self.pull_addr[1]}" if self.pull_addr else None,
            "counters": dict(self.counters),
            "upstreams": [
                {"name": u.name, "host": u.cfg.host, "port": u.cfg.port, "rx_only": u.cfg.rx_only,
                 "alive": u.alive,
                 "last_pull_ack_s_ago": round(time.time() - u.last_pull_ack, 1) if u.last_pull_ack else None,
                 "last_push_ack_s_ago": round(time.time() - u.last_push_ack, 1) if u.last_push_ack else None}
                for u in self.upstreams
            ],
        }
