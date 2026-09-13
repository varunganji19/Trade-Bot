# Changelog

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
