# Milestones A–C — Execution Plan (2026-09-10)

Source of requirements: `trade-bot-next-goals.md` (next-goal set). This file is the
orchestrator's cross-check + wave plan. Status markers update as waves land.

## Cross-check: FLAW_VALIDATION.md claims vs the code (audited 2026-09-10)

Baseline before any work: **121/121 tests green**, ruff clean, working tree clean.

| Claim in FLAW_VALIDATION | Verdict on the code | Evidence |
|---|---|---|
| 1.1 turtle exit fixed | **TRUE** | `bot/strategies/turtle.py:87-98` reads `don_exit_low/up` with `shift=1`; exit-mix test fires |
| 1.3 cost-aware BE | **TRUE** | `bot/strategies/scalper.py:218-233` buffers by taker fee + slippage per kind |
| 2.1 initial_stop latched | **TRUE** | `journal._migrate`/`record_fill` COALESCE-latch; `broker.restore_position` prefers it |
| 2.3 exact cash reconciliation | **TRUE** | `realized_cash_delta` + `entry_fee` columns; anchor-aware `closed_cash_delta_since` (`journal.py:469-503`); crash-window test to the cent |
| 3.1 moment-aware DSR | **TRUE** | `validation.py:269-342` de-annualizes → moments → re-annualizes; `se_model` field |
| 3.2 per-symbol-vol allocator | **TRUE** | `allocator.per_symbol_vols` on each symbol's own bars |
| 3.3 asset-filtered lexicon | **TRUE** | `sentiment.py:50-92` `_ASSET_KEYWORDS` + whole-batch fallback |
| Fix 1.2 exit ordering (PARTIAL, Phase 2) | **NOT LANDED** | `backtest.py` still scans `next_bar` stop/target BEFORE `check_exit` — a same-bar stop/target can win over an exit order already filled at that bar's open |
| Fix 2.2-lite gross cap (pulled forward by the goals file) | **NOT LANDED** | `RiskConfig` has no `max_gross_leverage`; `approve()` has no gross-notional gate — the ~1× bound is still only implicit (25% × 4) |
| Fix 4.2 correctness half (Kronos stamps) | **NOT LANDED** | `kronos_signal.py:278` still `pd.infer_freq(...) or "1h"`; engine passes no timeframe |
| Fix 4.1 cache freshness | **NOT LANDED** | rolling windows reuse only the same-day stamp |
| Manual "pause all trading" | **ABSENT** | no CLI command, no dashboard control |
| Kill-switch documentation | **ABSENT** | README has one bullet; dashboard has no explanation copy |
| Phase 0 pinned baselines | **NOT DONE** | Round 8 used rolling cached windows, not pinned `--start/--end` |

So Milestone A = the four unlanded fixes + pause control + docs + pinned
regeneration. The already-landed Phase-1 work is NOT redone.

## Milestone A — waves, ownership, contracts

**Phase 0 (orchestrator, before any code edit):** run 7 pinned backtests on the
pristine tree and store JSONs in `data/results/pinned_before/` (gitignored;
the numbers themselves go into BACKTESTS.md):

| Run | Window (pinned) | Strategy |
|---|---|---|
| BTC/USDT 1h | 2025-09-03 → 2026-09-04 | turtle_trend |
| ETH/USDT 1h | 2026-01-04 → 2026-09-04 | turtle_trend |
| SOL/USDT 1h | 2026-01-10 → 2026-09-04 | turtle_trend |
| BTC/USDT 4h | 2026-03-09 → 2026-09-06 | connors_meanrev |
| ETH/USDT 4h | 2026-03-09 → 2026-09-06 | connors_meanrev |
| BTC/USDT 15m | 2026-03-09 → 2026-09-06 | vwap_scalper |
| ETH/USDT 15m | 2026-01-05 → 2026-09-05 | vwap_scalper |

(Windows match the Round-8 cached frames so continuity is preserved.)

**Wave W1 (agent: risk-cap + pause + docs).** Owns `config.py`, `bot/risk.py`,
`bot/engine.py`, `main.py`, `bot/dashboard.py`, `README.md`, appends tests.
- `RiskConfig.max_gross_leverage: float = 1.0`.
- `RiskManager.approve(..., open_gross_notional: float = 0.0)` — new gate after
  sizing: `open_gross_notional + qty*price > max_gross_leverage × equity` →
  refuse with a clear reason. Engine computes gross from
  `broker.positions_snapshot()` marked at `_last_good_price` (fallback entry
  price); backtest call site passes its broker's gross the same way.
