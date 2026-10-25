"""FastAPI application: JSON API, WebSocket live feed, and the static web GUI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config
from .db import Database
from .gps import GpsMonitor
from .gwmp import GwmpMux
from .mesh import MeshIngest

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


def _read_cpu_temp(path: str) -> float | None:
    try:
        return int(Path(path).read_text().strip()) / 1000.0
    except Exception:
        return None


def create_app(cfg: Config, db: Database, mux: GwmpMux, mesh: MeshIngest, gps: GpsMonitor,
               extras: dict[str, Any] | None = None) -> FastAPI:
    """`extras` maps a name to an optional object with a status() method (meshsx, telegram)."""
    extras = extras or {}
    started = time.time()
    sockets: set[WebSocket] = set()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        loop_holder["loop"] = asyncio.get_running_loop()
        db.subscribe(on_insert)
        yield
        db.unsubscribe(on_insert)

    app = FastAPI(title=cfg.web.title, version=__version__, lifespan=lifespan)

    # -- live feed: db inserts -> websockets ------------------------------------------

    def on_insert(kind: str, row: dict[str, Any]) -> None:
        if not sockets:
            return
        msg = json.dumps({"kind": kind, "row": row}, default=str)
        loop = loop_holder.get("loop")
        if loop is None:
            return
        for ws in list(sockets):
            loop.create_task(_send(ws, msg))

    async def _send(ws: WebSocket, msg: str) -> None:
        try:
            await ws.send_text(msg)
        except Exception:
            sockets.discard(ws)

    @app.websocket("/ws")
    async def ws_feed(ws: WebSocket) -> None:
        await ws.accept()
        sockets.add(ws)
        try:
            while True:
                await ws.receive_text()  # keepalive / ignore client messages
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(ws)

    # -- status -----------------------------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        counts = await db.fetchone(
            "SELECT (SELECT COUNT(*) FROM lorawan_packets) AS lorawan, "
            "(SELECT COUNT(*) FROM mesh_packets) AS mesh, (SELECT COUNT(*) FROM mesh_nodes) AS nodes"
        )
        return {
            "version": __version__,
            "title": cfg.web.title,
            "uptime_s": round(time.time() - started),
            "cpu_temp_c": _read_cpu_temp(cfg.hardware.cpu_temp_path),
            "db": {"path": str(cfg.db_path), "retention_days": cfg.retention_days, **(counts or {})},
            "gwmp": mux.status(),
            "mesh": mesh.status(),
            "gps": gps.status(),
            **{name: (obj.status() if obj is not None else None) for name, obj in extras.items()},
        }

    @app.get("/api/gps/history")
    async def gps_history(hours: int = Query(24, le=24 * 90)) -> list[dict[str, Any]]:
        since = time.time() - hours * 3600
        return await db.fetch("SELECT * FROM gps_fixes WHERE ts >= ? ORDER BY ts", (since,))

    @app.get("/api/events")
    async def events(limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -- LoRaWAN ----------------------------------------------------------------------

    @app.get("/api/lorawan/packets")
    async def lorawan_packets(
        limit: int = Query(100, le=2000), before_id: int | None = None,
        direction: str | None = None, devaddr: str | None = None, mtype: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = {"id < ?": before_id, "direction = ?": direction,
                   "devaddr = ?": devaddr.upper() if devaddr else None, "mtype = ?": mtype}
        where = ["1=1", *[k for k, v in filters.items() if v]]
        params: list[Any] = [v for v in filters.values() if v]
        params.append(limit)
        rows = await db.fetch(f"SELECT * FROM lorawan_packets WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params)
        for r in rows:
            if r.get("forwarded"):
                try:
                    r["forwarded"] = json.loads(r["forwarded"])
                except Exception:
                    pass
        return rows

    @app.get("/api/lorawan/summary")
    async def lorawan_summary(hours: int = Query(24, le=24 * 90)) -> dict[str, Any]:
        since = time.time() - hours * 3600
        bucket = 3600 if hours > 48 else 900
        per_bucket = await db.fetch(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, direction, COUNT(*) AS n, "
            "SUM(CASE WHEN stat = -1 THEN 1 ELSE 0 END) AS crc_bad "
            "FROM lorawan_packets WHERE ts >= ? GROUP BY t, direction ORDER BY t", (bucket, bucket, since))
        by_mtype = await db.fetch(
            "SELECT mtype, COUNT(*) AS n FROM lorawan_packets WHERE ts >= ? GROUP BY mtype ORDER BY n DESC", (since,))
        by_datr = await db.fetch(
            "SELECT datr, COUNT(*) AS n FROM lorawan_packets WHERE ts >= ? AND direction='up' GROUP BY datr ORDER BY n DESC", (since,))
        by_freq = await db.fetch(
            "SELECT freq, COUNT(*) AS n, AVG(rssi) AS rssi, AVG(lsnr) AS snr FROM lorawan_packets "
            "WHERE ts >= ? AND direction='up' GROUP BY freq ORDER BY freq", (since,))
        devices = await db.fetch(
            "SELECT devaddr, COUNT(*) AS n, MAX(ts) AS last_ts, AVG(rssi) AS rssi, AVG(lsnr) AS snr, MAX(fcnt) AS fcnt "
            "FROM lorawan_packets WHERE ts >= ? AND devaddr IS NOT NULL GROUP BY devaddr ORDER BY n DESC LIMIT 50", (since,))
        totals = await db.fetchone(
            "SELECT COUNT(*) AS total, SUM(direction='up') AS up, SUM(direction='down') AS down, "
            "SUM(stat=-1) AS crc_bad, AVG(CASE WHEN direction='up' THEN rssi END) AS rssi, "
            "AVG(CASE WHEN direction='up' THEN lsnr END) AS snr FROM lorawan_packets WHERE ts >= ?", (since,))
        return {"hours": hours, "bucket_s": bucket, "totals": totals, "per_bucket": per_bucket,
                "by_mtype": by_mtype, "by_datr": by_datr, "by_freq": by_freq, "devices": devices}

    @app.get("/api/lorawan/stats")
    async def gateway_stats(limit: int = Query(200, le=5000)) -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM gateway_stats ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/lorawan/upstreams")
    async def upstreams() -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM upstream_status ORDER BY name")

    # -- Meshtastic -------------------------------------------------------------------

    @app.get("/api/mesh/packets")
    async def mesh_packets(
        limit: int = Query(100, le=2000), before_id: int | None = None,
        portnum: str | None = None, from_node: int | None = None, text_only: bool = False,
    ) -> list[dict[str, Any]]:
        filters = {"id < ?": before_id, "portnum = ?": portnum, "from_node = ?": from_node}
        where = ["1=1", *[k for k, v in filters.items() if v is not None]]
        params: list[Any] = [v for v in filters.values() if v is not None]
        if text_only:
            where.append("text IS NOT NULL")
        params.append(limit)
        rows = await db.fetch(f"SELECT * FROM mesh_packets WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params)
        for r in rows:
            if r.get("payload"):
                try:
                    r["payload"] = json.loads(r["payload"])
                except Exception:
                    pass
        return rows

    @app.get("/api/mesh/nodes")
    async def mesh_nodes() -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM mesh_nodes ORDER BY last_heard DESC NULLS LAST")

    @app.get("/api/mesh/summary")
    async def mesh_summary(hours: int = Query(24, le=24 * 90)) -> dict[str, Any]:
        since = time.time() - hours * 3600
        bucket = 3600 if hours > 48 else 900
        per_bucket = await db.fetch(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, COUNT(*) AS n, "
            "SUM(CASE WHEN hops_used > 0 THEN 1 ELSE 0 END) AS relayed, SUM(via_mqtt) AS via_mqtt, "
            "SUM(relayed_by_us) AS relayed_by_us, SUM(direction = 'tx') AS tx "
            "FROM mesh_packets WHERE ts >= ? GROUP BY t ORDER BY t", (bucket, bucket, since))
        by_port = await db.fetch(
            "SELECT portnum, COUNT(*) AS n FROM mesh_packets WHERE ts >= ? GROUP BY portnum ORDER BY n DESC", (since,))
        by_hops = await db.fetch(
            "SELECT hops_used, COUNT(*) AS n FROM mesh_packets WHERE ts >= ? AND hops_used IS NOT NULL GROUP BY hops_used ORDER BY hops_used", (since,))
        top_nodes = await db.fetch(
            "SELECT p.from_node, n.long_name, n.short_name, COUNT(*) AS n, AVG(p.rssi) AS rssi, AVG(p.snr) AS snr, MAX(p.ts) AS last_ts "
            "FROM mesh_packets p LEFT JOIN mesh_nodes n ON n.num = p.from_node WHERE p.ts >= ? "
            "GROUP BY p.from_node ORDER BY n DESC LIMIT 50", (since,))
        totals = await db.fetchone(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN hops_used > 0 THEN 1 ELSE 0 END) AS relayed, "
            "SUM(CASE WHEN hops_used = 0 THEN 1 ELSE 0 END) AS direct, SUM(via_mqtt) AS via_mqtt, "
            "SUM(text IS NOT NULL) AS text, AVG(rssi) AS rssi, AVG(snr) AS snr, COUNT(DISTINCT from_node) AS nodes, "
            "SUM(relayed_by_us) AS relayed_by_us, SUM(direction = 'tx') AS tx, SUM(encrypted) AS undecodable, "
            "SUM(airtime_ms) / 1000.0 AS airtime_s "
            "FROM mesh_packets WHERE ts >= ?", (since,))
        return {"hours": hours, "bucket_s": bucket, "totals": totals, "per_bucket": per_bucket,
                "by_port": by_port, "by_hops": by_hops, "top_nodes": top_nodes}

    # -- static GUI -------------------------------------------------------------------

    if WEB_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    else:
        log.warning("web directory %s not found; GUI disabled", WEB_DIR)

    return app
