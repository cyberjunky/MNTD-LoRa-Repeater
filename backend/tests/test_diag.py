import pytest

from lorarepeater import diag
from lorarepeater.diag import (
    decode_throttled,
    filter_log,
    mask_secrets,
    parse_env_file,
    parse_systemctl_show,
)

SYSTEMCTL_SHOW = """Id=lora-pkt-fwd.service
Description=Semtech SX1302 LoRa packet forwarder (RAK2287)
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=2
Result=success
ActiveEnterTimestamp=Sat 2026-10-25 16:03:32 CEST
ExecMainPID=1234

Id=systemd-timesyncd.service
Description=Network Time Synchronization
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=enabled
NRestarts=0
Result=success
ActiveEnterTimestamp=
ExecMainPID=0

Id=meshtasticd.service
Description=meshtasticd.service
LoadState=not-found
ActiveState=inactive
SubState=dead
"""

JOURNAL = """2026-10-25T16:03:32+0200 lora_pkt_fwd[1182]: INFO: syncword not found at idx 517
2026-10-25T16:03:32+0200 lora_pkt_fwd[1182]: INFO: concentrator started, packet can now be received
2026-10-25T16:03:33+0200 lora_pkt_fwd[1182]: WARNING: no temperature sensor found, reporting 25 C
2026-10-25T16:03:34+0200 lora_pkt_fwd[1182]: INFO: syncword not found at idx 518
"""


def test_parse_env_file():
    env = parse_env_file('# c\nSX1302_RESET_PIN=25\nLGW_SERVICE_SYNCWORD="0x2B"\nbroken line\n')
    assert env == {"SX1302_RESET_PIN": "25", "LGW_SERVICE_SYNCWORD": "0x2B"}
    assert parse_env_file(None) == {}


def test_parse_systemctl_show():
    blocks = parse_systemctl_show(SYSTEMCTL_SHOW)
    assert [b["Id"] for b in blocks] == ["lora-pkt-fwd.service", "systemd-timesyncd.service", "meshtasticd.service"]
    assert blocks[0]["NRestarts"] == "2" and blocks[1]["ActiveEnterTimestamp"] == ""


def test_mask_secrets():
    cfg = {"telegram": {"token": "123456:ABCDEFGHIJ", "notify_chat_id": 42},
           "meshsx": {"channels": [{"name": "LongFast", "psk": "AQ=="}]}, "web": {"port": 8080}, "empty": {"token": ""}}
    m = mask_secrets(cfg)
    assert m["telegram"]["token"].startswith("••••") and m["telegram"]["token"].endswith("IJ")
    assert m["meshsx"]["channels"][0]["psk"] == "••••••" and m["meshsx"]["channels"][0]["name"] == "LongFast"
    assert m["telegram"]["notify_chat_id"] == 42 and m["web"]["port"] == 8080 and m["empty"]["token"] == ""


def test_decode_throttled():
    assert decode_throttled(0) == []
    assert decode_throttled(0x50005) == ["under-voltage now", "throttled now",
                                         "under-voltage since boot", "throttled since boot"]


def test_filter_log():
    assert "syncword" not in filter_log(JOURNAL, None, True, 100)
    assert filter_log(JOURNAL, None, True, 100).count("\n") == 1
    assert filter_log(JOURNAL, "warn", False, 100).startswith("2026-10-25T16:03:33")
    assert filter_log(JOURNAL, None, False, 1) == JOURNAL.splitlines()[-1]
    assert filter_log(JOURNAL, "[unbalanced", False, 100) == ""   # bad regex falls back to a literal


@pytest.fixture
def fake_run(monkeypatch):
    calls: list[list[str]] = []

    async def run(cmd, timeout=15.0, cwd=None, env=None):
        calls.append(cmd)
        if cmd[0] == "systemctl" and cmd[1] == "show":
            return 0, SYSTEMCTL_SHOW
        if cmd[0] == "systemctl":
            return 0, ""
        if cmd[0] == "journalctl":
            return 0, JOURNAL
        if cmd[0] == "vcgencmd":
            return 0, "throttled=0x0"
        if cmd[0] == "./chip_id":
            return 0, "Note: chip version is 0x10 (v1.0)\nINFO: concentrator EUI: 0x0016C001FF000001\n"
        return 0, f"{cmd[0]} ok"

    monkeypatch.setattr(diag, "run", run)
    return calls


async def test_diag_endpoints(client, fake_run):
    svc = (await client.get("/api/diag/services")).json()
    assert svc[0]["unit"] == "lora-pkt-fwd" and svc[0]["restarts"] == 2 and svc[0]["restartable"]
    assert svc[1]["active"] == "inactive" and svc[1]["since"] is None
    assert len(svc) == 2   # meshtasticd is not installed: hidden

    log = (await client.get("/api/diag/log?source=lora-pkt-fwd&lines=50")).json()
    assert log["lines"] == 2 and "syncword" not in log["text"]
    txt = await client.get("/api/diag/log.txt?source=kernel&lines=50")
    assert txt.headers["content-type"].startswith("text/plain") and "syncword" in txt.text
    assert (await client.get("/api/diag/log?source=nope")).status_code == 404

    sysinfo = (await client.get("/api/diag/system")).json()
    assert sysinfo["throttled"] == [] and sysinfo["hostname"]

    cfg = (await client.get("/api/diag/config")).json()
    assert cfg["web"]["port"] == 8080 and "telegram" in cfg

    names = [a["name"] for a in (await client.get("/api/diag/actions")).json()]
    assert "restart:lora-pkt-fwd" in names and "probe-concentrator" in names
    assert (await client.post("/api/diag/action/restart:sshd")).status_code == 404
    assert (await client.post("/api/diag/action/nope")).status_code == 404

    r = (await client.post("/api/diag/action/restart:systemd-timesyncd")).json()
    assert r["ok"] and ["systemctl", "restart", "systemd-timesyncd.service"] in fake_run

    r = (await client.post("/api/diag/action/probe-concentrator")).json()
    assert r["ok"] and r["eui"] == "0016C001FF000001" and r["reset_pin"] == 25
    assert fake_run[-1] == ["systemctl", "start", "lora-pkt-fwd.service"]
    assert any(c[0].endswith("reset_lgw.sh") for c in fake_run)

    r = (await client.post("/api/diag/action/clear-logs")).json()
    assert r["ok"] and fake_run[-1] == ["systemctl", "start", "lorarepeater-clear-logs.service"]

    assert (await client.get("/api/mesh/nodes")).json()
    r = (await client.post("/api/diag/action/clear-database")).json()
    assert r["ok"] and "lorawan_packets 1" in r["output"] and "mesh_packets 1" in r["output"]
    assert (await client.get("/api/lorawan/packets")).json() == []
    assert (await client.get("/api/mesh/nodes")).json()          # node list kept
    assert (await client.post("/api/diag/action/clear-database-all")).json()["ok"]
    assert (await client.get("/api/mesh/nodes")).json() == []
