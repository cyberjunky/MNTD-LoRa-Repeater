#!/usr/bin/env python3
"""Patch Semtech sx1302_hal V2.1.0 for the MNTD Goldspot / RAK Hotspot Miner V2.

Three changes, all idempotent and verified against the V2.1.0 sources:

1. Temperature sensor optional (libloragw/src/loragw_hal.c)
   The RAK HAT has no STTS751. Upstream aborts in lgw_start() and later fails every
   lgw_receive()/lgw_send() because lgw_get_temperature() errors. We report 25 C instead.

2. Custom sync word on the LoRa *service* modem (libloragw/src/loragw_sx1302.c)
   Read from the environment variable LGW_SERVICE_SYNCWORD (e.g. "0x2B" for Meshtastic).
   The eight multi-SF channels keep the LoRaWAN word, so LoRaWAN keeps working on
   channels 0-7 while the single 250 kHz service channel listens to Meshtastic.
   Sync word -> SX1302 "peak position" registers: high nibble*2, low nibble*2
   (0x34 -> 6/8 and 0x12 -> 2/4 are the stock values; 0x2B -> 4/22).

3. Same sync word for transmissions at the service bandwidth (loragw_sx1302.c)
   Controlled by LGW_SERVICE_SYNCWORD_TX_BW (kHz, default 250, 0 disables). EU868 LoRaWAN
   downlinks are always 125 kHz, so a 250 kHz transmission is by definition a Meshtastic
   frame and gets the Meshtastic word; everything else keeps the LoRaWAN word.

Usage: patch_sx1302_hal.py /opt/sx1302_hal
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MARK = "lorarepeater"

HELPERS = r'''
/* ---- lorarepeater: custom sync word for the LoRa service modem / service-bandwidth TX ---- */
#include <stdlib.h>
static int lorarepeater_syncword_peaks(uint8_t *p1, uint8_t *p2) {
    const char *s = getenv("LGW_SERVICE_SYNCWORD");
    unsigned long v;
    if (s == NULL || *s == '\0') return 0;
    v = strtoul(s, NULL, 0);
    if (v == 0 || v > 0xFF) return 0;
    *p1 = (uint8_t)(((v >> 4) & 0x0F) << 1);
    *p2 = (uint8_t)((v & 0x0F) << 1);
    return 1;
}
static int lorarepeater_tx_uses_service_syncword(uint8_t bandwidth) {
    const char *s = getenv("LGW_SERVICE_SYNCWORD_TX_BW");
    unsigned long khz = (s != NULL && *s != '\0') ? strtoul(s, NULL, 0) : 250;
    uint8_t bw;
    if (khz == 0) return 0;
    bw = (khz == 125) ? BW_125KHZ : (khz == 250) ? BW_250KHZ : (khz == 500) ? BW_500KHZ : 0xFF;
    return (bw != 0xFF && bandwidth == bw) ? 1 : 0;
}
/* ---- end lorarepeater ---- */
'''

RX_OLD = re.compile(r"(?P<ind>[ \t]*)/\* LoRa Service modem configuration \*/\n[ \t]*if \(\(public == false\)")
RX_NEW = (
    "{ind}/* LoRa Service modem configuration */\n"
    "{ind}uint8_t lr_p1, lr_p2; /* " + MARK + " */\n"
    "{ind}if (lorarepeater_syncword_peaks(&lr_p1, &lr_p2)) {{\n"
    '{ind}    printf("INFO: configuring LoRa (Service) SF%u with custom syncword, peaks %u/%u [' + MARK + ']\\n", lora_service_sf, lr_p1, lr_p2);\n'
    "{ind}    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, lr_p1);\n"
    "{ind}    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, lr_p2);\n"
    "{ind}}} else if ((public == false)"
)

TX_OLD = re.compile(
    r"(?P<ind>[ \t]*)/\* Syncword \*/\n[ \t]*if \(\(lwan_public == false\) \|\| \(pkt_data->datarate == DR_LORA_SF5\)"
)
TX_NEW = (
    "{ind}/* Syncword */\n"
    "{ind}uint8_t lr_p1, lr_p2; /* " + MARK + " */\n"
    "{ind}if (lorarepeater_syncword_peaks(&lr_p1, &lr_p2) && lorarepeater_tx_uses_service_syncword(pkt_data->bandwidth)) {{\n"
    '{ind}    DEBUG_MSG("Setting custom LoRa syncword [' + MARK + ']\\n");\n'
    "{ind}    err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_0_PEAK1_POS(pkt_data->rf_chain), lr_p1);\n"
    "{ind}    CHECK_ERR(err);\n"
    "{ind}    err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_1_PEAK2_POS(pkt_data->rf_chain), lr_p2);\n"
    "{ind}    CHECK_ERR(err);\n"
    "{ind}}} else if ((lwan_public == false) || (pkt_data->datarate == DR_LORA_SF5)"
)


def fail(what: str) -> int:
    print(f"ERROR: could not find {what}; upstream source differs from V2.1.0, patch manually", file=sys.stderr)
    return 1


def _read(path: Path) -> tuple[str, str]:
    """Return (text with LF newlines, the file's own newline string) so CRLF sources stay CRLF."""
    raw = path.read_text(encoding="utf-8", newline="")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), eol


