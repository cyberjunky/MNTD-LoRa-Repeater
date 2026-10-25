import base64

import pytest

from lorarepeater.meshproto import (
    BROADCAST,
    DEFAULT_PSK,
    Header,
    airtime_ms,
    channel_hash,
    crypt,
    expand_psk,
    node_id_str,
    parse_datr,
)


def test_header_roundtrip():
    h = Header(to=BROADCAST, sender=0x12345678, packet_id=0xDEADBEEF, hop_limit=2, want_ack=True,
               via_mqtt=False, hop_start=3, channel_hash=8, next_hop=0x11, relay_node=0x78)
    raw = h.pack()
    assert len(raw) == 16
    assert raw[0:4] == b"\xff\xff\xff\xff" and raw[4:8] == bytes.fromhex("78563412")
    back = Header.parse(raw + b"payload")
    assert back == h
    assert back.hops_used == 1


def test_default_psk_and_longfast_hash():
    key = expand_psk("AQ==")
    assert key == DEFAULT_PSK
    # LongFast on the default key is channel hash 8 in every Meshtastic client
    assert channel_hash("LongFast", key) == 8
    assert expand_psk("") == b""
    assert expand_psk("AA==") == b""
    k2 = expand_psk("Ag==")
    assert k2[:-1] == DEFAULT_PSK[:-1] and k2[-1] == (DEFAULT_PSK[-1] + 1) & 0xFF
    own = base64.b64encode(bytes(range(32))).decode()
    assert expand_psk(own) == bytes(range(32))
    with pytest.raises(ValueError):
        expand_psk(base64.b64encode(b"short").decode())


def test_crypt_roundtrip_and_nonce_dependence():
    key = expand_psk("AQ==")
    plain = b"\x08\x01\x12\x05hello"
    ct = crypt(key, 0xDEADBEEF, 0x12345678, plain)
    assert ct != plain and len(ct) == len(plain)
    assert crypt(key, 0xDEADBEEF, 0x12345678, ct) == plain
    assert crypt(key, 0xDEADBEEE, 0x12345678, ct) != plain   # other packet id, other keystream
    assert crypt(b"", 1, 2, plain) == plain


def test_parse_datr():
    assert parse_datr("SF11BW250") == (11, 250_000)
    assert parse_datr("sf7bw125") == (7, 125_000)


def test_airtime_longfast():
    # SF11 / 250 kHz / CR 4/5, 32-byte frame, 16 symbol preamble, explicit header, CRC:
    # t_sym 8.192 ms, no LDRO, 38 payload symbols -> (16 + 4.25 + 38) * 8.192 = 477.2 ms
    assert airtime_ms(11, 250_000, 5, 32) == pytest.approx(477.18, abs=0.1)
    # SF12 / 125 kHz enables low-data-rate optimisation automatically
    assert airtime_ms(12, 125_000, 8, 32) > airtime_ms(12, 125_000, 8, 32, ldro=False)
    assert node_id_str(0x0A0B0C0D) == "!0a0b0c0d"
