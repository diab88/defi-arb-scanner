#!/usr/bin/env python3
"""
Local web dashboard for the DeFi yield-carry scanner.

Serves a single-page UI (stdlib HTTP server, only `requests` as a dep) with two views:

  * Scanner   - run scans, see ranked loop opportunities, click a row for a how-to guide.
  * Portfolio - strategies you've "applied". A background thread re-checks each one every
                hour; if its net APY drops more than your threshold (default 3pp) below the
                APY at entry, you get an in-app notification (and a Telegram message if a bot
                is configured via env vars).

Usage:
    python dashboard.py                 # http://127.0.0.1:8765
    python dashboard.py --port 9000

Env vars (optional, for Telegram alerts):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Env var (optional): DEFI_MONITOR_INTERVAL  (seconds between portfolio checks, default 3600)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import scanner  # reuse scan() + ScanConfig + recompute_strategy

DATA_DIR = os.environ.get("DEFI_DATA_DIR", "./data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
NOTIF_FILE = os.path.join(DATA_DIR, "notifications.json")
MONITOR_INTERVAL = int(os.environ.get("DEFI_MONITOR_INTERVAL", "3600"))

LOCK = threading.Lock()
PORTFOLIO: list[dict] = []
NOTIFICATIONS: list[dict] = []


# ----------------------------- persistence -----------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def load_state() -> None:
    global PORTFOLIO, NOTIFICATIONS
    os.makedirs(DATA_DIR, exist_ok=True)
    PORTFOLIO = _load(PORTFOLIO_FILE, [])
    NOTIFICATIONS = _load(NOTIF_FILE, [])
    # Backfill fields added in later versions so older saved items stay consistent.
    changed = False
    for it in PORTFOLIO:
        for k, v in {"min_apy_floor": 10.0, "alert_drop": bool(it.get("alert")),
                     "alert_floor": False}.items():
            if k not in it:
                it[k] = v
                changed = True
    if changed:
        save_portfolio()


def _save(path, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_portfolio() -> None:
    _save(PORTFOLIO_FILE, PORTFOLIO)


def save_notifications() -> None:
    _save(NOTIF_FILE, NOTIFICATIONS[:200])


# ----------------------------- telegram -----------------------------

def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def telegram_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=15)
    except requests.RequestException as e:
        print(f"  telegram send failed: {e}", file=sys.stderr)


# ----------------------------- portfolio logic -----------------------------

def make_label(legs: dict) -> str:
    c, b, d = legs["collateral"], legs["borrow"], legs["deploy"]
    return f"{c['project']} {c['symbol']} → borrow {b['symbol']} → {d['project']} {d['symbol']}"


def add_item(opp: dict, params: dict, threshold: float, floor: float = 10.0) -> dict:
    legs = {"collateral": opp["collateral"], "borrow": opp["borrow"], "deploy": opp["deploy"]}
    # Pin the USD value used for THIS loop (matters in 'native' mode where it's per-row),
    # so the hourly re-pricer uses the same basis as when you added it.
    params = dict(params or {})
    if opp.get("position_usd"):
        params["position_size_usd"] = opp["position_usd"]
    key = "|".join(legs[k]["pool_id"] for k in ("collateral", "borrow", "deploy"))
    with LOCK:
        for it in PORTFOLIO:
            if it["key"] == key:
                return it  # already tracked
        item = {
            "id": uuid.uuid4().hex[:8],
            "added_at": now_iso(),
            "key": key,
            "label": make_label(legs),
            "chain": legs["collateral"]["chain"],
            "legs": legs,
            "opportunity": opp,          # full entry snapshot, for the detail view
            "params": params,
            "baseline_net_apy": opp["net_apy_after_costs"],
            "current_net_apy": opp["net_apy_after_costs"],
            "drop_pp": 0.0,
            "last_checked": now_iso(),
            "alert": False,
            "alert_drop": False,        # pp-drop from entry breached
            "alert_floor": False,       # current APY fell below the floor
            "alert_threshold": float(threshold),   # pp drop that triggers alert_drop
            "min_apy_floor": float(floor),         # APY below this triggers alert_floor
        }
        PORTFOLIO.append(item)
        save_portfolio()
        return item


def remove_item(item_id: str) -> None:
    with LOCK:
        PORTFOLIO[:] = [it for it in PORTFOLIO if it["id"] != item_id]
        save_portfolio()


def update_item(item_id: str, threshold=None, floor=None) -> None:
    with LOCK:
        for it in PORTFOLIO:
            if it["id"] != item_id:
                continue
            if threshold is not None:
                it["alert_threshold"] = float(threshold)
            if floor is not None:
                it["min_apy_floor"] = float(floor)
            # Re-evaluate both alert conditions against current data immediately.
            if it.get("drop_pp") is not None:
                it["alert_drop"] = it["drop_pp"] >= it["alert_threshold"]
            cur = it.get("current_net_apy")
            it["alert_floor"] = cur is not None and cur < it.get("min_apy_floor", 10.0)
            it["alert"] = bool(it.get("alert_drop") or it.get("alert_floor"))
        save_portfolio()


def push_notification(item: dict, message: str) -> None:
    NOTIFICATIONS.insert(0, {
        "id": uuid.uuid4().hex[:8], "time": now_iso(),
        "item_id": item["id"], "label": item["label"],
        "message": message, "read": False,
    })


def check_all() -> int:
    """Re-price every portfolio strategy. Returns the number of NEW alerts raised."""
    with LOCK:
        if not PORTFOLIO:
            return 0
    try:
        pools_by_id, lb_by_id = scanner.fetch_market_maps()
    except requests.RequestException as e:
        print(f"  portfolio check: fetch failed: {e}", file=sys.stderr)
        return 0

    fired: list[tuple[dict, str]] = []
    with LOCK:
        for it in PORTFOLIO:
            cur = scanner.recompute_strategy(it["legs"], it["params"], pools_by_id, lb_by_id)
            it["last_checked"] = now_iso()
            if cur is None:
                it["current_net_apy"] = None
                it["drop_pp"] = None
                was_gone = it.get("alert_gone", False)
                it["alert_gone"] = True
                it["alert"] = True
                if not was_gone:
                    msg = "Strategy no longer found on DefiLlama (pool delisted/renamed) — review and exit."
                    push_notification(it, msg)
                    fired.append((it, msg))
                continue
            it["alert_gone"] = False
            net = cur["net_apy_after_costs"]
            it["current_net_apy"] = net
            it["current"] = cur
            drop = round(it["baseline_net_apy"] - net, 2)
            it["drop_pp"] = drop

            # Layer 1 — relative drop from entry (in percentage points)
            was_drop = it.get("alert_drop", False)
            now_drop = drop >= it["alert_threshold"]
            it["alert_drop"] = now_drop
            if now_drop and not was_drop:
                msg = (f"Net APY dropped {drop:.2f}pp: entry {it['baseline_net_apy']:.2f}% "
                       f"→ now {net:.2f}% (threshold {it['alert_threshold']:.1f}pp). Consider exiting and repaying.")
                push_notification(it, msg)
                fired.append((it, msg))

            # Layer 2 — absolute floor: APY fell below the "get out" level
            floor = it.get("min_apy_floor", 10.0)
            was_floor = it.get("alert_floor", False)
            now_floor = net < floor
            it["alert_floor"] = now_floor
            if now_floor and not was_floor:
                msg = (f"Net APY {net:.2f}% is below your {floor:.1f}% exit floor — "
                       f"this pool may no longer be worth the risk. Consider exiting.")
                push_notification(it, msg)
                fired.append((it, msg))

            it["alert"] = now_drop or now_floor
        save_portfolio()
        if fired:
            save_notifications()

    # network I/O outside the lock
    for it, msg in fired:
        telegram_send(f"⚠️ <b>DeFi loop alert</b>\n{it['label']}\n{msg}")
    if fired:
        print(f"[{now_iso()}] portfolio check: {len(fired)} new alert(s)", file=sys.stderr)
    return len(fired)


def monitor_loop() -> None:
    # small initial delay so the server is up, then check on the interval
    time.sleep(10)
    while True:
        try:
            check_all()
        except Exception as e:  # noqa: BLE001 - keep the thread alive
            print(f"  monitor loop error: {e}", file=sys.stderr)
        time.sleep(MONITOR_INTERVAL)


# ----------------------------- HTML page -----------------------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>DeFi Loop Scanner</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0e1116; color: #e6edf3; }
  header { padding: 14px 24px; border-bottom: 1px solid #232a33; display: flex; align-items: center; gap: 20px; }
  h1 { font-size: 17px; margin: 0; }
  nav { display: flex; gap: 6px; margin-left: 8px; }
  nav button { background: #161b22; color: #8b949e; border: 1px solid #30363d; border-radius: 6px;
               padding: 6px 14px; font-size: 13px; cursor: pointer; }
  nav button.active { background: #1f6feb22; color: #58a6ff; border-color: #1f6feb; }
  .bell { margin-left: auto; position: relative; cursor: pointer; font-size: 20px; user-select: none; }
  .badge { position: absolute; top: -6px; right: -8px; background: #f85149; color: #fff; font-size: 10px;
           font-weight: 700; border-radius: 9px; padding: 1px 5px; min-width: 14px; text-align: center; }
  .sub { color: #8b949e; font-size: 12px; }
  .controls { display: flex; flex-wrap: wrap; gap: 12px; padding: 16px 24px; border-bottom: 1px solid #232a33; align-items: end; }
  .ctl { display: flex; flex-direction: column; font-size: 11px; color: #8b949e; }
  .ctl input, .ctl select { margin-top: 4px; background: #161b22; color: #e6edf3; border: 1px solid #30363d;
              border-radius: 6px; padding: 6px 8px; font-size: 13px; }
  .ctl input { width: 90px; }
  button.go { background: #238636; color: #fff; border: 0; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }
  button.go:disabled { opacity: .5; cursor: wait; }
  .secbtn { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  .addbtn { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb55; border-radius: 6px; padding: 4px 9px; font-size: 12px; cursor: pointer; white-space: nowrap; }
  #stats, #pf-info { padding: 8px 24px; color: #8b949e; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1c2128; white-space: nowrap; }
  th { color: #8b949e; font-weight: 600; font-size: 11px; text-transform: uppercase; position: sticky; top: 0; background: #0e1116; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .net { font-weight: 700; color: #3fb950; }
  .down { color: #f85149; }
  .pill { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .LOW { background: #1b3a26; color: #3fb950; } .MEDIUM { background: #3a3320; color: #d29922; } .HIGH { background: #3a1f1f; color: #f85149; }
  .ok { background: #1b3a26; color: #3fb950; } .alert { background: #3a1f1f; color: #f85149; }
  .warn { color: #d29922; font-size: 11px; white-space: normal; max-width: 320px; }
  .muted { color: #8b949e; }
  td a { color: #58a6ff; text-decoration: none; } td a:hover { text-decoration: underline; }
  tbody tr.clickable { cursor: pointer; } tbody tr.clickable:hover { background: #161b22; }
  .tip { cursor: help; border-bottom: 1px dotted #5a636e; position: relative; }
  .tip:hover::after { content: attr(data-tip); position: absolute; left: 0; top: 150%; white-space: normal; width: 230px;
       background: #1c2128; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 8px 10px; font-size: 11px;
       font-weight: 400; text-transform: none; line-height: 1.45; z-index: 100; box-shadow: 0 6px 16px rgba(0,0,0,.5); }
  .hidden { display: none; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: none; align-items: flex-start; justify-content: center; overflow-y: auto; z-index: 50; }
  .overlay.open { display: flex; }
  .modal { background: #11161d; border: 1px solid #30363d; border-radius: 12px; max-width: 720px; width: calc(100% - 48px); margin: 48px 0; padding: 24px 28px; }
  .modal h2 { margin: 0 0 4px; font-size: 17px; } .modal .tag { color: #8b949e; font-size: 12px; margin-bottom: 16px; }
  .modal .big { font-size: 28px; font-weight: 800; color: #3fb950; }
  .modal ol { padding-left: 20px; line-height: 1.6; } .modal li { margin-bottom: 10px; }
  .modal .step-num { color: #58a6ff; font-weight: 700; }
  .modal .box { background: #0e1116; border: 1px solid #1c2128; border-radius: 8px; padding: 12px 14px; margin: 12px 0; font-size: 13px; }
  .modal .risk-box { border-color: #5a3a1a; } .modal h3 { font-size: 13px; text-transform: uppercase; color: #8b949e; margin: 18px 0 6px; }
  .modal .close { float: right; background: #21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding: 5px 12px; font-size: 13px; cursor:pointer; }
  .modal code { background: #0e1116; padding: 1px 5px; border-radius: 4px; }
  .modal .disclaimer { color: #8b949e; font-size: 11px; margin-top: 18px; border-top: 1px solid #1c2128; padding-top: 12px; }
  #notif-panel { position: fixed; right: 16px; top: 56px; width: 360px; max-height: 70vh; overflow-y: auto; background: #11161d;
                 border: 1px solid #30363d; border-radius: 10px; padding: 12px 14px; z-index: 60; box-shadow: 0 8px 24px rgba(0,0,0,.5); }
  .notif { border-bottom: 1px solid #1c2128; padding: 8px 0; font-size: 12px; }
  .notif.unread { border-left: 3px solid #f85149; padding-left: 8px; }
  .notif .nlabel { color: #e6edf3; font-weight: 600; } .notif .ntime { color: #8b949e; font-size: 10px; }
</style></head>
<body>
<header>
  <h1>DeFi Yield-Carry Loops</h1>
  <nav>
    <button id="tab-scanner" class="active" onclick="showView('scanner')">Scanner</button>
    <button id="tab-portfolio" onclick="showView('portfolio')">Portfolio <span id="pf-count" class="muted"></span></button>
  </nav>
  <div class="bell" onclick="toggleNotif()">🔔<span id="notif-badge" class="badge hidden">0</span></div>
</header>

<div id="notif-panel" class="hidden"></div>

<!-- SCANNER VIEW -->
<div id="view-scanner">
  <div class="controls">
    <label class="ctl">Min net APY %<input id="min_net_apy" type="number" value="3" step="0.5"></label>
    <label class="ctl">Min TVL $<input id="min_tvl" type="number" value="5000000" step="1000000"></label>
    <label class="ctl">LTV safety<input id="ltv_safety" type="number" value="0.8" step="0.05"></label>
    <label class="ctl">Reward discount<input id="reward_discount" type="number" value="0.5" step="0.1"></label>
    <label class="ctl">Max rating<select id="max_rating"><option>HIGH</option><option>MEDIUM</option><option>LOW</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Correlated = collateral & borrowed asset are the same class (stable↔stable, ETH↔ETH) and move together — low price risk. Uncorrelated = different classes (e.g. ETH collateral, stable debt) = directional price risk.">Asset pairing</span>
      <select id="pairing"><option value="all">all</option><option value="correlated">correlated</option><option value="uncorrelated">uncorrelated</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Restrict which asset you post as collateral. Pick 'eth' or 'btc' to screen for 'borrow against my ETH/BTC' loops (these are directional — see the hedge note when you open a row).">Collateral</span>
      <select id="collateral_class"><option value="all">all</option><option value="stable">stable</option><option value="eth">eth</option><option value="btc">btc</option><option value="sol">sol</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Amount you hold, in units of each loop's own collateral. E.g. 5 means 5 ETH on ETH loops, 5 WBTC on BTC loops, 5 SOL on SOL loops, or ~5 units (~$5) on stablecoin loops. The USD value is resolved per loop from live prices.">Position (collateral units)</span>
      <input id="position_size" type="number" value="10000" step="100" style="width:110px;"></label>
    <label class="ctl">Hold days<input id="hold_days" type="number" value="30" step="1"></label>
    <label class="ctl">Slippage bps<input id="slippage_bps" type="number" value="5" step="1"></label>
    <label class="ctl">Same chain<select id="same_chain"><option value="1">yes</option><option value="0">no</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Hide loops that a plain direct deposit of your collateral into the deploy pool would beat (same asset class). These add no value — borrowing just adds risk. Set to 'no' to see them flagged.">Hide inferior</span>
      <select id="hide_inferior"><option value="1">yes</option><option value="0">no</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Recursive looping: re-supply the deployed asset and borrow again, N times, to amplify the spread (leverage). Only applies to loops where collateral & deploy are the same asset class. 1 = single step. Higher = more APY but a liquidation wipes the whole leveraged stack.">Max loops</span>
      <input id="max_loops" type="number" value="1" min="1" max="10" step="1" style="width:60px;"></label>
    <label class="ctl"><span class="tip" data-tip="Filter by the deploy pool's recent APY trend (7-day change). 'rising' = APY went up, 'not falling' = flat or up. Helps avoid pools whose yield is decaying.">Trend</span>
      <select id="momentum"><option value="any">any</option><option value="not_falling">not falling</option><option value="rising">rising</option></select></label>
    <label class="ctl"><span class="tip" data-tip="Only show loops where the borrow is incentivized — net-negative borrow cost, i.e. the protocol pays you to borrow. Rare but high-signal.">Borrow incentive</span>
      <select id="borrow_incentive_only"><option value="0">any</option><option value="1">paid-to-borrow only</option></select></label>
    <label class="ctl">Limit<input id="limit" type="number" value="40" step="5"></label>
    <button id="run" class="go" onclick="runScan()">Run scan</button>
    <label class="ctl" style="flex-direction:row;align-items:center;gap:6px;">
      <input id="auto" type="checkbox" style="width:auto;margin:0;"> auto every
      <input id="interval" type="number" value="60" step="10" style="width:60px;"> s</label>
  </div>
  <div id="stats">Set filters and click <b>Run scan</b>.</div>
  <table><thead><tr>
    <th></th><th>#</th>
    <th><span class="tip" data-tip="Net APY after costs: your bottom-line annual return on equity, after subtracting estimated gas and slippage. Rows are ranked by this.">Net (after cost)</span></th>
    <th><span class="tip" data-tip="Gross net APY before costs = supply APY + leverage × (deploy APY − borrow cost).">Gross</span></th>
    <th><span class="tip" data-tip="Cost drag: gas + slippage expressed as an annual % hit, amortized over your hold period and position size.">Drag</span></th>
    <th><span class="tip" data-tip="Leverage = total exposure ÷ equity. A single borrow is already >1x; recursive looping (Max loops, highlighted purple) pushes it higher on loopable same-class loops. More leverage amplifies APY and the size wiped on liquidation.">Lev</span></th>
    <th><span class="tip" data-tip="Overall risk rating (LOW / MEDIUM / HIGH), combining liquidation buffer, yield sustainability, and price exposure.">Risk</span></th>
    <th><span class="tip" data-tip="Health Factor: collateral × liquidation threshold ÷ debt, at entry. Above 1 = solvent; the higher, the safer.">HF</span></th>
    <th><span class="tip" data-tip="Liquidation buffer: how far the collateral can fall in price relative to the debt before you get liquidated.">Buffer</span></th>
    <th><span class="tip" data-tip="The asset you deposit and the protocol you supply it to — with its supply APY and market TVL.">Collateral (supply)</span></th>
    <th><span class="tip" data-tip="The asset you borrow against your collateral (same protocol) — with its borrow APY and TVL.">Borrow (cost)</span></th>
    <th><span class="tip" data-tip="Where the borrowed asset is put to work — the pool, its yield APY, and TVL (liquidity depth).">Deploy (yield)</span></th>
    <th><span class="tip" data-tip="Risk flags: thin buffer, incentive-heavy yield, directional price risk, APY predictions, etc.">Warnings</span></th>
  </tr></thead><tbody id="rows"></tbody></table>
</div>

<!-- PORTFOLIO VIEW -->
<div id="view-portfolio" class="hidden">
  <div id="pf-info"></div>
  <div style="padding: 0 24px 12px;"><button class="secbtn" onclick="checkNow()" id="checknow">Check now</button></div>
  <table><thead><tr>
    <th>Strategy</th><th>Chain</th>
    <th><span class="tip" data-tip="Net APY at the moment you added this strategy to the portfolio.">Entry APY</span></th>
    <th><span class="tip" data-tip="Latest net APY, recomputed from live data on each hourly check.">Current APY</span></th>
    <th><span class="tip" data-tip="Drop in percentage points from entry APY to current. Positive = APY has fallen.">Δ drop (pp)</span></th>
    <th>Status</th>
    <th><span class="tip" data-tip="Layer 1 — relative alert: fires when the APY drop from entry (in pp) meets or exceeds this value. Edits save automatically.">Alert ≥ (pp)</span></th>
    <th><span class="tip" data-tip="Layer 2 — absolute floor: fires when the current net APY falls below this value, i.e. 'get out of this pool'. Edits save automatically.">Exit if < (APY)</span></th>
    <th>Added</th><th>Last checked</th><th></th>
  </tr></thead><tbody id="pf-rows"></tbody></table>
</div>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeDetail()"><div class="modal" id="modal"></div></div>

<script>
const IDS = ["min_net_apy","min_tvl","ltv_safety","reward_discount","max_rating","pairing","collateral_class","hide_inferior","max_loops","momentum","borrow_incentive_only","position_size","hold_days","slippage_bps","same_chain","limit"];
let timer = null, lastParams = null;

document.getElementById("auto").addEventListener("change", e => {
  if (timer) { clearInterval(timer); timer = null; }
  if (e.target.checked) timer = setInterval(runScan, Math.max(10, Number(document.getElementById("interval").value)) * 1000);
});

function showView(v) {
  document.getElementById("view-scanner").classList.toggle("hidden", v !== "scanner");
  document.getElementById("view-portfolio").classList.toggle("hidden", v !== "portfolio");
  document.getElementById("tab-scanner").classList.toggle("active", v === "scanner");
  document.getElementById("tab-portfolio").classList.toggle("active", v === "portfolio");
  if (v === "portfolio") loadPortfolio();
}

async function runScan() {
  const btn = document.getElementById("run");
  btn.disabled = true; btn.textContent = "Scanning...";
  document.getElementById("stats").textContent = "Fetching DefiLlama data...";
  const q = new URLSearchParams();
  IDS.forEach(id => q.set(id, document.getElementById(id).value));
  try {
    const res = await fetch("/api/scan?" + q.toString());
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    lastParams = data.params;
    render(data);
  } catch (e) { document.getElementById("stats").textContent = "Error: " + e.message; }
  finally { btn.disabled = false; btn.textContent = "Run scan"; }
}

function fmtUsd(n){ return "$" + Number(n).toLocaleString(); }
function localTime(iso){ const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); }
function fmtTvl(n){ if(n>=1e9)return "$"+(n/1e9).toFixed(1)+"B"; if(n>=1e6)return "$"+(n/1e6).toFixed(1)+"M"; if(n>=1e3)return "$"+(n/1e3).toFixed(0)+"K"; return "$"+Math.round(n); }
const link = (t,u) => u ? `<a href="${u}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${t}</a>` : t;
function momArrow(m){ if(m==="up")return '<span title="APY rising (7d)" style="color:#3fb950">↑</span>'; if(m==="down")return '<span title="APY falling (7d)" style="color:#f85149">↓</span>'; return '<span title="APY flat (7d)" class="muted">→</span>'; }

function render(data) {
  const s = data.stats;
  const pu = data.params.position_unit || "USD";
  const posTxt = pu === "USD" ? fmtUsd(data.params.position_size)
    : pu === "native" ? `${data.params.position_size} of each loop's collateral`
    : `${data.params.position_size} ${pu}`;
  document.getElementById("stats").innerHTML =
    `${data.count} matches from ${s.markets} markets / ${s.pools} pools · as of ${data.generated_at} · position ${posTxt}, ${data.params.hold_days}d hold`;
  const tb = document.getElementById("rows"); tb.innerHTML = "";
  if (data.count === 0) {
    const b = data.stats.best_net_apy, floor = data.params.min_net_apy;
    let msg;
    if (b == null) {
      msg = "No loops match these filters at all. Try Collateral = all, raise Max rating, or lower Min TVL.";
    } else if (b < floor) {
      msg = `No loops at ≥ <b>${floor}%</b> net APY with these filters. The best available is ` +
            `<b>${b.toFixed(2)}%</b> — lower <b>Min net APY</b> to ${Math.floor(b)} or below to see it.`;
    } else {
      msg = "No loops match. Try relaxing a filter (Min TVL, Max rating, or Collateral).";
    }
    tb.innerHTML = `<tr><td colspan="13" class="muted" style="padding:18px 24px;font-size:13px;">${msg}</td></tr>`;
    return;
  }
  data.opportunities.forEach((o, i) => {
    const r = o.risk, c = o.collateral, b = o.borrow, d = o.deploy;
    const tr = document.createElement("tr"); tr.className = "clickable";
    tr.onclick = () => openDetail(o, data.params);
    tr.innerHTML =
      `<td><button class="addbtn" onclick="event.stopPropagation();addToPortfolio(${i})">★ Add</button></td>` +
      `<td class="muted">${i+1}</td>` +
      `<td class="num net">${o.net_apy_after_costs.toFixed(2)}%</td>` +
      `<td class="num muted">${o.net_apy.toFixed(1)}%</td>` +
      `<td class="num muted">${o.cost_drag_apy.toFixed(1)}%</td>` +
      `<td class="num">${(o.loops || 1) > 1 ? `<b style="color:#bc8cff">${o.leverage.toFixed(1)}x</b>` : `${(o.leverage || 1).toFixed(1)}x`}</td>` +
      `<td><span class="pill ${r.rating}">${r.rating}</span></td>` +
      `<td class="num">${r.health_factor.toFixed(2)}</td>` +
      `<td class="num">${r.liq_buffer_pct.toFixed(0)}%</td>` +
      `<td>${link(c.project,c.url)}/${c.chain} <b>${c.symbol}</b> <span class="muted">(${c.supply_apy.toFixed(1)}% · TVL ${fmtTvl(c.tvl_usd)})</span></td>` +
      `<td>${link(b.project,b.url)} <b>${b.symbol}</b> <span class="muted">(${b.borrow_apy.toFixed(1)}% · TVL ${fmtTvl(b.tvl_usd)})</span>${o.borrow_incentivized ? ' <span title="Paid to borrow — net-negative borrow cost" style="color:#3fb950">⚡</span>' : ''}</td>` +
      `<td>${link(d.project,d.url)} <b>${d.symbol}</b> <span class="muted">(${d.apy.toFixed(1)}% · TVL ${fmtTvl(d.tvl_usd)})</span> ${momArrow(o.deploy_momentum)}</td>` +
      `<td class="warn">${r.warnings.join("; ")}</td>`;
    tb.appendChild(tr);
  });
  window._opps = data.opportunities;
  window._prices = data.prices || {};
}

const STABLES = new Set(["USDC","USDT","DAI","USDS","FRAX","LUSD","GUSD","USDE","CRVUSD","GHO"]);
function assetClass(sym){ sym=(sym||"").toUpperCase(); if(STABLES.has(sym))return "STABLE"; if(sym.includes("BTC"))return "BTC"; if(sym.includes("ETH"))return "ETH"; if(sym.includes("SOL"))return "SOL"; return "OTHER"; }

async function addToPortfolio(i) {
  const opp = window._opps[i];
  const res = await fetch("/api/portfolio", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ opportunity: opp, params: lastParams, alert_threshold: 3.0, min_apy_floor: 10.0 }) });
  const r = await res.json();
  if (r.ok) { refreshBadges(); alert("Added to portfolio. It will be re-checked automatically."); }
  else alert("Error: " + (r.error || "could not add"));
}

// ---- portfolio view ----
async function loadPortfolio() {
  const res = await fetch("/api/portfolio");
  const data = await res.json();
  const intMin = Math.round(data.monitor_interval / 60);
  document.getElementById("pf-info").innerHTML =
    `${data.items.length} strategies tracked · re-checked every ${intMin} min · ` +
    `Telegram alerts: <b>${data.telegram ? "on" : "off (set TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID)"}</b>`;
  const tb = document.getElementById("pf-rows"); tb.innerHTML = "";
  if (!data.items.length) { tb.innerHTML = `<tr><td colspan="11" class="muted" style="padding:18px 24px;">No strategies yet. Add one from the Scanner with the ★ Add button.</td></tr>`; return; }
  data.items.forEach(it => {
    const cur = it.current_net_apy, drop = it.drop_pp;
    const curTxt = cur == null ? '<span class="down">gone</span>' : cur.toFixed(2) + "%";
    const dropTxt = drop == null ? "—" : (drop > 0 ? `<span class="down">+${drop.toFixed(2)}</span>` : drop.toFixed(2));
    let status;
    if (it.alert) {
      const why = [];
      if (it.current_net_apy == null) why.push("gone");
      if (it.alert_floor) why.push("below floor");
      if (it.alert_drop) why.push("big drop");
      status = `<span class="pill alert">ALERT</span> <span class="muted" style="font-size:10px;">${why.join(", ")}</span>`;
    } else {
      status = '<span class="pill ok">OK</span>';
    }
    const floorVal = it.min_apy_floor != null ? it.min_apy_floor : 10;
    const c = it.legs.collateral, b = it.legs.borrow, d = it.legs.deploy;
    const lbl = `${link(c.project,c.url)} <b>${c.symbol}</b> → borrow ${link(b.project,b.url)} <b>${b.symbol}</b> → ${link(d.project,d.url)} <b>${d.symbol}</b>`;
    const tr = document.createElement("tr"); tr.className = "clickable";
    tr.onclick = () => openPortfolioDetail(it.id);
    tr.innerHTML =
      `<td>${lbl}</td><td>${it.chain}</td>` +
      `<td class="num">${it.baseline_net_apy.toFixed(2)}%</td>` +
      `<td class="num net">${curTxt}</td>` +
      `<td class="num">${dropTxt}</td>` +
      `<td>${status}</td>` +
      `<td class="num"><input type="number" value="${it.alert_threshold}" step="0.5" style="width:60px;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:3px 5px;" onclick="event.stopPropagation()" onchange="setThreshold('${it.id}', this.value)"></td>` +
      `<td class="num"><input type="number" value="${floorVal}" step="0.5" style="width:60px;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:3px 5px;" onclick="event.stopPropagation()" onchange="setFloor('${it.id}', this.value)"></td>` +
      `<td class="muted">${localTime(it.added_at)}</td>` +
      `<td class="muted">${localTime(it.last_checked)}</td>` +
      `<td><button class="secbtn" onclick="event.stopPropagation();removeItem('${it.id}')">Remove</button></td>`;
    tb.appendChild(tr);
  });
}

async function openPortfolioDetail(id) {
  try {
    const res = await fetch("/api/portfolio/detail", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({id}) });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }
    openDetail(data.opportunity, data.params, data.ctx, data.source);
  } catch (e) { alert("Could not load detail: " + e.message); }
}

async function checkNow() {
  const b = document.getElementById("checknow"); b.disabled = true; b.textContent = "Checking...";
  try { await fetch("/api/portfolio/check", { method: "POST" }); await loadPortfolio(); refreshBadges(); }
  finally { b.disabled = false; b.textContent = "Check now"; }
}
async function removeItem(id) {
  await fetch("/api/portfolio/remove", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({id}) });
  loadPortfolio(); refreshBadges();
}
async function setThreshold(id, v) {
  await fetch("/api/portfolio/update", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({id, alert_threshold: Number(v)}) });
  loadPortfolio(); refreshBadges();
}
async function setFloor(id, v) {
  await fetch("/api/portfolio/update", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({id, min_apy_floor: Number(v)}) });
  loadPortfolio(); refreshBadges();
}

// ---- notifications ----
async function refreshBadges() {
  const [pf, nf] = await Promise.all([fetch("/api/portfolio").then(r=>r.json()), fetch("/api/notifications").then(r=>r.json())]);
  document.getElementById("pf-count").textContent = pf.items.length ? `(${pf.items.length})` : "";
  const badge = document.getElementById("notif-badge");
  if (nf.unread > 0) { badge.textContent = nf.unread; badge.classList.remove("hidden"); }
  else badge.classList.add("hidden");
  window._notifs = nf.items;
}
async function toggleNotif() {
  const p = document.getElementById("notif-panel");
  if (!p.classList.contains("hidden")) { p.classList.add("hidden"); return; }
  const nf = await fetch("/api/notifications").then(r=>r.json());
  p.innerHTML = `<div style="font-weight:600;margin-bottom:8px;">Notifications</div>` +
    (nf.items.length ? nf.items.map(n =>
      `<div class="notif ${n.read?'':'unread'}"><div class="nlabel">${n.label}</div>${n.message}<div class="ntime">${localTime(n.time)}</div></div>`
    ).join("") : `<div class="muted">No notifications.</div>`);
  p.classList.remove("hidden");
  await fetch("/api/notifications/read", { method: "POST" });
  refreshBadges();
}

// ---- detail modal ----
function money(n) { return "$" + Math.round(n).toLocaleString(); }
function openDetail(o, params, ctx, source) {
  const c = o.collateral, b = o.borrow, d = o.deploy, r = o.risk;
  // Per-row USD value (set in native mode); fall back to the global resolved value.
  const equity = o.position_usd || params.position_size_usd || params.position_size;

  // Portfolio context box (only when opened from the Portfolio tab)
  let pfBox = "";
  if (ctx) {
    const cur = ctx.current, drop = (cur != null) ? (ctx.entry - cur) : null;
    const cf = ctx.params || {};
    pfBox = `<div class="box"><b>📌 Your tracked position</b> — added ${localTime(ctx.added_at)}.<br>` +
      `Entry net APY <b>${ctx.entry.toFixed(2)}%</b> → current <b>${cur != null ? cur.toFixed(2)+"%" : "gone"}</b>` +
      `${drop != null ? ` (Δ ${drop > 0 ? "+" : ""}${drop.toFixed(2)} pp)` : ""}.<br>` +
      `<span class="muted">${source === "entry" ? "Showing the snapshot from when you added it (entry TVL & rates)." : "Entry snapshot not stored — showing current live data."}</span><br>` +
      `<span class="muted">Scan config used: min net APY ${cf.min_net_apy}%, min TVL ${fmtTvl(cf.min_tvl)}, pairing ${cf.pairing}, collateral ${cf.collateral_class}, LTV safety ${cf.ltv_safety}, reward discount ${cf.reward_discount}, max rating ${cf.max_rating}.</span></div>`;
  }
  const borrowAmt = equity * o.target_ltv, deployAmt = borrowAmt;
  const annualProfit = equity * o.net_apy_after_costs / 100;
  const needsSwap = b.symbol.toUpperCase() !== d.symbol.toUpperCase();
  const lnk = (t,u) => u ? `<a href="${u}" target="_blank" rel="noopener">${t}</a>` : `<b>${t}</b>`;

  // Express the supplied amount in native units when the unit matches this loop's
  // collateral (fixed ETH/BTC), or always in 'native' mode (size is in collateral units).
  const unit = params.position_unit || "USD";
  let supplyTxt;
  if (unit === "native") {
    supplyTxt = `${params.position_size} ${c.symbol} (≈ ${money(equity)})`;
  } else {
    const unitMatchesCollateral = unit !== "USD" && assetClass(c.symbol) === unit;
    supplyTxt = unitMatchesCollateral ? `${params.position_size} ${unit} (≈ ${money(equity)})` : money(equity);
  }
  let mismatchNote = "";
  if (unit !== "USD" && unit !== "native" && assetClass(c.symbol) !== unit) {
    mismatchNote = `<div class="box risk-box">⚠️ <b>Your holding doesn't match this loop's collateral.</b> You set your position as <b>${unit}</b>, but this loop posts <b>${c.symbol}</b> as collateral — you can't supply ${unit} into a ${c.symbol} market. To use it you'd have to convert your ${unit} into ${c.symbol} first (i.e. sell your ${unit}, which defeats the point of "borrow against my ${unit}"). To find loops that use your <b>${unit}</b> directly as collateral, set the <b>Collateral</b> filter to <b>${unit.toLowerCase()}</b>. The amounts below use the USD value (${money(equity)}).</div>`;
  }

  // Hedge guidance for directional loops where the collateral is the volatile asset
  const cclass = assetClass(c.symbol);
  const wantHedge = r.directional && (cclass === "ETH" || cclass === "BTC" || cclass === "SOL");
  let hedgeNote = "", hedgeInner = "";
  if (wantHedge) {
    const px = (window._prices || {})[cclass] || 0;
    const units = px ? (equity / px) : null;
    const amt = units ? `~${units.toFixed(cclass === "BTC" ? 4 : 3)} ${cclass}` : `the equivalent of ${money(equity)}`;
    hedgeNote = `<div class="box risk-box"><b>⚖️ Hedge suggested (directional loop).</b> Your collateral is ${c.symbol}, so you are <b>net long ${cclass}</b>: if ${cclass} falls, your collateral loses value and you move toward liquidation. To run this <b>delta-neutral</b>, short <b>${amt}</b> in a futures/perp market (CEX, or on-chain via GMX / Hyperliquid). Then if ${cclass} drops, the short gains and offsets the collateral loss — leaving just the carry yield. Watch the <b>funding rate</b>: positive funding pays you to hold the short (bonus), negative funding is a cost.</div>`;
    hedgeInner = `Short <b>${amt}</b> in a futures/perp market to neutralize ${cclass} price risk and make this a delta-neutral carry trade. Size the short to your collateral value and rebalance if ${cclass} moves a lot.`;
  }

  // Recursive-leverage explanation
  let levNote = "";
  if ((o.loops || 1) > 1) {
    const totalDeployed = equity * ((o.leverage || 1) - 1);
    levNote = `<div class="box"><b>🔁 Leveraged ${o.leverage.toFixed(1)}x via ${o.loops} loops.</b> ` +
      `You repeat supply → borrow → deploy ${o.loops} times — each cycle re-supplies the deployed ${d.symbol} as collateral and borrows again, amplifying the spread. ` +
      `Total deployed ≈ <b>${money(totalDeployed)}</b> on ${money(equity)} of equity. ` +
      `Your liquidation buffer is unchanged (each loop holds the same LTV), but a liquidation now unwinds the <b>whole</b> stack and gas + any swap cost scale with the loop count. ` +
      `Flash-loan looping can collapse the gas. Only loops where collateral & deploy are the same asset class can be levered.</div>`;
  }

  // Borrow-incentive highlight: net-negative borrow cost
  let borrowIncNote = "";
  if (o.borrow_incentivized) {
    borrowIncNote = `<div class="box" style="border-color:#1b3a26">⚡ <b>Paid to borrow.</b> The net borrow cost on ${b.symbol} is <b>${b.borrow_apy.toFixed(2)}%</b> (negative) — the protocol's borrow incentives exceed the interest rate, so borrowing <i>adds</i> to your return. These are rare and the incentive can end; verify it's live on ${b.project}.</div>`;
  }

  // Direct-deposit warning: this loop is worse than just depositing the collateral
  let inferiorNote = "";
  if (o.beats_direct === false && o.direct_deposit_apy != null) {
    inferiorNote = `<div class="box risk-box">⚠️ <b>A direct deposit beats this loop.</b> Just depositing your ${c.symbol} straight into ${d.project} earns <b>${o.direct_deposit_apy.toFixed(2)}%</b> — more than this loop's <b>${o.net_apy_after_costs.toFixed(2)}%</b> net. The borrowing step adds steps and liquidation risk for a lower return. Only loop like this if you specifically want the leverage; otherwise deposit directly.</div>`;
  }

  let n = 0; const step = () => ++n;
  let steps = "";
  steps += `<li><span class="step-num">${step()}. Supply collateral.</span> Deposit <code>${supplyTxt}</code> of <b>${c.symbol}</b> into ${lnk(c.project,c.url)} on ${c.chain} (market TVL ${fmtTvl(c.tvl_usd)}). You earn its supply yield (<b>${c.supply_apy.toFixed(1)}%</b>) and it becomes borrowing collateral.</li>`;
  steps += `<li><span class="step-num">${step()}. Borrow.</span> Borrow <code>${money(borrowAmt)}</code> of <b>${b.symbol}</b> from ${lnk(b.project,b.url)} (market TVL ${fmtTvl(b.tvl_usd)}) — ${(o.target_ltv*100).toFixed(0)}% of collateral value, leaving a <b>${r.liq_buffer_pct.toFixed(0)}%</b> buffer (HF ${r.health_factor.toFixed(2)}). You pay <b>${b.borrow_apy.toFixed(1)}%</b>.</li>`;
  if (needsSwap) steps += `<li><span class="step-num">${step()}. Swap.</span> Swap borrowed <b>${b.symbol}</b> → <b>${d.symbol}</b> via a DEX aggregator (1inch/Matcha). Similar assets, low slippage — but see basis-risk note.</li>`;
  steps += `<li><span class="step-num">${step()}. Deploy.</span> Deposit <code>${money(deployAmt)}</code> of <b>${d.symbol}</b> into ${lnk(d.project,d.url)} (pool TVL ${fmtTvl(d.tvl_usd)}), earning <b>${d.apy.toFixed(1)}%</b> — the yield that pays for the loop. Keep your size small vs pool TVL so you can exit cleanly.</li>`;
  if (wantHedge) steps += `<li><span class="step-num">${step()}. Hedge (recommended).</span> ${hedgeInner}</li>`;
  steps += `<li><span class="step-num">${step()}. Monitor & exit.</span> Add this to your Portfolio to auto-track. If ${c.symbol} falls ~${r.liq_buffer_pct.toFixed(0)}% vs the debt you risk liquidation. Unwind in reverse: withdraw from ${d.project}${needsSwap?`, swap ${d.symbol}→${b.symbol}`:""}, repay ${b.project}, withdraw collateral${wantHedge?", and close the futures short":""}.</li>`;

  let risks = r.warnings.map(w => `<li>${w}</li>`).join("");
  if (needsSwap) risks += `<li><b>Cross-asset basis risk:</b> you hold ${d.symbol} but owe ${b.symbol}. If they de-peg, debt can grow vs deployed assets.</li>`;
  risks += `<li>Rates are variable — the ${b.borrow_apy.toFixed(1)}% borrow cost and ${d.apy.toFixed(1)}% deploy yield can move at any time.</li>`;
  document.getElementById("modal").innerHTML =
    `<button class="close" onclick="closeDetail()">Close ✕</button>` +
    `<h2>${c.project} ${c.symbol} loop → ${d.project} ${d.symbol}</h2>` +
    `<div class="tag">${c.chain} · <span class="pill ${r.rating}">${r.rating}</span> risk · position ${supplyTxt} over ${params.hold_days} days</div>` +
    `<div class="big">${o.net_apy_after_costs.toFixed(2)}% net APY</div>` +
    `<div class="box">Gross ${o.net_apy.toFixed(1)}% − ${o.cost_drag_apy.toFixed(1)}% drag = <b>${o.net_apy_after_costs.toFixed(2)}% net</b>. On ${money(equity)} ≈ <b>${money(annualProfit)}/year</b> if rates hold.</div>` +
    pfBox +
    levNote +
    borrowIncNote +
    inferiorNote +
    mismatchNote +
    hedgeNote +
    `<h3>How to run this loop</h3><ol>${steps}</ol>` +
    `<h3>Key risks</h3><div class="box risk-box"><ul>${risks}</ul></div>` +
    `<h3>Deploy pool APY history</h3><div id="poolchart" class="muted">Loading APY history…</div>` +
    `<div class="disclaimer">Educational walkthrough from live DefiLlama data. Not financial advice. Verify every rate, LTV and liquidation threshold on the protocol before committing funds. This tool does not execute anything.</div>`;
  document.getElementById("overlay").classList.add("open");
  loadPoolChart(d.pool_id, d.symbol);
}

async function loadPoolChart(poolId, sym) {
  const el = document.getElementById("poolchart");
  if (!el || !poolId) { if (el) el.textContent = "No pool id for history."; return; }
  try {
    const r = await fetch("/api/poolchart?pool_id=" + encodeURIComponent(poolId) + "&days=90");
    const data = await r.json();
    if (data.error || !data.series || !data.series.length) { el.innerHTML = '<span class="muted">No history available for this pool.</span>'; return; }
    el.innerHTML = renderApyChart(data, sym);
  } catch (e) { el.innerHTML = '<span class="muted">Could not load history.</span>'; }
}

function renderApyChart(data, sym) {
  const s = data.series, st = data.stats;
  const W = 660, H = 150, padL = 38, padR = 10, padT = 10, padB = 18, n = s.length;
  const apys = s.map(p => p.apy);
  let lo = Math.min(...apys), hi = Math.max(...apys);
  if (hi - lo < 0.5) { hi += 0.3; lo -= 0.3; }
  const x = i => padL + (n === 1 ? (W-padL-padR)/2 : i*(W-padL-padR)/(n-1));
  const y = v => padT + (H-padT-padB) * (1 - (v-lo)/(hi-lo));
  let g = "";
  for (let k = 0; k <= 3; k++) {
    const val = lo + (hi-lo)*k/3, yy = y(val);
    g += `<line x1="${padL}" y1="${yy}" x2="${W-padR}" y2="${yy}" stroke="#1c2128"/>`;
    g += `<text x="${padL-5}" y="${yy+3}" fill="#8b949e" font-size="9" text-anchor="end">${val.toFixed(1)}%</text>`;
  }
  const pts = s.map((p,i) => `${x(i).toFixed(1)},${y(p.apy).toFixed(1)}`).join(" ");
  g += `<polyline fill="none" stroke="#58a6ff" stroke-width="1.6" points="${pts}"/>`;
  const now = apys[apys.length-1];
  const caption = `<div class="muted" style="font-size:11px;margin-top:4px;">Last ${st.n} days · min <b>${st.min}%</b> · avg <b>${st.avg}%</b> · max <b>${st.max}%</b> · now <b>${now.toFixed(2)}%</b>. ` +
    `Use this to judge whether the ${sym} yield is stable or a recent spike.</div>`;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:150px;background:#0e1116;border:1px solid #1c2128;border-radius:8px;">${g}</svg>${caption}`;
}
function closeDetail() { document.getElementById("overlay").classList.remove("open"); }
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDetail(); });

refreshBadges();
setInterval(refreshBadges, 30000);
</script>
</body></html>"""


