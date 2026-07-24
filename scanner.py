#!/usr/bin/env python3
"""
DeFi yield-carry / loop arbitrage scanner (Phase 1 — read-only).

Strategy detected:
    1. Supply collateral asset A  -> earn supply APY
    2. Borrow asset B against it  -> pay borrow APY
    3. Deploy B into a pool       -> earn deploy APY
    Net only counts if deploy yield covers the borrow cost (plus margin).

Data: DefiLlama free APIs (no key required).
    - https://yields.llama.fi/poolsOld   (deploy pools, with apyBase/apyReward split)
    - https://yields.llama.fi/lendBorrow  (lending markets: supply + borrow rates, LTV)

This is a heuristic scanner for spotting candidates. It is NOT financial advice
and does NOT account for liquidation risk, slippage, gas, or reward-token decay
beyond a configurable discount. Always verify on-chain before acting.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

POOLS_URL = "https://yields.llama.fi/pools"
LENDBORROW_URL = "https://yields.llama.fi/lendBorrow"

# Assets we treat as interchangeable for matching borrow asset -> deploy pool.
# Symbols are uppercased and compared after stripping wrappers.
RATING_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
MAX_LOOPS_CAP = 10  # hard ceiling on recursive loops


def momentum_label(pct7d: float) -> str:
    """Classify a deploy pool's 7-day APY change into up / flat / down (0.5pp band)."""
    if pct7d is None:
        return "flat"
    if pct7d > 0.5:
        return "up"
    if pct7d < -0.5:
        return "down"
    return "flat"


def borrowed_multiple(t: float, loops: int) -> float:
    """Total borrowed (= total re-deployed) per unit of equity after `loops` recursive
    supply→borrow cycles at per-loop LTV `t`. Geometric series; reduces to `t` at loops=1.
    Aggregate debt/collateral stays `t`, so the health factor is unchanged by looping."""
    loops = max(1, min(loops, MAX_LOOPS_CAP))
    if loops == 1:
        return t
    if t >= 1.0:
        return t * loops
    return t * (1.0 - t ** loops) / (1.0 - t)

# Rough per-transaction gas cost in USD by chain (entry/exit each take several txs).
# Coarse estimates for cost modeling — tune for your conditions.
GAS_USD_PER_TX = {
    "Ethereum": 4.0,
    "Arbitrum": 0.10,
    "Optimism": 0.10,
    "Base": 0.05,
    "Polygon": 0.02,
    "BSC": 0.20,
    "Avalanche": 0.10,
    "Solana": 0.01,
    "Sui": 0.01,
}
DEFAULT_GAS_USD_PER_TX = 0.25  # fallback for chains not listed above

STABLES = {"USDC", "USDT", "DAI", "USDS", "FRAX", "LUSD", "GUSD", "USDE", "CRVUSD", "GHO"}
ETH_LIKE = {"ETH", "WETH", "STETH", "WSTETH", "WEETH", "RETH", "CBETH", "EZETH"}
BTC_LIKE = {"BTC", "WBTC", "TBTC", "CBBTC", "LBTC"}
SOL_LIKE = {"SOL", "WSOL", "MSOL", "JITOSOL", "JUPSOL", "BSOL", "INF", "HSOL", "JSOL", "BNSOL"}


def norm_symbol(sym: str) -> str:
    """Normalize a pool/market symbol to a single canonical asset where possible."""
    s = sym.upper().strip()
    # Single-asset markets only (skip LP pairs like "USDC-WETH").
    if "-" in s or "/" in s:
        return s
    if s.startswith("W") and s[1:] in (ETH_LIKE | BTC_LIKE):
        s = s[1:]
    return s


def asset_class(sym: str) -> str | None:
    s = norm_symbol(sym)
    if s in STABLES:
        return "STABLE"
    if s in ETH_LIKE:
        return "ETH"
    if s in BTC_LIKE:
        return "BTC"
    if s in SOL_LIKE:
        return "SOL"
    return None


@dataclass
class LendMarket:
    project: str
    chain: str
    symbol: str
    supply_apy: float       # net supply APY (base + reward)
    borrow_apy: float       # net borrow cost (base - reward incentive)
    ltv: float              # max loan-to-value (0..1)
    tvl_usd: float
    pool_id: str = ""       # DefiLlama pool id (for the pool page link)


@dataclass
class DeployPool:
    project: str
    chain: str
    symbol: str
    apy: float              # discounted total APY
    apy_base: float         # sustainable (organic) APY
    apy_reward: float       # incentive APY (pre-discount)
    tvl_usd: float
    il_risk: str            # 'yes' / 'no' from DefiLlama
    pred_class: str         # DefiLlama 30d outlook: Stable/Up/Down
    pred_prob: float        # confidence of that outlook
    pool_id: str = ""       # DefiLlama pool id (for the pool page link)
    apy_spot: float = 0.0   # discounted spot APY before spike-guarding
    apy_mean30d: float = 0.0
    apy_spiked: bool = False  # True when 30d avg was used instead of a spiked spot
    apy_pct7d: float = 0.0    # change in APY (pp) over the last 7 days (momentum)
    apy_pct30d: float = 0.0   # change in APY (pp) over the last 30 days


