async def test_status(client):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["lorawan"] == 1 and body["db"]["mesh"] == 1 and body["db"]["nodes"] == 1
    assert body["gwmp"]["upstreams"] == [] and "mesh" not in body
    assert body["time"] == {}


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


async def test_events(client):
    assert (await client.get("/api/events")).json()[0]["message"] == "test"


async def test_index_serves_gui(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert r.headers["cache-control"] == "no-cache"
    r = await client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "cache-control" not in (await client.get("/api/status")).headers



async def test_helium_traffic(client):
    r = (await client.get("/api/helium/traffic?hours=6")).json()
    assert r["offered"] == 0 and r["downlinks"] == [] and r["devices"] == [] and r["earned"]["HNT"]["amount"] is None


async def test_mesh_send_without_node(client):
    r = await client.post("/api/mesh/send", json={"text": "hi"})
    assert r.status_code == 503


async def test_mesh_send(tmp_path):
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from lorarepeater.api import create_app
    from lorarepeater.config import Config, GwmpConfig
    from lorarepeater.db import Database
    from lorarepeater.gwmp import GwmpMux
    from lorarepeater.helium import Helium
    from lorarepeater.timesync import TimeSync

    sent = []

    async def send_text(text, to, channel):
        sent.append((text, to, channel))
        return None if text == "fail" else 7

    mesh = SimpleNamespace(channels=[object()], send_text=send_text, status=lambda: {})
    cfg = Config(db_path=tmp_path / "s.db", gwmp=GwmpConfig(listen_port=0))
    db = Database(cfg.db_path)
    await db.open()
    app = create_app(cfg, db, GwmpMux(cfg.gwmp, db), TimeSync(), Helium(cfg.helium, db), extras={"meshsx": mesh})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/api/mesh/send", json={"text": "hello", "to": "!11223344"})).json() == {"packet_id": 7, "to": "!11223344"}
        assert (await c.post("/api/mesh/send", json={"text": "all"})).json()["to"] == "!ffffffff"
        assert (await c.post("/api/mesh/send", json={"text": "x" * 231})).status_code == 422
        assert (await c.post("/api/mesh/send", json={"text": "x", "channel": 1})).status_code == 422
        assert (await c.post("/api/mesh/send", json={"text": "x", "to": "!zz"})).status_code == 422
        assert (await c.post("/api/mesh/send", json={"text": "fail"})).status_code == 503
    assert sent[:2] == [("hello", 0x11223344, 0), ("all", 0xFFFFFFFF, 0)]
    await db.close()


async def test_settings_roundtrip_keeps_masked_secrets(client):
    from lorarepeater.config import Config

    s = (await client.get("/api/settings")).json()
    assert set(s) == {"mqtt", "telegram"} and s["mqtt"]["source"] == "config.yaml"
    mqtt = s["mqtt"]["value"]
    assert mqtt["meshtastic"]["password"].startswith("••") and mqtt["meshtastic"]["password"] != "large4cats"
    mqtt["meshtastic"]["enabled"] = True
    mqtt["homeassistant"].update(enabled=True, host="ha.local", password="secret!")
    r = await client.put("/api/settings/mqtt", json=mqtt)
    assert r.status_code == 200 and r.json()["restarting"]
    s = (await client.get("/api/settings")).json()
    assert s["mqtt"]["source"].endswith("settings.yaml")
    # what the service loads after the restart: config.yaml (none here) with the saved section on top
    db_path = s["mqtt"]["source"].replace("settings.yaml", "api.db")
    cfg = Config.load(None).model_copy(update={"db_path": __import__("pathlib").Path(db_path)})
    saved = cfg.saved_settings()["mqtt"]
    assert saved["meshtastic"]["password"] == "large4cats" and saved["meshtastic"]["enabled"] is True
    assert saved["homeassistant"]["password"] == "secret!" and saved["homeassistant"]["host"] == "ha.local"
    assert (await client.put("/api/settings/mqtt", json={"meshtastic": {"port": "x"}})).status_code == 422
    assert (await client.put("/api/settings/gwmp", json={})).status_code == 404
