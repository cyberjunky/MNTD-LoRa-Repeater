"""LoRaWAN PHYPayload decoder: everything that is readable without the device's session keys.

FRMPayload is encrypted with keys only the device owner has, so application data can never be
read here. What is in the clear (LoRaWAN 1.0.x) and decoded:

* MHDR message type; FHDR DevAddr, FCtrl flags (ADR, ADRACKReq, ACK, FPending/ClassB), FCnt;
* FOpts MAC commands, e.g. DevStatusAns (battery, link margin), LinkCheckAns (margin, gateway
  count), LinkADRReq (data rate, TX power), DeviceTimeAns (network time);
* the network operator from the DevAddr's NetID bits (LoRaWAN Backend Interfaces, TS002);
* JoinEUI / DevEUI of join and rejoin requests, and their vendor from the IEEE registry;
* airtime per frame from data rate, coding rate and size.

LoRaWAN 1.1 encrypts FOpts; such options then fail to parse and are kept as raw hex.
"""

from __future__ import annotations

import base64
import csv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .meshproto import airtime_ms

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
UPLINK = {0, 2, 4, 6}

# NetIDs with a public operator name. Others are shown as the raw NetID.
NETWORKS = {
    0x000000: "private/experimental",
    0x000001: "private/experimental",
    0x000013: "The Things Network",
    0x00003C: "Helium",
    0x60002D: "Helium",
    0xC00053: "Helium",
}

# DevAddr type: (prefix length, NwkID bits). TS002 table 3; types 3-7 only carry the NwkID,
# the low bits of a longer NetID.
DEVADDR_TYPES = [(1, 6), (2, 6), (3, 9), (4, 11), (5, 12), (6, 13), (7, 15), (8, 17)]

# MAC commands: CID -> (name, payload length), separately for uplink and downlink (TS001 1.0.4)
MAC_UP = {
    0x01: ("ResetInd", 1), 0x02: ("LinkCheckReq", 0), 0x03: ("LinkADRAns", 1), 0x04: ("DutyCycleAns", 0),
    0x05: ("RXParamSetupAns", 1), 0x06: ("DevStatusAns", 2), 0x07: ("NewChannelAns", 1),
    0x08: ("RXTimingSetupAns", 0), 0x09: ("TxParamSetupAns", 0), 0x0A: ("DlChannelAns", 1),
    0x0B: ("RekeyInd", 1), 0x0C: ("ADRParamSetupAns", 0), 0x0D: ("DeviceTimeReq", 0),
    0x0F: ("RejoinParamSetupAns", 1), 0x10: ("PingSlotInfoReq", 1), 0x11: ("PingSlotChannelAns", 1),
    0x12: ("BeaconTimingReq", 0), 0x13: ("BeaconFreqAns", 1),
}
MAC_DOWN = {
    0x01: ("ResetConf", 1), 0x02: ("LinkCheckAns", 2), 0x03: ("LinkADRReq", 4), 0x04: ("DutyCycleReq", 1),
    0x05: ("RXParamSetupReq", 4), 0x06: ("DevStatusReq", 0), 0x07: ("NewChannelReq", 5),
    0x08: ("RXTimingSetupReq", 1), 0x09: ("TxParamSetupReq", 1), 0x0A: ("DlChannelReq", 4),
    0x0B: ("RekeyConf", 1), 0x0C: ("ADRParamSetupReq", 1), 0x0D: ("DeviceTimeAns", 5),
    0x0E: ("ForceRejoinReq", 2), 0x0F: ("RejoinParamSetupReq", 1), 0x10: ("PingSlotInfoAns", 0),
    0x11: ("PingSlotChannelReq", 4), 0x12: ("BeaconTimingAns", 3), 0x13: ("BeaconFreqReq", 3),
}
GPS_EPOCH_UNIX = 315964800


