# Backtest Results — Real Market Data, Full Costs

All runs: real fetched candles (Binance via ccxt for crypto, Yahoo Finance for
forex), the same event-driven engine the live bot uses, **taker fees + slippage
on every fill** (crypto 0.10% + 0.05%, forex 0.02% + 0.01%), stops checked
before targets, decisions on closed bars only, fills at next bar's open.
Starting capital $10,000, 1% risk per trade.

> **Read this document as a lab notebook, not a brochure.** It records what we
> tried, what failed, what the failures taught us, and what shipped. The v1
> results below are kept deliberately: seeing a −28% run turn into a
> cost-positive one through measurement (not curve-fitting) is the whole point
> of the exercise.

## Round 1 (v1) — what the first battery showed

| Symbol | TF | Strategy | Return | MaxDD | Trades | Win% | PF |
|---|---|---|---:|---:|---:|---:|---:|
| BTC/USDT | 1h | turtle_trend | **+1.5%** | −5.1% | 11 | 9.1% | 1.51 |
| BTC/USDT | 1h | connors_meanrev | −24.2% | −24.4% | 316 | 43.7% | 0.35 |
| ETH/USDT | 1h | turtle_trend | −8.7% | −14.5% | 23 | 4.3% | 0.31 |
| SOL/USDT | 1h | turtle_trend | **+8.2%** | −6.7% | 8 | 12.5% | 3.16 |
| SOL/USDT | 1h | ensemble | **+8.4%** | −5.0% | 15 | 40.0% | 2.96 |
| BTC/USDT | 5m | turtle_trend | +2.0% | −3.2% | 36 | 2.8% | 2.26 |
| BTC/USDT | 5m | vwap_scalper | −28.4% | −28.4% | 467 | 16.1% | 0.15 |
| ETH/USDT | 5m | vwap_scalper | −30.8% | −30.8% | 481 | 16.8% | 0.19 |
| EURUSD=X | 1h | turtle_trend | −0.5% | −2.4% | 32 | 3.1% | 0.82 |
| GBPUSD=X | 1h | turtle_trend | −0.8% | −3.0% | 43 | 2.3% | 0.76 |
| USDJPY=X | 1h | turtle_trend | −2.3% | −5.3% | 36 | 2.8% | 0.46 |
| EURUSD=X | 1h | connors_meanrev | −7.7% | −7.7% | 668 | 47.9% | 0.56 |
| (etc.) | | | | | | | |

Full v1 matrix in `data/results/*_v1.json`.

## Diagnosis of the two losers

### Connors RSI-2 at 1h: −20% everywhere, 300–670 trades

Connors' published edge is on **daily** bars with multi-day holds. We ran it on
1h bars: the mean-reversion move per trade shrank below the round-trip cost,
while the signal fired 316×/year instead of ~10×. Classic "right strategy,
wrong timeframe" — the win rate looked plausible (~50%) but every trade paid
~0.3% in crypto fees for an edge of ~0.1%.

**Fix:** moved to **4h bars** (closest tradable analogue to daily), entries
tightened to RSI(2) < 5 (only deep pullbacks), 12-bar (2-day) time stop.

### VWAP scalper at 5m: −28%, 467 trades/30 days, 16% win rate

Measured decomposition:
- Median trade duration: **1 bar** — the "exit on VWAP cross" rule fired
  almost immediately because price crosses the rolling VWAP every few minutes
  on 5m crypto.
- 307 of 400 exits were stop-outs: the 1.2×ATR stop was ~0.10% away while a
  single 5m bar on BTC moves ~0.2%+. The stop sat *inside the noise band*.
