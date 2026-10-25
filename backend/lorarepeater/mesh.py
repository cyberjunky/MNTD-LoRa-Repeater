"""Meshtastic ingest.

Connects to meshtasticd (TCP API on port 4403) or a USB node (serial) with the official
`meshtastic` Python package, subscribes to every received packet, and stores packets plus
the node database. The node itself does the repeating (role ROUTER / REPEATER / CLIENT);
we only observe and record what passes through it.

The meshtastic package is optional: without it (or without a radio) this module logs a
warning and keeps retrying in the background so the rest of the service still runs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from .config import MeshConfig
from .db import Database

log = logging.getLogger(__name__)


def _node_num(value: Any) -> int | None:
    """Meshtastic ids come as int or '!aabbccdd'."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value)
    if s.startswith("!"):
        try:
            return int(s[1:], 16)
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


def packet_to_row(pkt: dict[str, Any]) -> dict[str, Any]:
    """Flatten a meshtastic-python packet dict into a mesh_packets row."""
    dec = pkt.get("decoded") or {}
    portnum = dec.get("portnum")
    hop_limit = pkt.get("hopLimit")
    hop_start = pkt.get("hopStart")
    hops_used = None
    if hop_limit is not None and hop_start is not None and hop_start >= hop_limit:
        hops_used = hop_start - hop_limit
    payload: dict[str, Any] = {}
    for key in ("position", "telemetry", "user", "routing", "neighborinfo", "traceroute", "admin", "waypoint"):
        if key in dec:
            payload[key] = dec[key]
    text = dec.get("text")
    if text is None and isinstance(dec.get("payload"), (bytes, bytearray)) and portnum == "TEXT_MESSAGE_APP":
        try:
            text = bytes(dec["payload"]).decode("utf-8", "replace")
        except Exception:
            text = None
    return {
        "rx_time": pkt.get("rxTime"),
        "packet_id": pkt.get("id"),
        "from_node": _node_num(pkt.get("from", pkt.get("fromId"))),
        "to_node": _node_num(pkt.get("to", pkt.get("toId"))),
        "channel": pkt.get("channel"),
        "portnum": portnum,
        "hop_limit": hop_limit,
        "hop_start": hop_start,
        "hops_used": hops_used,
        "relay_node": pkt.get("relayNode"),
        "next_hop": pkt.get("nextHop"),
        "rssi": pkt.get("rxRssi"),
        "snr": pkt.get("rxSnr"),
        "via_mqtt": 1 if pkt.get("viaMqtt") else 0,
        "want_ack": 1 if pkt.get("wantAck") else 0,
        "priority": pkt.get("priority"),
        "encrypted": 1 if ("encrypted" in pkt and not dec) else 0,
        "text": text,
        "payload": payload or None,
    }


def node_to_row(num: int, node: dict[str, Any]) -> dict[str, Any]:
    user = node.get("user") or {}
    pos = node.get("position") or {}
    dm = node.get("deviceMetrics") or {}
    return {
        "num": num,
        "node_id": user.get("id") or f"!{num:08x}",
        "long_name": user.get("longName"),
        "short_name": user.get("shortName"),
        "hw_model": user.get("hwModel"),
        "role": user.get("role"),
        "last_heard": node.get("lastHeard"),
        "snr": node.get("snr"),
        "rssi": node.get("rssi"),
        "hops_away": node.get("hopsAway"),
        "battery": dm.get("batteryLevel"),
        "voltage": dm.get("voltage"),
        "channel_util": dm.get("channelUtilization"),
        "air_util_tx": dm.get("airUtilTx"),
        "lat": pos.get("latitude"),
        "lon": pos.get("longitude"),
        "alt": pos.get("altitude"),
        "via_mqtt": 1 if node.get("viaMqtt") else 0,
        "is_favorite": 1 if node.get("isFavorite") else 0,
    }


