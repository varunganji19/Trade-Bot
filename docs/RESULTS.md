# Results: every strategy and experiment, with its verdict

*The one place results live. Each row states the hypothesis, how it was
measured, what happened, the verdict and why. Detailed write-ups are in
[`docs/archive/`](archive/) and linked per row; they are kept unchanged as
the lab notebook. How the measurements are produced is in
[METHODOLOGY.md](METHODOLOGY.md).*

**Headline: no strategy has a proven edge, and none is allowed to trade.**
Under the stricter promotion rule (v2, 2026-10-01) every standard-book
strategy is on probation or demoted, and every fast-book strategy is
demoted except a market maker whose result comes from an optimistic fill
model (§3). The failures are listed here on purpose.

Verdict words used below:

| Verdict | Meaning |
|---|---|
| **promoted** | rule v2: ≥ 100 out-of-sample trades, ≥ 10 in each of 3 market regimes, and the **lower** end of the 90% bootstrap interval on profit factor ≥ 1.0; may vote |
| **probation** | not proven either way (too few trades or regimes, or an interval that straddles 1.0); under v2 it does **not** vote |
| **demoted** | ≥ 30 trades and even the **upper** end of the interval is below 1.0; a measured loser |
| **rejected** | failed its own acceptance test; never voted |
| **shipped off / on** | a filter or setting kept in code, default chosen by the measurement |

---

## 1. Current gate verdicts (rule v2)

Written by `make evidence` from the pre-registered gate declarations
[`experiments/standard_gate.toml`](../experiments/standard_gate.toml) and
[`experiments/fast_gate.toml`](../experiments/fast_gate.toml); full results
in the matching `.results.json` files. Run 2026-10-01 in a scratch journal
directory, so a machine that has not run `make evidence` still shows its
older rule-v1 verdicts, marked "OLD RULE" on the dashboard.

### Standard book — 2 years, BTC/ETH/SOL/BNB/XRP at 15m/1h/4h, EUR/USD and GBP/USD at 1h

| Strategy | Hypothesis (lineage) | Out-of-sample PF (90% interval) | Trades | Verdict |
|---|---|---|---|---|
| `ts_momentum` | Absolute time-series momentum (SSRN 3345280 / 3510433 / 4587697) | 1.07 (0.81–1.37) | 744 | **probation** — interval straddles 1.0 |
| `turtle_trend` | Donchian breakout trend following (Turtle S1) with ADX filter | 0.89 (0.69–1.13) | 1,515 | **probation** |
| `connors_meanrev` | Deep RSI(2) pullbacks in an uptrend revert (Connors), half-life gate (Chan) | 0.75 (0.52–1.12) | 165 | **probation** |
| `fx_regime_meanrev` | FX z-score fade gated by half-life regime (SSRN 6087107) | 0.64 (0.57–0.72) | 3,001 | **demoted** |
| `vwap_scalper` | VWAP reclaim/loss with momentum and volume (Zarattini & Aziz ORB evidence) | 0.57 (0.49–0.65) | 2,245 | **demoted** |

**The small-sample trap, measured.** Under rule v1 (median fold PF ≥ 1.0
over ≥ 30 trades, on a 60-day to 2-year mix of fewer markets) Connors and
the VWAP scalper were the two strategies allowed to trade, at PF 1.56 and
1.55 on about 60 trades each. On two years and five markets the scalper is
a clear loser and Connors is unproven. All three regimes were covered for
every strategy, so the verdicts are not an artefact of one market phase.

### Fast book (experimental) — 90 days, 15 USDT pairs + 6 crosses, 5m, perp tier