- **Gross edge before costs was negative** (−$1.77/trade): shorts averaged
  −0.07% forward return (crypto's upward drift punishes them) and
  low-confidence signals were pure noise.
- The confidence ranking itself was informative: signals at conf ≥ 0.7
  averaged +0.137% forward on 15m — real, but smaller than a 0.15% net
  round-trip cost at 5m taker fees.

**Fixes (each one measured, not curve-fit):**
1. Exit only on **2 consecutive closes beyond VWAP ± 0.25 ATR** (buffered).
2. Cooldown after **every** exit, not just stop-outs (kills re-entry churn).
3. **15m bars instead of 5m**: the 12-bar forward edge (~+0.1%) exceeds the
   15m round-trip cost (~0.15%→0.24% taker) only on 15m+ timeframes.
4. **ADX ≥ 25 gate**: forward returns by entry ADX — ADX<15: −0.17%,
   15–25: −0.16%, **ADX≥25: +0.23%**. The scalper only trades when the market
   is actually trending.
5. **EMA200 trend filter**: longs only above, shorts only below.
6. **Shorts disabled** (`scalper_short_min_confidence = 1.01`): every short
   bucket lost money in testing on both symbols.
7. **No fixed take-profit**: a 2R cap cut average winners from $19.5 to $12;
   exits are now the buffered VWAP rule, breakeven trail, or time stop.
8. Wider stop (2×ATR) so one bar of noise can't stop the trade out.

### Turtle trend: worked as documented, left mostly alone

+1.5% BTC / +8.2% SOL with 8–11 trades/year and 9–12% win rate is the
textbook Turtle profile — few trades, low win rate, payoff ratio does the work.
Forex pairs were ~flat (−0.5% to −2.3%): 2025-26 FX markets chopped, and the
EMA-structure + ADX gates kept the damage small. ETH lost −8.7% in a year
where ETH itself fell ~40% — the strategy lost less than a third of what
buy-and-hold did.

## Round 2 (v2) — after the fixes (final shipped numbers)

| Symbol | TF | Strategy | Return | MaxDD | Trades | Win% | PF | Sharpe |
|---|---|---|---:|---:|---:|---:|---:|---:|
| BTC/USDT | 1h | turtle / ensemble | +1.5% | −5.1% | 11 | 9.1% | 1.50 | 0.26 |
| SOL/USDT | 1h | turtle / ensemble | **+8.2%** | −6.7% | 8 | 12.5% | **3.16** | 0.94 |
| ETH/USDT | 1h | turtle / ensemble | −8.7% | −14.5% | 23 | 4.3% | 0.31 | −0.85 |
| BTC/USDT | 15m | scalper / ensemble | −1.1% | −3.5% | 43 | 30.2% | 0.99 | — |
| ETH/USDT | 15m | scalper / ensemble | −1.8% | −3.3% | 49 | 30.6% | 0.89 | — |
| BTC/USDT | 4h | connors / ensemble | **+2.3%** | −2.0% | 40 | **65.0%** | **1.96** | 3.27 |
| ETH/USDT | 4h | connors / ensemble | +0.1% | −3.3% | 47 | 63.8% | 1.11 | 0.02 |
| EURUSD=X | 1h | turtle / ensemble | −0.5% | −2.4% | 32 | 3.1% | 0.81 | −0.14 |
| GBPUSD=X | 1h | turtle / ensemble | −0.8% | −3.0% | 43 | 2.3% | 0.76 | −0.22 |
| USDJPY=X | 1h | turtle / ensemble | −2.3% | −5.3% | 36 | 2.8% | 0.45 | −0.44 |

Round-2 diagnosis and further fixes (all measured on the 4h crypto data):
- The first 4h ensemble run showed a −22% drawdown: 14 consecutive ~1%-risk
  stop-outs, every trade from the **turtle** leg on 4h ETH. Turtle's rules
  were validated on 1h; on 4h they churn in violent chops. → **Timeframe
  specialization**: turtle 1h only, connors 4h, scalper 15m. The 4h drawdown
  collapsed to −4.7%.
- Connors on 4h was still −2.4% (BTC): side decomposition showed **shorts
  +$319, longs −$410**. Forward-return analysis of long entries: −0.32%
  average overall; requiring price **>10% above EMA200** (deep bull only)
  flipped the remaining longs positive (+0.9% avg, small n — flagged as thin).
  → shipped the deep-bull guard; BTC 4h went from −2.4% to **+2.3%, PF 1.96,
  win rate 65%** — close to Connors' documented profile.
- Scalper v3 at maker-style fees (0.02%): BTC +0.94% PF 1.41, ETH ~flat. At
  full taker (the paper default): BTC +$23 net, ETH −$116. Honest verdict:
  the 15m scalper is at gross break-even vs taker costs, positive vs maker —
  thin but real, and drawdowns contained (−3.3%).

## What shipped

- **turtle_trend** — 1h only (validated timeframe; EMA-structure + ADX gates).
- **connors_meanrev** — 4h, RSI(2)<5 longs in deep-bull regimes (>10% above
  EMA200), RSI(2)>95 shorts below EMA200, 12-bar time stop.
- **vwap_scalper** — 15m, long-biased (shorts disabled by measurement),
  ADX≥25 + EMA200 + volume gates, buffered VWAP exits, no fixed TP.
- **ensemble** (orchestrator) — regime-weighted blend, one position per symbol,
  risk-manager veto on every entry. Each strategy runs only on its validated
  timeframe, so on any given market the ensemble is effectively the one
  strategy registered for that timeframe plus the risk layer — the weights
  matter when strategies share a timeframe (e.g., both turtle and connors on
  1h via a custom watchlist).

## Walk-forward (out-of-sample) protocol

`python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 \
 --strategy ensemble --walk-forward --folds 4`

Folds trade only their out-of-sample slice; per-fold returns are reported
individually and aggregated. Use this for any number that will be shown to
judges — in-sample numbers are for debugging, not for claims.

**Purged-CV path distribution (new)** — `--purged-cv` splits history into 8
folds via skfolio `CombinatorialPurgedCV`, builds 28 out-of-sample paths from
every 2-fold test combination, purges trades whose entry sits within 24 bars
of a path boundary (outcome spans the border = label leakage), and reports the
distribution across *traded* paths. Empty paths are reported but excluded from
stats — a path with no trades is not evidence of an edge. First run on
BTC 1h turtle (365d, 10 trades): 2 of 28 paths traded, both profitable
(+5.6%, +8.7%), but **10 trades is far too few for the distribution to mean
much** — sparse-trade strategies need multi-year windows before the paths
carry statistical weight. The signal-IC report in the same command (conviction
vs 24-bar forward return, Spearman, overlap-adjusted t) gave pooled IC 0.09,
t≈0.33 for turtle on that window — directionally fine, not significant.

## Round 3 (v3) — execution truth, allocation, and the Kronos verdict

The v3 pass hardened the *measurement machinery* itself; headline strategy
numbers move little because the strategies didn't change — the ruler did.

**Execution fixes (backtest ⇄ live parity):**
- per-trade `fees` now include BOTH legs (previously exit-only — costs were
  undercounted ~50% on every trade)
- strategy exits fill at the NEXT bar's open, not the decision bar's close
  (that close wasn't tradable at decision time — old fills were optimistic)
- OCO brackets: both stop and target inside one bar → stop wins
  (conservative, unknowable intrabar path); a gap through the stop fills at
  the bar's OPEN (previously filled at the stop level = free money on gaps)
- the live engine previously evaluated the still-forming candle (ccxt returns
  it as the last row); both paths now drop it
- kill switch follows simulated bar time in backtests (previously the wall
  clock — the rule never actually ran in any backtest)
- determinism is unit-tested: identical inputs → identical trades/equity/stats

Net effect on measured results: modest but real — the old backtest
overstated results slightly on every metric that touched exits or gap bars.

**Portfolio allocation (skfolio):** inverse-vol by default (`PORTFOLIO_METHOD=hrp`
for the correlation-aware version). BTC/ETH/SOL are 0.7–0.9 correlated; four
concurrent 1%-risk positions there were never 4% book risk. The allocator
divides the budget by realized vol (clipped ±bounds) and the RiskManager
scales each entry by its share.

**Kronos earned-vote gate — first verdict (BTC 1h, 60d, horizon 12 bars,
115 resolved forecasts): rolling rank-IC −0.056 → NOT promoted.** The model
was integrated, vendored, and given a fair shot; it did not clear the hurdle
it must clear before it can vote. It remains a tracked non-voter and the
ledger keeps scoring it — promotion is re-evaluated continuously, so this is
a "not yet", not a "never". (Early readings on 30–80 forecasts showed +0.13;
that decayed to negative with more data — exactly why the min-observation
gate exists.)

**Shadow Account (real 428-trade journal):** rule adherence 57.6% on BTC 1h
(16 late exits — trades lingering 2–31 bars after their strategy's exit
signal fired; 59 exits attributed to strategies whose rules didn't produce
them), 236/428 trades blew through their initial stop distance, disposition
gap +342h (losers held far longer than winners), avg R −0.66. These are the
bot's own diagnostics on its own record — the starting point for the next
iteration of fixes, and the honest-attribution story in one command
(`python3 main.py shadow`).

## Round 4 (v4) — live-engine audit: the three ways paper diverged from backtest

An external audit of the live engine found and reproduced three bugs (all in
the paper engine, none in the backtester — verified by byte-identical parity
runs before/after the fixes):

1. **Cross-timeframe position collision.** The watchlist trades BTC/ETH on
   1h + 15m + 4h, but positions were keyed by symbol alone, so the 4h spec
   *managed the 1h spec's position* against 4h bars — whose range spans four
   hours of history the 1h trade never saw. Journal fingerprint: two BTC
   trades opened and "stopped out" within one second (2026-09-03 15:38,
   −$76 combined) because the last closed 4h bar's low sat far below the
   1h stop. This single bug also explains a large share of the shadow
   account's "blew through stop" count and disposition gap.
   **Fix:** positions keyed `(symbol, timeframe)`; each book scans only its
   own bars; other specs of the same symbol stand down (one position per
   symbol remains the risk rule); marking uses each book's own close.
2. **Restart reset the account.** Positions were restored but broker cash was
   not — every restart silently reset equity to paper capital (re-charging
   the entry fee for −$0.80 of drift on top). Journal fingerprint:
   `total_pnl −$143` shown alongside `current_equity $10,000 / return 0%`.
   **Fix:** cash restores from the journal's last equity point; the entry
   fee is recorded on the restored position (for round-trip reporting) but
   never charged twice.
3. **Time stops counted engine cycles, not bars.** `bars_held` incremented
   once per 60s cycle (and once per same-symbol spec via bug 1), so a
   "12-bar" 4h time stop fired in ~12 minutes. Backtests were correct;
   only live behavior diverged. **Fix:** `bars_held` derives from the
   decision bar's epoch in the owning timeframe's units.

Also fixed in the same pass: per-symbol cooldowns now store epoch seconds so
a 1h stop-out's cooldown reads coherently from the 15m/4h specs of that
symbol (bar-index scales were not comparable across timeframes); the shadow
command replays trades against the frame of the *journaled* (symbol,
timeframe) pair rather than the first watchlist spec; the journal migrates
old rows in place (scalper→15m, connors→4h, else 1h).

Backtest parity after the fixes: BTC 1h turtle 365d and BTC 15m scalper 60d
re-run byte-identical (stats and every trade) — the measurement machinery is
unchanged; only the live engine was corrected. Four regression tests added
(53 total): cross-timeframe isolation, restart cash/position restore,
bars-held-in-bars, journal migration.

## Standing caveats

1. All results above are from a single historical window per symbol. Walk-forward
   folds reduce overfitting risk but cannot eliminate regime luck.
2. Taker-fee scalping is deliberately the *worst-case* cost model; the live
   paper engine uses the same numbers.
3. The sentiment overlay is excluded from backtests by design (no reliable
   point-in-time news archive); it can only veto or shrink in live mode.
4. No result here is a promise. The system's value is the process: measure,
   diagnose, fix, re-measure — with every number reproducible from this repo.