class MeshIngest:
    def __init__(self, cfg: MeshConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.loop: asyncio.AbstractEventLoop | None = None
        self.iface: Any = None
        self.connected = False
        self.my_node: dict[str, Any] = {}
        self.counters = {"packets": 0, "text": 0, "nodes": 0}
        self.last_packet = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.cfg.enabled:
            log.info("mesh ingest disabled")
            return
        self.loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="mesh-ingest", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self.iface:
            try:
                self.iface.close()
            except Exception:
                pass

    # -- thread side (meshtastic lib is blocking) -------------------------------------

    def _run(self) -> None:
        try:
            import meshtastic  # noqa: F401
            from pubsub import pub
        except ImportError:
            self._post(self.db.add_event("mesh", "warn", "python package 'meshtastic' not installed; mesh ingest off"))
            return
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_connection, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        while not self._stop.is_set():
            try:
                self._connect()
                while not self._stop.is_set() and self.connected:
                    time.sleep(1)
            except Exception as e:  # connection refused, radio missing, ...
                self.connected = False
                log.warning("mesh connection failed: %s", e)
                self._post(self.db.add_event("mesh", "warn", f"connection failed: {e}"))
            if not self._stop.is_set():
                time.sleep(self.cfg.reconnect_s)

    def _connect(self) -> None:
        if self.cfg.transport == "serial":
            from meshtastic.serial_interface import SerialInterface
            self.iface = SerialInterface(devPath=self.cfg.device)
        else:
            from meshtastic.tcp_interface import TCPInterface
            self.iface = TCPInterface(hostname=self.cfg.host, portNumber=self.cfg.port)

    def _post(self, coro) -> None:
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _on_connection(self, interface=None, topic=None) -> None:
        self.connected = True
        try:
            info = interface.getMyNodeInfo() or {}
            self.my_node = {
                "num": info.get("num"),
                "id": (info.get("user") or {}).get("id"),
                "long_name": (info.get("user") or {}).get("longName"),
                "hw_model": (info.get("user") or {}).get("hwModel"),
                "role": (info.get("user") or {}).get("role"),
                "firmware": getattr(interface, "metadata", None) and getattr(interface.metadata, "firmware_version", None),
            }
        except Exception:
            self.my_node = {}
        self._post(self.db.add_event("mesh", "info", f"connected to node {self.my_node.get('id')} ({self.my_node.get('long_name')})"))
        self._sync_nodes(interface)

    def _on_lost(self, interface=None, topic=None) -> None:
        self.connected = False
        self._post(self.db.add_event("mesh", "warn", "connection to node lost"))

    def _sync_nodes(self, interface) -> None:
        nodes = getattr(interface, "nodes", None) or {}
        for key, node in list(nodes.items()):
            num = node.get("num") or _node_num(key)
            if num is None:
                continue
            self._post(self.db.upsert_mesh_node(node_to_row(num, node)))
        self.counters["nodes"] = len(nodes)

    def _on_receive(self, packet: dict[str, Any], interface=None) -> None:
        self.counters["packets"] += 1
        self.last_packet = time.time()
        row = packet_to_row(packet)
        if row["portnum"] == "TEXT_MESSAGE_APP":
            self.counters["text"] += 1
        self._post(self.db.add_mesh_packet(row))
        # NODEINFO / POSITION / TELEMETRY update the node table through the library's node db
        if row["portnum"] in ("NODEINFO_APP", "POSITION_APP", "TELEMETRY_APP") and interface is not None:
            frm = row["from_node"]
            nodes = getattr(interface, "nodes", None) or {}
            node = nodes.get(f"!{frm:08x}") if frm is not None else None
            if node:
                self._post(self.db.upsert_mesh_node(node_to_row(frm, node)))

    # -- status ----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "transport": self.cfg.transport,
            "target": self.cfg.device if self.cfg.transport == "serial" else f"{self.cfg.host}:{self.cfg.port}",
            "connected": self.connected,
            "my_node": self.my_node,
            "counters": dict(self.counters),
            "last_packet_s_ago": round(time.time() - self.last_packet, 1) if self.last_packet else None,
        }
