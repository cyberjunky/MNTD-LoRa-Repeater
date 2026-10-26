"""MQTT: the Meshtastic uplink and Home Assistant.

Meshtastic uplink: behaves like a Meshtastic node with MQTT enabled. Every frame heard on (or
sent from) a configured channel goes out encrypted, exactly as it was on air, as a
`ServiceEnvelope` on `<root_topic>/2/e/<channel>/<our node id>`. Frames whose sender cleared
"OK to MQTT", frames that already came through MQTT, and channels without a key are not sent.

Home Assistant (JSON under `<base_topic>`, discovery under `<discovery_prefix>`):

    <base>/status                 online / offline (retained, last will)
    <base>/state                  gateway counters, every minute (retained)
    <base>/node/<id>/state        one heard node: battery, SNR, environment, position (retained)
    <base>/text                   every received text message (event entity "Mesh text")
    <base>/send                   command: plain text = broadcast on the primary channel, or JSON
                                  {"text": "...", "to": "!aabbccdd", "channel": 0}
    <base>/send/result            {"ok": true, "packet_id": ...} or {"ok": false, "error": "..."}

The "Mesh broadcast" notify entity publishes to <base>/send, so an automation sends with
`notify.send_message`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import HomeAssistantMqttConfig, MeshtasticMqttConfig, MqttBroker, MqttConfig
from .db import Database
from .gwmp import GwmpMux
from .meshproto import BROADCAST, HEADER_LEN, MAX_TEXT_BYTES, Header, node_id_str, parse_node_id

log = logging.getLogger(__name__)

QUEUE_MAX = 500   # messages kept while the broker is unreachable; older ones are dropped


class Link:
    """One broker connection: reconnects, publishes from a queue, hands incoming messages over."""

    def __init__(self, name: str, cfg: MqttBroker, db: Database, client_id: str,
                 will: tuple[str, str] | None = None, subscribe: tuple[str, ...] = ()):
        self.name = name
        self.cfg = cfg
        self.db = db
        self.client_id = client_id
        self.will = will
        self.subscriptions = subscribe
        self.connected = False
        self.last_error: str | None = None
        self.counters = {"published": 0, "dropped": 0, "received": 0}
        self.on_connect: Any = None    # async () -> None
        self.on_message: Any = None    # async (topic, payload bytes) -> None
        self._queue: asyncio.Queue[tuple[str, bytes, bool]] = asyncio.Queue(QUEUE_MAX)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.cfg.enabled and self.cfg.host:
            self._task = asyncio.create_task(self._run(), name=f"mqtt-{self.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def publish(self, topic: str, payload: bytes | str | dict, retain: bool = False) -> None:
        if self._task is None:
            return
        if isinstance(payload, dict):
            payload = json.dumps(payload, default=str)
        data = payload.encode() if isinstance(payload, str) else payload
        if self._queue.full():
            self._queue.get_nowait()
            self.counters["dropped"] += 1
        self._queue.put_nowait((topic, data, retain))

    async def _run(self) -> None:
        import aiomqtt
        will = aiomqtt.Will(self.will[0], self.will[1], qos=1, retain=True) if self.will else None
        while True:
            try:
                async with aiomqtt.Client(
                    self.cfg.host, self.cfg.port, username=self.cfg.username or None,
                    password=self.cfg.password or None, identifier=self.client_id, will=will,
                    tls_context=ssl.create_default_context() if self.cfg.tls else None,
                ) as client:
                    self.connected, self.last_error = True, None
                    await self.db.add_event("mqtt", "info", f"{self.name}: connected to {self.cfg.host}:{self.cfg.port}")
                    for topic in self.subscriptions:
                        await client.subscribe(topic)
                    if self.on_connect:
                        await self.on_connect()
                    reader = asyncio.create_task(self._read(client))
                    try:
                        while True:
                            topic, data, retain = await self._queue.get()
                            await client.publish(topic, data, retain=retain)
                            self.counters["published"] += 1
                    except asyncio.CancelledError:
                        if self.will:   # a clean disconnect does not trigger the last will
                            await asyncio.wait_for(client.publish(*self.will, qos=1, retain=True), 3)
                        raise
                    finally:
                        reader.cancel()
            except aiomqtt.MqttError as e:
                if self.connected or self.last_error != str(e):
                    await self.db.add_event("mqtt", "warn", f"{self.name}: {self.cfg.host}:{self.cfg.port} {e}")
                self.connected, self.last_error = False, str(e)
                await asyncio.sleep(15)

    async def _read(self, client: Any) -> None:
        async for msg in client.messages:
            self.counters["received"] += 1
            try:
                await self.on_message(msg.topic.value, bytes(msg.payload))
            except Exception as e:   # a bad command must not drop the connection
                log.warning("%s: message on %s failed: %s", self.name, msg.topic.value, e)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.cfg.enabled, "host": self.cfg.host, "port": self.cfg.port,
                "connected": self.connected, "last_error": self.last_error, "counters": dict(self.counters)}


# ---------------------------------------------------------------- Meshtastic uplink

def service_envelope(raw: bytes, hdr: Header, channel_name: str, gateway: int, info: dict[str, Any]) -> bytes:
    """The frame as a Meshtastic node uplinks it: MeshPacket with the encrypted payload."""
    from meshtastic.protobuf import mesh_pb2, mqtt_pb2
    pkt = mesh_pb2.MeshPacket(to=hdr.to, id=hdr.packet_id, channel=hdr.channel_hash, encrypted=raw[HEADER_LEN:],
                              hop_limit=hdr.hop_limit, hop_start=hdr.hop_start, want_ack=hdr.want_ack,
                              via_mqtt=hdr.via_mqtt, next_hop=hdr.next_hop, relay_node=hdr.relay_node,
                              rx_time=info["rx_time"])
    setattr(pkt, "from", hdr.sender)
    if info.get("snr") is not None:
        pkt.rx_snr = float(info["snr"])
    if info.get("rssi") is not None:
        pkt.rx_rssi = int(info["rssi"])
    return mqtt_pb2.ServiceEnvelope(packet=pkt, channel_id=channel_name, gateway_id=node_id_str(gateway)).SerializeToString()


class MeshtasticUplink:
    def __init__(self, cfg: MeshtasticMqttConfig, db: Database, meshsx: Any):
        self.cfg = cfg
        self.meshsx = meshsx
        self.link = Link("meshtastic", cfg, db, f"lorarepeater-{node_id_str(meshsx.node_num)}")

    async def start(self) -> None:
        await self.link.start()
        if self.link._task:
            self.meshsx.add_frame_listener(self.on_frame)

    def on_frame(self, raw: bytes, hdr: Header, chan: Any, info: dict[str, Any]) -> None:
        if chan.name not in self.cfg.channels or not chan.key or hdr.via_mqtt or not info["ok_to_mqtt"]:
            return
        gw = self.meshsx.node_num
        self.link.publish(f"{self.cfg.root_topic}/2/e/{chan.name}/{node_id_str(gw)}",
                          service_envelope(raw, hdr, chan.name, gw, info))


# ---------------------------------------------------------------- Home Assistant

NODE_SENSORS = {   # node column -> (name, unit, device_class)
    "battery": ("Battery", "%", "battery"),
    "voltage": ("Voltage", "V", "voltage"),
    "snr": ("SNR", "dB", None),
    "rssi": ("RSSI", "dBm", "signal_strength"),
    "hops_away": ("Hops away", None, None),
    "temperature": ("Temperature", "°C", "temperature"),
    "humidity": ("Humidity", "%", "humidity"),
    "pressure": ("Pressure", "hPa", "atmospheric_pressure"),
    "last_heard": ("Last heard", None, "timestamp"),
}
GATEWAY_SENSORS = {   # state key -> (name, unit, device_class, state_class)
    "mesh_rx": ("Mesh packets heard", None, None, "total_increasing"),
    "mesh_relayed": ("Mesh packets relayed", None, None, "total_increasing"),
    "mesh_tx": ("Mesh packets sent", None, None, "total_increasing"),
    "mesh_airtime_pct": ("Mesh airtime (1 h)", "%", None, "measurement"),
    "mesh_nodes_24h": ("Mesh nodes heard (24 h)", None, None, "measurement"),
    "lorawan_rx": ("LoRaWAN packets received", None, None, "total_increasing"),
    "cpu_temp": ("CPU temperature", "°C", "temperature", "measurement"),
}


def node_state(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in NODE_SENSORS if row.get(k) is not None}
    if out.get("battery", 0) > 100:     # Meshtastic reports 101 for "on external power"
        out["battery"] = 100
    if "last_heard" in out:
        out["last_heard"] = datetime.fromtimestamp(out["last_heard"], UTC).isoformat()
    if row.get("lat") is not None and row.get("lon") is not None:
        out.update(lat=row["lat"], lon=row["lon"])
    return out


def parse_send(payload: bytes) -> tuple[str, int, int]:
    """Command payload -> (text, destination, channel index)."""
    body = payload.decode("utf-8").strip()
    if body.startswith("{"):
        cmd = json.loads(body)
        return str(cmd.get("text", "")).strip(), parse_node_id(cmd.get("to")), int(cmd.get("channel", 0))
    return body, BROADCAST, 0


class HomeAssistant:
    def __init__(self, cfg: HomeAssistantMqttConfig, db: Database, meshsx: Any, mux: GwmpMux,
                 title: str, cpu_temp_path: str):
        self.cfg = cfg
        self.db = db
        self.meshsx = meshsx
        self.mux = mux
        self.title = title
        self.cpu_temp_path = cpu_temp_path
        self.gw = node_id_str(meshsx.node_num)[1:] if meshsx else mux.cfg.gateway_eui.lower() or "gateway"
        b = cfg.base_topic
        self.link = Link("homeassistant", cfg, db, f"lorarepeater-ha-{self.gw}", will=(f"{b}/status", "offline"),
                         subscribe=(f"{b}/send",) if cfg.allow_send and meshsx else ())
        self.link.on_connect = self._on_connect
        self.link.on_message = self._on_message
        self._announced: dict[int, set[str]] = {}
        self._dirty: set[int] = set()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.link.start()
        if self.link._task is None:
            return
        if self.meshsx:
            self.meshsx.add_text_listener(self.on_text)
        self.db.subscribe(self._on_db)
        self._tasks = [asyncio.create_task(self._loop(), name="mqtt-ha")]

    async def stop(self) -> None:
        self.db.unsubscribe(self._on_db)
        for t in self._tasks:
            t.cancel()
        await self.link.stop()

    # -- discovery ----------------------------------------------------------------

    @property
    def device(self) -> dict[str, Any]:
        return {"identifiers": [f"lorarepeater_{self.gw}"], "name": self.title, "manufacturer": "MNTD",
                "model": "Goldspot (RAK2287 SX1302)", "sw_version": __version__}

    def _entity(self, component: str, key: str, cfg: dict[str, Any], device: dict[str, Any], uid: str) -> None:
        cfg = {"unique_id": f"{uid}_{key}", "device": device, "availability_topic": f"{self.cfg.base_topic}/status",
               **{k: v for k, v in cfg.items() if v is not None}}
        self.link.publish(f"{self.cfg.discovery_prefix}/{component}/{uid}/{key}/config", cfg, retain=True)

    def _announce_gateway(self) -> None:
        uid, b = f"lorarepeater_{self.gw}", self.cfg.base_topic
        for key, (name, unit, dclass, sclass) in GATEWAY_SENSORS.items():
            if key.startswith("mesh_") and not self.meshsx:
                continue
            self._entity("sensor", key, {"name": name, "state_topic": f"{b}/state", "value_template": f"{{{{ value_json.{key} }}}}",
                                         "unit_of_measurement": unit, "device_class": dclass, "state_class": sclass}, self.device, uid)
        if self.meshsx:
            self._entity("event", "mesh_text", {"name": "Mesh text", "state_topic": f"{b}/text", "event_types": ["text"]}, self.device, uid)
        if self.meshsx and self.cfg.allow_send:
            self._entity("notify", "mesh_broadcast", {"name": "Mesh broadcast", "command_topic": f"{b}/send"}, self.device, uid)

    def _announce_node(self, row: dict[str, Any], state: dict[str, Any]) -> None:
        num = row["num"]
        have = self._announced.setdefault(num, set())
        new = [k for k in [*NODE_SENSORS, "lat"] if k in state and k not in have]
        if not new:
            return
        nid = node_id_str(num)[1:]
        uid, topic = f"meshtastic_{nid}", f"{self.cfg.base_topic}/node/{nid}/state"
        device = {"identifiers": [uid], "name": row.get("long_name") or node_id_str(num), "manufacturer": "Meshtastic",
                  "model": row.get("hw_model") or "unknown", "via_device": f"lorarepeater_{self.gw}"}
        for key in new:
            if key == "lat":
                self._entity("device_tracker", "position", {
                    "name": "Position", "json_attributes_topic": topic, "source_type": "gps",
                    "json_attributes_template": '{{ {"latitude": value_json.lat, "longitude": value_json.lon} | tojson }}'}, device, uid)
                continue
            name, unit, dclass = NODE_SENSORS[key]
            self._entity("sensor", key, {"name": name, "state_topic": topic, "value_template": f"{{{{ value_json.{key} }}}}",
                                         "unit_of_measurement": unit, "device_class": dclass,
                                         "state_class": "measurement" if unit else None}, device, uid)
        have.update(new)

    # -- state --------------------------------------------------------------------

    async def _on_connect(self) -> None:
        self._announced.clear()
        self.link.publish(f"{self.cfg.base_topic}/status", "online", retain=True)
        self._announce_gateway()
        await self._publish_gateway()
        if self.cfg.node_entities:
            rows = await self.db.fetch("SELECT num FROM mesh_nodes WHERE last_heard >= ?", (time.time() - 86400,))
            self._dirty.update(r["num"] for r in rows)

    async def _publish_gateway(self) -> None:
        st: dict[str, Any] = {"lorawan_rx": self.mux.counters["rxpk"]}
        try:
            st["cpu_temp"] = int(Path(self.cpu_temp_path).read_text().strip()) / 1000.0
        except OSError:
            pass
        if self.meshsx:
            s = self.meshsx.status()
            nodes = await self.db.fetchone("SELECT COUNT(*) AS n FROM mesh_nodes WHERE last_heard >= ? AND num != ?",
                                           (time.time() - 86400, self.meshsx.node_num))
            st.update(mesh_rx=s["counters"]["rx"], mesh_relayed=s["counters"]["relayed"], mesh_tx=s["counters"]["tx"],
                      mesh_airtime_pct=s["duty_used_pct_1h"], mesh_nodes_24h=nodes["n"])
        self.link.publish(f"{self.cfg.base_topic}/state", st, retain=True)

    def _on_db(self, kind: str, row: dict[str, Any]) -> None:
        if kind == "mesh_nodes" and self.cfg.node_entities and row.get("num") != getattr(self.meshsx, "node_num", None):
            self._dirty.add(row["num"])

    async def _loop(self) -> None:
        last_gw = time.monotonic()
        while True:
            await asyncio.sleep(5)
            if self.link.connected and self._dirty:
                nums, self._dirty = list(self._dirty), set()
                for row in await self.db.fetch(f"SELECT * FROM mesh_nodes WHERE num IN ({','.join('?' * len(nums))})", nums):
                    state = node_state(row)
                    self._announce_node(row, state)
                    self.link.publish(f"{self.cfg.base_topic}/node/{node_id_str(row['num'])[1:]}/state", state, retain=True)
            if self.link.connected and time.monotonic() - last_gw >= 60:
                last_gw = time.monotonic()
                await self._publish_gateway()

    # -- texts in and out ---------------------------------------------------------

    def on_text(self, info: dict[str, Any]) -> None:
        task = asyncio.ensure_future(self._publish_text(info))
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)

    async def _publish_text(self, info: dict[str, Any]) -> None:
        node = await self.db.fetchone("SELECT long_name FROM mesh_nodes WHERE num = ?", (info["from"],))
        self.link.publish(f"{self.cfg.base_topic}/text", {
            "event_type": "text", "text": info["text"], "from": node_id_str(info["from"]),
            "from_name": (node or {}).get("long_name"), "to": "broadcast" if info["to"] == BROADCAST else node_id_str(info["to"]),
            "channel": info["channel_name"], "rssi": info["rssi"], "snr": info["snr"], "hops": info["hops"]})

    async def _on_message(self, topic: str, payload: bytes) -> None:
        result: dict[str, Any]
        try:
            text, to, channel = parse_send(payload)
            if not text or len(text.encode()) > MAX_TEXT_BYTES:
                raise ValueError(f"text must be 1 to {MAX_TEXT_BYTES} bytes")
            if not 0 <= channel < len(self.meshsx.channels):
                raise ValueError(f"no channel {channel}")
            pid = await self.meshsx.send_text(text, to=to, channel=channel)
            result = ({"ok": True, "packet_id": pid} if pid is not None
                      else {"ok": False, "error": "not transmitted (TX off, duty cycle, forwarder)"})
        except ValueError as e:     # also bad JSON and bad node ids
            result = {"ok": False, "error": str(e)}
        self.link.publish(f"{topic}/result", result)
        await self.db.add_event("mqtt", "info" if result["ok"] else "warn", f"homeassistant send: {result}")


class Mqtt:
    """Both MQTT roles; either may be off."""

    def __init__(self, cfg: MqttConfig, db: Database, meshsx: Any, mux: GwmpMux, title: str, cpu_temp_path: str):
        self.meshtastic = MeshtasticUplink(cfg.meshtastic, db, meshsx) if meshsx else None
        self.homeassistant = HomeAssistant(cfg.homeassistant, db, meshsx, mux, title, cpu_temp_path)

    async def start(self) -> None:
        if self.meshtastic:
            await self.meshtastic.start()
        await self.homeassistant.start()

    async def stop(self) -> None:
        if self.meshtastic:
            await self.meshtastic.link.stop()
        await self.homeassistant.stop()

    def status(self) -> dict[str, Any]:
        return {"meshtastic": self.meshtastic.link.status() if self.meshtastic else None,
                "homeassistant": self.homeassistant.link.status()}
