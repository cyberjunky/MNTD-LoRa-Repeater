"""Minimal LoRaWAN PHYPayload decoder: enough to label packets in the GUI
(message type, DevAddr, FCnt, FPort, DevEUI/JoinEUI for joins). No decryption."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass

MTYPES = {
    0: "JoinRequest",
    1: "JoinAccept",
    2: "UnconfirmedDataUp",
    3: "UnconfirmedDataDown",
    4: "ConfirmedDataUp",
    5: "ConfirmedDataDown",
    6: "RejoinRequest",
    7: "Proprietary",
}


@dataclass
class PhyInfo:
    mtype: str
    devaddr: str | None = None
    fcnt: int | None = None
    fport: int | None = None
    dev_eui: str | None = None
    join_eui: str | None = None

    def as_row(self) -> dict:
        return asdict(self)


def _eui(b: bytes) -> str:
    # EUIs are little-endian on the air
    return b[::-1].hex().upper()


def decode_phy(data_b64: str) -> PhyInfo | None:
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    if len(raw) < 1:
        return None
    mtype_n = raw[0] >> 5
    mtype = MTYPES.get(mtype_n, "Unknown")
    info = PhyInfo(mtype=mtype)
    try:
        if mtype_n == 0 and len(raw) >= 1 + 8 + 8 + 2 + 4:
            info.join_eui = _eui(raw[1:9])
            info.dev_eui = _eui(raw[9:17])
        elif mtype_n in (2, 3, 4, 5) and len(raw) >= 1 + 7 + 4:
            info.devaddr = raw[1:5][::-1].hex().upper()
            fctrl = raw[5]
            info.fcnt = int.from_bytes(raw[6:8], "little")
            fopts_len = fctrl & 0x0F
            port_idx = 8 + fopts_len
            # MIC is the last 4 bytes; FPort exists only if there is something after FOpts
            if len(raw) - 4 > port_idx:
                info.fport = raw[port_idx]
    except Exception:
        pass
    return info
