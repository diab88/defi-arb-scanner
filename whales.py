#!/usr/bin/env python3
"""
Whale watcher for Robinhood Chain (chain id 4663) via Etherscan's V2 multichain API.

Discovery pipeline (read-only, public on-chain data):
  1. Pick the hottest non-stable token among Robinhood Chain pools by 24h USD volume
     (from DefiLlama — reliable USD ranking, no key needed for this step).
  2. Pull that token's transfers over the last N hours from Etherscan (chainid=4663),
     aggregate by wallet, drop contract addresses (pools/routers), rank by volume.
  3. Those top wallets are the candidate whales to track.

Tracking: poll a watched wallet's recent transactions/token transfers; anything new since
last check is surfaced as an alert. Requires ETHERSCAN_API_KEY (free V2 multichain key).
"""

from __future__ import annotations

import os
import time

import requests

import scanner  # reuse fetch(), POOLS_URL, asset_class(), pair_parts()

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
ROBINHOOD_CHAIN_ID = 4663
_MIN_INTERVAL = 0.22  # ~5 req/s, Etherscan free-tier limit


def api_key() -> str:
    return os.environ.get("ETHERSCAN_API_KEY", "").strip()


def configured() -> bool:
    return bool(api_key())


def _es(params: dict) -> dict:
    """One Etherscan V2 call against Robinhood Chain, rate-limited."""
    p = dict(params)
    p["chainid"] = ROBINHOOD_CHAIN_ID
    p["apikey"] = api_key()
    time.sleep(_MIN_INTERVAL)
    r = requests.get(ETHERSCAN_V2, params=p, timeout=25)
    return r.json()


def _rows(res: dict) -> list:
    return res.get("result") if isinstance(res.get("result"), list) else []


def _is_stable(sym: str) -> bool:
    return scanner.asset_class(sym) == "STABLE"


def robinhood_pools() -> list[dict]:
    rows = scanner.fetch(scanner.POOLS_URL)
    return [r for r in rows
            if (r.get("chain") or "").lower() == "robinhood chain" and r.get("exposure") == "multi"]


def candidate_tokens(max_tokens: int = 18) -> list[dict]:
    """Distinct non-stablecoin tokens traded in Robinhood Chain pools: [{symbol,address,pool}]."""
    seen: dict[str, dict] = {}
    for p in robinhood_pools():
        parts = scanner.pair_parts(p.get("symbol", ""))
        uts = p.get("underlyingTokens") or []
        for i, part in enumerate(parts):
            # skip stablecoins and crypto majors — focus on stocks / memecoins / alt tokens
            if _is_stable(part) or scanner.asset_class(part) in ("ETH", "BTC", "SOL"):
                continue
            if i >= len(uts) or not uts[i]:
                continue
            a = uts[i].lower()
            seen.setdefault(a, {"symbol": part, "address": a, "pool": p.get("symbol")})
    return list(seen.values())[:max_tokens]


def _token_transfers_in_window(address: str, cutoff: float) -> list[dict]:
    txs = _rows(_es({"module": "account", "action": "tokentx", "contractaddress": address,
                     "page": 1, "offset": 300, "sort": "desc"}))
    out = []
    for t in txs:
        try:
            ts = int(t.get("timeStamp", 0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            break
        out.append(t)
    return out


def _is_contract(address: str) -> bool:
    try:
        code = _es({"module": "proxy", "action": "eth_getCode",
                    "address": address, "tag": "latest"}).get("result", "0x")
        return bool(code) and code not in ("0x", "0x0", "")
    except (requests.RequestException, ValueError):
        return False


def discover_whales(hours: int = 6, top_wallets: int = 10) -> dict:
    """Score candidate tokens by real transfer activity in the window, pick the hottest,
    then rank its biggest traders (EOAs; contracts/pools excluded)."""
    if not configured():
        raise RuntimeError("ETHERSCAN_API_KEY not set")
    cands = candidate_tokens()
    if not cands:
        return {"error": "No Robinhood Chain tokens found in DefiLlama data."}

    cutoff = time.time() - hours * 3600
    scored = []
    for c in cands:
        recent = _token_transfers_in_window(c["address"], cutoff)
        scored.append((len(recent), recent, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] == 0:
        return {"error": f"No token transfers on Robinhood Chain in the last {hours}h.",
                "hours": hours}
    _, txs, hc = scored[0]
    sym = (txs[0].get("tokenSymbol") if txs else "") or hc["symbol"]
    ht = {"symbol": sym, "address": hc["address"], "pool_symbol": hc["pool"],
          "vol24h": 0, "transfers": len(txs)}

    vol_by: dict[str, float] = {}
    cnt_by: dict[str, int] = {}
    last_by: dict[str, int] = {}
    for t in txs:
        try:
            ts = int(t.get("timeStamp", 0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            break  # sorted desc — everything older is out of window
        try:
            val = int(t.get("value", 0)) / (10 ** int(t.get("tokenDecimal") or 18))
        except (TypeError, ValueError):
            val = 0.0
        for who in (t.get("from"), t.get("to")):
            if not who:
                continue
            who = who.lower()
            vol_by[who] = vol_by.get(who, 0.0) + val
            cnt_by[who] = cnt_by.get(who, 0) + 1
            last_by[who] = max(last_by.get(who, 0), ts)

    ranked = sorted(vol_by.items(), key=lambda kv: kv[1], reverse=True)
    wallets, checked = [], 0
    for addr, vol in ranked:
        if len(wallets) >= top_wallets or checked >= 60:
            break
        if addr == ht["address"]:
            continue
        checked += 1
        if _is_contract(addr):
            continue
        wallets.append({"address": addr, "volume": round(vol, 2),
                        "tx_count": cnt_by[addr], "last_active": last_by[addr]})
    return {"hot_token": ht, "hours": hours, "traders_seen": len(vol_by), "wallets": wallets}


def wallet_new_activity(address: str, since_ts: int, limit: int = 25) -> list[dict]:
    """Recent normal + token transactions for `address` newer than since_ts (desc)."""
    out = []
    for action in ("txlist", "tokentx"):
        for t in _rows(_es({"module": "account", "action": action, "address": address,
                            "page": 1, "offset": limit, "sort": "desc"})):
            try:
                ts = int(t.get("timeStamp", 0))
            except (TypeError, ValueError):
                continue
            if ts <= since_ts:
                continue
            out.append({"hash": t.get("hash"), "ts": ts, "kind": action,
                        "token": t.get("tokenSymbol", ""), "from": (t.get("from") or "").lower(),
                        "to": (t.get("to") or "").lower(), "value": t.get("value")})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]
