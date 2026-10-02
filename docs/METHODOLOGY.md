# Methodology: how the evidence is produced

*What every number in [RESULTS.md](RESULTS.md) rests on: the data, the
execution and cost model, the risk layer, the promotion gate, the statistics,
and the checks that keep live trading and backtests identical. Each rule
names the code that enforces it.*

## 1. Data

- **Sources.** Crypto from Binance through ccxt, with a fallback chain
  (Binance → Bybit → OKX) so one exchange being down never stops the engine;
  forex from Yahoo Finance (`bot/data.py`).
- **Closed bars only.** The still-forming candle is dropped before the
  engine or the backtester sees it (unit-tested).
- **Validation.** Every OHLCV frame is checked before indicators see it:
  positive prices, high/low bracket the body, sorted unique index, a
  weekend-aware gap guard. The price caliber (raw or adjusted) is stamped on
  every frame.
- **Reproducibility.** Backtest data is disk-cached per day, and
  `--start/--end` pins a window; pinned runs are byte-identical and write a
  provenance manifest with SHA-256 checksums (Evidence tab → Data
  provenance; `scripts/pinned_runs.py`).

## 2. Execution and costs

The same `PaperBroker` fills both the live paper engine and the backtester
(`bot/broker.py`).

- **Timing.** A decision on bar *i*'s close fills at bar *i+1*'s open: that
  close was not tradable when the decision was made.
- **Fees on both legs** of every trade, plus slippage on market fills.
  Per-trade `fees` report the full round trip.

  | Book / tier | Maker | Taker | Slippage | Taker round trip |
  |---|---|---|---|---|
  | Standard, crypto | 10 bp | 10 bp | 5 bp | 30 bp |
  | Standard, forex | 1 bp | 2 bp | 1 bp | 6 bp |
  | Fast book, `perp` (default) | 2 bp | 5 bp | 3 bp | 16 bp |
  | Fast book, `spot` | 10 bp | 10 bp | 5 bp | 30 bp |

- **OCO brackets.** Stop and target are linked. If both fall inside one bar,
  the stop wins (the intrabar path is unknowable, so assume the worse). A gap
  through the stop fills at the bar's open: you get the market, not the
  level. A stop fill is never better than the stop (unit-tested).
- **Exit ordering.** A strategy exit decided at bar *i* fills at bar *i+1*'s
  open before that bar is scanned for stop or target.
- **Maker fills** are used only where a resting order would fill: bracket
  take-profits, and fast-book entries that the price trades through.
- **Cost floors.** Each fast-book strategy refuses a setup whose stop cannot
  pay its own round trip; the floors are derived from the active fee tier
  (`bot/hft/__init__.py`), so switching tier moves every floor together.

## 3. Risk layer

Applied identically in the backtester and the live engine (`bot/risk.py`,
`bot/allocator.py`).

- 1% of equity risked per trade, sized off the ATR stop distance.
- Portfolio allocation (skfolio inverse volatility by default, HRP optional)
  divides the risk budget across symbols, so correlated majors cannot each
  take a full 1%.
- At most 25% notional per position and 4 concurrent positions; total open
  notional across both books plus the next entry may not exceed 1.0× equity.
- Minimum confidence 0.55; stops wider than 10% of price and targets below
  1.2R are refused.
- One position per symbol across timeframes; each (symbol, timeframe)
  manages only its own position, so a 4h bar can never stop out a 1h trade.
- Positions, stops and cash survive restarts (journaled state).

Two controls can block **new entries**; neither ever force-closes a
position, whose stops, targets and strategy exits keep running:

- **Daily kill switch.** A 3% fall below start-of-day equity blocks entries
  for the rest of the UTC day; it resets by itself. In backtests it follows
  simulated bar time, so the same rule runs in both.
- **Manual pause.** `python3 main.py pause [note]` / `resume`, or the
  dashboard's Pause button. The flag is `trading_paused.json` next to the
  journal; an unreadable flag is treated as paused, so a failure leans
  toward not trading.

## 4. The decision path and the promotion gate