@dataclass
class Opportunity:
    collateral: LendMarket
    deploy: DeployPool
    borrow_market: LendMarket
    target_ltv: float
    net_apy: float          # net APY on equity (per $1 of collateral)
    raw_spread: float       # deploy_apy - borrow_apy
    # --- Phase 2 risk fields ---
    health_factor: float    # collateral*LT / debt at entry (>1 = solvent)
    liq_buffer_pct: float   # adverse price move (%) before liquidation
    reward_share: float     # fraction of deploy yield that is incentives (0..1)
    directional: bool       # collateral & borrow are different asset classes
    rating: str             # LOW / MEDIUM / HIGH overall risk
    warnings: list[str]
    # --- Phase 3 cost fields ---
    cost_drag_apy: float        # annualized drag from gas + slippage (%)
    net_apy_after_costs: float  # net_apy - cost_drag_apy
    cost_usd: float             # absolute round-trip cost in USD for the position
    # Direct-deposit benchmark: if you could just deposit the collateral into the deploy
    # pool (same asset class), what would that earn? None when not comparable.
    direct_deposit_apy: float | None = None
    beats_direct: bool = True   # False => a plain direct deposit beats this loop
    position_usd: float = 0.0   # USD value of the position used for this row's cost calc
    loops: int = 1              # recursive supply→borrow cycles applied
    leverage: float = 1.0       # total collateral / equity (1 + borrowed multiple)
    borrow_incentivized: bool = False  # net borrow cost < 0 (you're paid to borrow)
    deploy_momentum: str = "flat"      # deploy APY trend: "up" | "flat" | "down"


@dataclass
class ScanConfig:
    min_net_apy: float = 3.0
    min_tvl: float = 5_000_000
    ltv_safety: float = 0.8
    reward_discount: float = 0.5
    max_apy: float = 100.0
    same_chain: bool = True
    liq_premium: float = 0.05
    max_rating: str = "HIGH"
    # "all" | "correlated" (collateral & debt same asset class) | "uncorrelated" (directional)
    pairing: str = "all"
    # "all" | "stable" | "eth" | "btc" — restrict which asset class the collateral is
    collateral_class: str = "all"
    # Hide loops that a plain direct deposit of the collateral would beat (no borrowing needed)
    hide_inferior: bool = True
    # Recursive looping: re-supply the deployed asset and borrow again, N times, to amplify
    # the spread. Only applies to "loopable" loops (collateral & deploy same asset class).
    # 1 = single step (no extra leverage). Capped at MAX_LOOPS_CAP.
    max_loops: int = 1
    # Momentum filter on the deploy pool's recent APY trend: "any"|"rising"|"not_falling"
    momentum: str = "any"
    # Only show loops where the borrow is incentivized (net negative borrow cost)
    borrow_incentive_only: bool = False
    limit: int = 25
    # Phase 3 cost model
    position_size: float = 10_000.0  # amount in `position_unit` (USD by default)
    position_unit: str = "USD"       # "USD"|"ETH"|"BTC"|"native" (native = per-loop collateral)
    position_size_usd: float = 0.0   # resolved USD value (0 => use position_size as USD)
    prices: dict | None = None       # {ETH, BTC} prices, for native/per-row USD resolution
    hold_days: float = 30.0          # holding period to amortize fixed gas over
    tx_count: int = 8                # entry + exit transactions
    slippage_bps: float = 5.0        # per-swap slippage in basis points


CLASS_FILTER = {"stable": "STABLE", "eth": "ETH", "btc": "BTC", "sol": "SOL"}
PRICES_URL = "https://coins.llama.fi/prices/current/coingecko:ethereum,coingecko:bitcoin,coingecko:solana"


def fetch(url: str) -> list[dict]:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", data) if isinstance(data, dict) else data


def fetch_prices() -> dict:
    """Current ETH/BTC USD prices (DefiLlama coins API). Returns {} on failure."""
    try:
        coins = requests.get(PRICES_URL, timeout=15).json().get("coins", {})
        return {
            "ETH": coins.get("coingecko:ethereum", {}).get("price", 0.0),
            "BTC": coins.get("coingecko:bitcoin", {}).get("price", 0.0),
            "SOL": coins.get("coingecko:solana", {}).get("price", 0.0),
        }
    except (requests.RequestException, ValueError):
        return {}


def resolve_position_usd(position_size: float, unit: str, prices: dict) -> float:
    """Convert a position expressed in ETH/BTC units into USD; pass through if USD."""
    px = prices.get(unit) if prices else None
    if unit in ("ETH", "BTC") and px:
        return position_size * px
    return position_size


def position_usd_for(cfg: "ScanConfig", collateral_symbol: str) -> float:
    """USD value of the position for a specific loop. In 'native' mode the size is in
    units of that loop's collateral asset, so the USD value depends on the row."""
    if cfg.position_unit == "native":
        cls = asset_class(collateral_symbol)
        prices = cfg.prices or {}
        if cls in ("ETH", "BTC", "SOL") and prices.get(cls):
            return cfg.position_size * prices[cls]
        return cfg.position_size  # stablecoins (~$1) or unknown: treat units as USD
    return cfg.position_size_usd or cfg.position_size


