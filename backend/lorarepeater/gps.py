"""GPS and time-reference monitor.

Reads the live fix from gpsd (TCP 2947, JSON watch mode) and the chrony tracking state
(`chronyc -c tracking`), keeps the latest values in memory for /api/status, and samples
fixes into the `gps_fixes` table so the GUI can show fix quality over time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Any

from .config import GpsConfig
from .db import Database

log = logging.getLogger(__name__)

GPS_FIX_SCHEMA = """
CREATE TABLE IF NOT EXISTS gps_fixes (
    id   INTEGER PRIMARY KEY,
    ts   REAL NOT NULL,
    mode INTEGER,                -- 0 unknown, 1 no fix, 2 2D, 3 3D
    lat REAL, lon REAL, alt REAL,
    eph REAL, epv REAL,          -- estimated horizontal / vertical error, m
    sats_used INTEGER, sats_seen INTEGER, hdop REAL,
    gps_time TEXT,               -- ISO time from the receiver
    offset_s REAL                -- chrony system time offset at sample time
);
CREATE INDEX IF NOT EXISTS ix_gps_ts ON gps_fixes(ts);
"""

TRACKING_FIELDS = [
    "ref_id", "ref_name", "stratum", "ref_time", "system_offset", "last_offset", "rms_offset",
    "frequency_ppm", "residual_freq_ppm", "skew_ppm", "root_delay", "root_dispersion",
    "update_interval", "leap_status",
]


class GpsMonitor:
    def __init__(self, cfg: GpsConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.fix: dict[str, Any] = {}
        self.sky: dict[str, Any] = {}
        self.tracking: dict[str, Any] = {}
        self.sources: list[dict[str, Any]] = []
        self.connected = False
        self.last_tpv = 0.0
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.db.conn.executescript(GPS_FIX_SCHEMA)
        await self.db.conn.commit()
        self._tasks = [asyncio.create_task(self._chrony_loop(), name="chrony")]
        if self.cfg.enabled:
            self._tasks += [
                asyncio.create_task(self._gpsd_loop(), name="gpsd"),
                asyncio.create_task(self._sample_loop(), name="gps-sample"),
            ]
        else:
            log.info("gpsd client disabled; time sync from chrony only")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # -- gpsd ------------------------------------------------------------------------

    async def _gpsd_loop(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.cfg.gpsd_host, self.cfg.gpsd_port)
                writer.write(b'?WATCH={"enable":true,"json":true};\n')
                await writer.drain()
                self.connected = True
                await self.db.add_event("gps", "info", f"connected to gpsd at {self.cfg.gpsd_host}:{self.cfg.gpsd_port}")
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("gpsd closed the connection")
                    self._on_gpsd_line(line)
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
                if self.connected:
                    await self.db.add_event("gps", "warn", f"gpsd connection lost: {e}")
                self.connected = False
                log.debug("gpsd unavailable: %s", e)
            await asyncio.sleep(self.cfg.reconnect_s)

    def _on_gpsd_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        cls = msg.get("class")
        if cls == "TPV":
            self.last_tpv = time.time()
            self.fix = {
                "mode": msg.get("mode", 0),
                "lat": msg.get("lat"), "lon": msg.get("lon"),
                "alt": msg.get("altHAE", msg.get("alt")),
                "eph": msg.get("eph"), "epv": msg.get("epv"),
                "speed": msg.get("speed"), "time": msg.get("time"),
                "device": msg.get("device"),
            }
        elif cls == "SKY":
            sats = msg.get("satellites") or []
            self.sky = {
                "sats_used": sum(1 for s in sats if s.get("used")) if sats else msg.get("uSat"),
                "sats_seen": len(sats) if sats else msg.get("nSat"),
                "hdop": msg.get("hdop"), "vdop": msg.get("vdop"), "pdop": msg.get("pdop"),
            }

    # -- chrony ----------------------------------------------------------------------

    async def _chrony_loop(self) -> None:
        if not shutil.which("chronyc"):
            await self.db.add_event("gps", "warn", "chronyc not found; time-sync panel disabled")
            return
        while True:
            try:
                out = await self._run("chronyc", "-c", "tracking")
                parts = out.strip().split(",")
                if len(parts) >= len(TRACKING_FIELDS):
                    self.tracking = dict(zip(TRACKING_FIELDS, parts, strict=False))
                    for k in ("system_offset", "last_offset", "rms_offset", "frequency_ppm",
                              "root_delay", "root_dispersion", "update_interval"):
                        try:
                            self.tracking[k] = float(self.tracking[k])
                        except (ValueError, TypeError):
                            pass
                out = await self._run("chronyc", "-c", "sources")
                srcs = []
                for ln in out.strip().splitlines():
                    f = ln.split(",")
                    if len(f) >= 10:
                        srcs.append({"mode": f[0], "state": f[1], "name": f[2], "stratum": f[3],
                                     "poll": f[4], "reach": f[5], "last_rx": f[6],
                                     "offset": f[7], "measured": f[8], "error": f[9]})
                self.sources = srcs
            except Exception as e:  # chronyd not running, etc.
                log.debug("chronyc failed: %s", e)
            await asyncio.sleep(self.cfg.chrony_interval_s)

    @staticmethod
    async def _run(*argv: str) -> str:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode(errors="replace")

    # -- sampling --------------------------------------------------------------------

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sample_interval_s)
            if not self.fix:
                continue
            await self.db._insert("gps_fixes", {
                "ts": time.time(), "mode": self.fix.get("mode"),
                "lat": self.fix.get("lat"), "lon": self.fix.get("lon"), "alt": self.fix.get("alt"),
                "eph": self.fix.get("eph"), "epv": self.fix.get("epv"),
                "sats_used": self.sky.get("sats_used"), "sats_seen": self.sky.get("sats_seen"),
                "hdop": self.sky.get("hdop"), "gps_time": self.fix.get("time"),
                "offset_s": self.tracking.get("system_offset"),
            })

    # -- status ----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        mode = self.fix.get("mode", 0)
        return {
            "enabled": self.cfg.enabled,
            "gpsd": f"{self.cfg.gpsd_host}:{self.cfg.gpsd_port}",
            "connected": self.connected,
            "fix": {0: "unknown", 1: "none", 2: "2D", 3: "3D"}.get(mode, "unknown"),
            "fix_age_s": round(time.time() - self.last_tpv, 1) if self.last_tpv else None,
            **{k: v for k, v in self.fix.items() if k != "mode"},
            **self.sky,
            "time_sync": {
                "source": self.tracking.get("ref_name"),
                "ref_id": self.tracking.get("ref_id"),
                "stratum": self.tracking.get("stratum"),
                "offset_s": self.tracking.get("system_offset"),
                "rms_offset_s": self.tracking.get("rms_offset"),
                "leap": self.tracking.get("leap_status"),
                "sources": self.sources,
            },
        }
