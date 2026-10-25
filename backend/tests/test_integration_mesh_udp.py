"""Meshtastic frame through the real UDP path: fake packet forwarder -> GwmpMux -> MeshSxEngine
-> relayed frame back to the forwarder as PULL_RESP, and nothing of it reaching the LoRaWAN upstream."""

import asyncio
import base64
import json

import pytest

from lorarepeater.config import GwmpConfig, MeshSxConfig, UpstreamConfig
from lorarepeater.db import Database
from lorarepeater.gwmp import PULL_DATA, PULL_RESP, PUSH_ACK, PUSH_DATA, GwmpMux
from lorarepeater.meshproto import Header
from lorarepeater.meshsx import MeshSxEngine
from tests.test_gwmp import EUI, Endpoint, free_port, recv
from tests.test_meshsx import ME, OTHER, make_frame, rxpk


@pytest.fixture
async def stack(tmp_path):
    loop = asyncio.get_running_loop()
    up_port, mux_port = await free_port(), await free_port()
    up = Endpoint()
    await loop.create_datagram_endpoint(lambda: up, local_addr=("127.0.0.1", up_port))
    db = Database(tmp_path / "i.db")
    await db.open()
    mux = GwmpMux(GwmpConfig(listen_port=mux_port, pull_interval_s=5,
                             upstreams=[UpstreamConfig(name="lns", host="127.0.0.1", port=up_port)]), db)
    await mux.start()
    eng = MeshSxEngine(MeshSxConfig(enabled=True, node_num=ME, relay_delay_ms=0), db, mux)
    await eng.start()
    fwd = Endpoint()
    await loop.create_datagram_endpoint(lambda: fwd, remote_addr=("127.0.0.1", mux_port))
    yield mux, eng, db, up, fwd
    await eng.stop()
    await mux.stop()
    await db.close()
    up.transport.close()
    fwd.transport.close()


async def test_mesh_frame_is_claimed_relayed_and_not_forwarded(stack):
    mux, _eng, db, up, fwd = stack
    fwd.transport.sendto(bytes([2, 0, 1, PULL_DATA]) + EUI)
    frame = make_frame("over udp", sender=OTHER, pid=0x4242)
    lorawan = {"tmst": 5, "chan": 1, "rfch": 0, "freq": 868.3, "stat": 1, "modu": "LORA", "datr": "SF7BW125",
               "codr": "4/5", "rssi": -60, "lsnr": 9, "size": 16, "data": "QCsaASYABwACAQIDqrvM3Q=="}
    msg = {"rxpk": [rxpk(frame, tmst=7_000_000), lorawan], "stat": {"rxnb": 2, "rxok": 2}}
    fwd.transport.sendto(bytes([2, 0x11, 0x22, PUSH_DATA]) + EUI + json.dumps(msg).encode())
    await recv(fwd, PUSH_ACK)
    # the upstream gets the LoRaWAN packet and the stat block, but not the Meshtastic frame
    pushed, _ = await recv(up, PUSH_DATA)
    fwd_msg = json.loads(pushed[12:])
    assert [r["freq"] for r in fwd_msg["rxpk"]] == [868.3] and fwd_msg["stat"]["rxnb"] == 2
    # the engine relayed it: the forwarder receives a PULL_RESP with the hop-decremented copy
    resp, _ = await recv(fwd, PULL_RESP)
    txpk = json.loads(resp[4:])["txpk"]
    h = Header.parse(base64.b64decode(txpk["data"]))
    assert h.sender == OTHER and h.packet_id == 0x4242 and h.hop_limit == 2 and h.relay_node == ME & 0xFF
    assert txpk["freq"] == 869.525 and txpk["datr"] == "SF11BW250" and txpk["tmst"] > 7_000_000
    await asyncio.sleep(0.1)
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM lorawan_packets"))["n"] == 1
    mesh_rows = await db.fetch("SELECT text, relayed_by_us FROM mesh_packets")
    assert mesh_rows == [{"text": "over udp", "relayed_by_us": 1}]
    assert mux.counters["claimed"] == 1