def discounted_apy(base, reward, reward_discount: float) -> float:
    base = base or 0.0
    reward = reward or 0.0
    return base + reward * (1.0 - reward_discount)


# If a pool's spot APY exceeds its 30-day average by more than this factor, the spot
# figure is treated as a transient spike and the 30-day average is used instead — so a
# momentary 37% reading doesn't masquerade as a sustainable yield.
SPIKE_FACTOR = 2.0


def guarded_apy(row: dict, reward_discount: float) -> tuple[float, float, float, bool]:
    """Return (effective_apy, spot_apy, mean30d, spiked).

    effective_apy = the 30-day average when the discounted spot APY spikes well above it,
    otherwise the discounted spot APY. Protects rankings from transient APY spikes.
    """
    spot = discounted_apy(row.get("apyBase"), row.get("apyReward"), reward_discount)
    mean30 = row.get("apyMean30d") or 0.0
    spiked = mean30 > 0 and spot > mean30 * SPIKE_FACTOR
    return (mean30 if spiked else spot), spot, float(mean30), spiked


def is_outlier(meta: dict, apy: float, max_apy: float) -> bool:
    """DefiLlama flags transient APY spikes; also cap obviously broken numbers."""
    return bool(meta.get("outlier")) or apy > max_apy or apy < 0


def build_lend_markets(
    pool_rows: list[dict], reward_discount: float, min_tvl: float, max_apy: float
) -> list[LendMarket]:
    """Join the lendBorrow feed (borrow side + LTV) with /pools metadata + supply APY."""
    pool_meta = {p.get("pool"): p for p in pool_rows}
    rows = fetch(LENDBORROW_URL)
    markets: list[LendMarket] = []
    for r in rows:
        meta = pool_meta.get(r.get("pool"))
        if meta is None:
            continue
        symbol = meta.get("symbol", "")
        if asset_class(symbol) is None:
            continue
        if is_outlier(meta, meta.get("apy") or 0.0, max_apy):
            continue
        tvl = r.get("totalSupplyUsd") or 0.0
        if tvl < min_tvl:
            continue
        ltv = r.get("ltv") or 0.0
        if ltv <= 0 or not r.get("borrowable", True):
            continue
        supply, _, _, _ = guarded_apy(meta, reward_discount)
        # Borrow reward incentives REDUCE net borrow cost.
        borrow = (r.get("apyBaseBorrow") or 0.0) - (r.get("apyRewardBorrow") or 0.0) * (1.0 - reward_discount)
        markets.append(LendMarket(
            project=meta.get("project", "?"),
            chain=meta.get("chain", "?"),
            symbol=symbol,
            supply_apy=supply,
            borrow_apy=borrow,
            ltv=float(ltv),
            tvl_usd=float(tvl),
            pool_id=r.get("pool", ""),
        ))
    return markets


def build_deploy_pools(
    pool_rows: list[dict], reward_discount: float, min_tvl: float, max_apy: float
) -> list[DeployPool]:
    pools: list[DeployPool] = []
    for r in pool_rows:
        if asset_class(r.get("symbol", "")) is None:
            continue
        tvl = r.get("tvlUsd") or 0.0
        if tvl < min_tvl:
            continue
        if is_outlier(r, r.get("apy") or 0.0, max_apy):
            continue
        apy, spot, mean30, spiked = guarded_apy(r, reward_discount)
        if apy <= 0:
            continue
        pred = r.get("predictions") or {}
        pools.append(DeployPool(
            project=r.get("project", "?"),
            chain=r.get("chain", "?"),
            symbol=r.get("symbol", "?"),
            apy=apy,
            apy_base=r.get("apyBase") or 0.0,
            apy_reward=r.get("apyReward") or 0.0,
            tvl_usd=float(tvl),
            il_risk=r.get("ilRisk", "no"),
            pred_class=pred.get("predictedClass") or "?",
            pred_prob=float(pred.get("predictedProbability") or 0.0),
            pool_id=r.get("pool", ""),
            apy_spot=spot, apy_mean30d=mean30, apy_spiked=spiked,
            apy_pct7d=r.get("apyPct7D") or 0.0, apy_pct30d=r.get("apyPct30D") or 0.0,
        ))
    return pools


