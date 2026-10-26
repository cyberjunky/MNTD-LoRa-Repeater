"""The SX1302 Meshtastic engine against a fake packet-forwarder mux."""

import asyncio
import base64

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2

from lorarepeater.config import MeshSxConfig
from lorarepeater.db import Database
from lorarepeater.meshproto import BROADCAST, HEADER_LEN, Header, crypt, expand_psk
from lorarepeater.meshsx import MeshSxEngine

ME = 0x0A0B0C42
OTHER = 0x11223344
KEY = expand_psk("AQ==")


class FakeMux:
    def __init__(self):
        self.hooks = []
        self.sent = []
        self.pulling = True

    def add_rx_hook(self, fn):
        self.hooks.append(fn)

    async def send_txpk(self, txpk):
        if not self.pulling:
            return False
        self.sent.append(txpk)
        return True


def make_frame(text: str, sender=OTHER, to=BROADCAST, hop_limit=3, hop_start=3, pid=0xC0FFEE01, port="TEXT_MESSAGE_APP") -> bytes:
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value(port), payload=text.encode())
    hdr = Header(to=to, sender=sender, packet_id=pid, hop_limit=hop_limit, hop_start=hop_start, channel_hash=8)
    return hdr.pack() + crypt(KEY, pid, sender, data.SerializeToString())


def rxpk(frame: bytes, tmst=1_000_000, snr=5.5, rssi=-95) -> dict:
    return {"tmst": tmst, "freq": 869.525, "datr": "SF11BW250", "codr": "4/5", "stat": 1, "modu": "LORA",
            "rssi": rssi, "lsnr": snr, "size": len(frame), "data": base64.b64encode(frame).decode()}


@pytest.fixture
async def engine(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.open()
    mux = FakeMux()
    cfg = MeshSxConfig(enabled=True, node_num=ME, relay_delay_ms=0, hop_limit=3)
    eng = MeshSxEngine(cfg, db, mux)
    await eng.start()
    yield eng, mux, db
    await eng.stop()
    await db.close()


async def settle(eng):
    while eng._pending:
        await asyncio.gather(*list(eng._pending), return_exceptions=True)


async def test_decode_store_and_relay(engine):
    eng, mux, db = engine
    frame = make_frame("hello mesh")
    assert eng.on_rxpk(rxpk(frame)) is True
    await settle(eng)
    rows = await db.fetch("SELECT * FROM mesh_packets")
    assert len(rows) == 1
    r = rows[0]
    assert r["text"] == "hello mesh" and r["portnum"] == "TEXT_MESSAGE_APP" and r["from_node"] == OTHER
    assert r["encrypted"] == 0 and r["source"] == "sx1302" and r["relayed_by_us"] == 1
    assert r["hops_used"] == 0 and r["airtime_ms"] > 100
    # relayed copy: same payload, hop_limit - 1, our relay byte, scheduled on the SX1302 counter
    assert len(mux.sent) == 1
    tx = mux.sent[0]
    out = base64.b64decode(tx["data"])
    h = Header.parse(out)
    assert h.hop_limit == 2 and h.hop_start == 3 and h.relay_node == ME & 0xFF and h.sender == OTHER
    assert out[HEADER_LEN:] == frame[HEADER_LEN:]
    assert tx["freq"] == 869.525 and tx["datr"] == "SF11BW250" and tx["prea"] == 16 and tx["ipol"] is False
    assert tx["tmst"] > 1_000_000 and tx["imme"] is False
    assert eng.counters["relayed"] == 1
    node = await db.fetchone("SELECT * FROM mesh_nodes WHERE num = ?", (OTHER,))
    assert node["node_id"] == "!11223344" and node["snr"] == 5.5


async def test_duplicates_and_hop_limit(engine):
    eng, mux, _db = engine
    frame = make_frame("once")
    eng.on_rxpk(rxpk(frame))
    await settle(eng)
    eng.on_rxpk(rxpk(frame, tmst=2_000_000))      # heard again via another relay
    await settle(eng)
    assert len(mux.sent) == 1 and eng.counters["dropped_dup"] == 1
    eng.on_rxpk(rxpk(make_frame("dead end", hop_limit=0, pid=0x01)))
    await settle(eng)
    assert len(mux.sent) == 1 and eng.counters["dropped_hops"] == 1
    # directed to someone else with a different preferred next hop: not ours to relay
    f = make_frame("dm", to=0x55667788, pid=0x02)
    f = f[:14] + bytes([0x99]) + f[15:]
    eng.on_rxpk(rxpk(f))
    await settle(eng)
    assert len(mux.sent) == 1
    # not our channel: not consumed
    assert eng.on_rxpk({**rxpk(frame), "freq": 868.1, "datr": "SF7BW125"}) is False


async def test_text_listener_ack_and_send(engine):
    eng, mux, db = engine
    got = []
    eng.add_text_listener(got.append)
    dm = make_frame("for you", to=ME, pid=0x10)
    dm = dm[:12] + bytes([0x08 | 3 | (3 << 5)]) + dm[13:]   # set want_ack
    eng.on_rxpk(rxpk(dm))
    await settle(eng)
    assert got and got[0]["text"] == "for you" and got[0]["to"] == ME
    # a DM to us with want_ack gets a ROUTING ack, and is not relayed
    acks = [s for s in mux.sent if s["imme"]]
    assert len(acks) == 1 and len(mux.sent) == 1
    ack = Header.parse(base64.b64decode(acks[0]["data"]))
    assert ack.sender == ME and ack.to == OTHER
    # originate a broadcast text and decode it like another node would
    pid = await eng.send_text("hi from the box")
    tx = mux.sent[-1]
    raw = base64.b64decode(tx["data"])
    h = Header.parse(raw)
    assert h.sender == ME and h.to == BROADCAST and h.packet_id == pid and h.hop_limit == 3 and h.channel_hash == 8
    data = mesh_pb2.Data()
    data.ParseFromString(crypt(KEY, pid, ME, raw[HEADER_LEN:]))
    assert data.payload == b"hi from the box" and portnums_pb2.PortNum.Name(data.portnum) == "TEXT_MESSAGE_APP"
    rows = await db.fetch("SELECT direction, text FROM mesh_packets WHERE direction = 'tx' AND text IS NOT NULL")
    assert rows[0]["text"] == "hi from the box"


async def test_duty_cycle_and_no_forwarder(engine):
    eng, mux, db = engine
    eng.cfg.duty_cycle_pct = 0.001  # ~36 ms per hour: one LongFast frame already exceeds it
    assert not await eng.transmit(make_frame("x"))
    assert eng.counters["dropped_duty"] == 1
    eng.cfg.duty_cycle_pct = 10
    mux.pulling = False
    assert not await eng.transmit(make_frame("x"))
    assert (await db.fetch("SELECT * FROM events WHERE message LIKE '%not pulling%'"))


async def test_decodes_telemetry_neighbors_and_links(engine):
    from meshtastic.protobuf import telemetry_pb2

    eng, _mux, db = engine
    t = telemetry_pb2.Telemetry(environment_metrics=telemetry_pb2.EnvironmentMetrics(temperature=21.5, relative_humidity=40))
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("TELEMETRY_APP"), payload=t.SerializeToString())
    hdr = Header(to=BROADCAST, sender=OTHER, packet_id=0xBEEF0001, hop_limit=3, hop_start=3, channel_hash=8)
    eng.on_rxpk(rxpk(hdr.pack() + crypt(KEY, 0xBEEF0001, OTHER, data.SerializeToString())))
    ni = mesh_pb2.NeighborInfo(node_id=OTHER, neighbors=[mesh_pb2.Neighbor(node_id=0x11223344, snr=6.25)])
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("NEIGHBORINFO_APP"), payload=ni.SerializeToString())
    hdr = Header(to=BROADCAST, sender=OTHER, packet_id=0xBEEF0002, hop_limit=3, hop_start=3, channel_hash=8)
    eng.on_rxpk(rxpk(hdr.pack() + crypt(KEY, 0xBEEF0002, OTHER, data.SerializeToString())))
    await settle(eng)
    node = await db.fetchone("SELECT * FROM mesh_nodes WHERE num = ?", (OTHER,))
    assert node["temperature"] == 21.5 and node["humidity"] == 40 and node["packets"] == 2
    links = {(r["a"], r["b"], r["source"]): r["snr"] for r in await db.fetch("SELECT * FROM mesh_links")}
    assert links[(0x11223344, OTHER, "neighborinfo")] == 6.25
    assert (OTHER, ME, "rf") in links                       # we heard OTHER directly


