import base64
import json
import struct

import httpx

from lorarepeater.config import HeliumConfig
from lorarepeater.helium import TOKENS, Helium, b58encode, claim_from_tx, parse_recipient

ASSET = "B6KLmmB6TsLjvdsiRDvPfxXrSBUSV9kVLRZiUpqzesqY"
WALLET = "5eB7QY7uiVUo2nUfV4LYnH4Gd43pYiHTpxLujz2wTuWa"
TREASURY = "73zsmmqCXjvHHhNSib26Y8p3jYiH3UUuyKv71RJDnctW"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def recipient_b64(distributor: str, claimed: int) -> str:
    raw = bytes(8) + b58decode(distributor).rjust(32, b"\0") + b58decode(ASSET).rjust(32, b"\0") + struct.pack("<Q", claimed)
    return base64.b64encode(raw + bytes(128)).decode()


def claim_tx(mint: str, amount: int) -> dict:
    return {"meta": {
        "preTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": TREASURY, "uiTokenAmount": {"amount": "1000000000000"}},
                             {"accountIndex": 2, "mint": mint, "owner": WALLET, "uiTokenAmount": {"amount": "100"}}],
        "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": TREASURY, "uiTokenAmount": {"amount": str(1000000000000 - amount)}},
                              {"accountIndex": 2, "mint": mint, "owner": WALLET, "uiTokenAmount": {"amount": str(100 + amount)}}]}}


def test_b58_roundtrip_and_recipient():
    assert b58encode(b58decode(WALLET)) == WALLET
    hnt_distributor = TOKENS["HNT"][2]
    assert parse_recipient(recipient_b64(hnt_distributor, 540287958)) == (hnt_distributor, 540287958)


def test_claim_from_tx_picks_the_receiving_wallet():
    mint = TOKENS["HNT"][0]
    assert claim_from_tx(claim_tx(mint, 16_000_000), mint, 8) == (WALLET, 0.16)
    assert claim_from_tx(claim_tx(mint, 5), TOKENS["IOT"][0], 6) is None


async def test_refresh_builds_the_snapshot():
    hnt_mint, _, hnt_ld = TOKENS["HNT"]
    iot_mint, _, iot_ld = TOKENS["IOT"]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith("https://entities.nft.helium.io/"):
            return httpx.Response(200, json={"name": "Skinny Daisy Armadillo", "asset_id": ASSET,
                                             "attributes": [{"trait_type": "networks", "value": ["iot"]}],
                                             "hotspot_infos": {"iot": {"is_active": False, "lat": 51.0, "long": 4.0,
                                                                       "city": "Town", "elevation": "8", "gain": "45",
                                                                       "created_at": "2025-08-05T16:32:50Z"}}})
        if "hnt-rewards.oracle" in url:
            return httpx.Response(200, json={"currentRewards": "600000000"})
        if "iot-rewards.oracle" in url:
            return httpx.Response(200, json={"currentRewards": "229331737510"})
        if "coingecko" in url:
            return httpx.Response(200, json={"helium": {"eur": 2.5, "usd": 2.9}})
        body = json.loads(req.content)
        method, params = body["method"], body["params"]
        results = {
            "getProgramAccounts": lambda: [{"pubkey": "RecHNT", "account": {"data": [recipient_b64(hnt_ld, 540287958), "base64"]}},
                                           {"pubkey": "RecIOT", "account": {"data": [recipient_b64(iot_ld, 229331737510), "base64"]}}],
            "getSignaturesForAddress": lambda: [{"signature": f"sig-{params[0]}", "blockTime": 1790019980}],
            "getTransaction": lambda: claim_tx(hnt_mint if params[0] == "sig-RecHNT" else iot_mint, 16_000_000),
            "getBalance": lambda: {"value": 2_000_000_000},
            "getTokenAccountsByOwner": lambda: {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {
                "uiAmountString": "5.5" if params[1]["mint"] == hnt_mint else "1049.4"}}}}}}]},
        }
        result = results[method]()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    h = Helium(HeliumConfig(), transport=httpx.MockTransport(handler))

    async def gateway_info():
        return {"key": "112FYbEk", "name": "skinny-daisy-armadillo", "region": "EU868", "router": {"connected": True}}

    h._gateway_info = gateway_info
    await h.refresh()
    s = h.status()
    assert s["errors"] == {}
    assert s["hotspot"]["asset_id"] == ASSET and s["hotspot"]["gain_dbi"] == 4.5 and s["hotspot"]["active"] is False
    hnt, iot = s["rewards"]["tokens"]["HNT"], s["rewards"]["tokens"]["IOT"]
    assert hnt["lifetime"] == 6.0 and hnt["claimed"] == 5.40287958 and round(hnt["unclaimed"], 8) == 0.59712042
    assert iot["unclaimed"] == 0 and iot["claims"][0]["amount"] == 16.0
    assert s["rewards"]["wallet"] == WALLET
    assert s["wallet"]["balances"] == {"SOL": 2.0, "HNT": 5.5, "IOT": 1049.4}
    assert s["price"]["HNT"]["eur"] == 2.5
