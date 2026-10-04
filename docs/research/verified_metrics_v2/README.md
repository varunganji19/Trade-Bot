# Corrected research evidence — metrics version 2

> **Artifacts removed.** The v2 JSON evidence was superseded by
> [metrics v3](../verified_metrics_v3/README.md) and removed from the tree.
> It remains in git history at commit `9224627`
> (`git show 9224627:docs/research/verified_metrics_v2/comparison.json`).

These artifacts replay the available pinned research and experiment windows
using cached inputs. Network connections are disabled, and journals are
temporary. Historical reports are preserved.

Completed: **7 of 8 pinned windows and all 225 experiment cases**. The
recorded source hashes and 108 referenced input/declaration/report hashes
were verified after the run. `make verify` passed with 565 Python tests,
Python and JavaScript lint, and the deterministic parity smoke.

`comparison.json` records the source and input hashes, configuration, skipped
inputs, and comparisons with both the original backtest and archived reports.
The original and corrected backtests use identical current configuration and
shared risk/broker modules. Differences from archived reports can also reflect
historical configuration changes.

The corrected statistics use complete scored equity curves and net P&L after
fees. Walk-forward folds add independent fixed-capital P&L, and warmup gaps
between folds are excluded from observed one-bar Sharpe returns.

Each pinned JSON contains the full equity curve and full-precision trades.
The experiment comparison files contain per-case results and differences.
They do not pool independently funded cases into a portfolio statistic.

The exact pinned EURUSD cache is unavailable and is explicitly skipped.

The same-configuration pinned comparisons are:

| Window / strategy | Original net P&L | Corrected net P&L | Original Sharpe | Corrected Sharpe |
| --- | ---: | ---: | ---: | ---: |
| BTC 1h / Turtle | -471.86 | -440.66 | -1.32 | -0.67 |
| ETH 1h / Turtle | 38.48 | 151.27 | 0.26 | 0.31 |
| SOL 1h / Turtle | 1,036.49 | 1,048.81 | 3.27 | 1.71 |
| BTC 4h / Connors | 76.91 | 76.91 | 14.86 | 1.81 |
| ETH 4h / Connors | -23.16 | -23.17 | unavailable | -0.52 |
| BTC 15m / Scalper | -999.15 | -971.49 | -27.76 | -6.39 |
| ETH 15m / Scalper | -786.22 | -782.95 | -12.39 | -2.89 |

P&L is in USDT on independently funded 10,000 USDT runs. The unchanged BTC
Connors P&L with a much lower Sharpe illustrates the effect of including every
scored candle. Other changes also reflect corrected execution timing and
full-precision trade accounting.

| Experiment | Completed cases | Detailed comparison |
| --- | ---: | --- |
| Standard gate | 36 | [`standard_gate.comparison.json`](standard_gate.comparison.json) |
| Fast gate | 105 | [`fast_gate.comparison.json`](fast_gate.comparison.json) |
| Maker fill model | 84 | [`market_maker_fill_model.comparison.json`](market_maker_fill_model.comparison.json) |

[`export_checks.json`](export_checks.json) records independent checks of
the seven detailed pinned exports: summed net trade P&L reconciles with
equity within 1e-8 USDT; reported balances, fees, returns, win rates, drawdown
and Sharpe agree with the full-precision trades and equity curves at their
reported precision. Every experiment case is present with finite metrics.

Reproduce from the repository root:

```sh
ALGO_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 python3 scripts/rerun_corrected_research.py --workers 4 --output docs/research/verified_metrics_v2
```

Implementation and regression coverage are documented in
[`BUG_FIXES_V2.md`](../../BUG_FIXES_V2.md).
