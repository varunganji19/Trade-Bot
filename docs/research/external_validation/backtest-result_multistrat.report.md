# Strategy validation: backtest-result_multistrat.json

Source: freqtrade. Variants tried (--trials): not given, 1 assumed.

Probability of backtest overfitting (PBO) across the strategies in the file: **1.00** over 2 strategies (0.5 or more: picking the best of them is worse than a coin flip).

## StrategyTestV2: **LIKELY OVERFIT**

179 closed trades, 2018-01-10 to 2018-01-30; net P&L +0.02608549949 BTC; win rate 95.0%.

Why it is not robust:

- PBO 1.00: picking the best of the strategies in this file does worse than a coin flip out of sample
- number of variants tried not given (--trials): 1 was assumed, the most generous case
- regime coverage not checked: no daily prices (pass --regime-market or --prices)

What held up:

- profit-factor interval 2.65–99.00 stays above 1.0
- profitable in 4 of 4 quarters
- survives doubled fees (PF 5.58)
- Deflated Sharpe 1.00 for 1 variant(s) tried

| Check | Result |
|---|---|
| Profit factor (90% interval) | 6.07 (2.65–99.00) |
| Sharpe per trade (90% interval) | 0.374 (0.204–0.681) |
| Deflated Sharpe (1 variants) | 1.00 |

Stability over time (profit factor per quarter):

| Period | Trades | PF |
|---|---:|---:|
| 2018-01-10 → 2018-01-15 | 46 | 99.00 |
| 2018-01-15 → 2018-01-20 | 57 | 43.66 |
| 2018-01-20 → 2018-01-25 | 40 | 1.11 |
| 2018-01-25 → 2018-01-30 | 36 | 3.03 |

Cost sensitivity:

| Fees | PF | Net P&L |
|---|---:|---:|
| ×0.5 | 6.18 | +0.02653430187 BTC |
| ×1 | 6.07 | +0.02608549949 BTC |
| ×2 | 5.58 | +0.02518789474 BTC |

Market regimes (on the day each trade opened):

Not checked: no daily prices (pass --regime-market or --prices).

## TestStrategy: **LIKELY OVERFIT**

179 closed trades, 2018-01-10 to 2018-01-30; net P&L +0.02608549949 BTC; win rate 95.0%.

Why it is not robust:

- PBO 1.00: picking the best of the strategies in this file does worse than a coin flip out of sample
- number of variants tried not given (--trials): 1 was assumed, the most generous case
- regime coverage not checked: no daily prices (pass --regime-market or --prices)

What held up:

- profit-factor interval 2.65–99.00 stays above 1.0
- profitable in 4 of 4 quarters
- survives doubled fees (PF 5.58)
- Deflated Sharpe 1.00 for 1 variant(s) tried

| Check | Result |
|---|---|
| Profit factor (90% interval) | 6.07 (2.65–99.00) |
| Sharpe per trade (90% interval) | 0.374 (0.204–0.681) |
| Deflated Sharpe (1 variants) | 1.00 |

Stability over time (profit factor per quarter):

| Period | Trades | PF |
|---|---:|---:|
| 2018-01-10 → 2018-01-15 | 46 | 99.00 |
| 2018-01-15 → 2018-01-20 | 57 | 43.66 |
| 2018-01-20 → 2018-01-25 | 40 | 1.11 |
| 2018-01-25 → 2018-01-30 | 36 | 3.03 |

Cost sensitivity:

| Fees | PF | Net P&L |
|---|---:|---:|
| ×0.5 | 6.18 | +0.02653430187 BTC |
| ×1 | 6.07 | +0.02608549949 BTC |
| ×2 | 5.58 | +0.02518789474 BTC |

Market regimes (on the day each trade opened):

Not checked: no daily prices (pass --regime-market or --prices).

---

*Robust* means every check passed and every check ran: at least 100 trades, the pessimistic end of the profit-factor interval at or above 1.0, profitable in 3 of 4 quarters, still profitable with fees doubled, Deflated Sharpe ≥ 0.95 for the variants tried, PBO below 0.5 when measurable, and 10+ trades in each of 3 market regimes. *Likely overfit* means the evidence points the other way. Anything else is *fragile*: not disproven, not proven. This is a statistical check of a backtest, not investment advice.