The orchestrator (`bot/orchestrator.py`) classifies each market's regime
(ADX and EMA structure) and runs the strategies registered for that
timeframe. Each standard strategy ships on its own validated timeframe and
the ranges do not overlap, so every market has one strategy owner today; the
regime-weighted blend and conflict guard exist but only engage if two
strategies ever share a timeframe. The two books both trade 5m bars and are
kept apart by `BaseStrategy.book`.

**No LLM and no news feed sit in the decision path.** Both used to; the
backtester could replay neither, so live and backtest were running different
code. They were removed on 2026-09-19.

**The promotion gate** (`bot/promotion.py`) decides which strategies may
vote. `make evidence` runs each book's gate declaration (§9): every
strategy walks forward through 4 folds after full costs, and only the
**out-of-sample** trades are pooled into its verdict (rule v2):

| State | Rule | Effect |
|---|---|---|
| promoted | ≥ 100 OOS trades, ≥ 10 in each of 3 regimes, and the **lower** end of the 90% PF interval ≥ 1.0 | votes |
| probation | anything else that is not demoted | does **not** vote |
| demoted | ≥ 30 trades and the **upper** end of the interval < 1.0 | does not vote |

- **Interval:** a 90% circular block bootstrap over the time-ordered trade
  P&Ls (blocks of about n^(1/3) trades keep losing streaks intact; fixed
  seed, so verdicts are reproducible) — `bot/evidence_stats.py`.
- **Regimes:** each trade is labelled by the market's regime on the day it
  was opened: trend up / trend down (ADX(14) ≥ 20, sign of the 20-day change
  of the 200-day SMA) or range (ADX < 20), from daily bars and shifted one
  day so a label never sees its own day.
- A verdict file written under the older rule (median fold PF over ≥ 30
  trades, probation voting) keeps its old meaning but shows as "STALE — OLD
  RULE" until regenerated. A missing file shows as "promotion gate:
  UNMEASURED", never as a silent pass.
- Candidates (`CANDIDATE_STRATEGIES`) are measured but never vote, whatever
  their verdict; graduating one is a code change.
- `run_battery.py` and `make hft-battery` are descriptive batteries only;
  they never write verdicts, so a quick run cannot replace the gate's
  evidence.

## 5. Statistics

`bot/validation.py`, run with `python3 main.py validate` (report rendered
to the Evidence tab and optionally to Markdown).

- **Walk-forward.** Each fold trades only its out-of-sample slice; per-fold
  results are reported, never only the aggregate.
- **Purged combinatorial cross-validation** (skfolio
  `CombinatorialPurgedCV`): a distribution of out-of-sample paths instead of
  one number. Trades whose holding period spans a path boundary are purged;
  paths that never traded are reported but excluded from the statistics.
- **Probability of backtest overfitting (PBO)**, from combinatorially
  symmetric cross-validation across the configurations tried.
- **Deflated Sharpe ratio**, adjusted for the number of trials and for skew
  and kurtosis. `validate` reads the trials from the experiment registry
  (§9): one trial per distinct configuration per market, so re-running the
  same settings on newer data is more evidence, not another lottery ticket.
  `--trial-sharpes` overrides it.
- **Monte Carlo** resampling of trade order (5th-percentile terminal equity)
  and **minimum track-record length**.
- **Signal IC.** Rank correlation between a signal's conviction and the
  forward return, with an overlap-adjusted t-statistic.

## 6. Live equals backtest

- **Parity smoke** (`scripts/parity_smoke.py`, part of `make verify`): the
  same bars through the live engine's orchestrator and the backtester's
  must produce identical decisions, for both books; fast-book strategies
  must never vote on the standard book.
- **Causality** (`test_strategies_never_read_future`): truncating history at
  bar *i* cannot change the bar-*i* signal, for every strategy.
- **Determinism**: identical inputs give identical trades, equity curve and
  statistics.
- **Soak** (`make soak`): drives a running dashboard in a loop and flags
  breakdowns.

## 7. Self-audit: the Shadow Account

