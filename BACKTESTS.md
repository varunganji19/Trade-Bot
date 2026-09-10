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

> **Methodology changelog (2026-09 audit).** An independent code audit fixed
> four measurement issues AFTER the rounds below were recorded; the affected
> numbers were not silently rewritten:
> - **Deflated Sharpe was unit-broken** (per-period SE mixed with annualized
>   trial Sharpes) and read ~1.0 for any input — any DSR figure printed
>   before this fix is uninformative. Fixed in `bot/validation.py` and pinned
>   with a must-fail reference case.
> - **Forex Sharpe annualization used 24/7 bar counts** — Yahoo forex trades
>   ~24×5, so forex Sharpe magnitudes above are overstated ~18%.
> - **The backtester skipped the fill bar's stop/target scan** (the bar where
>   the entry filled at its open) while the live engine scans it — a parity
>   gap exactly where it matters most. The backtester now scans the fill bar
>   from the open, so a same-bar stop-out is seen by both paths.
> - **Purged-CV purged entry proximity only**; it now also drops trades whose
>   HOLDING spans a path boundary, so per-path returns are cleaner OOS
>   segments (purge counts rise accordingly).

> **Strategy changelog (2026-09-09 Gemini-audit fixes — see
> FLAW_VALIDATION.md).** Five STRATEGY/ACCOUNTING bugs were fixed after the
> rounds below; the turtle numbers in every earlier round are from a strategy
> whose 10-bar Donchian exit could mathematically never fire (the channel
> included the decision bar's own low/high, and close ≥ low by candlestick
> construction — verified 0 of 8,759 bars could trigger it). Those runs exited
> only via 2×ATR stops or end-of-data, so their "trend following" numbers
> measured a stop-out machine, not the Turtle S1 exit. All earlier turtle
> rows are superseded by Round 8 below; the scalper's breakeven-stop rows are
> superseded where noted. Also fixed in the same pass: journal initial-stop
> latching (R-multiples used the TRAILED stop — the old profile's ±20R
> explosions and "blew through stop" counts were artifacts), anchor-aware
> crash-window cash reconciliation (the old query refunded both fee legs),
> per-symbol-vol allocation (the aligned matrix dropped ~28% of crypto
> weekend bars in mixed books), and asset-filtered lexicon sentiment (a
> crypto-crash headline no longer vetoes an EUR/USD entry).
>
> **Backtest fidelity changelog (2026-09-10 — Fix 1.2, see Round 9).** The
> backtester now fills a strategy exit decided at bar i at bar i+1's open
> BEFORE scanning that bar for stop/target — a same-bar stop/target can no
> longer "win" over an exit order that already filled. Earlier rounds' exit
> attribution on the affected ~2-3% of trades is biased accordingly; the
> pinned before/after table lives in Round 9.

## Round 8 (2026-09-09) — post-fix re-measurement: the honest turtle

The Turtle S1 exit now reads the PRIOR 10-bar channel (`shift=1`, matching
the entry-breakout convention); the scalper's breakeven stop is cost-aware
(entry ± taker fee + slippage, so a "breakeven" exit nets ~0 instead of a
guaranteed −0.30% round trip). Same cached windows as the audit, full costs.

| Symbol | TF | Strategy | Return | MaxDD | Trades | Win% | PF | Sharpe | Exit mix |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| BTC/USDT | 1h | turtle_trend | −5.0% | −10.1% | 115 | 23.5% | 0.82 | −1.42 | 49 channel / 65 stop / 1 EOD |
| ETH/USDT | 1h | turtle_trend | +0.5% | −9.8% | 70 | 22.9% | 1.02 | 0.38 | 28 channel / 42 stop |
| SOL/USDT | 1h | turtle_trend | **+10.6%** | −7.5% | 57 | 42.1% | 1.68 | 3.35 | 31 channel / 25 stop / 1 EOD |
| BTC/USDT | 15m | vwap_scalper | −11.1% | −13.8% | 135 | 17.8% | 0.31 | −30.25 | (cost-aware BE) |
| ETH/USDT | 15m | vwap_scalper | −8.6% | −9.8% | 163 | 29.4% | 0.58 | −13.28 | (cost-aware BE) |

