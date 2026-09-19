# Changelog

## [Unreleased] — v1 adds: telemetry, the promotion gate, a cycle budget

The instrumentation half of the post-v1 pass. Every item here exists because
something failed silently this week.

- **Veto telemetry.** RiskManager.approve refused 100% of the fast book's
  entries for a week and the only trace was a log line, so nothing counted
  them and the dashboard showed a healthy engine with an empty trade table.
  RiskDecision now carries a machine-readable `category` (all 19 refusal
  sites, AST-pinned), the engine aggregates them, the cycle line prints them,
  and both pages render "N approved / M attempted" with the blockers ranked.
  A book that has attempted 10 entries and approved none says so in its
  health note, naming the top blocker.
- **The promotion gate** (bot/promotion.py). `hft_micro_breakout` carried the
  largest vote weight in the fast book because it descends from a published
  result; measured, it is the worst cell on the board (median PF 0.39 over 81
  trades). The battery now writes a verdict the orchestrator reads — promoted
  / probation / demoted — and a demoted strategy is skipped before
  evaluation. A missing verdicts file is permissive: the gate can only take a
  vote away on evidence.
- **Cycle budget.** A cycle that outruns its own interval is a latency bug
  that only appears in production (the inline forecast model made a 2s cycle
  take minutes). Every cycle is timed, slow ones are counted, and the health
  note says "cycle took 41.0s against a 10s interval".
- 268 tests.

## [Unreleased] — v1 cut: the fast book moves to 5m, three features retired

The post-v1 cut, decided on measurement rather than taste.

**The fast book trades 5m, not 1m.** 1m was chosen for speed and 1m is where
the cost wall wins: a 16bp modeled round trip against a 5-8bp 1m ATR means a
1-ATR stop cannot pay its own fees, so entries were vetoed as dust and the
book traded ZERO times in a week. Measured at 5m over 14 days (perp tier):
the book trades, and nothing has earned a vote yet — `hft_ofi_momentum` 1.26
on BTC, `hft_market_maker` 1.08 on ETH/BTC, `hft_exhaustion_fade` 1.05 on
BTC, while `hft_micro_breakout` — the strategy carrying the most vote weight
— is the worst cell on the board at PF 0.39/0.43. Vote weights came from
research lineage, not results; that is what the promotion gate is for.

- Every bar-denominated window was re-scaled to keep its wall-clock meaning
  (a 45-bar fade stop was 45 min at 1m; it is 9 bars at 5m). Cadence 2s ->
  10s, and the data-cache TTL now follows the cadence instead of a constant.
- **The books are separated EXPLICITLY now.** They were separated by
  timeframe alone, so moving the fast book to 5m silently made fast
  strategies eligible on the standard book's 5m specs and vice versa.
  `BaseStrategy.book` ("standard" | "fast") is the boundary; the
  orchestrator, the backtester, the Lab and the dashboard badge all filter
  on it. 1m left VALID_TIMEFRAMES — no strategy owns it, and an offerable
  timeframe that nothing trades is a spec that can only ever HOLD.
- **Triangular-arb monitor removed** (code, config, engine hooks, dashboard
  rows, tests): 0 opportunities in 4,319 aligned bars, max edge 11bp against
  a 24bp cost. The question is answered and the answer is in HFT.md.
- **Kronos left the live loop.** It never earned a vote, one 1m forecast cost
  ~41s of CPU against a cycle budget of seconds, and two books forecasting at
  once aborted the process on Metal. It is now an offline research job
  (`main.py kronos` writes the ledger, the Evidence tab reads it) with its
  promotion gate intact. The engine and orchestrator carry no Kronos
  coupling at all — pinned by an AST test, not a grep.
- `hft_market_maker` demoted to CANDIDATE: a maker with no order book cannot
  see its own adverse selection, so its near-breakeven PF is not evidence.

## [Unreleased] — running both engines no longer kills the process