@dataclass
class PhyInfo:
    mtype: str
    devaddr: str | None = None
    fcnt: int | None = None
    fport: int | None = None
    dev_eui: str | None = None
    join_eui: str | None = None
    adr: int | None = None
    adr_ack_req: int | None = None
    ack: int | None = None
    fpending: int | None = None        # downlink: more data pending; uplink: Class B
    fopts: str | None = None           # raw hex
    mac: list[dict[str, Any]] = field(default_factory=list)
    payload_len: int | None = None     # FRMPayload bytes (encrypted)
    netid: str | None = None
    network: str | None = None
    dev_vendor: str | None = None
    join_vendor: str | None = None
    battery: int | None = None         # DevStatusAns: 0 = external power, 1..254, None = unknown
    margin: int | None = None          # DevStatusAns / LinkCheckAns: dB above demodulation floor

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["mac"] = row["mac"] or None
        return row


def _eui(b: bytes) -> str:
    return b[::-1].hex().upper()        # EUIs are little-endian on the air


def netid_of(devaddr: int) -> tuple[str, str | None]:
    """(NetID or 'type N NwkID x', operator name) from a 32-bit DevAddr."""
    for typ, (plen, nwk_bits) in enumerate(DEVADDR_TYPES):
        if devaddr >> (32 - plen) == (1 << plen) - 2:     # prefix: plen-1 ones then a zero
            nwkid = (devaddr >> (32 - plen - nwk_bits)) & ((1 << nwk_bits) - 1)
            if typ <= 2:                                  # the NwkID is the whole NetID ID
                netid = (typ << 21) | nwkid
                return f"{netid:06X}", NETWORKS.get(netid)
            for netid, name in NETWORKS.items():
                if netid >> 21 == typ and netid & ((1 << nwk_bits) - 1) == nwkid:
                    return f"{netid:06X}", name
            return f"type {typ} NwkID {nwkid:X}", None
    return "invalid", None


def _mac_detail(name: str, p: bytes) -> dict[str, Any]:
    if name == "DevStatusAns":
        margin = p[1] & 0x3F
        return {"battery": None if p[0] == 255 else p[0], "margin": margin - 64 if margin & 0x20 else margin}
    if name == "LinkCheckAns":
        return {"margin": p[0], "gateways": p[1]}
    if name == "LinkADRReq":
        return {"datarate": p[0] >> 4, "txpower": p[0] & 0x0F, "chmask": f"{int.from_bytes(p[1:3], 'little'):04X}",
                "nbtrans": p[3] & 0x0F}
    if name == "LinkADRAns":
        return {"power_ack": bool(p[0] & 4), "datarate_ack": bool(p[0] & 2), "chmask_ack": bool(p[0] & 1)}
    if name == "DeviceTimeAns":
        return {"gps_time": GPS_EPOCH_UNIX + int.from_bytes(p[0:4], "little") + p[4] / 256}
    if name == "DutyCycleReq":
        return {"max_duty": f"1/{2 ** (p[0] & 0x0F)}"}
    if name == "RXTimingSetupReq":
        return {"rx1_delay_s": max(p[0] & 0x0F, 1)}
    if name == "NewChannelReq":
        return {"ch": p[0], "freq_mhz": int.from_bytes(p[1:4], "little") * 100 / 1e6, "dr_min": p[4] & 0x0F,
                "dr_max": p[4] >> 4}
    if name in ("ResetInd", "ResetConf", "RekeyInd", "RekeyConf"):
        return {"minor": p[0] & 0x0F}
    return {"hex": p.hex()} if p else {}


def parse_mac(data: bytes, uplink: bool) -> list[dict[str, Any]] | None:
    """MAC commands from FOpts (or a decrypted FPort 0 payload). None if it does not parse,
    which for LoRaWAN 1.1 means the options are encrypted."""
    table = MAC_UP if uplink else MAC_DOWN
    out, i = [], 0
    while i < len(data):
        cid = data[i]
        if cid not in table:
            return None
        name, n = table[cid]
        if i + 1 + n > len(data):
            return None
        out.append({"cmd": name, **_mac_detail(name, data[i + 1:i + 1 + n])})
        i += 1 + n
    return out


