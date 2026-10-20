# Strategy validation: backtest-result.json

Source: freqtrade. Variants tried (--trials): not given, 1 assumed.

Probability of backtest overfitting (PBO) across the strategies in the file: not measured; needs two or more strategies run on the same period (a freqtrade file with several strategies).

## StrategyTestV3: **FRAGILE**

179 closed trades, 2018-01-10 to 2018-01-30; net P&L +0.001041902986 BTC; win rate 95.0%.

Why it is not robust:

- number of variants tried not given (--trials): 1 was assumed, the most generous case
- regime coverage not checked: no daily prices (pass --regime-market or --prices)

What held up:

- profit-factor interval 1.44–5.95 stays above 1.0
- profitable in 4 of 4 quarters
- survives doubled fees (PF 1.19)
- Deflated Sharpe 0.99 for 1 variant(s) tried

| Check | Result |
|---|---|
| Profit factor (90% interval) | 2.42 (1.44–5.95) |
| Sharpe per trade (90% interval) | 0.247 (0.104–0.495) |
| Deflated Sharpe (1 variants) | 0.99 |

Stability over time (profit factor per quarter):

| Period | Trades | PF |
|---|---:|---:|
| 2018-01-10 → 2018-01-15 | 46 | 5.76 |
| 2018-01-15 → 2018-01-20 | 57 | 1.72 |
| 2018-01-20 → 2018-01-25 | 40 | 3.06 |
| 2018-01-25 → 2018-01-30 | 36 | 1.55 |

Cost sensitivity:

| Fees | PF | Net P&L |
|---|---:|---:|
| ×0.5 | 3.10 | +0.001490705364 BTC |
| ×1 | 2.42 | +0.001041902986 BTC |
| ×2 | 1.19 | +0.0001442982282 BTC |

Market regimes (on the day each trade opened):

Not checked: no daily prices (pass --regime-market or --prices).

---

*Robust* means every check passed and every check ran: at least 100 trades, the pessimistic end of the profit-factor interval at or above 1.0, profitable in 3 of 4 quarters, still profitable with fees doubled, Deflated Sharpe ≥ 0.95 for the variants tried, PBO below 0.5 when measurable, and 10+ trades in each of 3 market regimes. *Likely overfit* means the evidence points the other way. Anything else is *fragile*: not disproven, not proven. This is a statistical check of a backtest, not investment advice.
