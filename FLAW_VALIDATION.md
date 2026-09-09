# Gemini 3.8 Flash Flaw Audit — Validation Results & Fix Plan (2026-09-09)

Every claimed flaw was validated against the actual code AND, where refutable,
against empirical runs on the repo's own cached data and journal. Baseline:
114/114 tests pass. No code was changed for this document.

Verdict scale: **CONFIRMED** (mechanism + consequence verified) · **PARTIAL**
(mechanism real, consequence overstated or fix wrong) · **REFUTED**.

## Verdict table

| ID | Claim | Verdict | Corrected severity | Empirical evidence |
|----|-------|---------|--------------------|--------------------|
| 1.1 | Turtle opposite-channel exit impossible | **CONFIRMED** | P0 | 0 of 8,759 bars can satisfy `close < don_exit_low`; BTC 1h 365d: 10 trades, 0 channel exits (9 stops + 1 held ~10 months to end-of-backtest); unit test at `tests/test_bot.py:1681` is vacuous (`below=False` on its seed, so it asserts `None==None`) |
| 1.2 | Bar i+1 stop/target scanned before strategy exit | **PARTIAL** — real ordering flaw, *not* lookahead; ~2–3% of trades affected | P2 (was P1) | With 1.1 fixed, conflicting exits: 4/115 BTC, 2/70 ETH, 1/57 SOL |
| 1.3 | Breakeven stop ignores fees/slippage | **CONFIRMED** | P1 | Broker sim: exit at entry = −0.30% notional; journal shows real scalper rows `stop==entry` exiting "stop loss" with pnl −3.74 |
| 2.1 | Trailed stops overwrite initial stop in DB | **CONFIRMED** | P1 | Real journal: 80/431 closed trades have stop within 0.1% of entry; 79 excluded from R-stats (risk=0), 5 explosive R (max +22.2), 239/352 flagged "blew through stop" (mostly BE artifacts, not breaches) |
| 2.2 | No margin/principal accounting | **PARTIAL** — mechanism real, "infinite leverage" false | P2 | `max_position_pct` 25% × `max_open_positions` 4 caps gross at ~1× equity; it is a margin-style PnL-settlement model with no margin call/liquidation modeling — undocumented, not unbounded |
| 2.3 | Cash reconciliation double-counts fees | **CONFIRMED** (diagnosis); Gemini's prose mis-describes it; formula fix right for the standard case, wrong for one edge case | P2 | Broker sim: true close-event delta = `pnl + entry_fee`; query returns `pnl + fees` = gross → overstates by exactly the **exit** fee per reconciled trade (not "refunds entry fee") |
| 3.1 | DSR omits skew/kurtosis | **CONFIRMED** formula gap; measured impact small here | P3 (was P2) | On this bot's own equity curves the moment-adjusted SE is 1.00×/1.11×/0.91× the normal SE (kurtosis 10–14 but tiny per-bar SR makes the SR² term negligible) |
| 3.2 | Allocator `dropna(any)` drops crypto weekends | **CONFIRMED** | P2 | Real frames: BTC 199 bars → 141 aligned rows (29% dropped); weekend share 24% → 0.7% |
| 3.3 | Lexicon sentiment ignores asset_hint | **CONFIRMED** (live paper path only — backtests pass `include_sentiment=False`) | P2 | `assess()` lexicon branch never reads `asset_hint`; orchestrator passes it (`spec.display`) |
| 4.1 | Cache date-stamp forces refetch | **PARTIAL** — mechanism true; freshness semantics make blind reuse wrong | P3 | `data/cache` holds multiple day-stamps per (symbol, tf, days); but a 365d file from 3 days ago is a *different window* than requested — that's why pinned `--start/--end` exists |
| 4.2 | Kronos loop slow + `infer_freq` fallback | **PARTIAL** — both sub-issues real; **Gemini's batching fix is wrong** | P2 (freq) / P3 (perf) | Vendored `kronos.py:auto_regressive_inference` ends with `np.mean(preds, axis=1)` — `predict(sample_count=30)` returns ONE mean path, so batching would collapse P(up) to exactly 0/1 and dispersion to 0 |