class Vendors:
    """IEEE MA-L / MA-M / MA-S registry lookup (CSV files fetched by install.sh)."""

    FILES: ClassVar[dict[str, int]] = {"oui36.csv": 9, "mam.csv": 7, "oui.csv": 6}   # hex digits assigned

    def __init__(self, directory: Path | None):
        self.maps: list[tuple[int, dict[str, str]]] = []
        if directory is None:
            return
        for name, digits in self.FILES.items():
            path = Path(directory) / name
            if not path.is_file():
                continue
            with path.open(encoding="utf-8", errors="replace", newline="") as f:
                rows = csv.reader(f)
                next(rows, None)
                self.maps.append((digits, {r[1].upper(): r[2].strip() for r in rows if len(r) > 2}))

    def lookup(self, eui: str | None) -> str | None:
        if not eui:
            return None
        for digits, table in self.maps:
            name = table.get(eui[:digits].upper())
            if name:
                return name
        return None


NO_VENDORS = Vendors(None)


def lora_airtime_ms(datr: str | None, codr: str | None, size: int | None) -> float | None:
    """Airtime of a LoRa frame from GWMP fields (LoRaWAN: 8 symbol preamble, explicit header, CRC
    on uplinks)."""
    try:
        sf_s, bw_s = str(datr).upper().removeprefix("SF").split("BW")
        cr = int(str(codr or "4/5").split("/")[1])        # airtime_ms wants the 4/x denominator
        return round(airtime_ms(int(sf_s), int(bw_s) * 1000, cr, int(size), preamble=8), 1)
    except (ValueError, IndexError, TypeError):
        return None


def decode_phy(data_b64: str, vendors: Vendors = NO_VENDORS) -> PhyInfo | None:
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return None
    if not raw:
        return None
    mtype_n = raw[0] >> 5
    info = PhyInfo(mtype=MTYPES[mtype_n])
    if mtype_n == 0 and len(raw) == 23:
        info.join_eui, info.dev_eui = _eui(raw[1:9]), _eui(raw[9:17])
    elif mtype_n == 6 and len(raw) in (19, 24):
        rtype = raw[1]
        if rtype in (0, 2) and len(raw) == 19:          # NetID + DevEUI
            info.netid = raw[2:5][::-1].hex().upper()
            info.network = NETWORKS.get(int(info.netid, 16))
            info.dev_eui = _eui(raw[5:13])
        elif rtype == 1 and len(raw) == 24:             # JoinEUI + DevEUI
            info.join_eui, info.dev_eui = _eui(raw[2:10]), _eui(raw[10:18])
    elif mtype_n in (2, 3, 4, 5) and len(raw) >= 12:
        devaddr = int.from_bytes(raw[1:5], "little")
        info.devaddr = f"{devaddr:08X}"
        info.netid, info.network = netid_of(devaddr)
        fctrl = raw[5]
        info.adr = fctrl >> 7
        info.adr_ack_req = (fctrl >> 6) & 1 if mtype_n in UPLINK else None
        info.ack = (fctrl >> 5) & 1
        info.fpending = (fctrl >> 4) & 1
        info.fcnt = int.from_bytes(raw[6:8], "little")
        n_opts = fctrl & 0x0F
        fopts = raw[8:8 + n_opts]
        rest = raw[8 + n_opts:-4]                        # FPort + FRMPayload, MIC stripped
        if fopts:
            info.fopts = fopts.hex()
            info.mac = parse_mac(fopts, mtype_n in UPLINK) or []
        if rest:
            info.fport = rest[0]
            info.payload_len = len(rest) - 1
        for m in info.mac:
            if m["cmd"] == "DevStatusAns":
                info.battery, info.margin = m["battery"], m["margin"]
            elif m["cmd"] == "LinkCheckAns":
                info.margin = m["margin"]
    info.dev_vendor = vendors.lookup(info.dev_eui)
    info.join_vendor = vendors.lookup(info.join_eui)
    return info