- Pause: flag file `data/trading_paused.json` `{"paused": bool, "ts", "note"}`
  written atomically (tmp+replace, `save_watchlist` pattern). `RiskManager.paused`
  (in-memory; checked first in `approve`, reason states "blocks new entries
  only — open positions are still managed"). Engine reads the flag ONCE per
  cycle (no file IO inside backtests). Missing file → not paused; corrupt →
  quarantine + treat as PAUSED (fail-safe direction) with a loud message.
- CLI: `python3 main.py pause [note]` / `python3 main.py resume`; `status`
  shows pause state. Dashboard: `POST /api/trading/pause|resume`,
  `/api/engine/status` gains `paused`, button + banner next to engine
  start/stop. UI copy states explicitly: blocks NEW entries only, does not
  force-close open positions.
- README: "What the daily kill switch does and does not do" section + pause
  control; same summary copy on the dashboard.

**Wave W3 (agent: Kronos stamps + cache freshness; parallel to W1).** Owns
`bot/kronos_signal.py`, `bot/data.py`; in `bot/engine.py` may touch ONLY the
`self.kronos.evaluate(...)` call inside `_kronos_eval` (adds
`timeframe=spec.timeframe`).
- `KronosSignalEngine.evaluate(df, horizon=24, timeframe=None)`: derive future
  stamps from `TIMEFRAME_SECONDS[timeframe]` via a pure helper
  `_future_index(last_ts, horizon, timeframe)` (unit-testable without the
  model); `timeframe=None` keeps the `infer_freq` fallback. Keep the sequential
  sampling loop (FLAW_VALIDATION correction #3 — batching returns a mean path).
- `fetch_history` rolling reuse: glob `{safe}_{tf}_{days}d_*.parquet` (and the
  `{start}_now_*` form), newest by mtime, reuse when age ≤
  `CACHE_FRESHNESS_HOURS` (default 24, env-overridable, read in `data.py`);
  log reuse + age. **Pinned `--start/--end` keeps exact-name, byte-identical
  semantics — the glob must never touch it.**

**Interface contract W1↔W2/W3:** `approve()`'s new param is optional
positional-last keyword; nothing else changes signature. `RiskManager.paused`
is a plain attribute. No other module reads the pause flag directly.

**Intermediate check (orchestrator, after W1+W3):** re-run the 7 pinned
backtests into `data/results/pinned_mid/`. MUST be identical to `pinned_before`
(same cached parquet files, same stats) — proves W1/W3 are numerically inert
for backtests; any drift = a bug to fix before W2.

**Wave W2 (agent: backtest exit ordering + regeneration; runs alone).** Owns
`bot/backtest.py` (scan-order block only — NOT the `approve` call), BACKTESTS.md,
appends tests.
- Reorder per FLAW_VALIDATION Fix 1.2: (a) fill-bar (`cur_bar`) scan when
  `fill_scan_pending`; (b) `check_exit(ind, i, pos)` — apply any stop-trail
  update, and if it fires, exit at `next_open` with `exit_idx = i+1` and DO
  NOT scan `next_bar`; (c) otherwise scan `next_bar` stop/target (with the
  newly-trailed stop active — live parity: the engine trails at bar i's close,
  the next bar's scan uses the new level).
- Test: crafted frame where bar i+1 gaps through the target AND the strategy
  exit fired at bar i → assert the open fill wins, not "take profit".
- Re-run the 7 pinned windows into `data/results/pinned_after/`, diff all three
  sets, write BACKTESTS.md "Round 9" (before/mid/after table, exit mix, and
  the note that only Fix 1.2 moved numbers). Expected: low-single-digit trade
  deltas (audit measured conflicting exits 4/115 BTC, 2/70 ETH, 1/57 SOL).

**Non-vacuity protocol (every agent):** each new test must FAIL against the
pre-fix code — prove by temporarily reverting your own edit in-place (edit
back → run test → must fail → re-apply). Do NOT `git stash` shared files.
Agents never run `git commit`/`git push` (orchestrator commits at milestone
boundaries); never touch `data/trading.db`; tests use temp paths only.

## Milestone B — India market + Forex/India toggle (after A is green)

- **B1 (core):** `config.py` `SPECS_INDIA` (`^NSEI` + RELIANCE/TCS/HDFCBANK/
  INFY/ICICIBANK `.NS`), `MARKET_MODE` (`"forex"` default | `"india"`) with
  atomic persisted mode file `data/market_mode.json` + `get/set_market_mode()`;
  `infer_kind` learns `.NS`/`^` → `"india"`; `VALID_KINDS` extended. One active
  book at a time; the two lists never merge.
- `CostConfig` India branch, **rates verified against a published broker charge
  sheet via WebFetch and cited in a comment** (they change): brokerage (flat
  discount-broker), STT (delivery vs intraday differ), exchange txn, SEBI,
  stamp duty (buy-side), GST on brokerage+txn+sebi. `fee()`/`slippage()` branch
  on `kind == "india"` explicitly; leg-aware (stamp buy-only) — `fee(kind,
  maker, side)`, `side=None` → conservative max. Slippage for liquid large caps.
- `risk.size_position`: whole shares — the `round(qty, 0)` branch covers
  `"india"`.
- NSE calendar: 09:15–15:30 IST Mon–Fri + 2026 holiday list (verified, cited,
  "update annually" comment) in a new `bot/calendar.py`; session guard gates
  NEW entries only (exits/replays always run); `bars_per_year` kind-aware
  (~245 sessions). Note: the whole NSE session maps inside one UTC day, so
  the kill-switch UTC-day semantics already align — document it.
- `data.py`: india fetch via the yahoo path (`.NS`/`^` tickers, 4h resample,
  1d direct); Fix 3.2 (per-symbol vols) is already landed — the prerequisite.
- Acceptance: full 90-day pinned India backtest on real yfinance data with the
  modeled costs, BACKTESTS.md "India" section. (yfinance intraday limits: 15m
  max 60d — the India universe uses 1h/4h/1d specs so 90d is fetchable.)
- **B2 (UI/docs):** dashboard toggle at the top — **On = Forex active,
  Off = India active**; switching blocked while open paper positions exist
  (explicit-confirm path force-closes at last marks first — never orphans a
  position); mode persisted so restarts don't flip markets; journal/dashboard
  filtering + README/BACKTESTS describe the `india` kind.

## Milestone C — SSRN-grounded strategies (after B is green)

New separate modules; turtle/connors/scalper untouched. Each: docstring citing
the paper(s), params in `StrategyParams`, `BACKTESTS.md` entry, unit tests,
registered in `get_strategy`, **disabled by default** (`enabled = False` class
attribute on the strategy class) until its own measured entry survives
realistic costs. The orchestrator's decision loop skips disabled strategies
explicitly (`if not strat.enabled: continue`) — this is the actual gate, NOT
absence from any watchlist or `REGIME_WEIGHTS` dict (that dict assigns voting
weights per regime for *enabled* strategies only, not exclusion).

- **C1 India time-series momentum** (`bot/strategies/ts_momentum.py`, 1d bars):
  MVP path (a) from the goals file — absolute/time-series momentum per symbol
  (fits `BaseStrategy`; no cross-sectional ranking infrastructure). Grounded in
  SSRN 3345280 (momentum convexity/alpha in India), 3510433 (long-only
  systematic momentum in India), 4587697 (52-week-high momentum, India).
  Long-only (delivery equities — no shorts). The (b) cross-sectional decile
  portfolio is a deliberately deferred bigger lift, per the goals file.
- **C2 Forex regime-conditioned mean reversion**
  (`bot/strategies/fx_regime_meanrev.py`, 1h): single-pair MVP from SSRN
  6087107 — z-score deviation + regime gate (reuse the repo's AR(1) half-life
  machinery) with ATR stop/time stop. True cointegration pairs trading
  (SSRN 4771108) is the stretch goal, NOT in scope, flagged in the docstring.

## Rules for all waves

Paper mode only — anything that only makes sense for live trading gets flagged,
not built. Full suite green (≥121) + ruff clean at every boundary. Old turtle
numbers (pre-Round-8) are never carried forward anywhere.