def _write(path: Path, src: str, eol: str) -> None:
    path.write_text(src.replace("\n", eol), encoding="utf-8", newline="")


def patch_hal_c(path: Path) -> int:
    src, eol = _read(path)
    if MARK in src:
        print("already patched:", path)
        return 0
    m = re.search(r"^static uint8_t ts_addr = 0xFF;.*$", src, re.M)
    if not m:
        return fail("ts_addr declaration in loragw_hal.c")
    src = src[: m.end()] + f"\nstatic bool ts_fake = false; /* {MARK}: no STTS751 on the MNTD/RAK HAT */" + src[m.end():]

    # replacement callables: re.sub would otherwise interpret the "\n" inside the C string
    warn = 'printf("WARNING: no temperature sensor found, reporting 25 C [' + MARK + ']\\n"); ts_fake = true;'
    src, n = re.subn(
        r'printf\("ERROR: no temperature sensor found\.\\n"\);\s*return LGW_HAL_ERROR;',
        lambda _m: warn,
        src,
    )
    if n != 1:
        return fail("no-sensor error branch in lgw_start")

    fake = "\n    if (ts_fake) { *temperature = 25.0f; return LGW_HAL_SUCCESS; } /* " + MARK + " */"
    src, n = re.subn(
        r"int lgw_get_temperature\(float\s*\*\s*temperature\)\s*\{",
        lambda m: m.group(0) + fake,
        src,
    )
    if n != 1:
        return fail("lgw_get_temperature definition")
    _write(path, src, eol)
    print("patched:", path)
    return 0


def patch_sx1302_c(path: Path) -> int:
    src, eol = _read(path)
    if MARK in src:
        print("already patched:", path)
        return 0
    includes = list(re.finditer(r"^#include .*$", src, re.M))
    if not includes:
        return fail("includes in loragw_sx1302.c")
    end = includes[-1].end()
    src = src[:end] + "\n" + HELPERS + src[end:]

    m = RX_OLD.search(src)
    if not m:
        return fail("service modem syncword block in sx1302_lora_syncword")
    src = src[: m.start()] + RX_NEW.format(ind=m.group("ind")) + src[m.end():]

    m = TX_OLD.search(src)
    if not m:
        return fail("TX syncword block in sx1302_send")
    src = src[: m.start()] + TX_NEW.format(ind=m.group("ind")) + src[m.end():]
    _write(path, src, eol)
    print("patched:", path)
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    rc = patch_hal_c(root / "libloragw" / "src" / "loragw_hal.c")
    rc |= patch_sx1302_c(root / "libloragw" / "src" / "loragw_sx1302.c")
    return rc


if __name__ == "__main__":
    sys.exit(main())
