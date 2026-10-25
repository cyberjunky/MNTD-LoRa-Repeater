"""Front LED (TI LP5562 at I2C 0x30) through the kernel `leds-lp5562` driver.

With install/overlays/lp5562.dts loaded the channels appear as /sys/class/leds/{R,G,B,W}.
We only set static colours: green = everything healthy, orange = degraded, red = error,
blue = starting. Nothing here is required; failures are ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

LEDS = Path("/sys/class/leds")
COLOURS = {
    "off": (0, 0, 0, 0),
    "green": (0, 80, 0, 0),
    "orange": (120, 40, 0, 0),
    "red": (120, 0, 0, 0),
    "blue": (0, 0, 120, 0),
    "white": (0, 0, 0, 120),
}


def available() -> bool:
    return all((LEDS / c / "brightness").exists() for c in ("R", "G", "B", "W"))


def set_colour(name: str) -> None:
    if not available():
        return
    r, g, b, w = COLOURS.get(name, COLOURS["off"])
    for ch, val in zip(("R", "G", "B", "W"), (r, g, b, w), strict=True):
        try:
            (LEDS / ch / "brightness").write_text(str(val))
        except OSError as e:
            log.debug("led %s: %s", ch, e)
            return


def health_colour(forwarder_ok: bool, upstream_ok: bool, mesh_ok: bool, mesh_enabled: bool) -> str:
    if not forwarder_ok:
        return "red"
    if not upstream_ok or (mesh_enabled and not mesh_ok):
        return "orange"
    return "green"
