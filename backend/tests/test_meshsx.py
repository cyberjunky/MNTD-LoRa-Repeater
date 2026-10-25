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
