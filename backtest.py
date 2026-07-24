#!/usr/bin/env python3
"""
Backtest / time-series view over saved scan snapshots (Phase 3).

Reads the scan-*.json files written by `scanner.py --save DIR` and tracks how each
loop's cost-adjusted net APY evolved across scans: which spreads widened, which
collapsed, and which opened or closed entirely.

Usage:
    python scanner.py --save ./snapshots     # run this a few times over days
    python backtest.py ./snapshots           # then review the history
    python backtest.py ./snapshots --top 15  # show more rows
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys


def opp_key(o: dict) -> str:
    c, b, d = o["collateral"], o["borrow"], o["deploy"]
    return (f"{c['project']}/{c['chain']}:{c['symbol']}"
            f" -> borrow {b['project']}:{b['symbol']}"
            f" -> deploy {d['project']}:{d['symbol']}")


def load_snapshots(directory: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(directory, "scan-*.json")))
    snaps = []
    for f in files:
        try:
            with open(f) as fh:
                snaps.append(json.load(fh))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {f}: {e}", file=sys.stderr)
    snaps.sort(key=lambda s: s.get("generated_at", ""))
    return snaps


def sparkline(values: list[float | None]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    present = [v for v in values if v is not None]
    if not present:
        return ""
    lo, hi = min(present), max(present)
    span = (hi - lo) or 1.0
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        else:
            idx = int((v - lo) / span * (len(blocks) - 1))
            out.append(blocks[idx])
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest over saved scan snapshots.")
    ap.add_argument("directory", help="Directory containing scan-*.json snapshots")
    ap.add_argument("--top", type=int, default=20, help="Rows to show (default: 20)")
    ap.add_argument("--metric", default="net_apy_after_costs",
                    choices=["net_apy_after_costs", "net_apy", "raw_spread"],
                    help="Which metric to track (default: net_apy_after_costs)")
    args = ap.parse_args()

    snaps = load_snapshots(args.directory)
    if len(snaps) < 2:
        print(f"Need at least 2 snapshots in {args.directory} to compare "
              f"(found {len(snaps)}). Run `scanner.py --save {args.directory}` over time.")
        return 1

    times = [s.get("generated_at", "?") for s in snaps]
    print(f"Loaded {len(snaps)} snapshots: {times[0]}  ...  {times[-1]}\n")

    # Build per-key series aligned to snapshot order.
    series: dict[str, list[float | None]] = {}
    for i, s in enumerate(snaps):
        seen = {}
        for o in s.get("opportunities", []):
            seen[opp_key(o)] = o.get(args.metric)
        for key in set(series) | set(seen):
            series.setdefault(key, [None] * len(snaps))
            series[key][i] = seen.get(key)

    def latest(v: list[float | None]):
        for x in reversed(v):
            if x is not None:
                return x
        return None

    def first(v: list[float | None]):
        for x in v:
            if x is not None:
                return x
        return None

    ranked = sorted(series.items(), key=lambda kv: (latest(kv[1]) or -1e9), reverse=True)

    print(f"Tracking metric: {args.metric}  (sparkline = oldest -> newest)\n")
    print(f"{'LATEST':>7}  {'Δ vs first':>10}  {'TREND':<12}  OPPORTUNITY")
    print("-" * 100)
    for key, vals in ranked[:args.top]:
        last, fst = latest(vals), first(vals)
        delta = (last - fst) if (last is not None and fst is not None) else None
        last_s = f"{last:6.2f}%" if last is not None else "  --  "
        delta_s = f"{delta:+6.2f}%" if delta is not None else "   new"
        print(f"{last_s:>7}  {delta_s:>10}  {sparkline(vals):<12}  {key[:70]}")

    # Opened / closed between first and last snapshot.
    opened = [k for k, v in series.items() if v[0] is None and v[-1] is not None]
    closed = [k for k, v in series.items() if v[0] is not None and v[-1] is None]
    print(f"\nOpened since first snapshot: {len(opened)}   Closed since first snapshot: {len(closed)}")
    for k in opened[:5]:
        print(f"  + {k[:90]}")
    for k in closed[:5]:
        print(f"  - {k[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