def assess_risk(
    collateral: LendMarket,
    borrow: LendMarket,
    deploy: DeployPool,
    target_ltv: float,
    liq_premium: float,
) -> dict:
    """Phase 2 risk layer: liquidation buffer, yield sustainability, directional risk."""
    # Liquidation threshold is usually a few points above max LTV; DefiLlama
    # doesn't expose it, so approximate LT = min(max_ltv + premium, 0.98).
    lt = min(collateral.ltv + liq_premium, 0.98)
    # At entry debt/collateral == target_ltv, so HF = LT / (D/C).
    health_factor = lt / target_ltv if target_ltv > 0 else float("inf")
    liq_buffer_pct = max(0.0, 1.0 - target_ltv / lt) * 100.0

    total_yield = deploy.apy_base + deploy.apy_reward
    reward_share = deploy.apy_reward / total_yield if total_yield > 0 else 0.0
    directional = asset_class(collateral.symbol) != asset_class(borrow.symbol)

    score = 0
    warnings: list[str] = []
    if liq_buffer_pct < 10:
        score += 2
        warnings.append(f"thin liquidation buffer (~{liq_buffer_pct:.0f}% adverse move)")
    elif liq_buffer_pct < 20:
        score += 1
        warnings.append(f"moderate liquidation buffer (~{liq_buffer_pct:.0f}%)")
    if directional:
        score += 1
        warnings.append("directional: collateral & debt are different assets (price risk)")
    if reward_share > 0.6:
        score += 1
        warnings.append(f"yield is {reward_share*100:.0f}% incentives (may not persist)")
    if str(deploy.il_risk).lower() == "yes":
        score += 1
        warnings.append("deploy pool has impermanent-loss exposure")
    if deploy.pred_class == "Down" and deploy.pred_prob > 60:
        score += 1
        warnings.append(f"DefiLlama predicts deploy APY down ({deploy.pred_prob:.0f}%)")
    if deploy.apy_spiked:
        score += 1
        warnings.append(f"deploy APY spiking ({deploy.apy_spot:.0f}% spot vs "
                        f"{deploy.apy_mean30d:.1f}% 30d avg) — using the 30d avg")

    rating = "LOW" if score == 0 else "MEDIUM" if score <= 2 else "HIGH"
    return {
        "health_factor": round(health_factor, 2),
        "liq_buffer_pct": round(liq_buffer_pct, 1),
        "reward_share": round(reward_share, 2),
        "directional": directional,
        "rating": rating,
        "warnings": warnings,
    }


def assess_costs(
    collateral: LendMarket,
    borrow: LendMarket,
    deploy: DeployPool,
    borrowed_mult: float,
    loops: int,
    cfg: ScanConfig,
    pos_usd: float,
) -> dict:
    """Phase 3: gas (fixed) + slippage (proportional), annualized over the hold period.
    Recursive loops scale gas (more transactions) and the deployed notional."""
    gas_per_tx = GAS_USD_PER_TX.get(collateral.chain, DEFAULT_GAS_USD_PER_TX)
    gas_usd = cfg.tx_count * gas_per_tx * max(1, loops)

    # Slippage applies to the total borrowed/deployed notional (amplified by looping),
    # on the way in and out. Skip when borrow and deploy symbols are identical (no swap).
    deployed_notional = pos_usd * borrowed_mult
    swaps = 0 if borrow.symbol.upper() == deploy.symbol.upper() else 2
    slippage_usd = deployed_notional * (cfg.slippage_bps / 10_000.0) * swaps

    cost_usd = gas_usd + slippage_usd
    # Annualize: a fixed cost paid once is a smaller drag the longer you hold.
    years = max(cfg.hold_days, 1) / 365.0
    cost_drag_apy = (cost_usd / pos_usd) / years * 100.0 if pos_usd > 0 else 0.0
    return {
        "cost_usd": round(cost_usd, 2),
        "cost_drag_apy": round(cost_drag_apy, 2),
        "swaps": swaps,
    }


