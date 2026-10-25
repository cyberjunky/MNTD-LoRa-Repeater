async def test_status(client):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["lorawan"] == 1 and body["db"]["mesh"] == 1 and body["db"]["nodes"] == 1
    assert body["gwmp"]["upstreams"] == [] and body["mesh"]["enabled"] is False
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
    r = await client.get("/static/app.js")
    assert r.status_code == 200


def test_find_web_dir(tmp_path):
    from lorarepeater.api import find_web_dir
    assert find_web_dir(tmp_path) is None            # no index.html there
    (tmp_path / "index.html").write_text("<html></html>")
    assert find_web_dir(tmp_path) == tmp_path
