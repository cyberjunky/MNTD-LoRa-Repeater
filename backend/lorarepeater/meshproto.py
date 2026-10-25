"""Meshtastic on-air protocol, pure Python (no radio, fully unit-testable).

Frame layout (firmware src/mesh/RadioInterface.h, PacketHeader):

    offset  size  field
    0       4     destination node number, little endian (0xFFFFFFFF = broadcast)
    4       4     sender node number, little endian
    8       4     packet id, little endian
    12      1     flags: bits 0-2 hop_limit, bit 3 want_ack, bit 4 via_mqtt, bits 5-7 hop_start
    13      1     channel hash (xor of channel name bytes ^ xor of PSK bytes)
    14      1     next_hop  (last byte of the node that should relay next, 0 = no preference)
    15      1     relay_node (last byte of the node that relayed this copy)
    16      ...   payload: meshtastic.protobuf.Data, AES-CTR encrypted with the channel key

AES-CTR nonce (CryptoEngine::initNonce): packet id as 64-bit LE + sender as 32-bit LE + 4 zero
bytes. Default PSK index n maps to the well-known 16-byte key with the last byte + (n-1).

References: https://meshtastic.org/docs/overview/encryption/ and the firmware sources.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BROADCAST = 0xFFFFFFFF
HEADER_LEN = 16
MAX_HOPS = 7
DEFAULT_PSK = bytes([0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59, 0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01])

# Modem presets: (spreading factor, bandwidth Hz, coding rate denominator). EU_868 puts all of
# them in the 869.4-869.65 MHz band; the slot is derived below exactly like the firmware does.
PRESETS = {
    "LongFast": (11, 250_000, 5),
    "LongSlow": (12, 125_000, 8),
    "LongModerate": (11, 125_000, 8),
    "MediumFast": (9, 250_000, 5),
    "MediumSlow": (10, 250_000, 5),
    "ShortFast": (7, 250_000, 5),
    "ShortSlow": (8, 250_000, 5),
    "ShortTurbo": (7, 500_000, 5),
    "LongTurbo": (11, 500_000, 5),
}
EU_868_BAND = (869.4e6, 869.65e6)


@dataclass
class Header:
    to: int
    sender: int
    packet_id: int
    hop_limit: int = 3
    want_ack: bool = False
    via_mqtt: bool = False
    hop_start: int = 3
    channel_hash: int = 0
    next_hop: int = 0
    relay_node: int = 0

    @classmethod
    def parse(cls, raw: bytes) -> Header:
        if len(raw) < HEADER_LEN:
            raise ValueError(f"frame too short ({len(raw)} bytes)")
        flags = raw[12]
        return cls(
            to=int.from_bytes(raw[0:4], "little"),
            sender=int.from_bytes(raw[4:8], "little"),
            packet_id=int.from_bytes(raw[8:12], "little"),
            hop_limit=flags & 0x07,
            want_ack=bool(flags & 0x08),
            via_mqtt=bool(flags & 0x10),
            hop_start=(flags >> 5) & 0x07,
            channel_hash=raw[13],
            next_hop=raw[14],
            relay_node=raw[15],
        )

    def pack(self) -> bytes:
        flags = (self.hop_limit & 0x07) | (0x08 if self.want_ack else 0) | (0x10 if self.via_mqtt else 0) | ((self.hop_start & 0x07) << 5)
        return (
            self.to.to_bytes(4, "little") + self.sender.to_bytes(4, "little") + self.packet_id.to_bytes(4, "little")
            + bytes([flags, self.channel_hash & 0xFF, self.next_hop & 0xFF, self.relay_node & 0xFF])
        )

    @property
    def hops_used(self) -> int | None:
        return self.hop_start - self.hop_limit if self.hop_start and self.hop_start >= self.hop_limit else None


# -- keys and channels -------------------------------------------------------------

def expand_psk(psk_b64: str) -> bytes:
    """Meshtastic PSK rules: '' = none, 1 byte = default-key index, 16/32 bytes = AES128/256."""
    raw = base64.b64decode(psk_b64) if psk_b64 else b""
    if len(raw) == 0:
        return b""
    if len(raw) == 1:
        idx = raw[0]
        if idx == 0:
            return b""
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + idx - 1) & 0xFF
        return bytes(key)
    if len(raw) in (16, 32):
        return raw
    raise ValueError(f"PSK must be 0, 1, 16 or 32 bytes, got {len(raw)}")


def xor_hash(data: bytes) -> int:
    h = 0
    for b in data:
        h ^= b
    return h


def channel_hash(name: str, key: bytes) -> int:
    return xor_hash(name.encode()) ^ xor_hash(key)


def nonce(packet_id: int, sender: int) -> bytes:
    return packet_id.to_bytes(8, "little") + sender.to_bytes(4, "little") + bytes(4)


def crypt(key: bytes, packet_id: int, sender: int, data: bytes) -> bytes:
    """AES-CTR is symmetric: the same call encrypts and decrypts."""
    if not key:
        return data
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce(packet_id, sender)))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


# -- frequency slot (firmware RadioInterface::applyModemConfig) ---------------------

def djb2(name: str) -> int:
    h = 5381
    for c in name.encode():
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


def slot_frequency_hz(bw_hz: int, channel_name: str, band: tuple[float, float] = EU_868_BAND) -> float:
    lo, hi = band
    num = max(1, int((hi - lo) / bw_hz))
    slot = djb2(channel_name) % num
    return lo + bw_hz / 2 + slot * bw_hz


def preset_radio(preset: str, channel_name: str = "LongFast") -> dict:
    sf, bw, cr = PRESETS[preset]
    return {"sf": sf, "bw_hz": bw, "cr": cr, "freq_hz": slot_frequency_hz(bw, channel_name),
            "datr": f"SF{sf}BW{bw // 1000}", "codr": f"4/{cr}"}


def parse_datr(datr: str) -> tuple[int, int]:
    """'SF11BW250' -> (11, 250000)."""
    sf, bw = datr.upper().replace("SF", "").split("BW")
    return int(sf), int(bw) * 1000


# -- airtime (Semtech AN1200.13, same formula RadioLib uses) -----------------------

def airtime_ms(sf: int, bw_hz: int, cr: int, payload_len: int, preamble: int = 16,
               crc: bool = True, explicit_header: bool = True, ldro: bool | None = None) -> float:
    t_sym = (2 ** sf) / bw_hz
    de = ldro if ldro is not None else t_sym >= 0.01638
    ih = 0 if explicit_header else 1
    num = 8 * payload_len - 4 * sf + 28 + 16 * (1 if crc else 0) - 20 * ih
    den = 4 * (sf - (2 if de else 0))
    n_payload = 8 + max(math.ceil(num / den) * cr, 0)
    return (preamble + 4.25 + n_payload) * t_sym * 1000.0


def node_id_str(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"