## Corrections to the Gemini report worth recording

1. **1.2 is an event-ordering flaw, not lookahead** — nothing reads future
   data. The strategy exit decided at bar i's close fills at bar i+1's open;
   that open fill must execute *before* any intra-bar-i+1 stop/target scan.
   The current code scans first, so a same-bar stop/target can "win" over an
   exit order that was already filled at the open. Mixed-direction bias,
   ~2–3% of trades.
2. **2.3 arithmetic, precisely**: `pnl = gross − entry_fee − exit_fee`;
   `fees = entry_fee + exit_fee`; query `pnl+fees = gross`. The correct
   crash-window delta (entry fee already reflected in the anchor equity
   point) is `pnl + entry_fee = gross − exit_fee`. The query therefore
   overstates by the **exit fee only**. Gemini's fix formula is right for the
   standard crash window but **wrong for the edge case where the trade both
   opened and closed after the anchor** (equity writes are skipped when all
   held symbols fail to fetch): there the true delta is plain `pnl`, and
   `pnl + entry_fee` overstates by the entry fee. A per-trade realized-cash
   column is the robust fix.
3. **4.2 batching fix would corrupt the signal**: the vendored predictor
   averages the sample dimension before returning, so the loop of
   single-sample calls is currently the *only correct* way to get 30 distinct
   paths. Batching requires patching the vendored model (MIT-licensed, allowed
   — but it is a deliberate divergence) or accepting mean-path output.
4. **3.1's proposed snippet is dimensionally wrong**: the Bailey–LdP moment
   formula applies to the **per-period** SR. Feeding the *annualized* `best`
   into `(1 − skew·SR + (kurt−1)/4·SR²)` mis-scales the correction. Must
   de-annualize first (`SR_p = best / sqrt(bars_per_year)`), compute the
   moment variance, then re-annualize the SE.
5. **2.2 "infinite unconstrained leverage" is false** — gross notional is
   bounded near 1× equity by existing caps. The real gap is that the account
   model (margin-style PnL settlement, no principal escrow, no margin call,
   shorts impossible on spot) is nowhere documented.

---

# Fix plan (no fixes applied yet — implementation phases, file-level specs)

Execution order is by risk: strategy correctness first (it invalidates all
documented turtle numbers), then audit/accounting integrity, then engine
fidelity, then statistics and infra. Each phase lands green on the full suite.

## Phase 0 — pin the baseline (before touching code)

- Re-run the three standard windows with **pinned** `--start/--end` (BTC/ETH/SOL
  1h turtle, connors 4h, scalper 15m) and save results to `data/results/` so
  before/after drift is attributable, not re-rollable.
- Accept that **every turtle number in BACKTESTS.md is stale after Fix 1.1**
  (BTC 1h 365d goes from 10 trades to ~115, with 49 opposite-channel exits).

## Phase 1 — strategy & accounting correctness (P0/P1)