**Starting the standard and HFT books together aborted the app.** Not a
Python exception — a hard process abort:
`failed assertion _status < MTLCommandBufferStatusCommitted` from
IOGPUMetalCommandBuffer. The vendored KronosPredictor auto-selects the MPS
(Metal) backend on Apple Silicon, each TradingEngine built its OWN Kronos
stack, and Metal aborts the process when two threads submit work to it.

Underneath that was a latency problem that made it inevitable. Measured
(Kronos-small, CPU, 2026-09-19): one forecast path costs 0.5s at horizon 24
and 1.4s at horizon 60, and the paths run SEQUENTIALLY — 41s for a single 1m
symbol at 30 paths. The engine called `evaluate()` inline inside the cycle,
under the cycle lock, so the HFT book needed ~205s for a cycle designed
around a 1-3s bar-close-to-fill budget, the standard book ~180s for a 60s
interval, and two books together pegged every core without either finishing
a cycle. (The earlier 1m horizon bug had masked this: every 1m forecast
raised instantly, so Kronos cost nothing on the HFT book.)

- `KronosForecastService`: ONE worker thread per process, shared by both
  books, with a bounded per-market cache and queue coalescing. The trading
  cycle now only READS a cached forecast — it never blocks on inference.
- One model per process (`_SHARED`), and load + inference under one lock, so
  Metal only ever sees a single thread.
- Sub-5m books sample 8 forecast paths instead of 30 (the paths are a linear
  cost dial; the forecast is off-cycle either way).
- A cached forecast is served for at most `evaluate_every_bars` bars past its
  anchor bar — a forecast from an hour ago is not evidence about now.
- Per-BOOK IC ledgers (`kronos_ic_<mode>.json`): both engines wrote one file,
  so two trackers overwrote each other and the promotion IC mixed 1m
  forecasts with 1h ones. The Evidence tab reads both, plus the legacy file.
- Retiring a book drops its queued forecasts and stops nothing else.
- Verified: both books running together, 13% CPU, 828MB, HFT at 14 cycles and
  the standard book cycling, no errors. Before: 113% CPU, 1.0GB, cycle 0 on
  both, dead after ~6 minutes.
- 294 tests.

## [Unreleased] — the HFT book actually trades; live engine controls

**The 1m book had produced 15 entry decisions and 0 trades.** Two correct
pieces with nothing connecting them: `RiskManager.approve` refuses a stop
tighter than the modeled round trip ("tiny stop (dust)"), and the 1m
strategies sized stops off raw ATR with no reference to that number. On a
quiet tape (BTC ATR ~5-8bp vs a 16bp perp round trip) the market maker
quoted 2bp half-widths -> 6bp stop -> vetoed, every time, silently.

- Cost floors are now DERIVED from the book's fee tier in `build_hft_config`
  and carried in `StrategyParams`, so the engine and the backtester read the
  same numbers and `HFT_FEE_TIER=spot` moves them together (16 -> 30bp).
  Each 1m strategy refuses a setup it cannot pay for itself, with a readable
  rationale. Market maker, BTC 3d perp: trades 316 -> 63, win rate
  22% -> 70%, PF 0.11 -> 0.57 (still < 1 — honest trading, not a new edge).
- `hft_ofi_momentum` (Cont/Kukanov/Stoikov order-flow imbalance) added as a
  measured CANDIDATE: registered for the Lab and the battery, skipped by the
  live orchestrator. It did not beat the incumbents (PF 0.32/0.68), so it
  does not get a vote — same evidence standard Kronos lives under.
- **Engine control: the interval is changeable while the engine runs.** Both
  loops re-read their cadence every cycle (it was captured at thread start),
  `POST /api/engine/interval` + `/api/hft/engine/interval` set it, and the UI
  select is no longer disabled while running. A stopped engine now reports
  the CHOSEN cadence instead of the config default.