def test_links_from_traceroute_reply():
    from lorarepeater.meshsx import links_from

    hdr = Header(to=0xA, sender=0xD, packet_id=1, hop_limit=3, hop_start=3, channel_hash=8)
    payload = {"traceroute": {"route": [0xB], "snrTowards": [5.0, 2.5], "routeBack": [], "snrBack": [3.0]},
               "meta": {"requestId": 99}}
    assert links_from(payload, hdr) == [(0xA, 0xB, "traceroute", 5.0), (0xB, 0xD, "traceroute", 2.5),
                                        (0xD, 0xA, "traceroute", 3.0)]


def test_node_fields_map_report():
    from lorarepeater.meshsx import node_fields

    n = node_fields({"mapReport": {"longName": "Hill", "hwModel": "RAK4631", "firmwareVersion": "2.7.11",
                                   "region": "EU_868", "modemPreset": "MEDIUM_FAST", "latitude": 52.1, "longitude": 5.1,
                                   "numOnlineLocalNodes": 14}})
    assert n["firmware"] == "2.7.11" and n["modem_preset"] == "MEDIUM_FAST" and n["online_nodes"] == 14 and n["lat"] == 52.1


async def test_own_frame_echo_is_not_stored(engine):
    eng, _mux, db = engine
    eng.on_rxpk(rxpk(make_frame("me", sender=ME, pid=0xABCD0001)))
    await settle(eng)
    assert eng.counters["self_echo"] == 1 and eng.counters["rx"] == 1
    assert await db.fetch("SELECT * FROM mesh_packets WHERE direction = 'rx'") == []


async def test_frame_listener_ok_to_mqtt_and_own_sends(engine):
    eng, mux, _ = engine
    frames = []
    eng.add_frame_listener(lambda raw, hdr, chan, info: frames.append((raw, hdr, chan.name, info)))
    eng.on_rxpk(rxpk(make_frame("public")))
    # firmware >= 2.5 with "OK to MQTT" off: bitfield present, bit 0 clear
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.Value("TEXT_MESSAGE_APP"), payload=b"private", bitfield=0)
    hdr = Header(to=BROADCAST, sender=OTHER, packet_id=0x77, hop_limit=3, hop_start=3, channel_hash=8)
    eng.on_rxpk(rxpk(hdr.pack() + crypt(KEY, 0x77, OTHER, data.SerializeToString())))
    await settle(eng)
    assert [f[3]["ok_to_mqtt"] for f in frames] == [True, False]
    assert frames[0][2] == "LongFast" and frames[0][3]["snr"] == 5.5 and frames[0][0] == make_frame("public")
    pid = await eng.send_text("from us")
    assert frames[-1][1].packet_id == pid and frames[-1][3]["rssi"] is None
    mux.pulling = False      # not transmitted: no packet id, nothing for the uplink
    assert await eng.send_text("lost") is None and frames[-1][1].packet_id == pid
