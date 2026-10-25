#!/usr/bin/env python3
"""Patch Semtech sx1302_hal V2.1.0 for the MNTD Goldspot (RAK2287, no temperature sensor).

Expects the pristine V2.1.0 sources (install.sh resets the checkout before patching).

1. No temperature sensor (libloragw/src/loragw_hal.c)
   The RAK2287 has no STTS751. Upstream probes for one, aborts lgw_start() without it, and
   needs a reading for RSSI temperature compensation on every receive. The probe and the
   compensation are removed: RSSI gets the board's calibrated offset only, and no
   temperature is ever invented. lgw_stop() no longer closes the sensor fd that was never
   opened.

2. No temperature in the packet forwarder (packet_forwarder/src/lora_pkt_fwd.c)
   The status printout and the "stat" report sent to the network servers drop the
   temperature line and the "temp" field.

3. Custom sync word on the LoRa *service* modem (libloragw/src/loragw_sx1302.c)
   Read from the environment variable LGW_SERVICE_SYNCWORD (e.g. "0x2B" for Meshtastic).
   The eight multi-SF channels keep the LoRaWAN word, so LoRaWAN keeps working on
   channels 0-7 while the single 250 kHz service channel listens to Meshtastic.
   Sync word -> SX1302 "peak position" registers: high nibble*2, low nibble*2
   (0x34 -> 6/8 and 0x12 -> 2/4 are the stock values; 0x2B -> 4/22).

4. Same sync word for transmissions at the service bandwidth (loragw_sx1302.c)
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


HAL_EDITS = [  # (what, old: str | regex, new, expected matches)
    ("temperature sensor probe in lgw_start",
     re.compile(r"        /\* Find the temperature sensor on the known supported ports \*/\n.*?"
                r'            printf\("ERROR: no temperature sensor found\.\\n"\);\n'
                r"            return LGW_HAL_ERROR;\n        \}\n", re.S),
     "        /* " + MARK + ": the RAK2287 has no temperature sensor; none is probed */\n", 1),
    ("temperature variables in lgw_receive",
     "    float current_temperature = 0.0, rssi_temperature_offset = 0.0;\n", "", 1),
    ("temperature read in lgw_receive",
     re.compile(r"    /\* Apply RSSI temperature compensation \*/\n    res = lgw_get_temperature\(&current_temperature\);\n"
                r"    if \(res != LGW_I2C_SUCCESS\) \{\n.*?\n    \}\n\n", re.S),
     "    /* " + MARK + ": no temperature sensor, so no RSSI temperature compensation */\n\n", 1),
    ("temperature compensation in lgw_receive",
     re.compile(r"\n        rssi_temperature_offset = sx1302_rssi_get_temperature_offset\(.*?\n"
                r"        DEBUG_PRINTF\(\"INFO: RSSI temperature offset applied:[^\n]*\n", re.S),
     "\n", 1),
    ("temperature sensor close in lgw_stop",
     "        x = i2c_linuxdev_close(ts_fd);\n",
     "        x = (ts_fd >= 0) ? i2c_linuxdev_close(ts_fd) : 0; /* " + MARK + ": never opened */\n", 1),
]

PKT_FWD_EDITS = [
    ("temperature variable", "    float temperature;\n", "    /* " + MARK + ": no concentrator temperature */\n", 1),
    ("temperature printout",
     re.compile(r"        pthread_mutex_lock\(&mx_concent\);\n        i = lgw_get_temperature\(&temperature\);\n"
                r".*?printf\(\"### Concentrator temperature: %\.0f C ###\\n\", temperature\);\n        \}\n", re.S),
     "", 1),
    ("temp field in the stat reports", ',\\"temp\\":%.1f}"', '}"', 2),
    ("temperature argument of the stat reports", "cp_nb_tx_ok, temperature);", "cp_nb_tx_ok);", 2),
]


def _apply(path: Path, edits: list) -> int:
    src = path.read_text(encoding="utf-8")
    if MARK in src:
        print("already patched:", path)
        return 0
    for what, old, new, count in edits:
        if isinstance(old, re.Pattern):
            src, n = old.subn(lambda _m, new=new: new, src)
        else:
            n = src.count(old)
            src = src.replace(old, new)
        if n != count:
            return fail(what)
    path.write_text(src, encoding="utf-8", newline="\n")
    print("patched:", path)
    return 0


def patch_hal_c(path: Path) -> int:
    return _apply(path, HAL_EDITS)


def patch_pkt_fwd_c(path: Path) -> int:
    return _apply(path, PKT_FWD_EDITS)


def patch_sx1302_c(path: Path) -> int:
    src = path.read_text(encoding="utf-8")
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
    path.write_text(src, encoding="utf-8", newline="\n")
    print("patched:", path)
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    rc = patch_hal_c(root / "libloragw" / "src" / "loragw_hal.c")
    rc |= patch_sx1302_c(root / "libloragw" / "src" / "loragw_sx1302.c")
    rc |= patch_pkt_fwd_c(root / "packet_forwarder" / "src" / "lora_pkt_fwd.c")
    return rc


if __name__ == "__main__":
    sys.exit(main())
