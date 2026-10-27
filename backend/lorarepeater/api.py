"""FastAPI application: JSON API, WebSocket live feed, and the static web GUI."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__
from .config import GUI_SECTIONS, Config
from .db import Database
from .diag import Diag, add_diag_routes, mask_secrets, unmask_secrets
from .gwmp import GwmpMux
from .helium import Helium
from .meshproto import MAX_TEXT_BYTES, node_id_str, parse_node_id
from .timesync import TimeSync

log = logging.getLogger(__name__)

def _read_cpu_temp(path: str) -> float | None:
    try:
        return int(Path(path).read_text().strip()) / 1000.0
    except OSError:              # no thermal zone (not a Pi)
        return None


class SendText(BaseModel):
    text: str
    to: str | None = None       # "!aabbccdd", a node number, or empty for a broadcast
    channel: int = 0


def create_app(cfg: Config, db: Database, mux: GwmpMux, timesync: TimeSync, helium: Helium,
               extras: dict[str, Any] | None = None) -> FastAPI:
    """`extras` maps a name to an optional object with a status() method (meshsx, telegram, mqtt)."""
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
            "time": timesync.status(),
            "helium": helium.summary(),
            **{name: (obj.status() if obj is not None else None) for name, obj in extras.items()},
        }

    @app.get("/api/events")
    async def events(limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -- LoRaWAN ----------------------------------------------------------------------

    @app.get("/api/lorawan/packets/{pid}.json")
    async def lorawan_packet_export(pid: int) -> Response:
        return await packet_export("lorawan", pid)

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
            for k in ("forwarded", "mac"):
                if r.get(k):
                    r[k] = json.loads(r[k])
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
            "SELECT devaddr, MAX(network) AS network, MAX(netid) AS netid, COUNT(*) AS n, MAX(ts) AS last_ts, "
            "AVG(rssi) AS rssi, AVG(lsnr) AS snr, MAX(fcnt) AS fcnt, GROUP_CONCAT(DISTINCT datr) AS datr, "
            "GROUP_CONCAT(DISTINCT fport) AS fports, MAX(adr) AS adr, SUM(airtime_ms) AS airtime_ms, "
            "AVG(payload_len) AS payload_len, "
            "(SELECT battery FROM lorawan_packets b WHERE b.devaddr = p.devaddr AND b.battery IS NOT NULL ORDER BY id DESC LIMIT 1) AS battery, "
            "(SELECT margin FROM lorawan_packets b WHERE b.devaddr = p.devaddr AND b.margin IS NOT NULL ORDER BY id DESC LIMIT 1) AS margin "
            "FROM lorawan_packets p WHERE ts >= ? AND devaddr IS NOT NULL GROUP BY devaddr ORDER BY n DESC LIMIT 200", (since,))
        joins = await db.fetch(
            "SELECT dev_eui, MAX(dev_vendor) AS dev_vendor, MAX(join_eui) AS join_eui, MAX(join_vendor) AS join_vendor, "
            "COUNT(*) AS n, MAX(ts) AS last_ts, AVG(rssi) AS rssi, AVG(lsnr) AS snr, GROUP_CONCAT(DISTINCT datr) AS datr, GROUP_CONCAT(DISTINCT mtype) AS kinds "
            "FROM lorawan_packets WHERE ts >= ? AND dev_eui IS NOT NULL GROUP BY dev_eui ORDER BY last_ts DESC LIMIT 200", (since,))
        by_network = await db.fetch(
            "SELECT COALESCE(network, 'NetID ' || netid) AS network, COUNT(*) AS n, COUNT(DISTINCT devaddr) AS devices "
            "FROM lorawan_packets WHERE ts >= ? AND netid IS NOT NULL GROUP BY 1 ORDER BY n DESC", (since,))
        mac = await db.fetch(
            "SELECT devaddr, direction, mtype, mac, ts FROM lorawan_packets WHERE ts >= ? AND mac IS NOT NULL ORDER BY id DESC LIMIT 100", (since,))
        for r in mac:
            r["mac"] = json.loads(r["mac"])
        totals = await db.fetchone(
            "SELECT COUNT(*) AS total, SUM(direction='up') AS up, SUM(direction='down') AS down, "
            "SUM(stat=-1) AS crc_bad, AVG(CASE WHEN direction='up' THEN rssi END) AS rssi, "
            "AVG(CASE WHEN direction='up' THEN lsnr END) AS snr, SUM(airtime_ms) / 1000.0 AS airtime_s, "
            "COUNT(DISTINCT devaddr) AS devices, COUNT(DISTINCT dev_eui) AS joiners FROM lorawan_packets WHERE ts >= ?", (since,))
        return {"hours": hours, "bucket_s": bucket, "totals": totals, "per_bucket": per_bucket,
                "by_mtype": by_mtype, "by_datr": by_datr, "by_freq": by_freq, "devices": devices,
                "joins": joins, "by_network": by_network, "mac": mac}

    @app.get("/api/helium")
    async def helium_view() -> dict[str, Any]:
        """The snapshot plus the logged lifetime totals (the unclaimed history is derived from them)."""
        log = await db.fetch("SELECT ts, token, lifetime FROM helium_rewards ORDER BY ts")
        return {**helium.status(), "reward_log": log}

    @app.get("/api/helium/traffic")
    async def helium_traffic(hours: int = Query(24, le=24 * 90)) -> dict[str, Any]:
        """Facts about Helium in the period: what was offered, what Helium asked to transmit (and
        for whom), uplinks from those devices, and what the reward oracle added. Which uplinks the
        packet router bought is not visible locally; downlinks and rewards are."""
        since = time.time() - hours * 3600
        offered = await db.fetchone(
            "SELECT COUNT(*) AS n FROM lorawan_packets WHERE ts >= ? AND direction = 'up' "
            "AND forwarded LIKE '%\"helium\"%'", (since,))
        downlinks = await db.fetch(
            "SELECT id, ts, mtype, devaddr, netid, network, freq, datr, airtime_ms, forwarded FROM lorawan_packets "
            "WHERE ts >= ? AND direction = 'down' AND upstream = 'helium' ORDER BY ts DESC", (since,))
        for d in downlinks:
            d["forwarded"] = json.loads(d["forwarded"]) if d["forwarded"] else []
        # Helium-bought traffic is not marked on the uplink itself. Closest local signal: DevAddr
        # NetID is Helium, or Helium already sent that device a downlink through us.
        served = sorted({d["devaddr"] for d in downlinks if d["devaddr"]})
        he_filter = "network = 'Helium'"
        he_params: list[Any] = [since]
        if served:
            he_filter = f"({he_filter} OR devaddr IN ({','.join('?' * len(served))}))"
            he_params.extend(served)
        uplinks = await db.fetch(
            "SELECT id, ts, mtype, devaddr, network, netid, freq, datr, rssi, lsnr, fcnt, fport, size, "
            f"forwarded FROM lorawan_packets WHERE ts >= ? AND direction = 'up' "
            f"AND {he_filter} ORDER BY ts DESC LIMIT 200",
            he_params,
        )
        for u in uplinks:
            u["forwarded"] = json.loads(u["forwarded"]) if u["forwarded"] else []
        devices = await db.fetch(
            f"SELECT devaddr, MAX(netid) AS netid, MAX(network) AS network, COUNT(*) AS n, AVG(rssi) AS rssi, "
            f"AVG(lsnr) AS snr, GROUP_CONCAT(DISTINCT datr) AS datr, MAX(fcnt) AS fcnt, MAX(ts) AS last_ts "
            f"FROM lorawan_packets WHERE ts >= ? AND direction = 'up' "
            f"AND {he_filter} "
            f"GROUP BY devaddr ORDER BY n DESC", he_params)
        earned = {}
        for sym in ("HNT", "IOT"):
            rows = await db.fetch("SELECT ts, lifetime FROM helium_rewards WHERE token = ? ORDER BY ts", (sym,))
            before = [r for r in rows if r["ts"] < since]
            base = (before[-1] if before else (rows[0] if rows else None))
            earned[sym] = {"amount": rows[-1]["lifetime"] - base["lifetime"] if base else None,
                           "since": base["ts"] if base else None,
                           "history": [r for r in rows if r["ts"] >= since]}
        return {"hours": hours, "offered": offered["n"],
                "downlinks": downlinks, "uplinks": uplinks,
                "joins": sum(d["mtype"] == "JoinAccept" for d in downlinks),
                "netids": sorted({d["netid"] for d in downlinks if d["netid"]}),
                "devices": devices, "earned": earned}

    @app.get("/api/helium/nearby")
    async def helium_nearby() -> dict[str, Any]:
        return await helium.nearby()

    @app.get("/api/helium/hotspot/{key}")
    async def helium_hotspot(key: str) -> dict[str, Any]:
        return await helium.hotspot(key)

    @app.get("/api/lorawan/stats")
    async def gateway_stats(limit: int = Query(200, le=5000)) -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM gateway_stats ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/lorawan/upstreams")
    async def upstreams() -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM upstream_status ORDER BY name")

    # -- Meshtastic -------------------------------------------------------------------

    @app.get("/api/mesh/packets/{pid}.json")
    async def mesh_packet_export(pid: int) -> Response:
        return await packet_export("mesh", pid)

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
                r["payload"] = json.loads(r["payload"])
        return rows

    @app.get("/api/mesh/nodes")
    async def mesh_nodes() -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM mesh_nodes ORDER BY last_heard DESC NULLS LAST")

    @app.get("/api/mesh/links")
    async def mesh_links(hours: int = Query(24 * 7, le=24 * 90)) -> list[dict[str, Any]]:
        since = time.time() - hours * 3600
        return await db.fetch(
            "SELECT l.*, na.long_name AS a_name, nb.long_name AS b_name FROM mesh_links l "
            "LEFT JOIN mesh_nodes na ON na.num = l.a LEFT JOIN mesh_nodes nb ON nb.num = l.b "
            "WHERE l.ts >= ? ORDER BY l.ts DESC", (since,))

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
        by_channel = await db.fetch(
            "SELECT channel_hash, channel, SUM(encrypted) AS encrypted, COUNT(*) AS n, COUNT(DISTINCT from_node) AS nodes "
            "FROM mesh_packets WHERE ts >= ? AND direction = 'rx' GROUP BY channel_hash ORDER BY n DESC", (since,))
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
                "by_port": by_port, "by_hops": by_hops, "top_nodes": top_nodes, "by_channel": by_channel}

    @app.post("/api/mesh/send")
    async def mesh_send(req: SendText) -> dict[str, Any]:
        meshsx = extras.get("meshsx")
        if meshsx is None:
            raise HTTPException(503, "the Meshtastic node is disabled (meshsx.enabled)")
        text = req.text.strip()
        if not text or len(text.encode()) > MAX_TEXT_BYTES:
            raise HTTPException(422, f"text must be 1 to {MAX_TEXT_BYTES} bytes")
        if not 0 <= req.channel < len(meshsx.channels):
            raise HTTPException(422, f"no channel {req.channel}")
        try:
            to = parse_node_id(req.to)
        except ValueError:
            raise HTTPException(422, f"bad node id {req.to!r}") from None
        pid = await meshsx.send_text(text, to=to, channel=req.channel)
        if pid is None:
            raise HTTPException(503, "not transmitted: TX off, duty cycle used up, or forwarder not pulling")
        return {"packet_id": pid, "to": node_id_str(to)}

    # -- settings (sections editable in the GUI, stored next to the database) ---------

    def _settings() -> dict[str, Any]:
        saved = cfg.saved_settings()
        return {name: {"value": mask_secrets(getattr(cfg, name).model_dump(mode="json")),
                       "source": f"config.yaml + {cfg.settings_path}" if name in saved else "config.yaml"}
                for name in GUI_SECTIONS}

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        return _settings()

    @app.put("/api/settings/{section}")
    async def save_settings(section: str, body: dict[str, Any]) -> dict[str, Any]:
        if section not in GUI_SECTIONS:
            raise HTTPException(404, f"unknown section {section!r}")
        changes = unmask_secrets(body, getattr(cfg, section).model_dump(mode="json"))
        try:
            cfg.save_section(section, changes)
        except ValidationError as e:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from None
        await db.add_event("system", "info", f"{section} settings saved from the GUI; restarting")
        restart_self()
        return {"ok": True, "restarting": True}

    # -- diagnostics ------------------------------------------------------------------

    def restart_self() -> None:
        # systemd restarts us (Restart=always); delay so the HTTP response gets out first
        loop = loop_holder.get("loop")
        if loop is not None:
            loop.call_later(1.0, lambda: os._exit(0))

    add_diag_routes(app, Diag(cfg, db, restart_self=restart_self))

    # -- static GUI -------------------------------------------------------------------

    web_dir = cfg.web.dir
    if web_dir.is_dir():

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

        app.mount("/static", StaticFiles(directory=web_dir), name="static")

        @app.middleware("http")
        async def revalidate_gui(request, call_next):
            # browsers must re-check the GUI files (ETag) so an update shows without a hard reload
            response = await call_next(request)
            if request.url.path == "/" or request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache"
            return response
    else:
        log.warning("web.dir %s not found; GUI disabled", web_dir)

    async def packet_export(kind: str, pid: int) -> Response:
        """One stored packet as a JSON download: the raw frame (hex and base64), the radio metadata
        and every decoded field."""
        row = await db.fetchone(f"SELECT * FROM {kind}_packets WHERE id = ?", (pid,))
        if row is None:
            raise HTTPException(404, "no such packet")
        raw_b64 = row.pop("data" if kind == "lorawan" else "raw")
        raw = base64.b64decode(raw_b64) if raw_b64 else b""
        radio_keys = EXPORT_RADIO[kind]
        decoded = {k: v for k, v in row.items() if k not in radio_keys and k not in ("id", "ts") and v is not None}
        for k in ("forwarded", "mac", "payload"):
            if isinstance(decoded.get(k), str):
                decoded[k] = json.loads(decoded[k])
        if kind == "mesh":
            for k in ("from_node", "to_node"):
                if row.get(k) is not None:
                    decoded[k.replace("_node", "_id")] = node_id_str(row[k])
        doc = {
            "kind": kind, "id": pid, "received": datetime.fromtimestamp(row["ts"], UTC).isoformat(),
            "raw": {"length": len(raw), "hex": raw.hex(" ").upper(), "base64": raw_b64} if raw_b64 else None,
            "radio": {k: row[k] for k in radio_keys if row.get(k) is not None},
            "decoded": decoded,
        }
        return Response(json.dumps(doc, indent=2) + "\n", media_type="application/json",   # indented: read it as is
                        headers={"Content-Disposition": f'attachment; filename="{kind}-{pid}.json"'})

    return app


# columns that describe the reception rather than the frame's content
EXPORT_RADIO = {
    "lorawan": ("direction", "gateway_eui", "upstream", "tmst", "time", "freq", "chan", "rfch", "stat", "modu", "datr",
                "codr", "rssi", "lsnr", "size", "airtime_ms"),
    "mesh": ("direction", "source", "rx_time", "rssi", "snr", "airtime_ms"),
}
