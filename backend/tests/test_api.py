import time

import pytest
from httpx import ASGITransport, AsyncClient

from lorarepeater.api import create_app
from lorarepeater.config import Config, GwmpConfig, MeshConfig
from lorarepeater.db import Database
from lorarepeater.gps import GpsMonitor
from lorarepeater.gwmp import GwmpMux
from lorarepeater.mesh import MeshIngest


@pytest.fixture
async def client(tmp_path):
    cfg = Config(db_path=tmp_path / "api.db", gwmp=GwmpConfig(listen_port=0), mesh=MeshConfig(enabled=False))
    cfg.gps.enabled = False
    db = Database(cfg.db_path)
    await db.open()
    mux = GwmpMux(cfg.gwmp, db)          # not started: no sockets needed for the API
    mesh = MeshIngest(cfg.mesh, db)
    gps = GpsMonitor(cfg.gps, db)
    await gps.start()                    # only creates the table when disabled
    await db.add_lorawan_packet({"direction": "up", "freq": 868.1, "datr": "SF7BW125", "rssi": -80, "lsnr": 7,
                                 "stat": 1, "devaddr": "26011A2B", "fcnt": 3, "mtype": "UnconfirmedDataUp", "forwarded": ["x"]})
    await db.add_mesh_packet({"from_node": 1, "to_node": 0xFFFFFFFF, "portnum": "TEXT_MESSAGE_APP", "text": "hi",
                              "hop_limit": 2, "hop_start": 3, "hops_used": 1, "rssi": -90, "snr": 5})
    await db.upsert_mesh_node({"num": 1, "node_id": "!00000001", "long_name": "Test", "last_heard": time.time()})
    await db.add_event("system", "info", "test")
    app = create_app(cfg, db, mux, mesh, gps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.close()


async def test_status(client):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["lorawan"] == 1 and body["db"]["mesh"] == 1 and body["db"]["nodes"] == 1
    assert body["gwmp"]["upstreams"] == [] and body["mesh"]["enabled"] is False
    assert body["gps"]["fix"] == "unknown"


async def test_lorawan_endpoints(client):
    pk = (await client.get("/api/lorawan/packets")).json()
    assert pk[0]["devaddr"] == "26011A2B" and pk[0]["forwarded"] == ["x"]
    assert (await client.get("/api/lorawan/packets?devaddr=26011a2b")).json()
    assert (await client.get("/api/lorawan/packets?devaddr=DEADBEEF")).json() == []
    s = (await client.get("/api/lorawan/summary?hours=6")).json()
    assert s["totals"]["up"] == 1 and s["devices"][0]["devaddr"] == "26011A2B"
    assert s["by_freq"][0]["freq"] == 868.1


async def test_mesh_endpoints(client):
    pk = (await client.get("/api/mesh/packets?text_only=true")).json()
    assert pk[0]["text"] == "hi" and pk[0]["hops_used"] == 1
    nodes = (await client.get("/api/mesh/nodes")).json()
    assert nodes[0]["long_name"] == "Test"
    s = (await client.get("/api/mesh/summary")).json()
    assert s["totals"]["relayed"] == 1 and s["by_hops"][0]["hops_used"] == 1
    assert s["top_nodes"][0]["long_name"] == "Test"


async def test_events_and_gps(client):
    assert (await client.get("/api/events")).json()[0]["message"] == "test"
    assert (await client.get("/api/gps/history")).json() == []