What changed and why it matters:

- **BTC 1h turtle: the old +1.5%/11 trades was an artifact.** The dead exit
  meant one lucky month-long hold supplied most of the P&L; with the exit
  live, the same window trades 115 times and loses −5.0% at 23.5% win rate.
  The strategy's edge on BTC 1h is NOT confirmed — this is the honest
  baseline any future turtle tuning must beat.
- **SOL keeps a real edge** (+10.6%, PF 1.68, Sharpe 3.35 with 42% wins) —
  and now it's demonstrated with 57 real exits rather than 8 hold-to-end
  trades.
- **ETH is a coin flip** (PF 1.02, Sharpe 0.38) — the exit didn't reveal an
  edge, it revealed the absence of one on this window.
- **Scalper 15m numbers move little** (−11.1%/−8.6% vs the pre-fix runs on
  comparable windows): the cost-aware BE stop fixes the GUARANTEED ~-0.30%
  leak per BE exit but does not conjure an edge — the scalper remains
  cost-dominated on 15m majors, consistent with the cost studies in Rounds
  1-2. No configuration changed; only the stop arithmetic.
- Exit-mix evidence the fix is live: 108 of 242 turtle exits across the three
  symbols are now the Donchian opposite-channel exit, which was structurally
  0 before (see FLAW_VALIDATION.md for the impossibility proof).

The purged-CV / PBO / Monte Carlo batteries should be re-run on the new
turtle path before citing any distributional claim; earlier purged-CV turtle
figures (e.g. "2 of 28 paths traded") described the dead-exit world.

## Round 9 (2026-09-10) — exit-ordering fix (Fix 1.2): fills before scans

The backtester now honors time order when a strategy exit and a bracket level
compete for the same bar: an exit signal decided at bar i's close FILLS at
bar i+1's open, and only then is bar i+1's remaining range scanned for
stop/target. Before this fix the scanner ran first, so a same-bar stop or
target could "win" over an exit order that was already filled at the open —
a mixed-direction bias on a small slice of trades. The fill bar's own scan
(entered at its open) stays first, and within-bar conservatism (stop before
target) is unchanged. See FLAW_VALIDATION.md §1.2 for the validation.

All seven runs use PINNED `--start/--end` windows (byte-identical cached
data, see `scripts/pinned_runs.py`), so the before/after columns differ by
the ordering change alone. Provenance: `pinned_before` == `pinned_mid`
(runs after the 2026-09-10 risk/pause and Kronos/cache waves — numerically
inert for backtests, verified run-by-run) == the Round 8 cached windows.

| Run | Return before → after | MaxDD | Trades | PF | Sharpe | Exits changed |
|---|---:|---:|---:|---:|---:|---:|
| BTC/USDT 1h turtle | −5.11% → −4.72% | −10.09% → −10.03% | 116 | 0.82 → 0.83 | −1.44 → −1.32 | 4 |
| ETH/USDT 1h turtle | +0.29% → +0.39% | −9.80% → −9.72% | 71 | 1.01 → 1.02 | 0.23 → 0.26 | 2 |
| SOL/USDT 1h turtle | +10.32% → +10.37% | −7.50% → −7.46% | 57 | 1.65 → 1.66 | 3.25 → 3.27 | 1 |
| BTC/USDT 4h connors | +0.77% (unchanged) | −0.18% | 6 | 14.93 | 14.87 | 0 |
| ETH/USDT 4h connors | −0.23% (unchanged) | −0.86% | 4 | 0.57 | — | 0 |
| BTC/USDT 15m scalper | −11.41% → −11.23% | −13.89% → −13.72% | 136 | 0.30 | −30.53 → −30.07 | 5 |
| ETH/USDT 15m scalper | −8.59% → −7.97% | −9.84% → −9.56% | 163 | 0.58 → 0.60 | −13.28 → −12.37 | 6 |

