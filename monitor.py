#!/usr/bin/env python3
"""
Pool-change monitor for the DeFi yield-carry scanner.

Watches how the *opportunities* (and the underlying pool/borrow rates) change between
scans and reports alerts: loops that newly appear, ones that disappear, big swings in
cost-adjusted net APY, and risk-rating changes. It monitors the MARKET, not any wallet
or position you hold.

Designed for two modes:
    python monitor.py --once                 # one comparison pass (ideal for cron)
    python monitor.py --interval 300         # loop, re-checking every 5 minutes

State (last snapshot + alert log) lives in --state-dir (default ./monitor-state).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import scanner


def opp_key(o: dict) -> str:
    c, b, d = o["collateral"], o["borrow"], o["deploy"]
    return (f"{c['project']}/{c['chain']}:{c['symbol']}"
            f" -> {b['project']}:{b['symbol']}"
            f" -> {d['project']}:{d['symbol']}")


def index(snapshot: dict) -> dict[str, dict]:
    return {opp_key(o): o for o in snapshot.get("opportunities", [])}


def diff_snapshots(prev: dict, curr: dict, move_threshold: float) -> list[str]:
    """Return human-readable alert lines describing market changes."""
    p, c = index(prev), index(curr)
    alerts: list[str] = []

    for key in sorted(c.keys() - p.keys()):
        o = c[key]
        alerts.append(f"NEW    {o['net_apy_after_costs']:+6.2f}%  {o['risk']['rating']:<6}  {key}")
    for key in sorted(p.keys() - c.keys()):
        o = p[key]
        alerts.append(f"GONE   (was {o['net_apy_after_costs']:.2f}%)        {key}")

    for key in sorted(p.keys() & c.keys()):
        pv, cv = p[key], c[key]
        before, after = pv["net_apy_after_costs"], cv["net_apy_after_costs"]
        if abs(after - before) >= move_threshold:
            arrow = "UP" if after > before else "DOWN"
            alerts.append(f"{arrow:<6} {before:.2f}% -> {after:.2f}%  ({after-before:+.2f})  {key}")
        if pv["risk"]["rating"] != cv["risk"]["rating"]:
            alerts.append(f"RISK   {pv['risk']['rating']} -> {cv['risk']['rating']}        {key}")
    return alerts


def cfg_from_args(args) -> scanner.ScanConfig:
    return scanner.ScanConfig(
        min_net_apy=args.min_net_apy, min_tvl=args.min_tvl, ltv_safety=args.ltv_safety,
        reward_discount=args.reward_discount, max_rating=args.max_rating,
        same_chain=args.same_chain, position_size=args.position_size,
        hold_days=args.hold_days, slippage_bps=args.slippage_bps, limit=args.limit,
    )


def run_once(cfg: scanner.ScanConfig, state_dir: str, move_threshold: float) -> int:
    os.makedirs(state_dir, exist_ok=True)
    latest_path = os.path.join(state_dir, "latest.json")
    log_path = os.path.join(state_dir, "alerts.log")

    curr = scanner.scan(cfg)
    ts = curr["generated_at"]

    prev = None
    if os.path.exists(latest_path):
        try:
            with open(latest_path) as f:
                prev = json.load(f)
        except (json.JSONDecodeError, OSError):
            prev = None

    if prev is None:
        print(f"[{ts}] baseline recorded: {curr['count']} opportunities "
              f"(no previous scan to compare).")
    else:
        alerts = diff_snapshots(prev, curr, move_threshold)
        header = f"[{ts}] {len(alerts)} change(s) vs {prev.get('generated_at','?')}"
        print(header)
        for a in alerts:
            print("  " + a)
        if alerts:
            with open(log_path, "a") as f:
                f.write(header + "\n")
                for a in alerts:
                    f.write("  " + a + "\n")

    # Persist current as the new baseline + a timestamped snapshot for backtesting.
    with open(latest_path, "w") as f:
        json.dump(curr, f, indent=2)
    snap_path = os.path.join(state_dir, f"scan-{ts.replace(':', '-')}.json")
    with open(snap_path, "w") as f:
        json.dump(curr, f, indent=2)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor DeFi pool/spread changes between scans.")
    ap.add_argument("--once", action="store_true", help="Single pass then exit (good for cron)")
    ap.add_argument("--interval", type=int, default=300,
                    help="Loop interval in seconds when not --once (default: 300)")
    ap.add_argument("--state-dir", default="./monitor-state", help="Where to keep state + logs")
    ap.add_argument("--alert-threshold", type=float, default=1.0,
                    help="Net-APY change (pp) that triggers a MOVE alert (default: 1.0)")
    # Scan filters (subset of scanner.py)
    ap.add_argument("--min-net-apy", type=float, default=3.0)
    ap.add_argument("--min-tvl", type=float, default=5_000_000)
    ap.add_argument("--ltv-safety", type=float, default=0.8)
    ap.add_argument("--reward-discount", type=float, default=0.5)
    ap.add_argument("--max-rating", choices=["LOW", "MEDIUM", "HIGH"], default="HIGH")
    ap.add_argument("--same-chain", action="store_true", default=True)
    ap.add_argument("--cross-chain", dest="same_chain", action="store_false")
    ap.add_argument("--position-size", type=float, default=10_000.0)
    ap.add_argument("--hold-days", type=float, default=30.0)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    cfg = cfg_from_args(args)

    if args.once:
        try:
            return run_once(cfg, args.state_dir, args.alert_threshold)
        except Exception as e:  # noqa: BLE001
            print(f"Scan failed: {e}", file=sys.stderr)
            return 1

    print(f"Monitoring every {args.interval}s -> {args.state_dir} (Ctrl-C to stop)",
          file=sys.stderr)
    try:
        while True:
            try:
                run_once(cfg, args.state_dir, args.alert_threshold)
            except Exception as e:  # noqa: BLE001 - keep the loop alive on transient errors
                print(f"  scan failed: {e}", file=sys.stderr)
            time.sleep(max(30, args.interval))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
