"""Diagnostics API: service state, journal, hardware probe, effective config and admin actions.

Everything runs as the unprivileged service user. Restarting units works through the polkit
rule in install/polkit/10-lorarepeater.rules; the journal is readable because the unit adds
the systemd-journal group. Each shell-out is a fixed argv (no shell), with a timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .config import Config
from .db import Database

UNITS = ["lora-pkt-fwd", "lorarepeater", "systemd-timesyncd"]
RESTARTABLE = list(UNITS)
LOG_SOURCES = {**{u: ["-u", u] for u in UNITS}, "kernel": ["-k"], "all": []}
NOISE = ("syncword not found",)  # lora_pkt_fwd debug chatter
SECRET_KEYS = ("token", "psk", "password", "secret", "key")
BUTTON_PIN = 7
FORWARDER_ENV = Path("/etc/default/lora-pkt-fwd")
CLEAR_LOGS_UNIT = "lorarepeater-clear-logs.service"
GLOBAL_CONF = Path("/etc/lorarepeater/global_conf.json")
GLOBAL_CONF_KEYS = ("gateway_ID", "server_address", "serv_port_up", "serv_port_down")
DEV_GLOBS = ("spidev*", "i2c-*", "ttyS*", "ttyAMA*", "ttyACM*", "ttyUSB*", "gpiochip*")


async def run(cmd: list[str], timeout: float = 15.0, cwd: str | None = None,
              env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run argv, return (exit code, combined output). 127 if the tool is missing, 124 on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd,
            env={**os.environ, **(env or {})},
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
        return 127, f"{cmd[0]}: {e.strerror or e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"{' '.join(cmd)}: timeout after {timeout:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace")


def _read(path: Path, limit: int = 65536) -> str | None:
    try:
        return path.read_text(errors="replace")[:limit]
    except OSError:
        return None


def parse_env_file(text: str | None) -> dict[str, str]:
    """KEY=value lines (what /etc/default/lora-pkt-fwd holds); comments and blanks skipped."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def parse_systemctl_show(text: str) -> list[dict[str, str]]:
    """`systemctl show` prints one KEY=VALUE block per unit, blocks separated by blank lines."""
    units: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        units.append(cur)
    return units


def mask_secrets(obj: Any, key: str = "") -> Any:
    """Replace values whose key looks secret; empty values stay visible so 'not set' is obvious."""
    if isinstance(obj, dict):
        return {k: mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(v, key) for v in obj]
    if isinstance(obj, str) and obj and any(s in key.lower() for s in SECRET_KEYS):
        return "••••••" + (obj[-2:] if len(obj) > 8 else "")
    return obj


def undervoltage() -> bool | None:
    """The firmware's under-voltage alarm via the rpi_volt hwmon sensor (readable without root,
    unlike `vcgencmd`, which needs /dev/vcio)."""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        if (_read(hw / "name") or "").strip() == "rpi_volt":
            alarm = _read(hw / "in0_lcrit_alarm")
            return alarm.strip() == "1" if alarm else None
    return None


def filter_log(text: str, pattern: str | None, quiet: bool, lines: int) -> str:
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
    out = []
    for line in text.splitlines():
        if quiet and any(n in line for n in NOISE):
            continue
        if rx and not rx.search(line):
            continue
        out.append(line)
    return "\n".join(out[-lines:])