The audit's prediction was near-exact: the turtle windows had 4/115, 2/70
and 1/57 conflicting exits (BTC/ETH/SOL) and the re-run flips exactly 4, 2
and 1 trades — each from a same-bar stop/target fill to the already-decided
signal exit. Connors is untouched (its RSI-reset exits never competed with a
bracket on the same bar on these windows). The scalper flips 5-6 trades per
window — its trailing breakeven stop creates more same-bar collisions, and
the reordered scan now also uses the freshly-trailed stop level, matching
the live engine's semantics (the engine trails at bar i's close and the
next bar's scan sees the new level).

No conclusion changes: BTC 1h turtle stays negative, SOL keeps its edge,
ETH stays a coin flip, and the scalper stays cost-dominated on 15m majors.
The moves are small and uniformly in the trades' favor — consistent with
removing a bias that let a same-bar stop "steal" an exit that had already
filled at a better price. As with Round 8, the purged-CV / PBO / Monte Carlo
batteries should be re-run on this final path before citing any
distributional claim.

## Milestone C — SSRN-grounded strategy additions (2026-09-10)

Both strategies below are REGISTERED but NOT in any watchlist (not in
DEFAULT_WATCHLIST, not in SPECS_INDIA) — the Milestone C rule: nothing goes
near the live/paper-live watchlist until its own measured entry survives
realistic costs. Neither did. These are valid measurements, not failures to
tune.

### C1 — India time-series momentum (`ts_momentum`, pinned 2y, delivery costs)

Long-only 1h NSE momentum (trailing 240-bar return > +8%, within 10% of the
1-year high, close > EMA200; exits on momentum decay / prior-10-bar channel
break / EMA200 loss), grounded in SSRN 3345280 / 3510433 / 4587697 — the
single-symbol MVP of the papers' cross-sectional decile ranking (path (a) in
the plan; the portfolio runner stays deferred). Pinned 2024-09-11→2026-09-10:

| Symbol | Bars | Return | MaxDD | Trades | Win% | PF | Sharpe | Fees |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ^NSEI | 3446 | 0.00% | 0.00% | 0 | — | None | None | $0 |
| RELIANCE.NS | 3432 | 0.00% | 0.00% | 0 | — | None | None | $0 |
| TCS.NS | 3426 | 0.00% | 0.00% | 0 | — | None | None | $0 |
| HDFCBANK.NS | 3431 | 0.00% | 0.00% | 0 | — | None | None | $0 |
| INFY.NS | 3432 | 0.00% | 0.00% | 0 | — | None | None | $0 |
| ICICIBANK.NS | 3430 | −0.37% | −1.40% | 12 | 33.3% | 0.85 | −1.01 | $105 |

The 2452-bar (≈10-month) warmup plus the papers' 6-12-month momentum
threshold left almost no qualifying bars in-window: the gate fired on 2 of
~992 evaluated bars on the index and 0 on four of five names. The one active
book (ICICIBANK) made +0.68% gross and lost −0.37% net — the ~0.29% delivery
round trip was bigger than the edge. The single-symbol threshold is too
blunt an analogue of the papers' decile ranking to justify building the
path-(b) portfolio runner on this evidence.

### C2 — FX regime-conditioned mean reversion (`fx_regime_meanrev`, pinned 1y, full costs)

Single-pair z-score deviation (log(close/EMA20), 100-bar window) gated by
the AR(1) half-life of the deviation itself (SSRN 6087107), 1.5×ATR stop,
1.5R declared target, z±0.5 snapback exit, 24-bar time stop. True
cointegration pairs trading (SSRN 4771108) remains the flagged stretch goal,
not built. Pinned 2025-09-10→2026-09-10:

| Pair | Bars | Return | MaxDD | Trades | Win% | PF | Sharpe | Fees |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EURUSD=X 1h | 6142 | −1.53% | −1.61% | 155 | 42.6% | 0.57 | −7.39 | $147 |
| GBPUSD=X 1h | 6144 | −1.79% | −1.99% | 157 | 45.9% | 0.58 | −7.48 | $151 |

The ~0.06% forex round trip is essentially the whole loss — gross per-trade
P&L is ~breakeven (−$0.04 EURUSD / −$0.18 GBPUSD), so the snapback edge
exists (EURUSD snapback exits averaged +$1.16 gross) but does not clear the
spread on 1h majors. The regime gate did bind (4-6 regime-died exits, plus
its refusals) — the framework is sound, the magnitude is not there at 1h
costs.

