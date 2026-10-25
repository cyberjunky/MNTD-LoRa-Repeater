#!/usr/bin/env python3
"""Patch Semtech sx1302_hal so it runs on the MNTD/RAK HAT, which has no STTS751
temperature sensor. Upstream `lgw_start()` aborts when the sensor is missing and
`lgw_get_temperature()` fails, which makes `lgw_receive()`/`lgw_send()` fail too.

The patch makes the sensor optional and reports a fixed 25 C when it is absent
(RSSI temperature compensation then uses the calibration value for 25 C).

Usage: patch_sx1302_hal.py /opt/sx1302_hal        (idempotent)
"""

import re
import sys
from pathlib import Path

MARK = "/* lorarepeater: temperature sensor optional */"


def main() -> int:
    hal = Path(sys.argv[1]) / "libloragw" / "src" / "loragw_hal.c"
    src = hal.read_text()
    if MARK in src:
        print("already patched:", hal)
        return 0

    # 1. a flag next to the sensor file descriptor
    m = re.search(r"^static int ts_fd = -1;.*$", src, re.M)
    if not m:
        return fail("ts_fd declaration")
    src = src[: m.end()] + f"\nstatic bool ts_fake = false; {MARK}" + src[m.end():]

    # 2. missing sensor is no longer fatal in lgw_start()
    pat = re.compile(
        r'printf\("ERROR: no temperature sensor found\.\\n"\);\s*return LGW_HAL_ERROR;', re.S)
    src, n = pat.subn(
        'printf("WARNING: no temperature sensor found, reporting 25 C (MNTD/RAK HAT)\\n"); ts_fake = true;', src)
    if n != 1:
        return fail("no-sensor error branch in lgw_start")

    # 3. lgw_get_temperature() returns a constant when faking
    pat = re.compile(r"(int lgw_get_temperature\(float \* ?temperature\)\s*\{)")
    src, n = pat.subn(
        r"\1\n    if (ts_fake) { *temperature = 25.0f; return LGW_HAL_SUCCESS; } " + MARK, src)
    if n != 1:
        return fail("lgw_get_temperature definition")

    if "#include <stdbool.h>" not in src:
        src = src.replace("#include <stdint.h>", "#include <stdint.h>\n#include <stdbool.h>", 1)
    hal.write_text(src)
    print("patched:", hal)
    return 0


def fail(what: str) -> int:
    print(f"ERROR: could not find {what} in loragw_hal.c; upstream source changed, patch manually", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