`python3 main.py shadow` (`bot/shadow.py`) replays every journaled trade
against its owning strategy's exit rules on the same bars and reports rule
adherence (on rule, late, rule break), a behaviour profile (R-multiples,
disposition effect, trades that blew through their initial stop), and the
journal's P&L against the pure-strategy backtest over the same window. It
audits the bot's own paper record by default; the seeded replay rows
(`mode='demo'`) are only audited with `--include-demo`.

## 8. Signals must earn a vote: the Kronos gate

The same rule applies to model-based signals (`bot/kronos_signal.py`). A
forecaster starts as a tracked non-voter; a rolling rank-IC ledger scores
each forecast against what happened, and it could join the vote only after
60+ resolved forecasts with IC ≥ 0.02, judged per market. Kronos failed this
gate (RESULTS.md §2) and runs offline only (`python3 main.py kronos`).

## 9. Experiments: pre-registration and the registry

Every variant ever tried is a draw from the same lottery, so the platform
counts them (`bot/experiments.py`).

- **Declared before they run.** An experiment is a small TOML file in
  [`experiments/`](../experiments/): hypothesis, strategies, markets, window,
  folds, an optional holdout and the parameter variants. The runner refuses
  a declaration that is not committed to git or has uncommitted edits, so
  the plan provably predates its results, and it can run nothing that is
  not declared.
- **Two kinds.** A *gate* measures the shipped settings and writes the
  book's promotion verdicts. A *study* compares variants — on a selection
  window of walk-forward folds, judged on a later holdout it never saw — and
  never touches the gate.
- **Results next to the plan.** Each run writes `<id>.results.json` beside
  its declaration (pooled and per-market out-of-sample statistics, intervals,
  regime counts, holdout P&L, the declaration's hash and commit).
- **The registry** (`experiments.jsonl` beside the journal) is rebuilt from
  those files plus [`experiments/history.jsonl`](../experiments/history.jsonl),
  which records experiments run before the registry existed, each citing its
  archived source. The Evidence tab's Experiment log reads the same data.

`make evidence` reruns both gate declarations and rebuilds every verdict,
interval and the registry from scratch. It writes beside the journal, so
point `BOT_DB_PATH` at a scratch directory to keep a test run away from a
live journal. Run alone: `python3 main.py experiment run <file.toml>`,
`python3 main.py experiment list`.


## 10. The books reconcile with their own history

Every dashboard poll checks each paper book's ledger
(`Journal.ledger_check`): the paper broker settles like a margin account, so
independent of any market price

    journal cash = starting capital + net deposits + realized P&L
                   - entry fees of still-open trades

and equity is that cash plus unrealized P&L. A gap beyond rounding shows a
"Ledger inconsistent" banner naming each term, so headline numbers that
disagree with each other can never pass unnoticed.

## 11. A promoted strategy keeps its vote only while live results agree

The gate's verdict is a backtest; edges decay. The drift monitor
(`bot/drift.py`) compares every strategy the gate promotes (rule v2, so it
has a measured interval) with that interval, its **expected range**.

- Watching starts the first time an engine cycle sees the strategy promoted.
- At each completed UTC week (Monday 00:00) it takes the strategy's last 50
  closed live trades in its own book and computes their profit factor. The
  week is *below* when there are at least 20 trades and the PF is under the
  lower end of the interval.
- **Three consecutive weeks below → demoted automatically.** The strategy
  stops voting at the next cycle, the Strategies box lists it as
  "demoted — drift", and a banner names it for a week.
- The demotion lives in `results/drift_<book>.json`, beside the verdicts
  but not in them, so `make evidence` cannot quietly hand the vote back.
  Only `python3 main.py drift clear NAME --book BOOK` does.
  `python3 main.py drift` shows what is watched and each recent week.

The rule demotes faster than the gate promotes on purpose: a wrongly
demoted strategy only stops trading, while a decayed one that keeps voting
loses money. Live trades are credited to the strategy that set the trade's
stop, so in an ensemble a strategy is judged on the trades it led. Nothing
was promoted when this was written (2026-10-02), so the monitor was watching
nothing.
