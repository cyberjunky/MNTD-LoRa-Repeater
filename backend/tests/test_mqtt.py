"""MQTT payloads and routing, without a broker: the links' publish() is captured."""

import json
import time
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import mesh_pb2, mqtt_pb2

from lorarepeater.config import HomeAssistantMqttConfig, MeshtasticMqttConfig
from lorarepeater.db import Database
from lorarepeater.meshproto import BROADCAST, HEADER_LEN, Header, crypt, expand_psk
from lorarepeater.mqtt import (
    HomeAssistant,
    MeshtasticUplink,
    node_state,
    parse_send,
    service_envelope,
)

ME = 0x0A0B0C42
OTHER = 0x11223344
KEY = expand_psk("AQ==")


class FakeMesh:
    node_num = ME

    def __init__(self):
        self.channels = [SimpleNamespace(name="LongFast", key=KEY), SimpleNamespace(name="Private", key=b"")]
        self.sent = []
        self.text_listeners = []
        self.frame_listeners = []

    def add_text_listener(self, fn):
        self.text_listeners.append(fn)

    def add_frame_listener(self, fn):
        self.frame_listeners.append(fn)

    async def send_text(self, text, to=BROADCAST, channel=0):
        self.sent.append((text, to, channel))
        return 0x80000001

    def status(self):
        return {"counters": {"rx": 5, "relayed": 2, "tx": 1}, "duty_used_pct_1h": 0.4}


def capture(link):
    out = []
    link.publish = lambda topic, payload, retain=False: out.append((topic, payload, retain))
    return out


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "q.db")
    await d.open()
    yield d
    await d.close()


def test_service_envelope_carries_the_frame_as_heard():
    data = mesh_pb2.Data(portnum=1, payload=b"hello")
    hdr = Header(to=BROADCAST, sender=OTHER, packet_id=0x1234, hop_limit=2, hop_start=3, channel_hash=8, relay_node=0x44)
    raw = hdr.pack() + crypt(KEY, 0x1234, OTHER, data.SerializeToString())
    env = mqtt_pb2.ServiceEnvelope()
    env.ParseFromString(service_envelope(raw, hdr, "LongFast", ME, {"rx_time": 1700000000, "rssi": -101, "snr": 6.25}))
    p = env.packet
    assert env.channel_id == "LongFast" and env.gateway_id == "!0a0b0c42"
    assert getattr(p, "from") == OTHER and p.to == BROADCAST and p.id == 0x1234 and p.channel == 8
    assert p.hop_limit == 2 and p.hop_start == 3 and p.relay_node == 0x44 and p.rx_rssi == -101 and p.rx_snr == 6.25
    assert p.encrypted == raw[HEADER_LEN:] and not p.HasField("decoded")


async def test_uplink_publishes_only_allowed_frames(db):
    mesh = FakeMesh()
    up = MeshtasticUplink(MeshtasticMqttConfig(enabled=True, channels=["LongFast", "Private"]), db, mesh)
    out = capture(up.link)
    hdr = Header(to=BROADCAST, sender=OTHER, packet_id=1, channel_hash=8)
    raw = hdr.pack() + b"\x01\x02"
    info = {"rx_time": 1, "rssi": -90, "snr": 1.0, "ok_to_mqtt": True}
    up.on_frame(raw, hdr, mesh.channels[0], info)
    assert out[0][0] == "msh/EU_868/2/e/LongFast/!0a0b0c42"
    up.on_frame(raw, hdr, mesh.channels[0], {**info, "ok_to_mqtt": False})        # sender opted out
    up.on_frame(raw, hdr, mesh.channels[1], info)                                 # no key: not an encrypted frame
    up.on_frame(raw, Header(**{**hdr.__dict__, "via_mqtt": True}), mesh.channels[0], info)   # came from MQTT
    up.cfg.channels = []
    up.on_frame(raw, hdr, mesh.channels[0], info)                                 # channel not selected
    assert len(out) == 1