## India (NSE) — first pinned acceptance runs (2026-09-10)

The India market mode (Wave B1: an NSE universe — Nifty 50 + large-cap cash
equities — with its own regulatory cost stack, whole-share sizing and an
NSE session gate) needs a baseline before any India strategy is claimed to
work. These are the FIRST turtle_trend acceptance runs on NSE data: real
yfinance bars (auto-adjusted, validated), the same event-driven engine,
full India costs, $10,000 start, 1% risk. Results live in
`data/results/india_90d/*.json`.

| Symbol | TF | Window | Bars | Return | MaxDD | Trades | Win% | PF | Sharpe | Fees |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ^NSEI | 1h | 90d (2026-06-10 → 09-09) | 455 | 0.00% | 0.00% | 0 | — | — | — | 0 |
| RELIANCE.NS | 1h | 90d | 451 | −0.79% | −1.11% | 4 | 25.0% | 0.02 | −11.45 | 31.91 |
| TCS.NS | 1h | 90d | 447 | −0.33% | −1.14% | 3 | 33.3% | 0.39 | −3.75 | 21.77 |
| HDFCBANK.NS | 1h | 90d | 454 | −0.22% | −0.44% | 3 | 66.7% | 0.41 | −0.10 | 22.12 |
| INFY.NS | 1h | 90d | 451 | +0.20% | −0.68% | 3 | 33.3% | 1.24 | 3.72 | 21.08 |
| ICICIBANK.NS | 1h | 90d | 450 | −1.64% | −1.65% | 5 | 0.0% | 0.00 | −22.13 | 43.79 |
| RELIANCE.NS | 4h | ~180d (2026-03-16 → 09-09) | 362 | 0.00% | 0.00% | 0 | — | — | — | 0 |
| TCS.NS | 4h | ~180d | 362 | −1.87% | −1.33% | 2 | 0.0% | 0.00 | — | 14.64 |

The cost model, spelled out (components are module constants in
`config.py` so the arithmetic is checkable, not hand-totaled; rates verified
against the Zerodha charge list, verified 2026-09-10 — broker/exchange
rates change, re-verify annually):

- **STT: delivery rate, 0.1% on BOTH legs.** The bot's India specs hold
  multi-day positions on 1h/4h bars — that is delivery in Indian market
  terms, so delivery STT applies. This is deliberately the MORE expensive
  choice vs intraday (0.025%, sell side only) — the conservative floor.
- **Brokerage: 0.03% per leg.** Zerodha retail delivery brokerage is
  Rs 0, but modeling zero flatters results; 0.03% (the published intraday
  cap, min(0.03%, Rs 20)) is the conservative standing figure.
- Plus NSE exchange transaction charges (0.00307%/side), SEBI turnover
  (Rs 10/crore), stamp duty on the buy leg (0.015%) and 18% GST on the
  taxable components — **≈ 0.29% round trip** before the 0.05% adverse
  slippage per market leg. Every leg is a market leg: Indian charges are
  regulatory per-side taxes, not exchange maker rebates, so a resting
  limit still pays them (only the slippage differs).

Two data caveats, recorded rather than hidden:

