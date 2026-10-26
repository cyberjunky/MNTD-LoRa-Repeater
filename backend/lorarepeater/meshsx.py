"""Meshtastic node on the SX1302 concentrator.

The patched packet forwarder listens on its single 250 kHz "service" channel with the
Meshtastic sync word (0x2B) and hands us every frame as a normal `rxpk` through the GWMP mux.
This engine turns the box into a Meshtastic ROUTER_LATE (or ROUTER / CLIENT_MUTE) node:

* decodes and decrypts frames on the configured channels, stores them, updates the node table,
* rebroadcasts what should be rebroadcast (dedup, hop limit, next-hop rules, contention delay,
  EU duty cycle), scheduled precisely on the concentrator's microsecond counter,
* decodes every Meshtastic port the protobufs describe (telemetry of all kinds, traceroute,
  neighbour info, map reports, waypoints, paxcounter, store-and-forward, ...), keeps a node
  table and a link graph (who hears whom, with SNR),
* originates NodeInfo (and optionally a fixed Position), text messages and ACKs,
* calls registered listeners for received text (Telegram, Home Assistant) and for every frame
  on a known channel (the Meshtastic MQTT uplink).

It is deliberately a *repeater* implementation: no traceroute, no store-and-forward, no PKI
direct messages (those are relayed unread), no admin channel.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from google.protobuf import json_format

from .config import MeshSxConfig
from .db import Database
from .gwmp import GwmpMux
from .meshproto import (
    BROADCAST,
    HEADER_LEN,
    MAX_HOPS,
    MAX_TEXT_BYTES,
    Header,
    airtime_ms,
    channel_hash,
    crypt,
    expand_psk,
    node_id_str,
    parse_datr,
)

log = logging.getLogger(__name__)

TextListener = Callable[[dict[str, Any]], None]
# (frame as on air, header, channel, {"rx_time", "rssi", "snr", "ok_to_mqtt"}); rssi/snr None when sent by us
FrameListener = Callable[[bytes, "Header", "Channel", dict[str, Any]], None]


def _pb():
    """Import the Meshtastic protobufs lazily so the rest of the package has no hard dependency."""
    from meshtastic.protobuf import config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2
    return mesh_pb2, portnums_pb2, telemetry_pb2, config_pb2


def derive_node_num() -> int:
    """Node number = the last 32 bits of the Raspberry Pi serial, with a usable low byte."""
    with open("/proc/cpuinfo", encoding="utf-8") as f:
        serial = next(line for line in f if line.startswith("Serial"))
    num = int(serial.split(":")[1].strip()[-8:], 16)
    if (num & 0xFF) in (0x00, 0xFF):  # relay_node byte 0 means "none", 0xFF is reserved
        num = (num & 0xFFFFFF00) | 0x42
    return num


class Channel:
    def __init__(self, index: int, name: str, psk_b64: str):
        self.index = index
        self.name = name
        self.key = expand_psk(psk_b64)
        self.hash = channel_hash(name, self.key)


class MeshSxEngine:
    def __init__(self, cfg: MeshSxConfig, db: Database, mux: GwmpMux):
        self.cfg = cfg
        self.db = db
        self.mux = mux
        self.node_num = cfg.node_num or derive_node_num()
        self.my_byte = self.node_num & 0xFF
        self.channels = [Channel(i, c.name, c.psk) for i, c in enumerate(cfg.channels)]
        self.by_hash = {c.hash: c for c in self.channels}
        self.sf, self.bw_hz = parse_datr(cfg.datr)
        self.cr = int(cfg.codr.split("/")[1])
        self.seen: dict[tuple[int, int], float] = {}
        self.tx_log: deque[tuple[float, float]] = deque()   # (time, airtime_ms)
        self.listeners: list[TextListener] = []
        self.frame_listeners: list[FrameListener] = []
        self.counters = {"rx": 0, "decoded": 0, "relayed": 0, "dropped_dup": 0, "dropped_hops": 0,
                         "dropped_duty": 0, "tx": 0, "text_rx": 0, "undecodable": 0}
        self.last_rx = 0.0
        self.last_tx = 0.0
        self._tasks: list[asyncio.Task] = []
        self._pending: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        self.mux.add_rx_hook(self.on_rxpk)
        self._tasks = [asyncio.create_task(self._beacon_loop(), name="mesh-beacon"),
                       asyncio.create_task(self._housekeeping(), name="mesh-housekeeping")]
        await self.db.add_event("mesh", "info",
                                f"SX1302 Meshtastic node {node_id_str(self.node_num)} '{self.cfg.long_name}' "
                                f"role {self.cfg.role} on {self.cfg.freq_mhz} MHz {self.cfg.datr}, "
                                f"channels {[c.name for c in self.channels]}")
        await self.db.upsert_mesh_node(self._my_node_row())

    async def stop(self) -> None:
        for t in [*self._tasks, *self._pending]:
            t.cancel()

    def add_text_listener(self, fn: TextListener) -> None:
        self.listeners.append(fn)

    def add_frame_listener(self, fn: FrameListener) -> None:
        self.frame_listeners.append(fn)

    def _frame_heard(self, raw: bytes, hdr: Header, chan: Channel, info: dict[str, Any]) -> None:
        for fn in self.frame_listeners:
            try:
                fn(raw, hdr, chan, info)
            except Exception as e:  # a broken uplink must not stop the mesh
                log.warning("frame listener failed: %s", e)

    # -- receive -----------------------------------------------------------------

    def on_rxpk(self, rx: dict[str, Any]) -> bool:
        """Mux hook: claim frames on our channel. Returns True when consumed."""
        try:
            if abs(float(rx.get("freq", 0)) - self.cfg.freq_mhz) > 0.05 or str(rx.get("datr", "")).upper() != self.cfg.datr.upper():
                return False
        except (TypeError, ValueError):
            return False
        if rx.get("stat") == -1:
            self.counters["rx"] += 1
            return True  # CRC failure on our channel: ours, but useless
        task = asyncio.create_task(self._handle(rx))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def _handle(self, rx: dict[str, Any]) -> None:
        self.counters["rx"] += 1
        self.last_rx = time.time()
        try:
            raw = base64.b64decode(rx.get("data", ""))
            hdr = Header.parse(raw)
        except (ValueError, TypeError) as e:
            log.debug("bad mesh frame: %s", e)
            return
        if hdr.sender == self.node_num:
            # our own transmission, picked up by the concentrator's second radio: proves TX, not traffic
            self.counters["self_echo"] = self.counters.get("self_echo", 0) + 1
            return
        rssi, snr = rx.get("rssi"), rx.get("lsnr")
        row: dict[str, Any] = {
            "direction": "rx", "source": "sx1302", "rx_time": int(time.time()),
            "packet_id": hdr.packet_id, "from_node": hdr.sender, "to_node": hdr.to,
            "channel": None, "hop_limit": hdr.hop_limit, "hop_start": hdr.hop_start, "hops_used": hdr.hops_used,
            "relay_node": hdr.relay_node, "next_hop": hdr.next_hop, "rssi": rssi, "snr": snr,
            "via_mqtt": int(hdr.via_mqtt), "want_ack": int(hdr.want_ack), "priority": None,
            "encrypted": 1, "text": None, "payload": None, "portnum": None,
            "airtime_ms": airtime_ms(self.sf, self.bw_hz, self.cr, len(raw)),
        }
        chan = self.by_hash.get(hdr.channel_hash)
        decoded = None
        if chan is not None:
            decoded = self._decode(chan, hdr, raw[HEADER_LEN:])
        ok_to_mqtt = decoded.pop("ok_to_mqtt") if decoded is not None else True
        if decoded is not None:
            self.counters["decoded"] += 1
            row.update(decoded)
            row["channel"] = chan.index
            row["encrypted"] = 0
        else:
            self.counters["undecodable"] += 1
        row["relayed_by_us"] = 0
        row["channel_hash"] = hdr.channel_hash
        dup = (hdr.sender, hdr.packet_id) in self.seen
        if not dup:
            await self._heard(hdr, rssi, snr)
        pkt_id = await self.db.add_mesh_packet(row)
        if decoded is not None and not dup:
            await self._after_decode(chan, hdr, decoded, rssi, snr)
            self._frame_heard(raw, hdr, chan, {"rx_time": row["rx_time"], "rssi": rssi, "snr": snr, "ok_to_mqtt": ok_to_mqtt})
        await self._maybe_relay(hdr, raw, rx, pkt_id, dup)

    def _decode(self, chan: Channel, hdr: Header, body: bytes) -> dict[str, Any] | None:
        mesh_pb2, portnums_pb2, _, _ = _pb()
        try:
            plain = crypt(chan.key, hdr.packet_id, hdr.sender, body)
            data = mesh_pb2.Data()
            data.ParseFromString(plain)
        except Exception:  # wrong key, PKI DM, garbage: all equally undecodable
            return None
        if data.portnum == 0 and not data.payload:
            return None
        try:
            port = portnums_pb2.PortNum.Name(data.portnum)
        except ValueError:
            port = str(data.portnum)
        # firmware >= 2.5: a sender that cleared "OK to MQTT" sets the bitfield without bit 0
        ok_to_mqtt = not data.HasField("bitfield") or bool(data.bitfield & 1)
        out: dict[str, Any] = {"portnum": port, "payload": None, "text": None, "priority": None, "ok_to_mqtt": ok_to_mqtt}
        meta = {k: v for k, v in (("requestId", data.request_id), ("replyId", data.reply_id),
                                  ("wantResponse", data.want_response), ("emoji", data.emoji)) if v}
        try:
            if port in TEXT_PORTS:
                out["text"] = data.payload.decode("utf-8", "replace")
                payload: dict[str, Any] = {}
            elif port in PORT_MESSAGES:
                key, cls = PORT_MESSAGES[port]
                msg = _pb_class(cls)()
                msg.ParseFromString(data.payload)
                payload = {key: _clean(json_format.MessageToDict(msg), key)}
            else:
                payload = {"bytes": len(data.payload), "hex": data.payload[:64].hex()}
        except Exception as e:  # malformed inner protobuf; keep the port at least
            payload = {"error": str(e)[:80], "hex": data.payload[:64].hex()}
        if meta:
            payload["meta"] = meta
        out["payload"] = payload or None
        return out

    async def _after_decode(self, chan: Channel, hdr: Header, dec: dict[str, Any], rssi: Any, snr: Any) -> None:
        port, payload = dec["portnum"], dec.get("payload") or {}
        node = node_fields(payload)
        if node:
            await self.db.upsert_mesh_node({"num": hdr.sender, "node_id": node_id_str(hdr.sender), **node})
        for a, b, source, link_snr in links_from(payload, hdr):
            await self.db.upsert_mesh_link(a, b, source, link_snr)
        if port == "TEXT_MESSAGE_APP" and dec.get("text"):
            self.counters["text_rx"] += 1
            info = {"from": hdr.sender, "to": hdr.to, "channel": chan.index, "channel_name": chan.name,
                    "text": dec["text"], "rssi": rssi, "snr": snr, "hops": hdr.hops_used, "packet_id": hdr.packet_id}
            for fn in self.listeners:
                try:
                    fn(info)
                except Exception as e:  # a broken bridge must not stop the mesh
                    log.warning("text listener failed: %s", e)
        if hdr.to == self.node_num and hdr.want_ack and port != "ROUTING_APP":
            await self.send_ack(hdr.sender, hdr.packet_id, chan)

    async def _heard(self, hdr: Header, rssi: Any, snr: Any) -> None:
        """Header-only facts, available even for frames we cannot decrypt."""
        await self.db.upsert_mesh_node({"num": hdr.sender, "node_id": node_id_str(hdr.sender),
                                        "last_heard": time.time(), "snr": snr, "rssi": rssi,
                                        "hops_away": hdr.hops_used, "via_mqtt": int(hdr.via_mqtt)})
        await self.db.bump_node_packets(hdr.sender)
        if hdr.hops_used == 0 and not hdr.via_mqtt:
            await self.db.upsert_mesh_link(hdr.sender, self.node_num, "rf", snr)

    # -- relay -------------------------------------------------------------------

    async def _maybe_relay(self, hdr: Header, raw: bytes, rx: dict[str, Any], row_id: int, dup: bool) -> None:
        key = (hdr.sender, hdr.packet_id)
        if dup:
            self.counters["dropped_dup"] += 1
            return
        self.seen[key] = time.time()
        if self.cfg.role == "CLIENT_MUTE" or hdr.sender == self.node_num:
            return
        if hdr.hop_limit == 0:
            self.counters["dropped_hops"] += 1
            return
        if hdr.to == self.node_num:
            return  # addressed to us, nothing to forward
        if hdr.to != BROADCAST and hdr.next_hop not in (0, self.my_byte):
            return  # directed message with another preferred relay (next-hop routing, fw >= 2.6)
        new = Header(**{**hdr.__dict__, "hop_limit": hdr.hop_limit - 1, "relay_node": self.my_byte})
        frame = new.pack() + raw[HEADER_LEN:]
        delay_ms = self._contention_delay_ms(rx.get("lsnr"))
        tmst = rx.get("tmst")
        when = (int(tmst) + int(delay_ms * 1000)) & 0xFFFFFFFF if tmst is not None else None
        ok = await self.transmit(frame, tmst=when, kind="relay", row_id=row_id, of=hdr)
        if ok:
            self.counters["relayed"] += 1

    def _contention_delay_ms(self, snr: Any) -> float:
        """Firmware-like contention window: better SNR (closer sender) waits longer so that far
        nodes relay first. ROUTER_LATE adds a full window so clients go before us."""
        try:
            snr = float(snr)
        except (TypeError, ValueError):
            snr = 0.0
        slot = airtime_ms(self.sf, self.bw_hz, self.cr, 0, preamble=16)   # roughly one slot time
        cw_max = 2 + (6 if snr > 5 else 4 if snr > 0 else 2)
        base = random.uniform(0, cw_max) * slot
        if self.cfg.role == "ROUTER_LATE":
            base += cw_max * slot + random.uniform(0, 2) * slot
        return self.cfg.relay_delay_ms + base

    # -- transmit ----------------------------------------------------------------

    def _duty_ok(self, airtime: float) -> bool:
        now = time.time()
        while self.tx_log and now - self.tx_log[0][0] > 3600:
            self.tx_log.popleft()
        used = sum(a for _, a in self.tx_log)
        return used + airtime <= self.cfg.duty_cycle_pct / 100.0 * 3600_000.0

    async def transmit(self, frame: bytes, tmst: int | None = None, kind: str = "tx",
                       row_id: int | None = None, of: Header | None = None, text: str | None = None) -> bool:
        if not self.cfg.tx_enabled:
            return False
        at = airtime_ms(self.sf, self.bw_hz, self.cr, len(frame))
        if not self._duty_ok(at):
            self.counters["dropped_duty"] += 1
            await self.db.add_event("mesh", "warn", f"{kind} dropped: EU duty cycle budget exhausted")
            return False
        txpk = {"imme": tmst is None, "freq": self.cfg.freq_mhz, "rfch": 0, "powe": self.cfg.tx_power_dbm,
                "modu": "LORA", "datr": self.cfg.datr, "codr": self.cfg.codr, "ipol": False, "prea": 16,
                "ncrc": False, "size": len(frame), "data": base64.b64encode(frame).decode()}
        if tmst is not None:
            txpk["tmst"] = tmst
        ok = await self.mux.send_txpk(txpk)
        if not ok:
            await self.db.add_event("mesh", "warn", f"{kind} not sent: packet forwarder not pulling")
            return False
        self.tx_log.append((time.time(), at))
        self.counters["tx"] += 1
        self.last_tx = time.time()
        hdr = of or Header.parse(frame)
        if kind == "relay" and row_id is not None:
            async with self.db._lock:
                await self.db.conn.execute("UPDATE mesh_packets SET relayed_by_us = 1 WHERE id = ?", (row_id,))
                await self.db.conn.commit()
        else:
            await self.db.add_mesh_packet({
                "direction": "tx", "source": "sx1302", "rx_time": int(time.time()), "packet_id": hdr.packet_id,
                "from_node": hdr.sender, "to_node": hdr.to, "channel": 0, "portnum": kind.upper(),
                "hop_limit": hdr.hop_limit, "hop_start": hdr.hop_start, "hops_used": 0, "relay_node": hdr.relay_node,
                "next_hop": hdr.next_hop, "rssi": None, "snr": None, "via_mqtt": 0, "want_ack": int(hdr.want_ack),
                "priority": None, "encrypted": 0, "text": text,
                "payload": None, "airtime_ms": at, "relayed_by_us": 0,
            })
        return True

    def _new_packet_id(self) -> int:
        return (int.from_bytes(os.urandom(4), "little") | 0x80000000) & 0xFFFFFFFF  # never 0

    async def _send_data(self, data: Any, to: int, chan: Channel, want_ack: bool = False, kind: str = "tx",
                         hop_limit: int | None = None, text: str | None = None) -> int | None:
        """Returns the packet id, or None when it was not transmitted (TX off, duty cycle, forwarder)."""
        pid = self._new_packet_id()
        hops = min(MAX_HOPS, self.cfg.hop_limit if hop_limit is None else hop_limit)
        hdr = Header(to=to, sender=self.node_num, packet_id=pid, hop_limit=hops, hop_start=hops,
                     want_ack=want_ack, channel_hash=chan.hash, relay_node=self.my_byte)
        frame = hdr.pack() + crypt(chan.key, pid, self.node_num, data.SerializeToString())
        self.seen[(self.node_num, pid)] = time.time()
        if not await self.transmit(frame, kind=kind, of=hdr, text=text):
            return None
        self._frame_heard(frame, hdr, chan, {"rx_time": int(time.time()), "rssi": None, "snr": None, "ok_to_mqtt": True})
        return pid

    async def send_text(self, text: str, to: int = BROADCAST, channel: int = 0, want_ack: bool = False) -> int | None:
        mesh_pb2, portnums_pb2, _, _ = _pb()
        chan = self.channels[channel]
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("TEXT_MESSAGE_APP"), payload=text.encode("utf-8")[:MAX_TEXT_BYTES])
        return await self._send_data(data, to, chan, want_ack=want_ack, kind="text", text=text)

    async def send_ack(self, to: int, request_id: int, chan: Channel) -> None:
        mesh_pb2, portnums_pb2, _, _ = _pb()
        routing = mesh_pb2.Routing(error_reason=mesh_pb2.Routing.Error.Value("NONE"))
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("ROUTING_APP"), payload=routing.SerializeToString(),
                             request_id=request_id)
        await self._send_data(data, to, chan, kind="ack")

    async def send_nodeinfo(self) -> None:
        mesh_pb2, portnums_pb2, _, config_pb2 = _pb()
        user = mesh_pb2.User(id=node_id_str(self.node_num), long_name=self.cfg.long_name[:39], short_name=self.cfg.short_name[:4],
                             hw_model=mesh_pb2.HardwareModel.Value("PRIVATE_HW"),
                             role=config_pb2.Config.DeviceConfig.Role.Value(self.cfg.role if self.cfg.role != "CLIENT_MUTE" else "CLIENT_MUTE"))
        user.macaddr = bytes([0x02, 0x00]) + self.node_num.to_bytes(4, "big")
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("NODEINFO_APP"), payload=user.SerializeToString())
        await self._send_data(data, BROADCAST, self.channels[0], kind="nodeinfo")

    async def send_position(self) -> bool:
        pos = self.cfg.position
        if pos is None:
            return False
        f = {"lat": pos.lat, "lon": pos.lon, "alt": pos.alt}
        mesh_pb2, portnums_pb2, _, _ = _pb()
        bits = self.cfg.position_precision_bits
        lat_i, lon_i = int(f["lat"] * 1e7), int(f["lon"] * 1e7)
        if 0 < bits < 32:  # same coarsening the firmware applies for "approximate" positions
            mask = ~((1 << (32 - bits)) - 1)
            lat_i, lon_i = lat_i & mask, lon_i & mask
        pos = mesh_pb2.Position(latitude_i=lat_i, longitude_i=lon_i, altitude=int(f.get("alt") or 0),
                                time=int(time.time()), precision_bits=bits or 32)
        data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("POSITION_APP"), payload=pos.SerializeToString())
        await self._send_data(data, BROADCAST, self.channels[0], kind="position")
        return True

    # -- background --------------------------------------------------------------

    async def _beacon_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.send_nodeinfo()
                if self.cfg.position_broadcast:
                    await self.send_position()
            except Exception as e:
                log.warning("beacon failed: %s", e)
            await asyncio.sleep(self.cfg.nodeinfo_interval_s)

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - 600
            self.seen = {k: v for k, v in self.seen.items() if v > cutoff}

    def _my_node_row(self) -> dict[str, Any]:
        row = {"num": self.node_num, "node_id": node_id_str(self.node_num), "long_name": self.cfg.long_name,
               "short_name": self.cfg.short_name, "hw_model": "SX1302 (MNTD)", "role": self.cfg.role,
               "last_heard": time.time(), "hops_away": 0}
        if self.cfg.position:   # exact site position for the local map; the air gets the coarsened one
            row.update(lat=self.cfg.position.lat, lon=self.cfg.position.lon, alt=self.cfg.position.alt)
        return row

    # -- status ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = time.time()
        used = sum(a for t, a in self.tx_log if now - t <= 3600)
        return {
            "enabled": self.cfg.enabled, "node_num": self.node_num, "node_id": node_id_str(self.node_num),
            "long_name": self.cfg.long_name, "max_text_bytes": MAX_TEXT_BYTES,
            "role": self.cfg.role, "freq_mhz": self.cfg.freq_mhz, "datr": self.cfg.datr,
            "channels": [c.name for c in self.channels], "tx_enabled": self.cfg.tx_enabled,
            "duty_used_pct_1h": round(used / 36000.0, 3), "duty_limit_pct": self.cfg.duty_cycle_pct,
            "counters": dict(self.counters),
            "last_rx_s_ago": round(now - self.last_rx, 1) if self.last_rx else None,
            "last_tx_s_ago": round(now - self.last_tx, 1) if self.last_tx else None,
        }



# ---------------------------------------------------------------- decoding tables

TEXT_PORTS = {"TEXT_MESSAGE_APP", "RANGE_TEST_APP", "DETECTION_SENSOR_APP", "ALERT_APP", "REPLY_APP"}
PORT_MESSAGES = {   # port -> (payload key, "module.Message")
    "NODEINFO_APP": ("user", "mesh_pb2.User"),
    "POSITION_APP": ("position", "mesh_pb2.Position"),
    "TELEMETRY_APP": ("telemetry", "telemetry_pb2.Telemetry"),
    "ROUTING_APP": ("routing", "mesh_pb2.Routing"),
    "TRACEROUTE_APP": ("traceroute", "mesh_pb2.RouteDiscovery"),
    "NEIGHBORINFO_APP": ("neighborInfo", "mesh_pb2.NeighborInfo"),
    "WAYPOINT_APP": ("waypoint", "mesh_pb2.Waypoint"),
    "MAP_REPORT_APP": ("mapReport", "mqtt_pb2.MapReport"),
    "PAXCOUNTER_APP": ("paxcount", "paxcount_pb2.Paxcount"),
    "STORE_FORWARD_APP": ("storeForward", "storeforward_pb2.StoreAndForward"),
    "ADMIN_APP": ("admin", "admin_pb2.AdminMessage"),
}


def _pb_class(path: str):
    import importlib
    module, cls = path.split(".")
    return getattr(importlib.import_module(f"meshtastic.protobuf.{module}"), cls)


def _deg(d: dict[str, Any]) -> None:
    for k in ("latitude", "longitude"):
        if f"{k}I" in d:
            d[k] = d.pop(f"{k}I") / 1e7


def _clean(d: dict[str, Any], key: str) -> dict[str, Any]:
    """Human units for the few fields protobuf stores scaled; drop raw key material."""
    if key in ("position", "mapReport", "waypoint"):
        _deg(d)
    if key == "user":
        d["hasPublicKey"] = bool(d.pop("publicKey", None))
        d.pop("macaddr", None)
    if key == "traceroute":
        for f in ("snrTowards", "snrBack"):        # int8, SNR x4
            if f in d:
                d[f] = [s / 4 for s in d[f]]
    return d


def node_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Node table columns from a decoded payload."""
    n: dict[str, Any] = {}
    if u := payload.get("user"):
        n.update(long_name=u.get("longName"), short_name=u.get("shortName"), hw_model=u.get("hwModel"),
                 role=u.get("role", "CLIENT"), has_pki=int(u.get("hasPublicKey", False)),
                 is_licensed=int(u.get("isLicensed", False)))
    if p := payload.get("position"):
        if p.get("latitude") is not None:
            n.update(lat=p["latitude"], lon=p.get("longitude"), alt=p.get("altitude"))
        if p.get("satsInView"):
            n["sats"] = p["satsInView"]
    if t := payload.get("telemetry"):
        if d := t.get("deviceMetrics"):
            n.update(battery=d.get("batteryLevel"), voltage=d.get("voltage"), channel_util=d.get("channelUtilization"),
                     air_util_tx=d.get("airUtilTx"), uptime_s=d.get("uptimeSeconds"))
        if e := t.get("environmentMetrics"):
            n.update(temperature=e.get("temperature"), humidity=e.get("relativeHumidity"),
                     pressure=e.get("barometricPressure"))
        if s := t.get("localStats"):
            n.update(online_nodes=s.get("numOnlineNodes"), total_nodes=s.get("numTotalNodes"),
                     uptime_s=s.get("uptimeSeconds"), channel_util=s.get("channelUtilization"),
                     air_util_tx=s.get("airUtilTx"))
    if m := payload.get("mapReport"):
        n.update(long_name=m.get("longName"), short_name=m.get("shortName"), hw_model=m.get("hwModel"),
                 role=m.get("role", "CLIENT"), firmware=m.get("firmwareVersion"), region=m.get("region"),
                 modem_preset=m.get("modemPreset"), online_nodes=m.get("numOnlineLocalNodes"))
        if m.get("latitude") is not None:
            n.update(lat=m["latitude"], lon=m.get("longitude"), alt=m.get("altitude"))
    return {k: v for k, v in n.items() if v is not None}


def links_from(payload: dict[str, Any], hdr: Header) -> list[tuple[int, int, str, float | None]]:
    """(transmitter, receiver, source, snr) edges from neighbour info and traceroute replies."""
    out: list[tuple[int, int, str, float | None]] = []
    if ni := payload.get("neighborInfo"):
        me = ni.get("nodeId") or hdr.sender
        for nb in ni.get("neighbors", []):
            if nb.get("nodeId"):
                out.append((nb["nodeId"], me, "neighborinfo", nb.get("snr")))
    tr = payload.get("traceroute")
    if tr is not None and (payload.get("meta") or {}).get("requestId"):
        # a reply travels from the traced node (sender) back to the requester (to)
        for path, snrs in (([hdr.to, *tr.get("route", []), hdr.sender], tr.get("snrTowards", [])),
                           ([hdr.sender, *tr.get("routeBack", []), hdr.to], tr.get("snrBack", []))):
            for i in range(len(path) - 1):
                if BROADCAST not in (path[i], path[i + 1]) and i < len(snrs):
                    out.append((path[i], path[i + 1], "traceroute", snrs[i]))
    return out
