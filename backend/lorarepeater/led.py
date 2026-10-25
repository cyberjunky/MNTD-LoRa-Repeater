"""Health on the Raspberry Pi's own LEDs, which the Goldspot enclosure pipes to the front.

The HAT in this unit has no LED driver (the LP5562 footprint U6 and LED1 are unpopulated), so
the two Pi LEDs are the indicators: ACT (green) and PWR (red).

    green         forwarder, upstreams and mesh node all fine
    green + red   degraded (an upstream or the mesh node is down)
    red           forwarder not seen
    off           backend stopped; the kernel triggers (mmc0 / default-on) take over again

install/udev/99-lorarepeater-leds.rules makes brightness and trigger writable for the gpio
group, which the service user is in. Nothing here is required; failures are logged and ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

LEDS = Path("/sys/class/leds")
GREEN, RED = "ACT", "PWR"
KERNEL_TRIGGER = {GREEN: "mmc0", RED: "default-on"}
STATES = {"off": (0, 0), "green": (1, 0), "orange": (1, 1), "red": (0, 1)}


def available() -> bool:
    return all((LEDS / led / "brightness").exists() for led in (GREEN, RED))


def _write(led: str, attr: str, value: str) -> bool:
    try:
        (LEDS / led / attr).write_text(value)
        return True
    except OSError as e:
        log.debug("led %s/%s: %s", led, attr, e)
        return False


def set_state(name: str) -> None:
    """Drive both LEDs; 'off' hands them back to their kernel triggers."""
    if not available():
        return
    if name == "off":
        for led, trigger in KERNEL_TRIGGER.items():
            _write(led, "trigger", trigger)
        return
    green, red = STATES[name]
    for led, on in ((GREEN, green), (RED, red)):
        _write(led, "trigger", "none") and _write(led, "brightness", "1" if on else "0")


def health_state(forwarder_ok: bool, upstream_ok: bool, mesh_ok: bool) -> str:
    if not forwarder_ok:
        return "red"
    if not upstream_ok or not mesh_ok:
        return "orange"
    return "green"