| Strategy | Hypothesis (lineage) | Out-of-sample PF (90% interval) | Trades | Verdict |
|---|---|---|---|---|
| `hft_market_maker` (candidate) | Volatility-scaled quoting (Avellaneda & Stoikov 2008) on candles | 1.41 (1.36–1.47) | 36,094 | "promoted" by the rule, **not credible**: see the fill-model study in §3; as a candidate it never votes |
| `hft_exhaustion_fade` | Fade a volume-spike exhaustion bar back to its mean (Carver 2025) | 0.67 (0.53–0.82) | 880 | **demoted** |
| `hft_cross_reversion` (candidate) | Cross-pair spreads mean-revert | 0.60 (0.50–0.71) | 898 | **demoted** |
| `hft_micro_breakout` | Rolling micro-range breakout (Zarattini & Aziz 2023) | 0.53 (0.49–0.56) | 9,918 | **demoted** |
| `hft_funding_reversion` (candidate) | Perpetual funding extremes mark a crowded side that reverts | 0.48 (0.37–0.61) | 254 | **demoted** |
| `hft_ofi_momentum` (candidate) | Order-flow imbalance (Cont, Kukanov & Stoikov 2014), proxied by CLV × volume | 0.44 (0.41–0.48) | 10,138 | **demoted** — the proxy is not real order flow (roadmap M8) |

The fade, the fast book's only voter under rule v1, is a measured loser on
the larger sample, so under rule v2 the fast book has nothing allowed to
trade either.

---

## 2. Signals and filters