# ----------------------------- HTTP handler -----------------------------

def cfg_from_query(qs: dict) -> scanner.ScanConfig:
    def g(name, cast, default):
        if name in qs and qs[name]:
            try:
                return cast(qs[name][0])
            except ValueError:
                return default
        return default
    return scanner.ScanConfig(
        min_net_apy=g("min_net_apy", float, 3.0),
        min_tvl=g("min_tvl", float, 5_000_000),
        ltv_safety=g("ltv_safety", float, 0.8),
        reward_discount=g("reward_discount", float, 0.5),
        max_rating=g("max_rating", str, "HIGH"),
        pairing=g("pairing", str, "all"),
        collateral_class=g("collateral_class", str, "all"),
        hide_inferior=g("hide_inferior", lambda v: v == "1", True),
        max_loops=g("max_loops", int, 1),
        momentum=g("momentum", str, "any"),
        borrow_incentive_only=g("borrow_incentive_only", lambda v: v == "1", False),
        same_chain=g("same_chain", lambda v: v == "1", True),
        position_size=g("position_size", float, 10_000.0),
        position_unit=g("position_unit", str, "native"),  # dashboard always uses native
        hold_days=g("hold_days", float, 30.0),
        slippage_bps=g("slippage_bps", float, 5.0),
        limit=g("limit", int, 40),
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p.path == "/api/scan":
            try:
                self._send(200, json.dumps(scanner.scan(cfg_from_query(parse_qs(p.query)))))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}))
        elif p.path == "/api/portfolio":
            with LOCK:
                items = json.loads(json.dumps(PORTFOLIO))  # shallow copy for response
            self._send(200, json.dumps({"items": items, "monitor_interval": MONITOR_INTERVAL,
                                        "telegram": telegram_configured()}))
        elif p.path == "/api/notifications":
            with LOCK:
                items = NOTIFICATIONS[:50]
                unread = sum(1 for n in NOTIFICATIONS if not n.get("read"))
            self._send(200, json.dumps({"items": items, "unread": unread}))
        elif p.path == "/api/poolchart":
            qs = parse_qs(p.query)
            pid = (qs.get("pool_id") or [""])[0]
            try:
                days = int((qs.get("days") or ["90"])[0])
            except ValueError:
                days = 90
            if not pid:
                self._send(400, json.dumps({"error": "pool_id required"}))
                return
            try:
                r = requests.get(f"https://yields.llama.fi/chart/{pid}", timeout=20)
                rows = r.json().get("data", [])
            except (requests.RequestException, ValueError) as e:
                self._send(502, json.dumps({"error": str(e)}))
                return
            rows = rows[-days:]
            series = [{"t": x.get("timestamp"), "apy": x.get("apy")}
                      for x in rows if x.get("apy") is not None]
            apys = [s["apy"] for s in series]
            stats = ({"min": round(min(apys), 2), "max": round(max(apys), 2),
                      "avg": round(sum(apys) / len(apys), 2), "n": len(apys)} if apys else {})
            self._send(200, json.dumps({"series": series, "stats": stats}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        p = urlparse(self.path)
        body = self._body()
        if p.path == "/api/portfolio":
            try:
                item = add_item(body["opportunity"], body.get("params") or {},
                                body.get("alert_threshold", 3.0), body.get("min_apy_floor", 10.0))
                self._send(200, json.dumps({"ok": True, "id": item["id"]}))
            except (KeyError, TypeError) as e:
                self._send(400, json.dumps({"error": f"bad payload: {e}"}))
        elif p.path == "/api/portfolio/remove":
            remove_item(body.get("id", ""))
            self._send(200, json.dumps({"ok": True}))
        elif p.path == "/api/portfolio/update":
            update_item(body.get("id", ""), threshold=body.get("alert_threshold"),
                        floor=body.get("min_apy_floor"))
            self._send(200, json.dumps({"ok": True}))
        elif p.path == "/api/portfolio/check":
            n = check_all()
            self._send(200, json.dumps({"ok": True, "new_alerts": n}))
        elif p.path == "/api/portfolio/detail":
            with LOCK:
                item = next((it for it in PORTFOLIO if it["id"] == body.get("id")), None)
            if not item:
                self._send(404, json.dumps({"error": "not found"}))
                return
            ctx = {"added_at": item["added_at"], "entry": item["baseline_net_apy"],
                   "current": item.get("current_net_apy"), "params": item["params"]}
            if item.get("opportunity"):
                self._send(200, json.dumps({"opportunity": item["opportunity"],
                                            "params": item["params"], "ctx": ctx, "source": "entry"}))
            else:
                # Older item without a stored snapshot — recompute live from pool ids.
                try:
                    pm, lb = scanner.fetch_market_maps()
                    opp = scanner.recompute_strategy(item["legs"], item["params"], pm, lb)
                except Exception as e:  # noqa: BLE001
                    self._send(500, json.dumps({"error": str(e)}))
                    return
                if not opp:
                    self._send(200, json.dumps({"error": "A leg of this strategy is no longer on DefiLlama."}))
                    return
                self._send(200, json.dumps({"opportunity": opp, "params": item["params"],
                                            "ctx": ctx, "source": "live"}))
        elif p.path == "/api/notifications/read":
            with LOCK:
                for n in NOTIFICATIONS:
                    n["read"] = True
                save_notifications()
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main() -> int:
    ap = argparse.ArgumentParser(description="Local web dashboard for the loop scanner.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    load_state()
    threading.Thread(target=monitor_loop, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard at {url} · portfolio monitor every {MONITOR_INTERVAL//60} min · "
          f"telegram {'on' if telegram_configured() else 'off'}  (Ctrl-C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