- **HFT tab:** a live 1m price chart (close + EMA20, `GET /api/hft/candles`,
  the same frames the engine decides on) — the page could previously only
  plot a flat equity line, so a running engine looked like a dead market.
  The strategy filter is seeded from the registry instead of from the trade
  history, so it is populated on an empty book. The HFT engine card gained
  its own interval select.
- 289 tests.

## [Unreleased] — Kronos horizon policy for sub-5m books

- **Kronos was a permanent silent no-op on the whole HFT book.** The
  per-timeframe horizon normalized to ~1 day of bars, i.e. 1440 bars on a 1m
  book — but the vendored `KronosPredictor` is autoregressive over its own
  context and generates at most `max_context` (512) steps, so every 1m
  forecast raised `Shape of passed values is (512, 6), indices imply
  (1440, 6)` and `evaluate()` swallowed it into `last_error`.
- The horizon policy now lives in `bot/kronos_signal.py`
  (`kronos_horizon` / `validate_horizon_policy`): ~1 day ahead on 5m and
  slower, **1 hour ahead (60 bars) on sub-5m books** — matched to the HFT
  book's own 45-60 bar time stops rather than a day it never holds through,
  and well inside the predictor's reach. The IC ledger resolves on the same
  number, so Kronos is scored on the horizon it was asked for.
- An unproducible horizon now fails **loudly at config time**:
  `TradingEngine._init_kronos` validates the policy against the predictor's
  `max_context` outside the degrade-gracefully path, `evaluate()` raises
  instead of hiding it in `last_error`, and `main.py kronos --horizon`
  rejects out-of-range values up front.
- 281/281 tests green (3 new Kronos horizon regressions).

## [1.4.3] 2026-09-13 — final audit: security, docs truth, live GUI verification

Three more audit passes (security, docs-vs-code truth, and the first LIVE
browser test of the dashboard) plus fixes for everything they found.
191/191 tests green; dashboard exercised end-to-end in a real browser
(Lab backtest run, HFT book toggle, theme switch — screenshots verified).

- **Security (MED)**: the documented `.env` workflow never loaded the file —
  a DASHBOARD_TOKEN placed in `.env` silently left auth OFF. main.py now
  loads `.env` (dependency-free parser, existing env wins) before config.
- **Security (LOW)**: RSS downloads abort past the 2MB cap instead of
  buffering the whole body first; the retired `testserver` hostname is no
  longer in the TrustedHost allowlist (tests pin 127.0.0.1 and now assert
  testserver is refused); non-ASCII token probes return 401 not 500
  (byte compare_digest).
