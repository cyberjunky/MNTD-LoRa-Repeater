import base64

from lorarepeater.lorawan import Vendors, decode_phy, lora_airtime_ms, netid_of


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


def test_fctrl_flags_mac_and_network():
    # ConfirmedDataUp from a TTN DevAddr, FCtrl ADR|ACK + 3 bytes FOpts: DevStatusAns(battery 200, margin -3)
    fopts = bytes([0x06, 200, 0x3D])
    phy = bytes([0x80, 0x01, 0x00, 0x00, 0x26, 0xA0 | len(fopts), 0x05, 0x00]) + fopts + b"\x0a\x01\x02" + b"\x00" * 4
    info = decode_phy(_b64(phy))
    assert info.devaddr == "26000001" and info.netid == "000013" and info.network == "The Things Network"
    assert info.adr == 1 and info.ack == 1 and info.adr_ack_req == 0 and info.fpending == 0
    assert info.mac == [{"cmd": "DevStatusAns", "battery": 200, "margin": -3}]
    assert info.battery == 200 and info.margin == -3
    assert info.fport == 10 and info.payload_len == 2 and info.fopts == "06c83d"


def test_downlink_mac_and_private_netid():
    fopts = bytes([0x02, 12, 3, 0x06])                       # LinkCheckAns(margin 12, 3 gateways), DevStatusReq
    phy = bytes([0x60, 0x91, 0x00, 0x0E, 0x00, 0x30 | len(fopts), 0x01, 0x00]) + fopts + b"\x00" * 4
    info = decode_phy(_b64(phy))
    assert info.devaddr == "000E0091" and info.network == "NetID 0 (non-roaming)"
    assert info.mac == [{"cmd": "LinkCheckAns", "margin": 12, "gateways": 3}, {"cmd": "DevStatusReq"}]
    assert info.fpending == 1 and info.adr_ack_req is None and info.fport is None


def test_encrypted_fopts_kept_raw():
    fopts = bytes([0xEE, 0x01])                               # not a valid 1.0 MAC command: 1.1 encrypted
    phy = bytes([0x40, 0x01, 0x00, 0x00, 0x26, len(fopts), 0x01, 0x00]) + fopts + b"\x00" * 4
    info = decode_phy(_b64(phy))
    assert info.mac == [] and info.fopts == "ee01"


def test_netid_types():
    assert netid_of(0x78000001) == ("00003C", "Helium")
    assert netid_of(0xFC00AC01)[0].startswith("type 6") or netid_of(0xFC00AC01)[1] == "Helium"
    assert netid_of(0xFFFFFFFF) == ("invalid", None)


def test_vendors_and_airtime(tmp_path):
    (tmp_path / "oui.csv").write_text("Registry,Assignment,Organization Name,Organization Address\nMA-L,0004A3,Microchip Technology Inc.,x\n")
    (tmp_path / "mam.csv").write_text("Registry,Assignment,Organization Name,Organization Address\n")
    (tmp_path / "oui36.csv").write_text("Registry,Assignment,Organization Name,Organization Address\nMA-S,70B3D57ED,The Things Industries B.V.,x\n")
    v = Vendors(tmp_path)
    join_eui = bytes.fromhex("70B3D57ED0000001")[::-1]
    dev_eui = bytes.fromhex("0004A30B001C0530")[::-1]
    info = decode_phy(_b64(b"\x00" + join_eui + dev_eui + b"\x12\x34" + b"\x00" * 4), v)
    assert info.dev_vendor == "Microchip Technology Inc." and info.join_vendor == "The Things Industries B.V."
    assert lora_airtime_ms("SF7BW125", "4/5", 23) == 61.7
    assert lora_airtime_ms("SF12BW125", "4/5", 23) == 1482.8
    assert lora_airtime_ms(None, None, None) is None