class Diag:
    def __init__(self, cfg: Config, db: Database, restart_self: Any | None = None):
        self.cfg = cfg
        self.db = db
        self.hal_dir = Path(cfg.hardware.hal_dir)
        self.reset_script = cfg.hardware.reset_script
        self.restart_self = restart_self
        self._probe_lock = asyncio.Lock()

    # -- read-only ----------------------------------------------------------------

    async def system(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "time": time.time(),
        }
        osr = parse_env_file(_read(Path("/etc/os-release")))
        info["os"] = osr.get("PRETTY_NAME")
        up = _read(Path("/proc/uptime"))
        info["uptime_s"] = float(up.split()[0]) if up else None
        try:
            info["load"] = [round(v, 2) for v in os.getloadavg()]
        except (OSError, AttributeError):
            info["load"] = None
        mem: dict[str, int] = {}
        for line in (_read(Path("/proc/meminfo")) or "").splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.split()[0]) * 1024
        info["mem_total"] = mem.get("MemTotal")
        info["mem_available"] = mem.get("MemAvailable")
        try:
            du = shutil.disk_usage("/")
            info["disk"] = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            info["disk"] = None
        model = _read(Path("/proc/device-tree/model"))
        info["model"] = model.rstrip("\0") if model else None
        info["undervoltage"] = undervoltage()
        info["forwarder_env"] = parse_env_file(_read(FORWARDER_ENV))
        info["global_conf"] = None
        gc = _read(GLOBAL_CONF)
        if gc:
            try:
                g = json.loads(gc).get("gateway_conf", {})
                info["global_conf"] = {k: g.get(k) for k in GLOBAL_CONF_KEYS}
            except ValueError:
                pass
        ver = _read(self.hal_dir / "VERSION")
        info["hal_version"] = ver.strip() if ver else None
        return info

    async def services(self) -> list[dict[str, Any]]:
        props = ("Id,Description,LoadState,ActiveState,SubState,UnitFileState,NRestarts,Result,"
                 "ActiveEnterTimestamp,ExecMainPID")
        _, out = await run(["systemctl", "show", "-p", props, *[f"{u}.service" for u in UNITS]], 10)
        rows = []
        for blk in parse_systemctl_show(out):
            unit = blk.get("Id", "").removesuffix(".service")
            rows.append({
                "unit": unit, "description": blk.get("Description"), "load": blk.get("LoadState"),
                "active": blk.get("ActiveState"), "sub": blk.get("SubState"),
                "enabled": blk.get("UnitFileState"), "restarts": int(blk.get("NRestarts") or 0),
                "result": blk.get("Result"), "since": blk.get("ActiveEnterTimestamp") or None,
                "pid": int(blk.get("ExecMainPID") or 0) or None,
                "restartable": unit in RESTARTABLE,
            })
        return rows

    async def log(self, source: str, lines: int, pattern: str | None, quiet: bool) -> str:
        if source not in LOG_SOURCES:
            raise HTTPException(404, f"unknown log source {source!r}")
        # fetch more than asked when filtering so the filter still yields `lines` rows
        fetch = lines * (4 if (pattern or quiet) else 1)
        rc, out = await run(["journalctl", *LOG_SOURCES[source], "-n", str(fetch), "--no-pager",
                             "-o", "short-iso", "--no-hostname"], 20)
        if rc != 0 and not out.strip():
            out = f"journalctl failed (rc={rc})"
        return filter_log(out, pattern, quiet, lines)

    async def hardware(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        _, txt = await run(["i2cdetect", "-y", "1"], 10)
        out["i2cdetect"] = txt.strip()
        pins = f"{BUTTON_PIN},{self.reset_pin()}"
        _, txt = await run(["pinctrl", "get", pins], 5)
        out["gpio"] = txt.strip()
        dev = Path("/dev")
        out["devices"] = [str(p) for pat in DEV_GLOBS for p in sorted(dev.glob(pat))]
        leds = Path("/sys/class/leds")
        out["leds"] = sorted(p.name for p in leds.iterdir()) if leds.is_dir() else []
        out["i2c_drivers"] = {}
        i2c = Path("/sys/bus/i2c/devices")
        if i2c.is_dir():
            for d in sorted(i2c.iterdir()):
                name = _read(d / "name")
                if name and d.name[0].isdigit():
                    drv = d / "driver"
                    bound = drv.resolve().name if drv.exists() else "no driver"
                    out["i2c_drivers"][d.name] = f"{name.strip()} ({bound})"
        try:
            raw = Path("/sys/bus/i2c/devices/1-0050/eeprom").read_bytes()[:64]
            out["eeprom_head"] = raw.hex(" ")
        except OSError:
            out["eeprom_head"] = None
        rc, txt = await run(["lsusb"], 5)
        out["usb"] = txt.strip() if rc == 0 else None
        out["reset_pin"] = self.reset_pin()
        return out

    def reset_pin(self) -> int:
        env = parse_env_file(_read(FORWARDER_ENV))
        try:
            return int(env.get("SX1302_RESET_PIN", 25))
        except ValueError:
            return 25

    def config(self) -> dict[str, Any]:
        return mask_secrets(self.cfg.model_dump(mode="json"))

    # -- actions ------------------------------------------------------------------

    def actions(self) -> list[dict[str, str]]:
        acts = [{"name": f"restart:{u}", "label": f"Restart {u}", "desc": f"systemctl restart {u}"}
                for u in RESTARTABLE]
        acts.append({"name": "probe-concentrator", "label": "Probe concentrator",
                     "desc": "stop the forwarder, pulse reset, run chip_id, start the forwarder "
                             "again (about 15 s)"})
        acts += [
            {"name": "clear-database", "label": "Clear database",
             "desc": "delete all logged packets, gateway stats and events; keep the node list"},
            {"name": "clear-database-all", "label": "Clear database incl. nodes",
             "desc": "as clear-database, and forget all known Meshtastic nodes"},
            {"name": "clear-logs", "label": "Clear logs",
             "desc": "empty the systemd journal (all units; journald cannot delete per unit)"},
        ]
        return acts

    async def action(self, name: str) -> dict[str, Any]:
        if name.startswith("restart:"):
            unit = name.split(":", 1)[1]
            if unit not in RESTARTABLE:
                raise HTTPException(404, f"unknown unit {unit!r}")
            if unit == "lorarepeater" and self.restart_self is not None:
                self.restart_self()
                return {"ok": True,
                        "output": "restarting the backend in 1 s; the page reconnects by itself"}
            rc, out = await run(["systemctl", "restart", f"{unit}.service"], 30)
            return {"ok": rc == 0, "output": out.strip() or f"{unit} restarted"}
        if name == "probe-concentrator":
            return await self.probe_concentrator()
        if name in ("clear-database", "clear-database-all"):
            counts = await self.db.clear(include_nodes=name == "clear-database-all")
            await self.db.add_event("system", "info", f"database cleared from the GUI: {counts}")
            return {"ok": True, "output": "deleted rows: " + ", ".join(f"{t} {n}" for t, n in counts.items())}
        if name == "clear-logs":
            rc, out = await run(["systemctl", "start", CLEAR_LOGS_UNIT], 60)
            return {"ok": rc == 0, "output": out.strip() or "journal cleared"}
        raise HTTPException(404, f"unknown action {name!r}")

    async def probe_concentrator(self) -> dict[str, Any]:
        if self._probe_lock.locked():
            raise HTTPException(409, "a probe is already running")
        async with self._probe_lock:
            pin = str(self.reset_pin())
            env = {"SX1302_RESET_PIN": pin}
            steps = []
            rc, out = await run(["systemctl", "stop", "lora-pkt-fwd.service"], 30)
            steps.append(f"$ systemctl stop lora-pkt-fwd  (rc={rc})\n{out.strip()}")
            rc, out = await run([self.reset_script, "start"], 10, env=env)
            steps.append(f"$ SX1302_RESET_PIN={pin} {self.reset_script} start  (rc={rc})\n{out.strip()}")
            chip_dir = self.hal_dir / "util_chip_id"
            rc, out = await run(["./chip_id", "-d", "/dev/spidev0.0"], 25, cwd=str(chip_dir), env=env)
            steps.append(f"$ {chip_dir}/chip_id -d /dev/spidev0.0  (rc={rc})\n{out.strip()}")
            m = re.search(r"EUI: 0x([0-9a-fA-F]+)", out)
            rc2, out2 = await run(["systemctl", "start", "lora-pkt-fwd.service"], 30)
            steps.append(f"$ systemctl start lora-pkt-fwd  (rc={rc2})\n{out2.strip()}")
            return {"ok": bool(m), "eui": m.group(1).upper() if m else None, "reset_pin": int(pin),
                    "output": "\n\n".join(steps)}


def add_diag_routes(app: FastAPI, diag: Diag) -> None:
    @app.get("/api/diag/system")
    async def diag_system() -> dict[str, Any]:
        return await diag.system()

    @app.get("/api/diag/services")
    async def diag_services() -> list[dict[str, Any]]:
        return await diag.services()

    @app.get("/api/diag/log")
    async def diag_log(source: str = "lora-pkt-fwd", lines: int = Query(200, ge=10, le=5000),
                       filter: str | None = None, quiet: bool = True) -> dict[str, Any]:
        text = await diag.log(source, lines, filter, quiet)
        return {"source": source, "lines": text.count("\n") + (1 if text else 0), "text": text}

    @app.get("/api/diag/log.txt", response_class=PlainTextResponse)
    async def diag_log_txt(source: str = "lora-pkt-fwd", lines: int = Query(1000, ge=10, le=20000),
                           filter: str | None = None, quiet: bool = False) -> str:
        return await diag.log(source, lines, filter, quiet)

    @app.get("/api/diag/hardware")
    async def diag_hardware() -> dict[str, Any]:
        return await diag.hardware()

    @app.get("/api/diag/config")
    async def diag_config() -> dict[str, Any]:
        return diag.config()

    @app.get("/api/diag/actions")
    async def diag_actions() -> list[dict[str, str]]:
        return diag.actions()

    @app.post("/api/diag/action/{name}")
    async def diag_action(name: str) -> dict[str, Any]:
        return await diag.action(name)
