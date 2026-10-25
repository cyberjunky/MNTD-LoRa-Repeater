"""Command line entry point: `lorarepeater serve --config /etc/lorarepeater/config.yaml`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import uvicorn

from . import __version__, led
from .api import create_app
from .config import Config
from .db import Database
from .gps import GpsMonitor
from .gwmp import GwmpMux
from .mesh import MeshIngest
from .meshsx import MeshSxEngine
from .telegram import TelegramBridge


async def _serve(cfg: Config) -> None:
    db = Database(cfg.db_path)
    await db.open()
    mux = GwmpMux(cfg.gwmp, db)
    mesh = MeshIngest(cfg.mesh, db)
    gps = GpsMonitor(cfg.gps, db)
    meshsx = MeshSxEngine(cfg.meshsx, db, mux, gps) if cfg.meshsx.enabled else None
    telegram = TelegramBridge(cfg.telegram, db, meshsx=meshsx, mesh=mesh if cfg.mesh.enabled else None)
    if cfg.hardware.led_enabled:
        led.set_state("orange")
    await mux.start()
    await mesh.start()
    await gps.start()
    if meshsx:
        await meshsx.start()
    await telegram.start()
    await db.add_event("system", "info", f"lorarepeater {__version__} started")

    async def housekeeping_loop() -> None:
        last_prune = 0.0
        while True:
            await asyncio.sleep(30)
            if cfg.hardware.led_enabled:
                st = mux.status()
                fwd_ok = st["forwarder_seen_s_ago"] is not None and st["forwarder_seen_s_ago"] < 120
                up_ok = not st["upstreams"] or any(u["alive"] for u in st["upstreams"])
                mesh_ok = mesh.connected if cfg.mesh.enabled else True
                led.set_state(led.health_state(fwd_ok, up_ok, mesh_ok))
            if asyncio.get_running_loop().time() - last_prune > 6 * 3600:
                await db.prune(cfg.retention_days)
                last_prune = asyncio.get_running_loop().time()

    prune_task = asyncio.create_task(housekeeping_loop())
    app = create_app(cfg, db, mux, mesh, gps, extras={"meshsx": meshsx, "telegram": telegram})
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port, log_level="info", loop="asyncio"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
        except NotImplementedError:  # Windows
            pass
    try:
        await server.serve()
    finally:
        prune_task.cancel()
        await telegram.stop()
        if meshsx:
            await meshsx.stop()
        await gps.stop()
        await mesh.stop()
        await mux.stop()
        if cfg.hardware.led_enabled:
            led.set_state("off")
        await db.add_event("system", "info", "lorarepeater stopped")
        await db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lorarepeater", description="LoRaWAN + Meshtastic repeater backend")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the mux, mesh ingest and web GUI")
    s.add_argument("--config", "-c", default=None, help="YAML config file (defaults are used when omitted)")
    s.add_argument("--debug", action="store_true")
    c = sub.add_parser("check-config", help="load and print the effective configuration")
    c.add_argument("--config", "-c", default=None)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.load(args.config)
    if args.cmd == "check-config":
        print(cfg.model_dump_json(indent=2))
        return 0
    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