def test_parse_send_and_node_state():
    assert parse_send(b" hello all ") == ("hello all", BROADCAST, 0)
    assert parse_send(json.dumps({"text": "hi", "to": "!11223344", "channel": 1}).encode()) == ("hi", OTHER, 1)
    with pytest.raises(ValueError):
        parse_send(b'{"text": "x", "to": "!nothex"}')
    st = node_state({"num": OTHER, "battery": 101, "snr": 4.5, "lat": 52.1, "lon": 5.2, "voltage": None, "last_heard": 0})
    assert st == {"battery": 100, "snr": 4.5, "last_heard": "1970-01-01T00:00:00+00:00", "lat": 52.1, "lon": 5.2}


async def test_home_assistant_discovery_state_and_send(db):
    mesh = FakeMesh()
    mux = SimpleNamespace(counters={"rxpk": 42}, cfg=SimpleNamespace(gateway_eui=""))
    ha = HomeAssistant(HomeAssistantMqttConfig(enabled=True, host="ha.local"), db, mesh, mux, "MNTD", "/nonexistent")
    out = capture(ha.link)
    assert ha.link.subscriptions == ("lorarepeater/send",)
    await db.upsert_mesh_node({"num": OTHER, "node_id": "!11223344", "long_name": "Hill", "hw_model": "TBEAM",
                               "battery": 80, "lat": 52.0, "lon": 5.0, "last_heard": time.time()})
    await ha._on_connect()
    topics = {t: (p, r) for t, p, r in out}
    assert topics["lorarepeater/status"] == ("online", True)
    assert topics["lorarepeater/state"][0] == {"lorawan_rx": 42, "mesh_rx": 5, "mesh_relayed": 2, "mesh_tx": 1,
                                                "mesh_airtime_pct": 0.4, "mesh_nodes_24h": 1}
    notify = topics["homeassistant/notify/lorarepeater_0a0b0c42/mesh_broadcast/config"][0]
    assert notify["command_topic"] == "lorarepeater/send" and notify["availability_topic"] == "lorarepeater/status"
    cpu = topics["homeassistant/sensor/lorarepeater_0a0b0c42/cpu_temp/config"][0]
    assert cpu["unit_of_measurement"] == "°C" and "state_class" in cpu
    assert "unit_of_measurement" not in topics["homeassistant/sensor/lorarepeater_0a0b0c42/mesh_rx/config"][0]
    # the node heard in the last day is published with only the entities it has data for
    assert ha._dirty == {OTHER}
    out.clear()
    row = await db.fetchone("SELECT * FROM mesh_nodes WHERE num = ?", (OTHER,))
    ha._announce_node(row, node_state(row))
    assert {t.split("/")[3] for t, _, _ in out} == {"battery", "last_heard", "position"}
    tracker = next(p for t, p, _ in out if "/device_tracker/" in t)
    assert tracker["device"]["name"] == "Hill" and tracker["device"]["via_device"] == "lorarepeater_0a0b0c42"
    # commands: plain text broadcast, JSON DM, and a refusal
    out.clear()
    await ha._on_message("lorarepeater/send", b"storm warning")
    await ha._on_message("lorarepeater/send", b'{"text": "hi", "to": "!11223344"}')
    await ha._on_message("lorarepeater/send", b'{"text": "x", "channel": 5}')
    assert mesh.sent == [("storm warning", BROADCAST, 0), ("hi", OTHER, 0)]
    results = [p for t, p, _ in out if t == "lorarepeater/send/result"]
    assert results[0]["ok"] and results[1]["ok"] and results[2] == {"ok": False, "error": "no channel 5"}
    # received texts become events
    out.clear()
    await ha._publish_text({"from": OTHER, "to": BROADCAST, "channel_name": "LongFast", "text": "yo", "rssi": -90, "snr": 3, "hops": 1})
    assert out[0][0] == "lorarepeater/text" and out[0][1]["event_type"] == "text" and out[0][1]["from_name"] == "Hill"