def find_opportunities(
    markets: list[LendMarket],
    pools: list[DeployPool],
    cfg: ScanConfig,
) -> tuple[list[Opportunity], float | None]:
    """Return (opportunities, best_net_apy) where best_net_apy is the highest net APY
    among candidates passing all filters EXCEPT the min_net_apy floor — used to explain
    empty results (None when no candidate passes the other filters at all)."""
    # Best deploy pool per (chain, asset_class), and per asset_class across all chains.
    best_deploy: dict[tuple[str, str], DeployPool] = {}
    best_deploy_any_chain: dict[str, DeployPool] = {}
    for p in pools:
        cls = asset_class(p.symbol)
        key = (p.chain, cls)
        if key not in best_deploy or p.apy > best_deploy[key].apy:
            best_deploy[key] = p
        if cls not in best_deploy_any_chain or p.apy > best_deploy_any_chain[cls].apy:
            best_deploy_any_chain[cls] = p

    want_class = CLASS_FILTER.get(cfg.collateral_class)
    best_net: float | None = None
    opps: list[Opportunity] = []
    for collateral in markets:
        if want_class and asset_class(collateral.symbol) != want_class:
            continue
        for borrow in markets:
            if borrow is collateral:
                continue
            # A real loop requires the supply and borrow legs in the SAME lending
            # protocol on the SAME chain: you can only borrow against collateral the
            # protocol itself holds. (The deploy leg below may be any protocol.)
            if borrow.project != collateral.project or borrow.chain != collateral.chain:
                continue
            bclass = asset_class(borrow.symbol)
            # Deploy leg: same chain by default; if cross-chain is allowed, take the
            # best pool for the asset on any chain (you'd bridge the borrowed asset).
            if cfg.same_chain:
                deploy = best_deploy.get((borrow.chain, bclass))
            else:
                deploy = best_deploy_any_chain.get(bclass)
            if deploy is None:
                continue

            target_ltv = collateral.ltv * cfg.ltv_safety
            # Recursive looping amplifies the spread, but only when the deployed asset is
            # the same class as the collateral (so it can be re-supplied as collateral).
            loopable = asset_class(collateral.symbol) == asset_class(deploy.symbol)
            loops = cfg.max_loops if loopable else 1
            b_mult = borrowed_multiple(target_ltv, loops)
            # Net APY on $1 of equity:
            #   supply yield on collateral + (borrowed/deployed multiple) * (deploy - borrow)
            net = collateral.supply_apy + b_mult * (deploy.apy - borrow.borrow_apy)
            leverage = round(1.0 + b_mult, 2)
            risk = assess_risk(collateral, borrow, deploy, target_ltv, cfg.liq_premium)
            if loops > 1:
                risk = dict(risk)
                risk["warnings"] = risk["warnings"] + [
                    f"leveraged {leverage:.1f}x ({loops} loops): same liquidation buffer, "
                    f"but a liquidation wipes the whole stack and any rate/peg move is amplified"]
            if RATING_ORDER[risk["rating"]] > RATING_ORDER[cfg.max_rating]:
                continue
            # Asset pairing filter: correlated = collateral & debt share an asset class
            # (move together, low price risk); uncorrelated = directional (price risk).
            if cfg.pairing == "correlated" and risk["directional"]:
                continue
            if cfg.pairing == "uncorrelated" and not risk["directional"]:
                continue
            # Momentum filter on the deploy pool's recent APY trend
            momentum = momentum_label(deploy.apy_pct7d)
            if cfg.momentum == "rising" and momentum != "up":
                continue
            if cfg.momentum == "not_falling" and momentum == "down":
                continue
            # Borrow-incentive: net borrow cost below zero means you're paid to borrow
            borrow_incentivized = borrow.borrow_apy < 0
            if cfg.borrow_incentive_only and not borrow_incentivized:
                continue
            pos_usd = position_usd_for(cfg, collateral.symbol)
            costs = assess_costs(collateral, borrow, deploy, b_mult, loops, cfg, pos_usd)
            net_after = net - costs["cost_drag_apy"]
            # Direct-deposit benchmark: if the collateral is the same asset class as the
            # deploy pool, you could skip the loop entirely and just deposit it there.
            # Such a loop only adds value if it beats that direct yield.
            same_class = asset_class(collateral.symbol) == asset_class(deploy.symbol)
            direct_apy = round(deploy.apy, 2) if same_class else None
            beats_direct = direct_apy is None or net_after > direct_apy
            if cfg.hide_inferior and not beats_direct:
                continue
            if best_net is None or net_after > best_net:
                best_net = net_after
            # Filter on the cost-adjusted return — that's the number that matters.
            if net_after < cfg.min_net_apy:
                continue
            opps.append(Opportunity(
                collateral=collateral,
                deploy=deploy,
                borrow_market=borrow,
                target_ltv=target_ltv,
                net_apy=net,
                raw_spread=deploy.apy - borrow.borrow_apy,
                health_factor=risk["health_factor"],
                liq_buffer_pct=risk["liq_buffer_pct"],
                reward_share=risk["reward_share"],
                directional=risk["directional"],
                rating=risk["rating"],
                warnings=risk["warnings"],
                cost_drag_apy=costs["cost_drag_apy"],
                net_apy_after_costs=round(net_after, 2),
                cost_usd=costs["cost_usd"],
                direct_deposit_apy=direct_apy,
                beats_direct=beats_direct,
                position_usd=round(pos_usd, 2),
                loops=loops,
                leverage=leverage,
                borrow_incentivized=borrow_incentivized,
                deploy_momentum=momentum,
            ))

    opps.sort(key=lambda o: o.net_apy_after_costs, reverse=True)
    return opps, best_net


def pool_url(pool_id: str) -> str:
    """Link to the DefiLlama page for a specific pool/market."""
    return f"https://defillama.com/yields/pool/{pool_id}" if pool_id else ""


def fetch_market_maps() -> tuple[dict, dict]:
    """Fetch fresh DefiLlama data as lookups keyed by pool id (no filtering)."""
    pools_by_id = {p.get("pool"): p for p in fetch(POOLS_URL)}
    lb_by_id = {r.get("pool"): r for r in fetch(LENDBORROW_URL)}
    return pools_by_id, lb_by_id