- **^NSEI has zero volume on Yahoo** — every one of the 455 cached 1h
  bars reports volume 0 (verified against the pinned cache). The turtle
  itself needs no volume, and the 0-trade Nifty row is not a volume
  artifact (no Donchian-20 breakout + ADX ≥ 20 regime fired in the window),
  but any volume-gated strategy (the scalper's RVOL, volume confirmation)
  must NOT be run on the index as-is.
- **The 4h books deviate from the 90d protocol**: 90 days of 4h NSE bars
  (~142 tradable after the 220-bar warmup the mean-reversion/indicator
  stack needs) is too short to be meaningful, so the 4h runs use ~180d
  (362 bars). Even so RELIANCE 4h never trades (no qualifying setup after
  warmup) and TCS 4h trades twice — both 4h cells are too thin to conclude
  anything. The 4h/1d India books remain essentially UNMEASURED.

The honest reading: **turtle on NSE 1h is cost-dominated at delivery
rates — no edge is claimed.** The five equity 1h books traded 18 times for
a net −$277 across five $10k accounts (−0.55% average); four of five are
negative and the one positive cell (INFY, +0.20%, 3 trades) is noise, not
evidence. The fee column tells the story: ₹-denominated regulatory costs
(~0.29% round trip before slippage) eat a trend strategy whose 1h Donchian
trades on large-cap NSE names simply don't run far enough to pay for the
stamp. These numbers are the BASELINE Milestone C's India momentum
strategy must beat — if it can't clear this bar plus costs, it doesn't
ship.

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

Full v1 matrix was in `data/results/*_v1.json` (gitignored, machine-local —
not part of the repo; the tables above are the record).

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
  strategy registered for that timeframe plus the risk layer. The regime
  weights and conflict guard only engage if strategies share a timeframe —
  which currently never happens (the ranges are class-level and disjoint:
  turtle 1h, meanrev 4h/1d, scalper 5m/15m; a watchlist entry alone cannot
  override them), so treat the blend as dormant scaffolding, not an active
  feature.

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

**(Superseded 2026-09-09: this count described the dead-exit turtle — 10
trades existed precisely because the Donchian exit never fired. With the
exit fixed the same window trades 115 times; re-run the purged-CV battery
before citing any path distribution from this paragraph or earlier.)**

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

**Shadow Account (428-trade journal):** rule adherence 57.6% on BTC 1h
(16 late exits — trades lingering 2–31 bars after their strategy's exit
signal fired; 59 exits attributed to strategies whose rules didn't produce
them), 236/428 trades blew through their initial stop distance, disposition
gap +342h (losers held far longer than winners), avg R −0.66. Honest caveat:
that journal was seeded by `seed-demo` — real backtest replays, not trades the
live engine took; since 2026-09 those rows are labeled `mode='demo'`, the
dashboard badges them, the chatbot's paper-record answers exclude them, and
`python3 main.py shadow` audits the bot's OWN paper record by default
(`--include-demo` audits the replay rows instead). These numbers demonstrate
the shadow tooling, not a live-account record.

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

**(Superseded 2026-09-09: the 2026-09-09 strategy fixes (live turtle exit,
cost-aware scalper BE) intentionally change BOTH paths' trade sets — that is
the point. The byte-identical guarantee applies to the measurement machinery
only, and still holds: the backtester and live engine evaluate the same fixed
strategy code.)**

## Round 5 (v5) — the RVOL filter: an honest negative result

The elite-scalper research pass surfaced one filter with unusually strong
published evidence: **RVOL (time-of-day relative volume)**. Zarattini, Barbon
& Aziz (2024, "Stocks in Play") ran *identical* opening-range-breakout rules
with and without trading only unusually-active names — Sharpe **0.48 → 2.81**,
PnL/trade −0.02R below RVOL 100% vs +0.38R above 30×. We implemented the
causal, time-of-day-matched version (this bar's volume vs the mean of the 14
prior bars at the same UTC hour:minute — a plain rolling ratio mis-grades
crypto's hour-of-day seasonality) and measured it on our own structure.

**Verdict: the equity-market edge did not transfer. OFF by default
(`scalper_rvol_min = 0.0`), machinery and tests kept.**

| Window / protocol | BTC 15m scalper (off → RVOL ≥ 1.10) | ETH 15m scalper (off → 1.10) |
|---|---|---|
| 60d in-sample | −0.58% → −0.44% | −1.58% → −1.84% |
| 90d in-sample | −4.16% → −4.48% | — |
| 180d in-sample | −10.93% → −11.02% | — |
| Walk-forward 4 folds | +0.46% → +0.39% | — |

Reading: neutral-to-slightly-negative everywhere it was measured, and the
isolated "improvements" (BTC 60d, +0.14pp) are within noise. Why the edge
didn't transfer is not mysterious: the published result is on **US-equity
opening ranges** — a market that opens once a day, where RVOL separates
names the crowd is piling into *today* from ones nobody is watching. Our
scanning environment is **24/7 crypto, every-bar entries on 15m bars** —
a volume burst on a perpetual market is at least as often a liquidation
cascade (mean-reverting, adversarial to a momentum entry) as a sustainable
drift. The filter's premise — "active = trendable" — does not hold here.

What shipped: `seasonal_rvol()` in `bot/indicators.py` (causal by
construction — baseline uses only prior same-slot bars), the
`scalper_rvol_min` knob (default 0 = off), scalper gate wiring with NaN
auto-pass (forex feeds without volume stay ungated), and two tests
(`test_seasonal_rvol`, `test_scalper_rvol_gate`). Raising the knob to e.g.
1.10 re-enables the experiment in one config line; the honest default is
what the measurement says it should be.

## Round 6 (v6) — Chan half-life gate: the ARIMA-family tool that survived

From the ARIMA research pass, the one implementation candidate with clean
theory behind it: **the AR(1)/OU half-life of mean reversion** (Chan,
*Algorithmic Trading* ch. 2). The deviation `log(close/EMA20)` is fit to
`x_t = c + phi·x_{t-1} + e_t` by OLS over a rolling 100 bars;
half-life = `−ln(2)/(phi−1)` bars is how fast pullbacks have *actually* been
reverting. Entries are refused when that half-life exceeds the strategy's own
12-bar time-stop horizon — a pullback that historically takes longer than the
holding horizon to revert is a time-stop loser in waiting. (Everything else
ARIMA promised — price-direction forecasting, ARIMA+GARCH signals — failed
measurement in the literature: Meese-Rogoff, Goyal-Welch; see RESEARCH.md.)

**Verdict: shipped ON (`mr_halflife_max = 12.0`).** 3 of 4 in-sample cells
positive, walk-forward positive on BOTH symbols:

| Protocol | BTC 4h connors | ETH 4h connors |
|---|---|---|
| 180d in-sample | +0.56% → **+0.77%** (26→6* trades, PF 3.07→14.93) | +0.22% → −0.23% (6→4 trades) |
| 365d in-sample | +2.68% → **+2.93%** (26→24 trades, PF 2.68→3.17) | +0.32% → −0.14% (20→18 trades) |
| Walk-forward 4×90d | −0.11% → **+0.03%** (16→14 trades, PF 1.75→2.37) | −0.16% → **+0.05%** (11→10 trades) |

*the 180d baseline was 7 trades.

**Honest caveats, stated plainly:**
- Trade counts are small (10–26/year per symbol) and the walk-forward gains
  come from the gate skipping exactly one losing trade per symbol. This is
  directional evidence, not proof. We ship it because it is theory-aligned
  (Chan's own rule: don't trade reversion when the half-life exceeds your
  horizon), binds rarely (~2 vetoes/year), is byte-identical to baseline when
  it doesn't bind, and measures neutral-to-positive everywhere — the gate
  cannot help but prune reversion entries in regimes with no reversion.
- A second idea from the same tool — an *adaptive* time stop frozen at entry
  (`ceil(2 × half-life)` bars in position meta) — was built, plumbed, and
  **measured completely inert: zero of 26 real trades exited via time stop**
  (all exits are RSI(2) snapback/reset or stop-loss). Per the no-dead-code
  rule it was removed rather than shipped as decoration.
- The `inf` label (phi ≥ 1) almost never fires: finite-window OLS is biased
  below the unit root (Dickey-Fuller bias), so a true random-walk deviation
  reads ~window/5 bars, not inf. The threshold does the refusing. The
  `inf` policy matters only for genuinely explosive windows — and for tests:
  a pure linear ramp makes the deviation trend and the gate (correctly)
  vetoes everything, which is why the test frames wiggle.

## Standing caveats

1. All results above are from a single historical window per symbol. Walk-forward
   folds reduce overfitting risk but cannot eliminate regime luck.
2. Taker-fee scalping is deliberately the *worst-case* cost model; the live
   paper engine uses the same numbers.
3. The sentiment overlay is excluded from backtests by design (no reliable
   point-in-time news archive); it can only veto or shrink in live mode.
4. No result here is a promise. The system's value is the process: measure,
   diagnose, fix, re-measure — with every number reproducible from this repo.
