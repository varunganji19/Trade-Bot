# DEMO — the 90-second runbook

The thesis is **the edge is the process**: every claim on this page is a
function you can run, not a promise. This is the scripted flow, the offline
fallback, and the three questions every panel asks.

## Prereqs (once, needs network)

```bash
pip install -r requirements.txt
python3 main.py seed-demo     # real backtest replay -> mode='demo' journal rows
```

## The 90 seconds

| Time | Do | What it proves |
|---|---|---|
| 0–10s | `make demo` → open <http://127.0.0.1:8000> | one command, zero config |
| 10–40s | **Overview** tab: equity curve, headline stats, decision terminal | every decision (HOLDs included) is journaled with its full reasoning |
| 40–70s | **Portfolio** tab (strategy attribution, `demo` badges) → **Chat** tab: ask *"why did you buy BTC?"* | the bot answers for its own record from its own journal — and labels seeded rows honestly |
| 70–90s | **Evidence** tab: Kronos IC ledger vs its 0.02 promotion hurdle, purged-CV path distribution, PBO / Deflated-Sharpe / MinTRL verdict cards, shadow adherence | the honesty layer is a UI, not a footnote: a foundation model was measured, failed its hurdle, and the bot says so |

Run `make validate` beforehand to generate `REPORT.md` + the JSON the Evidence
tab renders, and `python3 main.py shadow` for the adherence report.

## If the venue wifi dies

- Candles are disk-cached per day (`data/cache/`) and the journal is local
  SQLite — **the dashboard and chatbot need no exchange connection**.
- Start the engine once with network before the demo so the cache is warm;
  the engine then runs on cached bars (stale-data guards keep it honest about
  what it can see).
- Final fallback: the screenshots in `docs/` and `REPORT.md`.

## The three questions every panel asks

1. **"What happens if the exchange API dies?"** — crypto fetches fall through
   Binance → Bybit → OKX. With an OPEN position behind a dead feed, the engine
   warns after 3 consecutive failures and force-closes at the last known good
   mark after 10 (`TradingEngine.FETCH_FAIL_WARN/CLOSE`) — cutting a loser on
   stale data beats holding it blind.
2. **"How is a decision attributed?"** — every evaluation is journaled
   (decision rows: per-strategy signals, regime, confidence, reasoning text;
   trades carry the strategy + rationale of the fill). `python3 main.py chat`
   queries that record; the shadow account replays it against the bot's own
   rules.
3. **"What would real money change?"** — the broker is an adapter: market kind
   already drives per-market fees/slippage, and a live broker implements the
   same fill interface the backtester and paper engine share. The honest part
   of the answer: nothing in BACKTESTS.md is a promise (RESEARCH.md §4) —
   paper results are a process demonstration, not an edge claim.

## Operator notes (read once before the run)

- **Rehearse, then demo-day: safe to re-run.** `python3 main.py seed-demo`
  *replaces* the demo rows (idempotent) — it no longer stacks history on every
  run, so `make demo` twice is fine.
- **The engine auto-resumes on dashboard boot** if the last session left it
  running (that's the point: the bot survives restarts). Don't want that on a
  rehearsal machine? `ALGO_NO_AUTO_RESUME=1 python3 main.py dashboard`. On
  stage you'll see a toast the moment it happens; stop it from the Overview
  tab. A stopped state file always stays stopped.
- **Port already in use fails loudly now.** If port 8000 is taken, the
  launcher names the squatter PID and exits non-zero (`make demo` aborts
  instead of "succeeding" with nothing served) — retry on `--port 8001`.
- **Ctrl-C any time is safe.** Every state write is atomic (tmp+rename or a
  SQLite transaction); a SIGINT mid-cycle leaves `integrity_check=ok`.
- **Reset backups:** each reset writes `data/trading.backup.<ts>.db`
  (microsecond-named, so double-clicks don't overwrite) and keeps the newest
  five — a full WAL checkpoint runs first, so the backup is a complete database.
- **Wifi-dies arithmetic:** the outage ladder is per *cycle*, and a cycle
  includes a network-timeout sweep — with a 60s interval, force-close lands
  roughly 10–20 minutes after the wifi dies (the health banner on the UI
  counts it out). Speed it up by lowering the interval before the demo.
- **First visible cycle:** with Kronos enabled, the first cycle takes 1–3
  minutes (weights load + forecasts for every book) — decisions arrive in a
  burst after it. Start the engine *before* the audience sits, or demo the
  seeded journal first. The Overview "Cycles" card counts them as they land.
- **`DASHBOARD_TOKEN` mode is browser-friendly now:** the page shell loads,
  then a one-time dialog asks for the token (stored in that browser's
  localStorage only). If you set the token and the dialog appears, paste it
  and the dashboard proceeds; every API call carries it from then on.