def recompute_strategy(legs: dict, params: dict,
                       pools_by_id: dict, lb_by_id: dict) -> dict | None:
    """Recompute the CURRENT net APY for a specific stored strategy by its pool ids,
    regardless of scan filters. Returns None if any leg's pool no longer exists.

    `legs` has collateral/borrow/deploy dicts each with a `pool_id`.
    `params` carries ltv_safety / position_size / hold_days / slippage_bps / etc.
    """
    rd = params.get("reward_discount", 0.5)
    coll_id = legs["collateral"]["pool_id"]
    bor_id = legs["borrow"]["pool_id"]
    dep_id = legs["deploy"]["pool_id"]

    cmeta, clb = pools_by_id.get(coll_id), lb_by_id.get(coll_id)
    bmeta, blb = pools_by_id.get(bor_id), lb_by_id.get(bor_id)
    dmeta = pools_by_id.get(dep_id)
    if not (cmeta and clb and bmeta and blb and dmeta):
        return None  # a leg vanished from DefiLlama (delisted / renamed)

    coll_supply, _, _, _ = guarded_apy(cmeta, rd)
    collateral = LendMarket(
        project=cmeta.get("project", "?"), chain=cmeta.get("chain", "?"),
        symbol=cmeta.get("symbol", "?"),
        supply_apy=coll_supply,
        borrow_apy=0.0, ltv=float(clb.get("ltv") or 0.0),
        tvl_usd=float(clb.get("totalSupplyUsd") or 0.0), pool_id=coll_id)
    borrow = LendMarket(
        project=bmeta.get("project", "?"), chain=bmeta.get("chain", "?"),
        symbol=bmeta.get("symbol", "?"), supply_apy=0.0,
        borrow_apy=(blb.get("apyBaseBorrow") or 0.0) - (blb.get("apyRewardBorrow") or 0.0) * (1.0 - rd),
        ltv=float(blb.get("ltv") or 0.0),
        tvl_usd=float(blb.get("totalSupplyUsd") or 0.0), pool_id=bor_id)
    pred = dmeta.get("predictions") or {}
    dep_apy, dep_spot, dep_mean30, dep_spiked = guarded_apy(dmeta, rd)
    deploy = DeployPool(
        project=dmeta.get("project", "?"), chain=dmeta.get("chain", "?"),
        symbol=dmeta.get("symbol", "?"),
        apy=dep_apy,
        apy_base=dmeta.get("apyBase") or 0.0, apy_reward=dmeta.get("apyReward") or 0.0,
        tvl_usd=float(dmeta.get("tvlUsd") or 0.0), il_risk=dmeta.get("ilRisk", "no"),
        pred_class=pred.get("predictedClass") or "?",
        pred_prob=float(pred.get("predictedProbability") or 0.0), pool_id=dep_id,
        apy_spot=dep_spot, apy_mean30d=dep_mean30, apy_spiked=dep_spiked,
        apy_pct7d=dmeta.get("apyPct7D") or 0.0, apy_pct30d=dmeta.get("apyPct30D") or 0.0)

    cfg = ScanConfig(
        ltv_safety=params.get("ltv_safety", 0.8),
        reward_discount=rd,
        position_size=params.get("position_size", 10_000.0),
        position_size_usd=params.get("position_size_usd", 0.0),
        hold_days=params.get("hold_days", 30.0),
        slippage_bps=params.get("slippage_bps", 5.0),
        tx_count=params.get("tx_count", 8),
        liq_premium=params.get("liq_premium", 0.05),
        max_loops=params.get("max_loops", 1),
    )
    target_ltv = collateral.ltv * cfg.ltv_safety
    same_class = asset_class(collateral.symbol) == asset_class(deploy.symbol)
    loops = cfg.max_loops if same_class else 1
    b_mult = borrowed_multiple(target_ltv, loops)
    net = collateral.supply_apy + b_mult * (deploy.apy - borrow.borrow_apy)
    leverage = round(1.0 + b_mult, 2)
    # Per-loop USD pinned at add time (params.position_size_usd); fallback to size.
    pos_usd = cfg.position_size_usd or cfg.position_size
    costs = assess_costs(collateral, borrow, deploy, b_mult, loops, cfg, pos_usd)
    risk = assess_risk(collateral, borrow, deploy, target_ltv, cfg.liq_premium)
    net_after = net - costs["cost_drag_apy"]
    direct_apy = round(deploy.apy, 2) if same_class else None
    opp = Opportunity(
        collateral=collateral, deploy=deploy, borrow_market=borrow, target_ltv=target_ltv,
        net_apy=net, raw_spread=deploy.apy - borrow.borrow_apy,
        health_factor=risk["health_factor"], liq_buffer_pct=risk["liq_buffer_pct"],
        reward_share=risk["reward_share"], directional=risk["directional"],
        rating=risk["rating"], warnings=risk["warnings"],
        cost_drag_apy=costs["cost_drag_apy"], net_apy_after_costs=round(net_after, 2),
        cost_usd=costs["cost_usd"], direct_deposit_apy=direct_apy,
        beats_direct=(direct_apy is None or net_after > direct_apy),
        position_usd=round(pos_usd, 2), loops=loops, leverage=leverage,
        borrow_incentivized=(borrow.borrow_apy < 0),
        deploy_momentum=momentum_label(deploy.apy_pct7d),
    )
    # Full opportunity-shaped dict (incl. legs/risk/TVL); check_all reads net_apy_after_costs.
    return opp_to_dict(opp)


