"""Helium view of this hotspot: the local gateway-rs service plus public Helium and Solana data.

Sources, all public and read-only:

* gateway-rs local API (`helium_gateway info`): key, animal name, region, packet-router session;
* Helium entity service: the hotspot NFT (asset id, asserted location, gain, elevation, active);
* Helium reward oracles: lifetime rewards per token (HNT now, IOT before HIP-138);
* Solana mainnet RPC: the lazy-distributor recipient accounts (rewards claimed so far), the
  claim transactions (history and the wallet that receives them) and that wallet's balances;
* CoinGecko: the HNT price, for a value estimate.

Nothing is stored; the snapshot is rebuilt every REFRESH_S and served by /api/helium.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Any

import httpx

from .config import HeliumConfig

log = logging.getLogger(__name__)

REFRESH_S = 900
ENTITY_API = "https://entities.nft.helium.io/{key}"
ORACLES = {"HNT": "https://hnt-rewards.oracle.helium.io/?assetId={asset}",
           "IOT": "https://iot-rewards.oracle.helium.io/?assetId={asset}"}
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=helium&vs_currencies=eur,usd"
LAZY_DISTRIBUTOR_PROGRAM = "1azyuavdMyvsivtNxPoz6SucD18eDHeXzFCUPq5XU7w"
TOKENS = {  # symbol: (mint, decimals, lazy distributor)
    "HNT": ("hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux", 8, "6gcZXjHgKUBMedc2V1aZLFPwh8M1rPVRw7kpo2KqNrFq"),
    "IOT": ("iotEVVZLEywoTn1QdwNPddxPWszn3zFhEot3MfL9fns", 6, "37eiz5KzYwpAdLgrSh8GT1isKiJ6hcE5ET86dqaoCugL"),
}
CLAIM_HISTORY = 25          # claim transactions per token
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def parse_recipient(data_b64: str) -> tuple[str, int]:
    """Lazy-distributor RecipientV0: discriminator, lazy_distributor, asset, total_rewards (claimed)."""
    raw = base64.b64decode(data_b64)
    return b58encode(raw[8:40]), struct.unpack_from("<Q", raw, 72)[0]


def claim_from_tx(tx: dict[str, Any], mint: str, decimals: int) -> tuple[str, float] | None:
    """(receiving wallet, amount) of a claim: the owner whose balance of `mint` went up the least
    among those that rose (the treasury goes down, the hotspot owner up)."""
    meta = tx.get("meta") or {}
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", []) if b.get("mint") == mint}
    best = None
    for b in meta.get("postTokenBalances", []):
        if b.get("mint") != mint:
            continue
        before = int(pre.get(b["accountIndex"], {}).get("uiTokenAmount", {}).get("amount", 0))
        delta = int(b["uiTokenAmount"]["amount"]) - before
        if delta > 0 and (best is None or delta < best[1]):
            best = (b["owner"], delta)
    return (best[0], best[1] / 10 ** decimals) if best else None


class Helium:
    def __init__(self, cfg: HeliumConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport          # tests inject a mock transport
        self.state: dict[str, Any] = {"enabled": cfg.enabled}
        self._claims: dict[str, dict[str, Any]] = {}    # signature -> claim (transactions never change)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.cfg.enabled:
            self._task = asyncio.create_task(self._loop(), name="helium")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> dict[str, Any]:
        return self.state

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self.refresh()
            except Exception as e:  # network trouble must not kill the loop
                log.warning("helium refresh failed: %s", e)
                self.state["error"] = str(e)[:200]
            await asyncio.sleep(REFRESH_S)

    async def refresh(self) -> None:
        state: dict[str, Any] = {"enabled": True, "updated": time.time(), "errors": {}}
        state["gateway"] = await self._gateway_info()
        key = (state["gateway"] or {}).get("key")
        async with httpx.AsyncClient(timeout=20, transport=self.transport) as http:
            if key:
                state["hotspot"] = await self._section(state, "hotspot", self._entity(http, key))
            asset = (state.get("hotspot") or {}).get("asset_id")
            if asset:
                state["rewards"] = await self._section(state, "rewards", self._rewards(http, asset))
            wallet = (state.get("rewards") or {}).get("wallet")
            if wallet:
                state["wallet"] = await self._section(state, "wallet", self._wallet(http, wallet))
            state["price"] = await self._section(state, "price", self._price(http))
        self.state = state

    @staticmethod
    async def _section(state: dict[str, Any], name: str, coro: Any) -> Any:
        try:
            return await coro
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            state["errors"][name] = str(e)[:200]
            return None

    async def _gateway_info(self) -> dict[str, Any] | None:
        proc = await asyncio.create_subprocess_exec(
            self.cfg.cli, "-c", self.cfg.settings, "info", "key", "name", "region", "router", "fw",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        try:
            return json.loads(out)
        except ValueError:
            return {"error": out.decode(errors="replace").strip()[-200:]}

    async def _entity(self, http: httpx.AsyncClient, key: str) -> dict[str, Any]:
        r = await http.get(ENTITY_API.format(key=key))
        r.raise_for_status()
        e = r.json()
        iot = (e.get("hotspot_infos") or {}).get("iot") or {}
        return {
            "name": e.get("name"), "asset_id": e.get("asset_id"), "key": key,
            "networks": next((a["value"] for a in e.get("attributes", []) if a.get("trait_type") == "networks"), []),
            "active": iot.get("is_active"), "lat": iot.get("lat"), "lon": iot.get("long"), "h3": iot.get("location"),
            "city": iot.get("city"), "state": iot.get("state"), "country": iot.get("country"),
            "elevation_m": _num(iot.get("elevation")), "gain_dbi": (_num(iot.get("gain")) or 0) / 10 or None,
            "onboarded": iot.get("created_at"), "dc_onboarding_fee_paid": _num(iot.get("dc_onboarding_fee_paid")),
        }

    async def _rpc(self, http: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
        r = await http.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise ValueError(f"{method}: {body['error'].get('message')}")
        return body["result"]

    async def _rewards(self, http: httpx.AsyncClient, asset: str) -> dict[str, Any]:
        accounts = await self._rpc(http, "getProgramAccounts", [LAZY_DISTRIBUTOR_PROGRAM, {
            "encoding": "base64", "filters": [{"memcmp": {"offset": 40, "bytes": asset}}]}])
        recipients = {}
        for a in accounts:
            distributor, claimed = parse_recipient(a["account"]["data"][0])
            recipients[distributor] = (a["pubkey"], claimed)
        tokens, wallets = {}, {}
        for sym, (mint, decimals, distributor) in TOKENS.items():
            r = await http.get(ORACLES[sym].format(asset=asset))
            r.raise_for_status()
            lifetime = int(r.json()["currentRewards"])
            recipient, claimed = recipients.get(distributor, (None, 0))
            claims = await self._claim_history(http, recipient, mint, decimals) if recipient else []
            for c in claims:
                wallets[c["wallet"]] = wallets.get(c["wallet"], 0) + 1
            tokens[sym] = {"lifetime": lifetime / 10 ** decimals, "claimed": claimed / 10 ** decimals,
                           "unclaimed": (lifetime - claimed) / 10 ** decimals, "recipient": recipient, "claims": claims}
        return {"tokens": tokens, "wallet": max(wallets, key=wallets.get) if wallets else None}

    async def _claim_history(self, http: httpx.AsyncClient, recipient: str, mint: str, decimals: int) -> list[dict[str, Any]]:
        sigs = await self._rpc(http, "getSignaturesForAddress", [recipient, {"limit": CLAIM_HISTORY}])
        claims = []
        for s in sigs:
            sig = s["signature"]
            if sig not in self._claims and not s.get("err"):
                tx = await self._rpc(http, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                got = claim_from_tx(tx or {}, mint, decimals)
                self._claims[sig] = {"signature": sig, "time": s.get("blockTime"), "wallet": got[0], "amount": got[1]} if got else {}
                await asyncio.sleep(0.25)      # stay polite to the public RPC
            if self._claims.get(sig):
                claims.append(self._claims[sig])
        return claims

    async def _wallet(self, http: httpx.AsyncClient, wallet: str) -> dict[str, Any]:
        sol = await self._rpc(http, "getBalance", [wallet])
        balances = {"SOL": sol["value"] / 1e9}
        for sym, (mint, _, _) in TOKENS.items():
            res = await self._rpc(http, "getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
            balances[sym] = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]) for a in res["value"])
        return {"address": wallet, "balances": balances}

    async def _price(self, http: httpx.AsyncClient) -> dict[str, Any]:
        r = await http.get(PRICE_API)
        r.raise_for_status()
        return {"HNT": r.json()["helium"]}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
