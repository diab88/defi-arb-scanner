# DeFi Yield-Carry Loop Scanner (Phase 1)

A local, read-only scanner that spots **leveraged yield-carry opportunities**:

> Supply collateral A → borrow asset B against it → deploy B into a higher-yield pool
> whose yield covers the borrow cost (plus margin).

Powered by [DefiLlama](https://defillama.com)'s free APIs — **no API key required**.

## Setup

```bash
cd defi-arb-scanner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python scanner.py                       # default scan
python scanner.py --min-net-apy 5       # only show >=5% net APY
python scanner.py --min-tvl 20000000    # only deep markets/pools (>=20M TVL)
python scanner.py --reward-discount 1   # ignore reward/incentive APY entirely (conservative)
python scanner.py --cross-chain         # allow cross-chain (warning: ignores bridge cost)
python scanner.py --max-rating LOW      # only conservative, non-directional loops
python scanner.py --collateral-class eth          # only loops where you post ETH-like collateral
python scanner.py --position-size 5 --position-unit ETH   # size the scenario in ETH, not USD
python scanner.py --json                # machine-readable output to stdout
python scanner.py --save ./snapshots    # write a timestamped JSON snapshot (diff over time)
python scanner.py --limit 50            # show more rows
```

Run it anytime — it pulls fresh data on every invocation.

## Risk layer (Phase 2)

Every opportunity now carries a risk assessment:

- **RATING** — LOW / MEDIUM / HIGH, combining the factors below.
- **HF** (health factor) — `liquidation_threshold / (debt/collateral)` at entry. >1 = solvent; higher = safer.
- **BUF** (liquidation buffer) — the adverse price move (%) that would trigger liquidation.
- **reward_share** — fraction of deploy yield that is incentive emissions (high = less sustainable).
- **directional** — true when collateral and debt are different asset classes (carries price risk; same-class loops only face depeg/rate risk).
- **warnings** — human-readable flags (thin buffer, incentive-heavy yield, IL exposure, DefiLlama "APY down" prediction).

Tuning knobs: `--ltv-safety` (how much of max LTV to use), `--liq-premium` (assumed gap between
liquidation threshold and max LTV, since DefiLlama doesn't expose the threshold directly),
`--max-rating` (filter by risk).

### Snapshots

`--save DIR` writes `scan-<timestamp>.json` with the params and ranked opportunities, so you can
diff scans over time (e.g. track when a spread opens or closes) or feed them into other tooling.

## How the math works

Per $1 of collateral equity:

```
net_apy = supply_apy(A) + target_ltv * (deploy_apy(B) - borrow_cost(B))
target_ltv = max_ltv(A) * ltv_safety
```

- **supply_apy / deploy_apy**: base APY + reward APY, with rewards discounted (`--reward-discount`).
- **borrow_cost**: base borrow APY minus any borrow incentive (also discounted).
- **target_ltv**: a safety fraction of the protocol's max LTV (default 80%).

## Important limitations (read before trusting a number)

- **Liquidation modeling is approximate** — the threshold is estimated as `max_ltv + --liq-premium`
  because DefiLlama doesn't expose the true per-asset liquidation threshold. Verify on the protocol.
- **No gas / slippage / bridging costs** — small positions or cross-chain may be unprofitable.
- **Reward yield is discounted, not validated** — emissions tokens may not be exitable at quoted price.
- **Rates are variable** — borrow APY rises with utilization; your own entry moves it.
- **Asset matching is heuristic** — single-asset symbols grouped into STABLE / ETH / BTC classes.

This is a candidate-spotter, not an execution or risk engine. Verify on-chain before acting.

## Cost model (Phase 3)

Headline APYs ignore the cost of entering and exiting a loop. The scanner now subtracts:

- **Gas** — `--tx-count` transactions at a per-chain USD estimate (Ethereum ~$4/tx, L2s/Solana cents).
- **Slippage** — `--slippage-bps` per swap, applied to the deployed notional in and out
  (skipped when borrow and deploy assets are the same symbol).

Both are **annualized over `--hold-days`** and divided by `--position-size`, giving a `DRAG` APY
that's subtracted from gross to produce **NET(cost)** — the number rows are ranked and filtered by.

```bash
python scanner.py --position-size 100000 --hold-days 180   # big position, long hold -> tiny drag
python scanner.py --position-size 2000 --hold-days 7       # small + short -> gas dominates
```

A $10k / 30-day Ethereum loop carries ~5% drag; the same loop at $100k / 180-day drops to ~0.2%.

## Backtesting over snapshots (Phase 3)

Save snapshots over time, then review how spreads moved:

```bash
python scanner.py --save ./snapshots     # run on a schedule (cron) over days/weeks
python backtest.py ./snapshots           # time-series view with sparklines
python backtest.py ./snapshots --top 15 --metric net_apy
```

Shows each loop's latest value, change vs. the first snapshot, a sparkline trend, and which
opportunities **opened** or **closed** over the window.

## Web dashboard (run scans visually)

```bash
python dashboard.py                    # -> http://127.0.0.1:8765
python dashboard.py --port 9000
python dashboard.py --seed ./snapshots # preload the trend chart from saved snapshots
```

A single-page UI (stdlib only, no extra deps) with all the filters as controls and a
risk-coloured, ranked table. It reuses the same `scanner.scan()` code path as the CLI,
so results are identical. Features:

- **Run scan** — pulls fresh DefiLlama data and renders the ranked table.
- **Trend chart** — every scan is recorded in an in-memory history; the SVG line chart
  plots the top loops' cost-adjusted net APY over successive scans. Seed it from saved
  snapshots with `--seed DIR`.
- **Auto-refresh** — tick the box to re-scan every N seconds and watch spreads move live.
- **Collateral filter** — restrict to `stable` / `eth` / `btc` collateral, e.g. to screen "borrow
  against my ETH" loops.
- **Position in ETH/BTC** — set the Position unit to ETH or BTC and enter how much you hold; the
  scenario (borrow/deploy amounts, costs) is built on its live USD value.
- **Hedge guidance** — when you open a *directional* loop whose collateral is ETH/BTC (so you're
  net long it), the detail panel adds a "⚖️ Hedge suggested" note and step explaining how to short
  the asset in futures to run it delta-neutral, sized to your collateral.

## Portfolio + alerts (in the dashboard)

The dashboard has two tabs: **Scanner** and **Portfolio**.

- On any scanned row, click **★ Add** to add that strategy to your portfolio — meaning you've
  applied it and are running it. It stores the strategy's pool IDs, the params used, and the
  **net APY at the moment you added it** (the baseline).
- A background thread re-prices every portfolio strategy **every hour** (configurable) using
  live DefiLlama data — even if the strategy no longer passes the scan filters.
- If a strategy's net APY drops by **≥ your threshold (default 3 pp)** below the entry APY, it's
  flagged **ALERT**, a 🔔 notification appears in-app, and (if configured) a **Telegram** message
  is sent — your cue to exit and repay before losses mount.
- Per-strategy you can edit the alert threshold inline, hit **Check now** to re-price immediately,
  or **Remove** it. The portfolio and notifications persist to `./data/` across restarts.

> The threshold is in **percentage points** of net APY (entry − current). A strategy added at
> 10% that falls to 6.5% has dropped 3.5 pp and would alert at the default threshold.

### Telegram alerts (optional)

1. Message **@BotFather** on Telegram → `/newbot` → copy the **bot token**.
2. Message your new bot once (say "hi"), then get your **chat id**: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`.
3. Create a `.env` file next to `docker-compose.yml`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=987654321
   DEFI_MONITOR_INTERVAL=3600
   ```
4. `docker compose up -d` — the Portfolio tab will show **Telegram alerts: on**.

## Pool-change monitoring (CLI)

Watch how the **market** moves between scans — which loops appear, disappear, swing in
net APY, or change risk rating. This monitors pools/spreads, *not* any wallet or position.

```bash
python monitor.py --once                       # one comparison pass (ideal for cron)
python monitor.py --interval 300               # loop, re-checking every 5 min
python monitor.py --once --alert-threshold 0.5 # more sensitive net-APY move alerts
```

State (last snapshot + `alerts.log`) lives in `--state-dir` (default `./monitor-state`).
Each pass also writes a timestamped `scan-*.json`, so `backtest.py ./monitor-state` works
on the monitor's own history. Alert types: `NEW`, `GONE`, `UP`/`DOWN` (net-APY move past the
threshold), and `RISK` (rating change).

Run it on a schedule with cron, e.g. every 30 minutes:

```cron
*/30 * * * * cd /path/to/defi-arb-scanner && python3 monitor.py --once >> monitor.out 2>&1
```

## Possible next steps

- Alert delivery (desktop notification, webhook, email) when `monitor.py` fires.
- More asset classes / chains in the matching logic.
- CSV export of scan results.

Execution (moving real funds) is intentionally **out of scope** — this is a research and
monitoring tool only.