def opp_to_dict(o: Opportunity) -> dict:
    return {
        "net_apy": round(o.net_apy, 2),
        "net_apy_after_costs": o.net_apy_after_costs,
        "cost_drag_apy": o.cost_drag_apy,
        "cost_usd": o.cost_usd,
        "direct_deposit_apy": o.direct_deposit_apy,
        "beats_direct": o.beats_direct,
        "position_usd": o.position_usd,
        "loops": o.loops,
        "leverage": o.leverage,
        "borrow_incentivized": o.borrow_incentivized,
        "deploy_momentum": o.deploy_momentum,
        "raw_spread": round(o.raw_spread, 2),
        "target_ltv": round(o.target_ltv, 3),
        "risk": {
            "rating": o.rating,
            "health_factor": o.health_factor,
            "liq_buffer_pct": o.liq_buffer_pct,
            "reward_share": o.reward_share,
            "directional": o.directional,
            "warnings": o.warnings,
        },
        "collateral": {"project": o.collateral.project, "chain": o.collateral.chain,
                       "symbol": o.collateral.symbol, "supply_apy": round(o.collateral.supply_apy, 2),
                       "max_ltv": o.collateral.ltv, "tvl_usd": round(o.collateral.tvl_usd),
                       "pool_id": o.collateral.pool_id, "url": pool_url(o.collateral.pool_id)},
        "borrow": {"project": o.borrow_market.project, "chain": o.borrow_market.chain,
                   "symbol": o.borrow_market.symbol, "borrow_apy": round(o.borrow_market.borrow_apy, 2),
                   "tvl_usd": round(o.borrow_market.tvl_usd),
                   "pool_id": o.borrow_market.pool_id, "url": pool_url(o.borrow_market.pool_id)},
        "deploy": {"project": o.deploy.project, "chain": o.deploy.chain,
                   "symbol": o.deploy.symbol, "apy": round(o.deploy.apy, 2),
                   "apy_base": round(o.deploy.apy_base, 2), "apy_reward": round(o.deploy.apy_reward, 2),
                   "tvl_usd": round(o.deploy.tvl_usd),
                   "pool_id": o.deploy.pool_id, "url": pool_url(o.deploy.pool_id)},
    }


def scan(cfg: ScanConfig) -> dict:
    """Run a full scan and return a JSON-serializable snapshot. Shared by CLI + dashboard."""
    prices = fetch_prices()
    cfg.prices = prices
    cfg.position_size_usd = resolve_position_usd(cfg.position_size, cfg.position_unit, prices)
    pool_rows = fetch(POOLS_URL)
    markets = build_lend_markets(pool_rows, cfg.reward_discount, cfg.min_tvl, cfg.max_apy)
    pools = build_deploy_pools(pool_rows, cfg.reward_discount, cfg.min_tvl, cfg.max_apy)
    opps, best_net = find_opportunities(markets, pools, cfg)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_at": ts,
        "params": cfg.__dict__.copy(),
        "prices": prices,
        "stats": {"markets": len(markets), "pools": len(pools), "matches": len(opps),
                  "best_net_apy": round(best_net, 2) if best_net is not None else None},
        "count": len(opps),
        "opportunities": [opp_to_dict(o) for o in opps[:cfg.limit]],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="DeFi yield-carry loop scanner (read-only).")
    ap.add_argument("--min-net-apy", type=float, default=3.0,
                    help="Minimum net APy %% to report (default: 3.0)")
    ap.add_argument("--min-tvl", type=float, default=5_000_000,
                    help="Minimum TVL in USD for both markets and pools (default: 5M)")
    ap.add_argument("--ltv-safety", type=float, default=0.8,
                    help="Fraction of max LTV to actually use, 0..1 (default: 0.8)")
    ap.add_argument("--reward-discount", type=float, default=0.5,
                    help="Discount applied to reward/incentive APY, 0..1 (default: 0.5)")
    ap.add_argument("--max-apy", type=float, default=100.0,
                    help="Reject markets/pools whose APY exceeds this %% as outliers (default: 100)")
    ap.add_argument("--same-chain", action="store_true", default=True,
                    help="Require borrow and deploy on the same chain (default: on)")
    ap.add_argument("--cross-chain", dest="same_chain", action="store_false",
                    help="Allow cross-chain (ignores bridging cost!)")
    ap.add_argument("--liq-premium", type=float, default=0.05,
                    help="Assumed gap between liquidation threshold and max LTV (default: 0.05)")
    ap.add_argument("--max-rating", choices=["LOW", "MEDIUM", "HIGH"], default="HIGH",
                    help="Only show opportunities at or below this risk rating (default: HIGH)")
    ap.add_argument("--pairing", choices=["all", "correlated", "uncorrelated"], default="all",
                    help="Filter by collateral/debt correlation: correlated = same asset class "
                         "(low price risk), uncorrelated = directional (default: all)")
    ap.add_argument("--collateral-class", choices=["all", "stable", "eth", "btc", "sol"], default="all",
                    help="Restrict collateral to a class, e.g. 'eth' to post ETH-like assets (default: all)")
    ap.add_argument("--show-inferior", dest="hide_inferior", action="store_false", default=True,
                    help="Include loops that a plain direct deposit would beat (hidden by default)")
    ap.add_argument("--max-loops", type=int, default=1,
                    help="Recursive loop count for loopable loops (same-class collateral & deploy); "
                         "1 = single step, higher = leveraged (default: 1)")
    ap.add_argument("--momentum", choices=["any", "rising", "not_falling"], default="any",
                    help="Filter by deploy-pool APY trend: rising / not_falling (default: any)")
    ap.add_argument("--borrow-incentive-only", action="store_true", default=False,
                    help="Only loops where the borrow is incentivized (net-negative borrow cost)")
    ap.add_argument("--position-unit", choices=["USD", "ETH", "BTC", "native"], default="USD",
                    help="Unit for --position-size; 'native' = units of each loop's own collateral (default: USD)")
    ap.add_argument("--position-size", type=float, default=10_000.0,
                    help="Position equity in USD, for cost modeling (default: 10000)")
    ap.add_argument("--hold-days", type=float, default=30.0,
                    help="Holding period in days to amortize gas over (default: 30)")
    ap.add_argument("--tx-count", type=int, default=8,
                    help="Entry+exit transaction count for gas estimate (default: 8)")
    ap.add_argument("--slippage-bps", type=float, default=5.0,
                    help="Per-swap slippage in basis points (default: 5)")
    ap.add_argument("--limit", type=int, default=25, help="Rows to print (default: 25)")
    ap.add_argument("--json", action="store_true", help="Emit results as JSON to stdout")
    ap.add_argument("--save", metavar="DIR", help="Save a timestamped JSON snapshot to DIR")
    args = ap.parse_args()

    cfg = ScanConfig(
        min_net_apy=args.min_net_apy, min_tvl=args.min_tvl, ltv_safety=args.ltv_safety,
        reward_discount=args.reward_discount, max_apy=args.max_apy, same_chain=args.same_chain,
        liq_premium=args.liq_premium, max_rating=args.max_rating, limit=args.limit,
        pairing=args.pairing, collateral_class=args.collateral_class,
        hide_inferior=args.hide_inferior, max_loops=args.max_loops,
        momentum=args.momentum, borrow_incentive_only=args.borrow_incentive_only,
        position_size=args.position_size, position_unit=args.position_unit,
        hold_days=args.hold_days, tx_count=args.tx_count, slippage_bps=args.slippage_bps,
    )

    try:
        print("Fetching DefiLlama data...", file=sys.stderr)
        snapshot = scan(cfg)
    except requests.RequestException as e:
        print(f"Network error fetching DefiLlama: {e}", file=sys.stderr)
        return 1

    s = snapshot["stats"]
    print(f"Loaded {s['markets']} lending markets, {s['pools']} deploy pools, "
          f"{s['matches']} matches.", file=sys.stderr)

    if args.save:
        import os
        os.makedirs(args.save, exist_ok=True)
        fname = os.path.join(args.save, f"scan-{snapshot['generated_at'].replace(':', '-')}.json")
        with open(fname, "w") as f:
            json.dump(snapshot, f, indent=2)
        print(f"Saved snapshot -> {fname}", file=sys.stderr)

    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        # Rebuild lightweight Opportunity-like view from the snapshot for printing.
        print_snapshot(snapshot, args.limit)
    return 0


