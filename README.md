# Algo — a trading research platform that refuses to fool itself

[![CI](https://github.com/varunganji19/Trade-Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/varunganji19/Trade-Bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Most trading backtests lie without meaning to: the best of many tries gets
reported, fees are ignored, and the live bot runs different code from the
backtest. This project is a **paper-trading platform for crypto and forex
built to catch all three**, and the evidence of what it proved and disproved
when pointed at its own strategies.

**Headline result: no proven edge — and under the platform's own rule,
nothing is allowed to trade.**

- **Standard book**, 2 years, 7 markets
  ([declaration](experiments/standard_gate.toml)): no strategy's 90%
  profit-factor interval clears 1.0. Three are unproven (best: time-series
  momentum, PF 1.07, interval 0.81–1.37) and two are measured losers
  ([results](experiments/standard_gate.results.json)). The two strategies
  that passed the older, looser gate (PF ≈ 1.55 on ≈ 60 trades each) did
  not survive the larger sample ([RESULTS §1](docs/RESULTS.md)).
- **Fast book** (5-minute bars, **experimental**), 90 days, 21 markets
  ([declaration](experiments/fast_gate.toml),
  [results](experiments/fast_gate.results.json)): every strategy is a
  measured loser except a market maker whose profit vanishes once quotes
  must trade through by 5 bp — an artefact of the fill model, shown by a
  pre-registered study
  ([results](experiments/market_maker_fill_model.results.json)).
- A published foundation model (Kronos, AAAI'26) had to earn a vote like
  any strategy; on BTC 1h it scored **IC −0.075** against a +0.02 hurdle
  and was rejected ([registry](experiments/history.jsonl),
  [RESULTS §2](docs/RESULTS.md)).

Every verdict, interval and study is in [docs/RESULTS.md](docs/RESULTS.md),
and is reproduced by `make evidence` from the pre-registered declarations
in [experiments/](experiments/).

**How it keeps itself honest** (each is code and tests, not a claim):

- **Promotion gate.** A strategy votes only if the *lower* end of a 90%
  bootstrap interval on its out-of-sample profit factor clears 1.0 after
  fees, over 100+ trades in trending-up, trending-down and ranging markets
  ([`bot/promotion.py`](bot/promotion.py): `V2_MIN_TRADES`, `V2_RULE`;
  test `test_rule_v2_promotes_only_proven_strategies`).
- **Pre-registration.** Experiments are declared in git before they run,
  every variant is recorded, and the Deflated Sharpe reads its trial count
  from that registry (`bot/experiments.py`).
- **Live = backtest.** The engine and the backtester run the same decision
  code; `make verify` fails if they ever diverge (`scripts/parity_smoke.py`).
  No LLM or news feed sits in the decision path.
- **Cost realism.** Fees on both legs, taker fees and slippage on market
  fills, maker pricing only where a resting order would fill, gap-aware stops.
- **Overfitting statistics.** Purged cross-validation, probability of
  backtest overfitting (PBO) and the Deflated Sharpe ratio
  (`bot/validation.py`).
- **Self-audit.** The Shadow Account replays journaled trades against the
  bot's own rules and counts the trades that blew through their initial
  stop; the dashboard shows the count for the current journal
  (`python3 main.py shadow`, [`bot/shadow.py`](bot/shadow.py); test
  `test_shadow_behavior_profile_math`).
- **Causality and determinism are tested**: truncating history at bar *i*
  cannot change the bar-*i* signal; identical inputs give identical trades.
  390+ tests ([tests/](tests/)), a parity smoke and
  [CI](.github/workflows/ci.yml) on every push.

Paper trading only. No strategy here is presented as profitable.

| ![Overview — equity, engine control, promotion gate](docs/screenshots/dashboard-overview.png) | ![Evidence — validation verdicts, Kronos IC, purged-CV paths, shadow account](docs/screenshots/dashboard-evidence.png) |
|---|---|
| **Overview** — equity, realized P&L, the engine and which strategies may vote, every decision journaled with its reasoning | **Evidence** — PBO / Deflated Sharpe verdicts, Kronos verdict, purged-CV path returns, shadow-account adherence |

![Portfolio — searchable trade history with strategy attribution](docs/screenshots/dashboard-portfolio.png)

## Five-minute demo

```bash
pip install -r requirements.txt
ALGO_NO_AUTO_RESUME=1 python3 main.py dashboard   # → http://127.0.0.1:8000
```

1. **Overview** — the Engine card's Strategies box is the promotion gate:
   which strategies may vote, and the measured reason the others may not.
2. **Evidence** — the validation verdicts (PBO, Deflated Sharpe), the
   foundation model that measured too weak to earn a vote, and the Shadow
   Account auditing the bot against its own rules.
3. **Strategy Lab** — pick BTC/USDT, 1h, **Compare all**, **Run backtest**:
   every registered strategy on the same real data, after fees and slippage.
4. **Fast book (experimental)** — a separate 5-minute account; each
   strategy's verdict is listed, and none has proven an edge.

A talk track for this path, with likely questions, is in
[docs/DEMO.md](docs/DEMO.md).

`ALGO_NO_AUTO_RESUME=1` keeps the engine stopped until you click Start. A
fresh, consistent record is one click away under **Paper account → Reset**
(the journal is backed up first).


```
 candles (ccxt Binance→Bybit→OKX, yfinance for forex;
          closed bars only, validated + caliber-stamped)
    │
    ▼
 Indicators ──▶ Strategies ──▶ Orchestrator ──▶ RiskManager ──▶ PaperBroker
 (RSI, ATR,     (Turtle,        (regime +        (sizing, caps,   (fees + slippage,
  ADX, VWAP,     Connors,        weighted vote,   kill switch,     OCO brackets,
  EMA, …)        Scalper,        promotion gate)  pause, final     gap-aware fills)
                 Momentum, FX)        ▲           veto)                 │
                                      │                                 ▼
                      Allocator (skfolio risk budget per symbol)   SQLite journal
                                                                        │
      deterministic end to end: no LLM or news feed in the decision     ▼
                                                     Dashboard (FastAPI + Chart.js)
 Offline research: Kronos IC ledger · purged-CV / PBO / Deflated Sharpe ·
                   Shadow Account (journal vs its own rules) · Strategy Lab
```

## Quick start

```bash
# 1. backtest on real exchange data (no account needed)
python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 --strategy turtle_trend
python3 main.py backtest --symbol ETH/USDT --timeframe 5m --days 30 --strategy ensemble --walk-forward

# 1b. pinned window — byte-identical reruns + a provenance manifest
python3 main.py backtest --symbol BTC/USDT --timeframe 1h \
  --start 2025-01-01 --end 2026-01-01 --strategy turtle_trend

# 2. paper trade (one cycle or forever)
python3 main.py run --once
python3 main.py run                 # loops every 60s, journals everything

# 2b. honesty tooling
python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 \
  --strategy turtle_trend --purged-cv      # OOS path distribution + signal IC
python3 main.py validate --symbol BTC/USDT --timeframe 1h --days 365 \
  --strategy turtle_trend --report REPORT.md   # purged-CV, PBO, DSR, MC + rendered report
python3 main.py shadow                     # journal vs its own rules
python3 main.py validate-trades backtest-result.json --trials 20 \
  --regime-market BTC/USDT                 # someone else's backtest (freqtrade export,
                                           # trade CSV): robust / fragile / likely overfit

# 3. dashboard
python3 main.py dashboard           # → http://127.0.0.1:8000
#   (start/stop the engine from the UI; the Evidence tab is the honesty
#    layer, rendered)

# 3a-2. Strategy Lab — pick any crypto/forex pair, apply strategies, backtest
#    (dashboard: the Lab tab; works for BOTH books via a toggle)
python3 main.py dashboard            # → http://127.0.0.1:8000/#lab
#    crypto/forex symbols normalize from aliases ("btcusdt", "eurusd");
#    "Compare all" runs every registered strategy on one fetched frame;
#    async runs poll /api/lab/status; artifacts land in data/results/lab_*.json

# 3b. the fast paper book — EXPERIMENTAL, no proven edge (separate account + dashboard tab)
python3 main.py hft-backtest --symbol BTC/USDT --days 14 \
  --strategy hft_micro_breakout          # 5m bars, perp fee tier
python3 main.py hft-battery             # every strategy x symbol x fee tier
                                        # (descriptive; verdicts: make evidence)
python3 main.py hft-run                 # live 5m paper engine (mode='hft')
python3 main.py hft-status              # ALL fast-book trades, one place
#   (the dashboard's Fast book tab has its own engine controls, equity curve,
#    full fast-book trade history and decision feed)

# 4. other commands
python3 main.py status              # paper-book summary (+ pause state)
python3 main.py pause "note"        # manual halt: new entries only, nothing force-closed
python3 main.py resume              # clear the manual pause
python3 main.py config              # effective settings + where each came from
python3 main.py track-record append # seal finished days of the paper book (docs/TRACK_RECORD.md)
python3 main.py track-record verify # recompute every sealed day from the journal
python3 main.py drift               # promoted strategies' live weeks vs their expected range
make verify                         # tests + lint + live-vs-backtest parity smoke
make evidence                       # rerun both gate declarations: every verdict + the registry
make soak                           # drive the RUNNING dashboard and flag breakdowns
make test / make lint / make battery / make config
python3 -m pytest tests/ -q         # 340+ tests
```

## How the evidence is produced

[docs/METHODOLOGY.md](docs/METHODOLOGY.md) covers the data rules, the
execution and cost model, the risk layer, the promotion gate, the
statistics, the live-equals-backtest checks and the Shadow Account.
[docs/RESULTS.md](docs/RESULTS.md) has every strategy and experiment with
its verdict.

## The strategies

Lineage and design only; whether each one may trade is a measurement, listed
in [docs/RESULTS.md](docs/RESULTS.md) and live on the dashboard.

| Strategy | Lineage | Style | Timeframe |
|---|---|---|---|
| **Turtle Trend** | Donchian / Richard Dennis's Turtles + ADX regime filter (SSRN 6272239) | trend-following breakout, 2×ATR stop, opposite-channel exit | 1h |
| **Connors Mean Reversion** | Larry Connors RSI(2) + EMA(200) trend filter + Chan AR(1)/OU half-life gate | buy deep pullbacks in uptrends while pullbacks are measurably reverting (half-life ≤ 12 bars), snapback exits, 3×ATR stop + time stop | 4h / 1d |
| **TS Momentum** | Momentum papers (SSRN 3345280/3510433/4587697) | long-only absolute momentum: 240-bar return >8% + near 52-week high + EMA200 | 1h / 4h |
| **FX Regime Mean-Rev** | Regime-conditioned FX reversion (SSRN 6087107) | z-score stretch fade with AR(1) half-life regime gate | 1h |
| **VWAP Scalper** | Opening-range-breakout evidence (Zarattini & Aziz 2023, SSRN 4416622) + VWAP benchmark | VWAP reclaim/loss with momentum + volume confirmation, breakeven trail, time stop; optional RVOL filter (off by default) | 15m |
| **Fast book** (separate account, **experimental**) | Carver 2025, Zarattini & Aziz 2023, Avellaneda & Stoikov 2008, Cont, Kukanov & Stoikov 2014 | exhaustion fade, micro-breakout, candle-based market making, order-flow proxy, cross-pair and funding reversion | 5m |

Every number in the Style column is a setting in [bot/params.py](bot/params.py)
(e.g. `turtle_stop_atr = 2.0`, `mr_halflife_max = 12`, `tsmom_lookback = 240`,
`tsmom_min_ret = 0.08`).

The detailed research behind each one is in
[docs/archive/RESEARCH.md](docs/archive/RESEARCH.md) and
[docs/archive/HFT.md](docs/archive/HFT.md).

## Markets

**Crypto and forex, in US dollars.** The standard book trades BTC, ETH and SOL
on 1h (BTC and ETH also on 15m and 4h) plus EUR/USD and GBP/USD on 1h; the
fast book trades five 5m markets with its own capital and fee tier
(`DEFAULT_WATCHLIST` and `HFT_WATCHLIST` in [config.py](config.py)). A
watchlist entry whose kind or timeframe the bot no longer trades is dropped
at load with a printed reason.

## Extras

Kept, tested, and outside the main demo path.

- **Ask the journal (chatbot).** Plain-language answers from the trading
  journal: "how much did you earn?", "why did you buy BTC?". With
  `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set, an LLM phrases free-form
  answers and every dollar figure it quotes is cross-checked against the
  journal; without a key it answers from templates. It never touches a
  trading decision. `python3 main.py chat "explain the connors strategy"`.
- **Kronos.** `bot/kronos_signal.py` wraps
  [Kronos](https://github.com/shiyu-coder/Kronos) (AAAI 2026, MIT), a
  foundation model its authors pre-trained on K-lines from 45+ exchanges. It was given
  the same chance to earn a vote as any strategy and failed (RESULTS.md §2),
  so it runs offline only: `python3 main.py kronos --symbol BTC/USDT --days 60`
  (slow on CPU). It is kept as evidence that a signal has to
  earn its vote. Its torch/transformers dependencies are optional; the bot
  and the test suite run without them.

## Layout

One trade, candle to journal, in five files: `bot/data/__init__.py`
(closed bars) → `bot/engine.py` (the cycle) → `bot/orchestrator.py` (the
vote and the promotion gate) → `bot/positions.py` (risk approval, fill,
management, close — via `bot/risk.py` and `bot/broker.py`) →
`bot/journal/trades.py` (the record). [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
draws each step.

```
config.py            environment, costs, risk, markets (strategy params: bot/params.py)
main.py              CLI parser; the commands live in bot/cli/
experiments/         pre-registered experiment declarations (TOML), their
                     results, and history.jsonl (experiments run earlier)
track_record/        paper.jsonl: the hash chain of sealed paper-book days
                     (created by the first `track-record append`)
docs/
  ARCHITECTURE.md    decision path, risk gates, promotion gate, fill model
  SELF_CHECK.md      ten likely reviewer questions, each answered from the code
  COMPETITION.md     who else does this, sourced, and where they are better
  COMPLIANCE.md      what the project does not do; questions for an adviser
  METHODOLOGY.md     how the evidence is produced
  RESULTS.md         every strategy and experiment with its verdict
  DEMO.md            the 7-minute demo and talk track
  TRACK_RECORD.md    the tamper-evident forward record and its limits
  VALIDATION_SAMPLE.md  the validator run on this repo's own strategies
  ROADMAP.md         the plan from the mentor and VC reviews
  archive/           the round-by-round lab notebook, kept unchanged
bot/
  data/              fetch_history + MarketData; sources.py (ccxt fallback
                     chain, Yahoo), validate.py (OHLCV checks, forming bar),
                     cache.py (parquet cache + provenance manifest)
  indicators.py      Wilder RSI/ATR/ADX, EMA, Donchian, VWAP (session + rolling)
  params.py          every strategy tunable, with the reason for its value
  strategies/        turtle, meanrev, scalper, ts_momentum, fx_regime_meanrev,
                     hft (fast book) — stateless, testable
  orchestrator.py    regime detection, vote, book separation, promotion gate
  promotion.py       the gate: rule v2 verdicts, who may vote, gate state
  evidence_stats.py  bootstrap intervals and regime labels behind the gate
  experiments.py     pre-registration, the runner and the registry
  risk.py            sizing, caps, kill switch, cooldowns, leverage caps
  pause.py           manual "pause all trading" flag file (entries-only halt)
  allocator.py       skfolio inverse-vol / HRP cross-symbol risk budget
  broker.py          paper fills with fees/slippage; OCO brackets, gap-aware fills
  engine.py          the live cycle (lease, run_cycle, health, run_forever)
  positions.py       what a cycle does to each market: restore, mark, approve,
                     fill, manage, close
  backtest.py        event-driven backtester + walk-forward
  validation.py      purged CV, PBO, Deflated Sharpe, Monte Carlo, MinTRL
  validator.py       validate-trades: a backtest's trade list -> a verdict report
  shadow.py          Shadow Account: rule-adherence replay + behavior profile
  track_record.py    seal, verify and render the forward record (read-only)
  drift.py           drift monitor: demote a voter whose live PF leaves its range
  kronos_signal.py   Kronos forecaster and its IC ledger (offline only)
  hft/               the fast (5m, experimental) book: config, fee tiers,
                     cost floors, descriptive harness
  lab.py             Strategy Lab backtests for both books
  journal/           SQLite journal: schema.py, ledger.py (ownership, cash,
                     reconciliation), trades.py, reads.py
  chatbot.py, llm.py the journal chatbot (Extras); the LLM never trades
  dashboard.py       FastAPI app, shared state, static files
  engines.py         start/stop/auto-resume of both books' engines
  api/               routers: standard.py, fast.py, account.py, lab.py,
                     evidence.py, models.py
  static/            index.html + app.css + app.js (the dashboard page)
  cli/               research.py and operate.py (the CLI commands)
  report.py          validate --report renderer
scripts/
  parity_smoke.py    live-vs-backtest parity + causality + book separation
  soak.py            drive the RUNNING dashboard in a loop, flag breakdowns
  pinned_runs.py     pinned windows (byte-identical reruns)
models/kronos/       vendored Kronos model source (MIT; weights via HF Hub)
tests/               340+ tests: causality, determinism, fills, risk, the gate,
                     experiments, ledger, journal, engines, dashboard
run_battery.py       descriptive standard-book battery (no verdicts)
LICENSE              MIT
pyproject.toml       ruff + pytest config (the lint floor CI enforces)
Makefile             setup / test / lint / verify / evidence / battery / ...
CHANGELOG.md         dated changes
```

## Environment

- Python 3.11+ — `pip install -r requirements.txt`
  (core: `pandas numpy ccxt yfinance fastapi uvicorn requests pyarrow skfolio`;
  Kronos extras: `torch transformers huggingface_hub einops tqdm`)
- Vendor the Kronos model source once: `git clone https://github.com/shiyu-coder/Kronos models/kronos`
- Network access for market data (no API keys required)
- Optional: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (free-form chatbot answers), `OPENAI_BASE_URL`
- `PAPER_CAPITAL`, `LIVE_INTERVAL`, `BOT_DB_PATH`, `PORTFOLIO_METHOD`,
  `PORTFOLIO_ALLOC`, `MAKER_PRICING` env overrides
- `DASHBOARD_TOKEN` — optional: set it to require `Authorization: Bearer <token>`
  on every dashboard API request (default off; the dashboard binds to 127.0.0.1
  only). The page shell itself loads unguarded and the browser UI prompts for
  the token once, storing it in localStorage.
- `ALGO_NO_AUTO_RESUME=1` — stop the engine auto-resuming on dashboard boot.
  The engine auto-resumes on dashboard restart if it was running when the last
  session ended (a manual stop stays stopped); the UI shows a toast the moment
  a boot-resume happens, so trading never silently begins.
- Without torch or the vendored model, the bot runs normally — Kronos reports
  "unavailable" and never touches the vote

## How this was built

- **AI coding assistants were used** to write much of the code and the
  documents in this repository.
- **No change is trusted on the assistant's word.** Each one is a separate
  commit that has passed `make verify`: the full test suite, ruff and
  eslint, and the live-vs-backtest parity smoke
  (`scripts/parity_smoke.py`). UI changes are checked in a browser at
  desktop and phone widths in both themes.
- **Claims about strategies are measured, not argued.** Experiments are
  declared in git before they run (`experiments/*.toml`), their results are
  written next to the declaration, and `make evidence` regenerates every
  verdict from scratch.
- **Decisions are recorded where they were taken:** the positioning, the
  stricter promotion bar, the licence and the testnet-only rule are in
  [docs/ROADMAP.md](docs/ROADMAP.md); the reasoning behind individual changes
  is in [CHANGELOG.md](CHANGELOG.md) and the commit history.
- [docs/SELF_CHECK.md](docs/SELF_CHECK.md) lists ten questions a reviewer is
  likely to ask, each answered with the code and the test that back it.

## Status & scope

Paper trading only. No strategy here is presented as profitable. The
`MarketSpec` and broker interfaces are where a real exchange adapter would
slot in; the roadmap builds readiness on the Binance **testnet** first, and
any real-money step is a separate decision for the owner, not something this
repo does.
