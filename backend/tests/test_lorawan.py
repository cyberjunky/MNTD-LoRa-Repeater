import base64

from lorarepeater.lorawan import decode_phy


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_unconfirmed_uplink_with_port():
    # MHDR 0x40 (UnconfirmedDataUp), DevAddr 26011A2B (LE on air), FCtrl 0, FCnt 7, FPort 2, 3 bytes payload, MIC
    phy = bytes([0x40, 0x2B, 0x1A, 0x01, 0x26, 0x00, 0x07, 0x00, 0x02]) + b"\x01\x02\x03" + b"\xaa\xbb\xcc\xdd"
    info = decode_phy(_b64(phy))
    assert info is not None
    assert info.mtype == "UnconfirmedDataUp"
    assert info.devaddr == "26011A2B"
    assert info.fcnt == 7
    assert info.fport == 2


def test_data_frame_without_fport():
    phy = bytes([0x80, 0x01, 0x00, 0x00, 0x01, 0x00, 0x03, 0x00]) + b"\x00\x00\x00\x00"
    info = decode_phy(_b64(phy))
    assert info.mtype == "ConfirmedDataUp"
    assert info.fport is None
    assert info.fcnt == 3


def test_join_request():
    join_eui = bytes.fromhex("70B3D57ED0000001")[::-1]
    dev_eui = bytes.fromhex("0004A30B001C0530")[::-1]
    phy = b"\x00" + join_eui + dev_eui + b"\x12\x34" + b"\x00\x00\x00\x00"
    info = decode_phy(_b64(phy))
    assert info.mtype == "JoinRequest"
    assert info.join_eui == "70B3D57ED0000001"
    assert info.dev_eui == "0004A30B001C0530"


def test_garbage_is_safe():
    assert decode_phy("not base64!!") is None
    assert decode_phy(_b64(b"")) is None
    assert decode_phy(_b64(b"\xe0")).mtype == "Proprietary"