| Idea | Hypothesis | Method | Result | Verdict | Detail |
|---|---|---|---|---|---|
| **Kronos** foundation model (AAAI'26) as a voter | A model pre-trained on K-lines from 45+ exchanges forecasts direction | Rolling rank-IC ledger; must reach IC ≥ 0.02 over 60+ resolved forecasts | BTC 1h: IC −0.056 over 115 forecasts, then **−0.075 over 128** (2026-09-19); early +0.13 on 30–80 forecasts decayed | **rejected**; runs offline only | README "Extras"; [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 3 |
| **RVOL** time-of-day relative-volume filter | "Stocks in Play": Sharpe 0.48 → 2.81 on US-equity opening ranges | Same scalper with and without RVOL ≥ 1.10; 60/90/180-day and 4-fold walk-forward | neutral to slightly negative everywhere (e.g. BTC 180d −10.93% → −11.02%) | **shipped off** | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 5 |
| **Chan half-life gate** on Connors | Refuse reversion entries whose measured half-life exceeds the holding horizon | 180/365-day in-sample and 4×90-day walk-forward, BTC and ETH 4h | walk-forward positive on both (BTC −0.11% → +0.03%, ETH −0.16% → +0.05%), but from skipping about one losing trade per symbol | **shipped on** (directional evidence, not proof) | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 6 |
| Adaptive time stop (2 × half-life) | Hold reversion trades for as long as they historically take | Built and backtested | zero of 26 trades ever exited by it | **removed** (inert) | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 6 |
| News sentiment and LLM tie-breaker in the decision path | An LLM and headline sentiment improve entries | Not measurable: the backtester could not replay either | live and backtest were running different decision code | **removed** 2026-09-19; the LLM stays in the chatbot only | [CHANGELOG.md](../CHANGELOG.md) |
| Connors at 1h instead of daily | The RSI(2) edge transfers to 1h | Full-cost battery | −20% to −24%, 300–670 trades/year: edge per trade smaller than fees | **moved to 4h** with deeper entries | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 1 |
| VWAP scalper at 5m | Intraday VWAP scalping on crypto majors | Full-cost battery and cost autopsy | −28% in 30 days, 467 trades, stops inside the noise band, negative gross edge | **moved to 15m**, with ADX/EMA gates, shorts disabled | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 1 |
| One strategy across all timeframes | Strategies generalise across bar sizes | 4h ensemble run | −22% drawdown from turtle churn on 4h | **timeframe specialisation** (turtle 1h, Connors 4h, scalper 15m) | [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 2 |
| India time-series momentum (NSE) | Momentum on Indian large caps | Pinned 2-year runs with delivery costs | 0 trades on 4 of 5 names; the one active name lost to costs | **rejected**; the NSE universe was later removed (zero trades ever) | [archive/BACKTESTS.md](archive/BACKTESTS.md) Milestone C |

---

## 3. Fast-book studies (2026-09 to 2026-10)

All on real Binance data with the perp fee tier (maker 2 bp, taker 5 bp,
slippage 3 bp). The three fade studies were first run before the registry
existed ([archive/HFT_TRADE_FREQUENCY.md](archive/HFT_TRADE_FREQUENCY.md))
and re-run under pre-registered declarations in [`experiments/`](../experiments/),
which reproduced them closely; figures below are from the re-runs.

| Study | Hypothesis | Method | Result | Verdict |
|---|---|---|---|---|
| 1-minute book | Short-horizon strategies can pay their fees at 1m | 24-cell battery, 3 days, both fee tiers | every cell negative; 15 live decisions, 0 trades (no stop could pay its fees) | **abandoned 1m**; moved to 5m |
| Triangular arbitrage monitor | BTC/ETH/ETH-BTC mispricings exceed the 3-leg cost | 4,319 aligned 1m bars | max mispricing 8.3 bp vs 24 bp cost; zero opportunities | **rejected** |
| Cross-pair and funding reversion | Two replacements for the demoted strategies | 60 days, 4 walk-forward folds per market | PF 0.58 and 0.39; both negative before fees | **demoted** (confirmed by the v2 gate, §1) |
| [Fade thresholds](../experiments/fade_thresholds.toml) | Looser thresholds raise frequency without killing the edge | 15 markets, 90 days; 9 settings compared on 60 days, judged on the last 30 | every selection interval below or straddling 1.0 (best PF 0.73); live setting −$406 on the holdout | **no change** (three 2.0×-volume settings are positive on the holdout, but nothing on the selection window would have chosen them) |
| [Fade limit exits](../experiments/fade_limit_exit.toml) | A resting exit at the mean cuts costs enough to flip the result | 9 variants, same split | best −$170 over 30 days; −$256 once the entry must trade through | **no change**; option kept off |
| [Fade hold length](../experiments/fade_hold.toml) | The reversion needs more than 45 minutes | 9 variants (45 min to 6 h), same split | the 6 h hold looks best on selection (PF 0.96) and loses $526 on the holdout, vs $170 for 45 min | **no change** — a measured example of selection bias |
| [Market-maker fill model](../experiments/market_maker_fill_model.toml) | The market maker's gate result survives fills that must trade through the quote | 21 markets, 90 days; entries filled at touch, 1, 2 and 5 bp through | PF 1.41 → 1.29 → 1.19 → **0.96** (0.92–1.00); take-profits still touch-filled | **artefact of the fill model** (the declaration set "below 1.0 is conclusive" before the run) |

**What the fast book has shown:** breadth delivers frequency, the fade has a
small gross edge that fees take entirely, and the one apparently profitable
strategy is profitable only under the most optimistic fill assumption.
Market making needs order-book data to be measured at all (roadmap V7); the
remaining signal lever is real order flow (roadmap M8).

---

## 4. Measurement bugs that changed earlier results

The platform's own audits found these; affected numbers were superseded, not
rewritten. Each is now covered by a test.

| Bug | Effect on results | Found / fixed |
|---|---|---|
| Turtle exit channel included the decision bar, so the Donchian exit could never fire | every turtle result before 2026-09-09 measured a stop-out machine; BTC 1h went from +1.5% (11 trades) to −5.0% (115 trades) | 2026-09-09; [archive/BACKTESTS.md](archive/BACKTESTS.md) Round 8 |
| Deflated Sharpe mixed per-period and annualised units | any earlier DSR read about 1.0 regardless of input | 2026-09 audit |
| Fees charged on one leg only | costs undercounted by about 50% on every trade | Round 3 |
| Live engine let a 4h bar manage a 1h position; restarts reset cash; time stops counted cycles, not bars | live diverged from backtest | Round 4 |
| Exit order: a same-bar stop could win over an exit that had already filled | small bias on 2–3% of trades | Round 9 |
| Broker rounded every crypto stop to 2 decimals | on sub-$1 pairs (ETH/BTC) every trade stopped out on its first bar; all earlier ETH/BTC cells affected | 2026-10-01 |
| Kronos evidence chart pooled every market and horizon | showed IC 0.28 "above the hurdle" for a rejected model | 2026-10-01; the Evidence tab now leads with the per-market verdict |