### Fix 1.1 — Turtle exit channel: use the prior 10-bar channel
- `bot/strategies/turtle.py:87,91`: `_at(df, "don_exit_low"/"don_exit_up", i, shift=1)`.
- Matches entry semantics (`don_up20/low20` already read with `shift=1`) and
  the classic Turtle S1 exit (prior channel, never the decision bar's own).
- **Tests**: make `tests/test_bot.py:1681` non-vacuous — assert the exit
  *fires* on a trend-then-reversal synthetic (the current seed frame has 14
  bars satisfying the shifted condition; construct a position/bar that hits
  one). Add a property test: for random frames, `check_exit` long fires iff
  `close < rolling-10 low shifted 1`.
- **Docs**: BACKTESTS.md turtle rows + §"Backtest parity" re-measured; README
  strategy table wording unchanged (it already says "opposite-channel exit").

### Fix 2.1 — persist the initial stop
- `bot/journal.py` schema + `_migrate`: add `initial_stop_price REAL` column;
  backfill `initial_stop_price = stop_price` for existing rows (best available
  for legacy data — document the approximation).
- `open_trade(...)`: accept and write `initial_stop` (engine passes the
  fill-derived stop once at entry).
- `update_trade_stops`: keeps overwriting `stop_price` only; never touches
  `initial_stop_price`.
- `bot/broker.py restore_position`: `risk_per_unit` from
  `initial_stop_price` (fallback `stop_price`), so a post-restart scalper
  BE-trail computes R against the *initial* risk.
- `bot/shadow.py`: `_pos_view` and `behavior_profile` use
  `t.get("initial_stop_price") or t.get("stop_price")`.
- `bot/backtest.py _trade_dict`: include `initial_stop` (shadow's profile also
  consumes backtest trade dicts).
- **Test**: trail a stop via `update_trade_stops`, assert `initial_stop_price`
  unchanged; behavior_profile on a BE-trailed loser no longer reports R < −5.

### Fix 1.3 — cost-aware breakeven
- `bot/strategies/scalper.py:218-223`: for longs `be = entry * (1 + fee + slip)`,
  shorts `be = entry * (1 − fee − slip)` with `fee = CONFIG.costs.fee(kind, maker=False)`,
  `slip = CONFIG.costs.slippage(kind, maker=False)` (stop exits are taker legs;
  the scalper needs kind — take from CONFIG or pass spec into check_exit via
  the position's timeframe/spec; simplest: read CONFIG.costs and infer kind
  from the symbol convention `infer_kind`).
- Precision note: exact BE also owes `entry_fee/qty` and the fee on the exit
  notional; the multiplicative buffer is within a rounding of it and simpler —
  keep the buffer, comment the derivation.
- **Test**: BE-trailed position exiting at `be` realizes ≥ −0.01% notional.

### Fix 2.3 — exact realized-cash reconciliation
- `bot/trades` schema: add `realized_cash_delta REAL` (migration + backfill
  NULL for legacy rows).
- `bot/broker.py close_position`: return the cash delta it applied
  (`gross − exit_fee`) alongside existing returns (or the split fees).
- `engine._close` writes it at `close_trade`; `close_trade` gains the param.
- `journal.closed_cash_delta_since`: sum
  `COALESCE(realized_cash_delta, pnl + fees/2)` — the `fees/2` term
  approximates the entry fee for legacy rows (standard crash window),
  documented in the docstring.
- **Test**: crash-window simulation — open, write equity point, close, no
  equity write, restore; broker cash must equal the uninterrupted path to the
  cent, for both a trade opened before and after the anchor.

## Phase 2 — engine & infra fidelity (P2)

### Fix 1.2 — exit ordering in the backtester
- `bot/backtest.py:157-192` reorder to: (a) fill-bar (`cur_bar`) stop/target
  scan when `fill_scan_pending` (its whole range is post-fill, pre-decision),
  (b) `check_exit(ind, i, pos)` — if it fires, exit at `next_open` with
  `exit_idx = i+1` and **do not scan `next_bar`**, (c) otherwise scan
  `next_bar` for stop/target.
- Preserves within-bar conservatism (stop before target) and the live path's
  semantics (engine closes at the closed bar's close).
- **Test**: crafted frame where bar i+1 both gaps through the target and the
  strategy exit fired at bar i — assert the open fill, not the target.
- Re-measure the three windows; expect low-single-digit trade deltas.

### Fix 3.2 — allocator weekend massacre
- Default `inverse_vol`: compute per-symbol vol on each symbol's **own bars**
  (no alignment needed) — crypto keeps 24/7 bars, forex its trading bars.
- `hrp`: keep the aligned matrix but restrict to common *trading* rows
  (weekday overlap) for the correlation structure only, documented.
- **Test**: crypto+forex frames → inverse-vol weights must use ≥95% of crypto
  bars; a weekend-heavy crypto regime must move weights.

### Fix 3.3 — asset-relevant lexicon sentiment
- `bot/sentiment.py` lexicon branch: filter headlines by an asset keyword map
  (BTC→bitcoin/btc, ETH→ethereum/ether, SOL→solana, EURUSD→euro/dollar/ECB,
  shared macro keys fed/rates/inflation apply to all); fall back to all
  headlines when nothing matches (Gemini's fallback is right).
- Map lives next to the lexicon; `asset_hint` already flows from
  `spec.display`.
- **Test**: crypto-crash headline vetoes a BTC long but not an EUR/USD long.

### Fix 4.2 (correctness half) — Kronos forecast stamps
- Derive the future-stamp frequency from the bar spacing actually passed in:
  add `timeframe` (or `freq`) param to `KronosSignalEngine.evaluate`; the
  engine already knows `spec.timeframe` (`TIMEFRAME_SECONDS[tf]` seconds).
  Call sites: `engine._kronos_eval`, tests.
- Keeps the sequential sampling loop (see correction #3 — batching without a
  vendored patch returns one mean path).

### Fix 2.2-lite — document the account model + gross cap
- `RiskManager.approve`: new gate `gross_notional(open) + new ≤
  risk.max_gross_leverage × equity` (default 1.0× — matches today's implicit
  bound, now explicit and enforced across mixed timeframes).
- README/RESEARCH §accounting: state plainly — margin-style PnL settlement,
  principal not escrowed, no liquidation modeling, shorts imply perp-style
  execution; the cap is the enforced bound.
- Full principal/margin accounting (spot cash deduction, margin reservation,
  margin calls) is a **separate scope decision** — it changes equity/cash
  semantics, restart reconciliation, dashboard, and many tests. Do not
  bundle it here.

### Fix 4.1 — freshness-bounded cache reuse
- `_disk_cache_path`/`fetch_history`: glob `{safe}_{tf}_{days}d_*.parquet`,
  reuse the newest if its mtime is ≤ `CACHE_FRESHNESS_HOURS` (default 24,
  env-overridable) — bounded staleness for rolling windows instead of a 14-day
  free-for-all; log the reuse and age. Pinned `start/end` windows keep exact
  byte-identical semantics.

## Phase 3 — statistics & perf polish (P3)

### Fix 3.1 — moment-aware DSR (done correctly)
- `deflated_sharpe(sharpes, n_obs, bars_per_year, returns=None)`: when a
  returns series is supplied, `SR_p = best / sqrt(bars_per_year)`;
  `var_p = (1 − skew·SR_p + (kurt−1)/4·SR_p²)/(n_obs−1)`;
  `se = sqrt(bars_per_year · var_p)`. No returns → current normal SE, and say
  so in the returned dict (`se_model: "normal"|"moments"`).
- Caller (`main.py cmd_validate`): pass the primary run's equity-curve returns;
  docstring notes the approximation (trial sharpes come from sibling configs).
- Optionally the same correction for `min_trl` later.

### Fix 4.2 (perf half, optional) — Kronos sampling cost
- Options, in preference order: (a) lower `sample_count` (30 → 10–15) and
  measure the P(up)/dispersion stability loss; (b) keep 30 but make the IC
  ledger tolerate fewer; (c) patch the vendored `kronos.py` behind a flag to
  return per-sample paths (`return_samples=True`) so one batched call yields
  30 paths — MIT-licensed, allowed, but record the divergence in
  `models/kronos/README` notes. No change unless the engine cadence actually
  hurts (kronos is throttled to every 4 closed bars and is a non-voter).

## Regeneration & docs (same commits as the fixes)

- BACKTESTS.md: re-measure every turtle table row and the parity note
  (10 → ~115 trades on BTC 1h 365d; exit mix now 49 channel / 65 stop / 1 EOB).
- README strategy table + RESEARCH.md §2.5: no behavior claims change except
  turtle's exit (now truly the prior-10-bar channel) and the new accounting
  paragraph (2.2-lite).
- JUDGE_REPORT.md items stay as-is (no overlap with these flaws except item 12,
  already fixed).

## Explicit non-goals / risks

- **Do not** batch Kronos via `sample_count` as Gemini proposed — it returns
  a mean path; P(up) and dispersion would be destroyed.
- **Do not** "fix" 2.3 with `pnl + entry_fee` alone — it overstates the
  open-after-anchor edge case; the realized-cash column is the honest version.
- **Do not** implement spot-principal accounting inside this plan (2.2) —
  it is an account-model redesign; ship the doc + gross cap and decide
  separately.
- Expect BACKTESTS.md turtle numbers to move a lot after 1.1 — that is the
  point (the old numbers were produced by a strategy with a dead exit).
