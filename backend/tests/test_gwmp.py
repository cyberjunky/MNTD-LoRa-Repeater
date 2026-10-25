"""End-to-end test of the GWMP mux with a fake packet forwarder and a fake upstream,
all on localhost UDP sockets."""

import asyncio
import json

import pytest

from lorarepeater.config import GwmpConfig, UpstreamConfig
from lorarepeater.db import Database
from lorarepeater.gwmp import PULL_ACK, PULL_DATA, PULL_RESP, PUSH_ACK, PUSH_DATA, TX_ACK, GwmpMux

EUI = bytes.fromhex("AA555A0000000001")
RXPK = {"rxpk": [{"tmst": 123456, "chan": 2, "rfch": 0, "freq": 868.5, "stat": 1, "modu": "LORA",
                  "datr": "SF7BW125", "codr": "4/5", "rssi": -70, "lsnr": 8.5, "size": 13,
                  "data": "QCsaASYABwACAQID3d3d3Q=="}],
        "stat": {"time": "2026-10-25 12:00:00 GMT", "rxnb": 1, "rxok": 1, "rxfw": 1, "ackr": 100.0, "dwnb": 0, "txnb": 0}}


class Endpoint(asyncio.DatagramProtocol):
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.q.put_nowait((data, addr))


async def free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def setup(tmp_path):
    loop = asyncio.get_running_loop()
    up_port, mux_port = await free_port(), await free_port()
    # fake upstream LNS
    up = Endpoint()
    await loop.create_datagram_endpoint(lambda: up, local_addr=("127.0.0.1", up_port))
    db = Database(tmp_path / "t.db")
    await db.open()
    cfg = GwmpConfig(listen_port=mux_port, pull_interval_s=0.2,
                     upstreams=[UpstreamConfig(name="lns", host="127.0.0.1", port=up_port)])
    mux = GwmpMux(cfg, db)
    await mux.start()
    # fake packet forwarder
    fwd = Endpoint()
    await loop.create_datagram_endpoint(lambda: fwd, remote_addr=("127.0.0.1", mux_port))
    yield mux, db, up, fwd
    await mux.stop()
    await db.close()
    up.transport.close()
    fwd.transport.close()


async def recv(ep: Endpoint, ident: int, timeout=3.0):
    """Wait for a datagram of the given identifier, skipping others."""
    end = asyncio.get_running_loop().time() + timeout
    while True:
        data, addr = await asyncio.wait_for(ep.q.get(), timeout=max(0.01, end - asyncio.get_running_loop().time()))
        if data[3] == ident:
            return data, addr


async def test_uplink_is_acked_logged_and_forwarded(setup):
    mux, db, up, fwd = setup
    fwd.transport.sendto(bytes([2, 0x12, 0x34, PUSH_DATA]) + EUI + json.dumps(RXPK).encode())
    ack, _ = await recv(fwd, PUSH_ACK)
    assert ack[1:3] == b"\x12\x34"
    pushed, _ = await recv(up, PUSH_DATA)
    assert pushed[4:12] == EUI
    assert json.loads(pushed[12:]) == RXPK
    await asyncio.sleep(0.1)
    rows = await db.fetch("SELECT * FROM lorawan_packets")
    assert len(rows) == 1
    assert rows[0]["devaddr"] == "26011A2B" and rows[0]["fcnt"] == 7 and rows[0]["fport"] == 2
    assert json.loads(rows[0]["forwarded"]) == ["lns"]
    stats = await db.fetch("SELECT * FROM gateway_stats")
    assert stats[0]["rxok"] == 1
    assert mux.counters["rxpk"] == 1


async def test_keepalive_and_downlink_roundtrip(setup):
    mux, db, up, fwd = setup
    # forwarder opens its pull channel; mux learns the EUI from a PUSH first
    fwd.transport.sendto(bytes([2, 0, 1, PUSH_DATA]) + EUI + b'{"stat":{"rxnb":0}}')
    fwd.transport.sendto(bytes([2, 0, 2, PULL_DATA]) + EUI)
    ack, _ = await recv(fwd, PULL_ACK)
    assert ack[1:3] == b"\x00\x02"
    # mux keeps a PULL_DATA keepalive to the upstream and the upstream acks it
    pull, addr = await recv(up, PULL_DATA)
    assert pull[4:12] == EUI
    up.transport.sendto(bytes([2, pull[1], pull[2], PULL_ACK]), addr)
    await asyncio.sleep(0.1)
    assert mux.upstreams[0].alive
    # upstream sends a downlink; it must reach the forwarder with the same token
    txpk = {"txpk": {"imme": False, "tmst": 1000000, "freq": 869.525, "rfch": 0, "powe": 14, "modu": "LORA",
                     "datr": "SF9BW125", "codr": "4/5", "ipol": True, "size": 12, "data": "YAEAAAAAAQABAgMEBQ=="}}
    up.transport.sendto(bytes([2, 0xBE, 0xEF, PULL_RESP]) + json.dumps(txpk).encode(), addr)
    resp, _ = await recv(fwd, PULL_RESP)
    assert resp[1:3] == b"\xbe\xef"
    assert json.loads(resp[4:]) == txpk
    # forwarder acknowledges transmission; the ack is routed back to the upstream
    fwd.transport.sendto(bytes([2, 0xBE, 0xEF, TX_ACK]) + EUI + b'{"txpk_ack":{"error":"NONE"}}')
    txack, _ = await recv(up, TX_ACK)
    assert txack[1:3] == b"\xbe\xef"
    await asyncio.sleep(0.1)
    rows = await db.fetch("SELECT * FROM lorawan_packets WHERE direction='down'")
    assert len(rows) == 1 and rows[0]["upstream"] == "lns" and json.loads(rows[0]["forwarded"]) == ["radio"]
    # a second upstream answer for the same tmst is a collision and is dropped
    up.transport.sendto(bytes([2, 0xC0, 0xDE, PULL_RESP]) + json.dumps(txpk).encode(), addr)
    await asyncio.sleep(0.2)
    rows = await db.fetch("SELECT forwarded FROM lorawan_packets WHERE direction='down' ORDER BY id")
    assert json.loads(rows[1]["forwarded"]) == ["dropped:collision"]
