from lorarepeater.mesh import node_to_row, packet_to_row


def test_packet_to_row_text():
    pkt = {
        "from": 0x12345678, "to": 0xFFFFFFFF, "id": 42, "rxTime": 1761390000, "rxSnr": 6.25, "rxRssi": -95,
        "hopLimit": 1, "hopStart": 3, "channel": 0, "wantAck": False, "priority": "DEFAULT",
        "viaMqtt": False, "relayNode": 0x78, "fromId": "!12345678", "toId": "^all",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hello mesh", "text": "hello mesh"},
    }
    row = packet_to_row(pkt)
    assert row["from_node"] == 0x12345678 and row["to_node"] == 0xFFFFFFFF
    assert row["hops_used"] == 2 and row["relay_node"] == 0x78
    assert row["text"] == "hello mesh" and row["portnum"] == "TEXT_MESSAGE_APP"
    assert row["encrypted"] == 0 and row["payload"] is None


def test_packet_to_row_encrypted_and_ids():
    pkt = {"fromId": "!0a0b0c0d", "toId": "!ffffffff", "id": 1, "hopLimit": 3, "encrypted": b"\x00\x01"}
    row = packet_to_row(pkt)
    assert row["from_node"] == 0x0A0B0C0D
    assert row["hops_used"] is None   # hop_start unknown
    assert row["encrypted"] == 1


def test_packet_to_row_position_payload():
    pkt = {"from": 1, "to": 2, "hopLimit": 3, "hopStart": 3,
           "decoded": {"portnum": "POSITION_APP", "position": {"latitude": 52.1, "longitude": 4.3}}}
    row = packet_to_row(pkt)
    assert row["hops_used"] == 0
    assert row["payload"] == {"position": {"latitude": 52.1, "longitude": 4.3}}


def test_node_to_row():
    node = {"num": 7, "user": {"id": "!00000007", "longName": "Roof", "shortName": "RF", "hwModel": "HELTEC_V3", "role": "ROUTER"},
            "position": {"latitude": 52.0, "longitude": 4.0, "altitude": 10}, "deviceMetrics": {"batteryLevel": 101, "voltage": 4.1},
            "lastHeard": 1761390000, "snr": 7.5, "hopsAway": 1}
    row = node_to_row(7, node)
    assert row["node_id"] == "!00000007" and row["long_name"] == "Roof" and row["role"] == "ROUTER"
    assert row["lat"] == 52.0 and row["battery"] == 101 and row["hops_away"] == 1