def print_snapshot(snapshot: dict, limit: int) -> None:
    """Render a snapshot dict (from scan()) as the text table."""
    opps = snapshot["opportunities"]
    if not opps:
        best = snapshot.get("stats", {}).get("best_net_apy")
        if best is not None:
            print(f"No loops at >= {snapshot['params']['min_net_apy']}% net APY with these filters. "
                  f"Best available is {best:.2f}% — lower --min-net-apy to {int(best)} or below.")
        else:
            print("No opportunities passed the filters. Try lowering --min-tvl, "
                  "raising --max-rating, or widening --collateral-class.")
        return
    header = (
        f"{'#':>2}  {'NET(cost)':>9}  {'GROSS':>7}  {'DRAG':>5}  {'RISK':<6}  {'HF':>4}  {'BUF':>4}  "
        f"{'COLLATERAL (supply)':<28}  {'BORROW (cost)':<26}  {'DEPLOY (yield)':<28}"
    )
    print(header)
    print("-" * len(header))
    for i, o in enumerate(opps[:limit], 1):
        c, b, d, r = o["collateral"], o["borrow"], o["deploy"], o["risk"]
        col = f"{c['project']}/{c['chain']} {c['symbol']} ({c['supply_apy']:.1f}%)"
        bor = f"{b['project']}/{b['chain']} {b['symbol']} ({b['borrow_apy']:.1f}%)"
        dep = f"{d['project']}/{d['chain']} {d['symbol']} ({d['apy']:.1f}%)"
        print(f"{i:>2}  {o['net_apy_after_costs']:>8.2f}%  {o['net_apy']:>6.1f}%  "
              f"{o['cost_drag_apy']:>4.1f}%  {r['rating']:<6}  {r['health_factor']:>4.2f}  "
              f"{r['liq_buffer_pct']:>3.0f}%  {col:<28.28}  {bor:<26.26}  {dep:<28.28}")
    print()
    print("NET(cost) = gross net APY minus gas+slippage drag, annualized over hold period.")
    print("GROSS = supply + ltv*(deploy - borrow).  DRAG = cost as APY.  HF/BUF = liquidation safety.")
    print("Top warnings:")
    for i, o in enumerate(opps[:min(limit, 5)], 1):
        if o["risk"]["warnings"]:
            print(f"  [{i}] {o['risk']['rating']}: " + "; ".join(o["risk"]["warnings"]))
    print("\nReminder: cost model is an estimate; verify gas, slippage, and liquidation thresholds on-chain.")


if __name__ == "__main__":
    raise SystemExit(main())