- **GUI bugs caught by the live browser test**: the `hidden` attribute was
  beaten by any display-setting CSS class (global `[hidden] guard; the Lab
  fee-tier selector showed on the Standard book); the Lab status pill kept
  stale "fetching" text after a run completed; switching the Lab to the HFT
  book on 5m produced an empty strategy menu (HFT book is now 1m-only in
  the Lab — its strategies register 1m).
- **Docs truth pass**: HFT.md's stale 20s cadence claims -> 2s; README test
  counts 188 -> 191; README/RESEARCH.md "disjoint/dormant orchestrator"
  story updated (the HFT trio votes live; ts_momentum/fxmr evaluate on
  1h/4h); README forex-universe wording corrected (SOL is 1h-only); the
  two Milestone-C strategies added to the strategy tables; Layout map
  gained lab.py/calendar.py/scripts; MILESTONES C1 "1d bars" -> "1h/4h";
  CACHE_FRESHNESS_HOURS documented in .env.example; pyproject registers
  bot.hft.
- **Hygiene**: data/hft_engine_state.json + data/market_mode.json +
  nvidia_test.py gitignored (market_mode untracked — runtime state;
  manifest.json stays tracked deliberately as fetch provenance); the stale
  design-system/ folder removed; engine.run_forever sleeps in 0.25s slices
  (Ctrl-C responsive; the HFT wake-granularity claim now true on the CLI
  path too).

## [1.4.2] 2026-09-13 — dead-code / duplicate-logic / complexity cleanup

Three parallel audits (core modules, dashboard, CLI/tests/config) plus
vulture; every finding grep-verified before fixing. 191/191 tests green.

- **Fixed real UI bugs the audit surfaced**: `.stat-grid` was a typo of
  `.stats-grid` (HFT and Lab stat cards rendered stacked, not gridded);
  `.chart-wrap` had no CSS rule (HFT/Lab charts fell back to ~150px
  default height — now 260px); the HFT Stop button used a nonexistent
  `danger` class (now `btn-danger`); a stray duplicate `</section>` tag;
  the Lab result re-rendered and re-fired its completion toast on EVERY
  4s poll (now renders once per run); Evidence charts kept stale colors
  after a theme switch (now rebuilt on next visit); `/api/engine/status`
  reported the configured interval instead of the running engine's.
- **Dead code removed**: `StrategyParams.hft_mm_sigma_window` (zero
  readers), `RiskManager.current_drawdown` (write-only),
  `lab.sorted_strategies_for` (forwarding wrapper), the unused
  `use_confidence` branch in `validation.signal_ic_series`, unused `book`
  params on `lab.days_cap/default_days`, dead "30m" entry in
  TIMEFRAME_SECONDS, `_watchlist_to_dicts` (≡ `MarketSpec.to_dict`),
  redundant re-imports in main.py/scripts, dead toast condition, unused
  JS variable, two shadowing local time imports, orphaned element ids.
- **Duplicates unified**: TRIANGULAR_LEGS (4 hardcoded copies -> one
  constant), true-range formula (2 copies in indicators.py -> one
  `_true_range`), the three equity-chart builders (-> one
  `buildLineChart` factory), strategy-filter sync / stat cards /
  decision rows / P&L bars renderers (copy-paste blocks -> shared JS
  helpers), one Escape-key handler instead of two.
- **Simplification**: the allocator no longer builds the unused aligned
  returns matrix on the default inverse-vol path (was rebuilt and
  discarded every engine cycle / backtest bar); `/api/lab/status` is no
  longer double-polled.
- **Left as-is, deliberately**: the engine-book spawn/state mirrors in
  dashboard.py (documented, concurrency-sensitive), Journal.stats vs
  BTResult.stats arithmetic (different input shapes/keys), scalper
  long/short mirrors (the asymmetry is the point), run_battery DAYS
  future entries.
- Lint now covers scripts/ (Makefile + CI) — its one unused import was
  the reason it was excluded. Makefile .PHONY: phantom `report` removed.

## [1.4.1] 2026-09-13 — UI pass + HFT latency minimization

- **Layout**: the Strategy Lab is two-column — equity chart LEFT, the
  stock picker RIGHT; scrolling down puts the trade history on the right
  and a new "how much each strategy made" P&L bar card on the left (best
  on top, single-strategy runs show their row). Single column under 980px.
- **Engine start/stop moved into the nav bar** — reachable from every tab
  (same button ids, so all existing logic is unchanged); the Overview card
  keeps the interval selector and pause button.
- **Theme**: the grayish dark palette is gone — dark is now TRUE black
  (one dark theme; a stored "black" preference maps to it).
- **HFT latency package** (it's paper — poll as fast as the exchange
  allows): default poll 20s -> **2s** (API floor 5s -> 1s), data-cache TTL
  override 2s (was 30s on 1m), 0.25s loop wake, and a new-bar gate so the
  same closed candle is never re-decided (applies per market AND the
  TRI-ETH monitor; the standard book keeps its behavior). End-to-end:
  ~1-3s from bar close to a paper fill. Measured and documented in
  HFT.md's latency budget table.
- Candlestick charts: possible (TradingView lightweight-charts or Chart.js
  financial plugin over the already-fetched OHLCV) — noted, not implemented.
- 188 -> 191 tests (latency profile, new-bar gate, monitor gate).

## [1.4.0] 2026-09-13 — the Strategy Lab (pick a stock, apply strategies, backtest)

A new dashboard tab serving BOTH books (standard forex+crypto+NSE and the
HFT book): pick any stock/pair — aliases normalize per kind ("BTCUSDT" ->
"BTC/USDT", "EURUSD" -> "EURUSD=X", "RELIANCE" -> "RELIANCE.NS", "NIFTY" ->
"^NSEI") — and apply every strategy REGISTERED for the chosen timeframe
(derived from the registry), or "ALL strategies" for a comparison run on one
fetched frame.

- **Async runs**: POST /api/lab/run spawns a worker thread (one at a time;
  a second request is refused), the UI polls /api/lab/status at 1.5s,
  results land in data/results/lab_*.json. Pure backtest — own
  broker/risk per run, zero journal writes, zero engine interference.
- **Honest guardrails**: yfinance history caps enforced with a visible note
  (1m forex/india = 7d, 5m/15m = 60d), per-timeframe lab compute caps,
  warmup sized per book (220 standard / 400 on 1m), kind-aware cost stacks
  (the NSE regulatory stack rides along automatically).
- **Orchestrator vote fix (regression-tested)**: FLAT strategies are
  abstentions — their weight no longer enters the vote denominator. Under
  the old math, any timeframe with several registered strategies diluted a
  lone directional signal into permanent HOLD (the HFT 1m ensemble never
  traded; measured: 0 trades before the fix, 326 after on the same window).
- **HFT market maker gate**: quotes only near the mean (within 1 ATR of
  EMA20) — quoting into a runaway drift is how MMs get run over; also stops
  the strategy emitting a signal on every bar. Measured: ETH 1m market_maker
  improved -4.65% -> -2.35% on the same 3-day window.
- Endpoints /api/lab/{run,status,meta}; Lab tab UI with suggestion chips,
  filtered timeframe/strategy menus, comparison table, equity chart, exit
  histogram. 182 -> 188 tests.

## [1.3.0] 2026-09-13 — the high-frequency paper book (HFT)

The separate high-frequency account: 1-minute bars, maker-fill simulation,
its own fee tiers, capital, risk dials, dashboard page and journal record
(mode='hft') — the standard book is untouched. Research scraped via the
agent-reach channels + SSRN/arXiv/GitHub (HFT.md carries the citations and
the fee math).

- **Strategies** (bot/strategies/hft.py, 1m only): `hft_market_maker`
  (Avellaneda-Stoikov-inspired maker quotes, vol-scaled width, drift skew),
  `hft_exhaustion_fade` (Carver 4-8min reversion + 3x volume-spike +
  CLV capitulation filter, maker entry at the exhaustion close),
  `hft_micro_breakout` (Zarattini-Aziz ORB analogue, 2R target, ATR floor),
  and the `hft_triangular_arb` monitor (ETH/USDT x ETH/BTC x BTC/USDT).
- **Maker/limit entries are first-class**: `Signal.limit_price` -> resting
  orders in BOTH the backtester and the live engine (one shared fill model
  in broker.limit_fill_price: touch/gap/penetration semantics, expiry,
  maker fee, no slippage). Journal-first entries happen only AT fill.
- **Fee-tier harness**: perp (maker 2bp/taker 5bp) vs spot (10bp/10bp)
  scenarios per cell; `main.py hft-battery` writes the measured matrix to
  data/results/hft_battery.json. First run: every cell net-negative after
  costs (the documented cost wall), triangular monitor fired ZERO times
  (max mispricing 8.3bp vs 24bp 3-leg cost — the literature reproduced).
- **Book isolation**: journal `open_trades()` is mode-filtered (a standard-
  book OPEN row can never restore into the HFT broker, regression-tested);
  HFT risk dials (0.5% risk, -2% kill switch, 0.3 reward floor for inverted
  MM brackets) ride the HFT config; the HFT engine auto-resumes like the
  standard one (hft_engine_state.json, same ALGO_NO_AUTO_RESUME escape).
- **Dashboard HFT tab**: separate engine start/stop, HFT equity curve, ALL
  high-frequency trades in one table (strategy filter), decision feed
  including the TRI-ETH arb monitor. Endpoints /api/hft/*.
- **CLI**: hft-backtest (--triangular, --fee-tier, pinned windows),
  hft-run, hft-status, hft-battery. India 1m is backtestable (kind-aware
  costs); the live HFT book stays USD-only (crypto + forex).
- **1m timeframe** wired through data (ccxt + yfinance 7d cap), validation,
  watchlists and bars_per_year. 169 -> 182 tests.

BACKTESTS.md is the real lab notebook (six measured rounds, negative results
included). This file maps that history onto the repo, newest first.

## [1.2.0] — 2026-09-10 — India market mode: NSE universe + the forex ↔ india toggle

Wave B1 added the `india` market kind (NSE cash equities + Nifty 50 via
yfinance, a regulatory cost stack — delivery STT both legs, 0.03% standing
brokerage, exchange/SEBI/stamp/GST ≈ 0.29% round trip, Zerodha-verified —
whole-share sizing, an NSE session gate, and a persisted single market mode:
`data/market_mode.json`, with `data/watchlist.json` kept in lockstep as derived
state; one active book at a time because the USD and INR books cannot mix).
Wave B2 (this pass) put the mode in the operator's hands:

- **Dashboard market toggle** (Overview tab, next to the engine controls):
  On = Forex (crypto + forex), Off = India (NSE). `GET/POST /api/market/mode`
  switches the persisted mode, rewrites the watchlist and hot-installs the new
  universe into the running engine's CONFIG; `/api/engine/status` and
  `/api/stats` report `market_mode` so the blue market banner (the pause-banner
  pattern) always shows the active universe — a restart can never silently
  flip markets. CLI equivalent: `python3 main.py market [--mode forex|india]`.
- **The orphan guard** (the safety core): a switch while paper positions are
  open (checked in BOTH the live engine and the journal's OPEN rows — a stale
  engine process could still hold rows) is refused with 409 +
  `requires_confirm`; only an explicit `confirm_close_positions` closes every
  position first — via the engine's own `close_manual` path when one is live,
  via a journal close at the entry mark with the conservative fee estimate
  when none is — and only then flips the mode. If anything fails mid-close
  the mode is NOT switched. The UI's first click only opens a plain-language
  confirmation dialog; nothing is force-closed by one click.
- **First pinned NSE acceptance runs** (BACKTESTS.md, `data/results/india_90d/`):
  turtle_trend on 90d of 1h NSE bars — 18 trades across the five equity books,
  net −0.55% average, four of five books negative. Honest verdict recorded:
  cost-dominated at delivery rates, no edge claimed; this is the baseline
  Milestone C's India momentum strategy must beat. Two data caveats pinned:
  ^NSEI has zero volume on Yahoo (volume-gated strategies must not run on the
  index), and the 4h books deviate to ~180d because 90d of 4h NSE bars cannot
  clear the 220-bar warmup — the 4h/1d India books remain essentially
  unmeasured.
- v1 scope stated in README: NSE cash/index equities only, NO options/F&O
  (no options infrastructure exists — deliberately out of scope); journal
  rows keep no market-mode column (old-market trades stay visible in history,
  labeled honestly on the Portfolio tab) — flagged for a later schema pass.
- 147 → 151 tests (four market-toggle tests incl. the orphan-guard non-vacuity
  proof: guard stripped in place → both guard tests fail → re-applied); ruff
  zero.

## [1.1.1] — 2026-09-08 — verification-gap pass

Re-audit of the working tree against the (externally re-derived) JUDGE_REPORT
flaw list confirmed every Part-3/Part-4 fix present, then closed the gaps
where a fix had shipped with no regression test standing guard: reset-409
while the engine is 'stopping', torn kronos ledger quarantine + restart
roundtrip, per-timeframe forecast horizon, forex ×5/7 bars/year, deterministic
journal-connection close, and the warmup-RSI/short-gate interaction (with the
100.0 counterfactual proving the NaN is what disarms the gate). Stale
"Chart.js via CDN" comment corrected. 108 → 114 tests; ruff zero; no trading
logic touched.

## [1.1.0] — 2026-09-06 — independent audit + hardening (loop engineering)

A two-agent audit (`JUDGE_REPORT.md`) found 20 flaws; 18 fixed and re-verified
by a fresh judge pass, plus the presentation layer (this release's scaffolding).
Highlights, each pinned by a test where testable:

- **Deflated Sharpe unit fix** — the SE was per-period while trial Sharpes were
  annualized, so DSR read ~1.0 for any input and could never reject. Pinned
  reference case: Sharpe 1.0 / 5 trials / 26k bars → 0.89 ("suggestive").
- **Backtest/live fill-bar parity** — the backtester now scans the bar the
  entry filled in (its whole range is post-fill), matching the live engine.
- **Purged-CV** now drops trades whose whole holding spans a path boundary,
  not just entry proximity.
- **Kronos IC ledger** keyed per market (a BTC forecast can no longer resolve
  against ETH's closes); atomic saves; torn-file quarantine; horizon
  normalized to ~1 day ahead on every timeframe.
- **seed-demo rows labeled `mode='demo'`** — badged in the dashboard, excluded
  from paper-record answers (chatbot) and from `shadow` by default.
- Journal timestamps canonicalized (write-time + boot migration); SQLite
  connections closed deterministically; data-outage force-close retries every
  cycle; account reset refuses while the engine thread is still stopping;
  forex Sharpe annualization ×5/7; RSI warmup returns NaN; dashboard startup
  moved to lifespan; torch/transformers split into `requirements-kronos.txt`.
- 92 → 98 tests (six new regression tests); ruff pyflakes baseline unchanged.
- Presentation scaffolding: LICENSE (MIT), `.env.example`, `pyproject.toml`
  (committed ruff + pytest config), `Makefile`, GitHub Actions CI, `DEMO.md`,
  the dashboard **Evidence** tab, `--start/--end` pinned reproducibility with a
  data manifest, and `validate --report REPORT.md`.

## [1.0.0] — 2026-09 — six measured rounds (see BACKTESTS.md)

- **Round 1 (v1):** full-cost battery across crypto/forex. Connors RSI-2 on 1h
  −24% (per-trade edge < round-trip cost); VWAP scalper on 5m −28…−31%; cost
  decomposition instead of parameter tweaking.
- **Round 2 (v2):** timeframe specialization (turtle 1h / connors 4h / scalper
  15m), deep-bull guard for connors longs, scalper re-priced at maker-style
  fees — final shipped numbers with the honest verdicts attached.
- **Round 3 (v3):** execution truth + portfolio allocation + the Kronos
  verdict: rolling IC −0.056 → **NOT promoted**; the earned-vote gate held.
- **Round 4 (v4):** live-engine self-audit — three paper/backtest divergences
  found from journal fingerprints and fixed.
- **Round 5 (v5):** time-of-day RVOL filter (Zarattini–Barbon–Aziz 2024)
  measured neutral on 24/7 crypto → **ships OFF** (an honest negative result).
- **Round 6 (v6):** Chan AR(1)/OU half-life gate for connors — 3 of 4 cells
  positive, walk-forward positive on both symbols → ships ON (`mr_halflife_max`).
- **Autonomy + security:** downtime stop-replay, data-outage guard, engine
  auto-resume, optional token auth, CSRF-hardened mutating endpoints,
  TrustedHost anti-rebinding.
