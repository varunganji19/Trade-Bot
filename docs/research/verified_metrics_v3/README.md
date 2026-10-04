# Corrected research evidence — metrics version 3

The signal/time re-entry regression is fixed. These offline artifacts replay
**7 of 8 pinned windows and all 225 experiment cases** using the same
configuration and exact inputs as metrics v2. Historical and v2 reports
are preserved. The exact pinned EURUSD cache is unavailable and skipped.

Every v3 result was computed afresh. The run reused only original v1
comparison results after verifying prior evidence, source, configuration,
and exact input hashes. The original backtest is pinned to revision
`0c74d1b372ffbe29894e13d9ea5800a924d6e7a9`. Networking was disabled and
journals were temporary. Source hashes identify the actual working-tree
implementation; `git_head` records the revision at replay startup.

Signal/time exits fill at the following open and start cooldown at the
preceding decision candle. Stop/target behavior, complete scored equity,
net P&L accounting, additive fold aggregation and observed-return Sharpe
remain corrected. All figures below use independently funded 10,000 USDT
runs; experiment cases are not pooled into a portfolio statistic.

| Window / strategy | v2 trades | v3 trades | v2 net P&L | v3 net P&L | v2 Sharpe | v3 Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BTC 1h / Turtle | 115 | 115 | -440.66 | -440.66 | -0.67 | -0.67 |
| ETH 1h / Turtle | 69 | 70 | 151.27 | 78.44 | 0.31 | 0.18 |
| SOL 1h / Turtle | 56 | 56 | 1,048.81 | 1,048.81 | 1.71 | 1.71 |
| BTC 4h / Connors_Meanrev | 6 | 6 | 76.91 | 76.91 | 1.81 | 1.81 |
| ETH 4h / Connors_Meanrev | 4 | 4 | -23.17 | -23.17 | -0.52 | -0.52 |
| BTC 15m / Vwap_Scalper | 113 | 114 | -971.49 | -968.90 | -6.39 | -6.35 |
| ETH 15m / Vwap_Scalper | 155 | 157 | -782.95 | -840.78 | -2.89 | -3.10 |

Re-entry changes some Turtle and Scalper runs and can improve or worsen
P&L. BTC 4h Connors remains at 76.91 USDT P&L and Sharpe 1.81; the original
same-configuration Sharpe was 14.86. Re-entry does not reverse the correction
from including flat/HOLD candles in the full equity curve.

| Experiment | Completed cases | Detailed comparison |
| --- | ---: | --- |
| standard_gate | 36 | [standard_gate.comparison.json](standard_gate.comparison.json) |
| fast_gate | 105 | [fast_gate.comparison.json](fast_gate.comparison.json) |
| market_maker_fill_model | 84 | [market_maker_fill_model.comparison.json](market_maker_fill_model.comparison.json) |

[`comparison.json`](comparison.json) records configuration, input/source
hashes, baseline provenance, missing input, prior reuse and per-window
comparisons. Each pinned JSON contains full-precision trades and the entire
equity curve. Detailed experiment comparisons contain every independent
case with v1, v2 and v3 statistics.

All reported numeric metrics are finite. Four flat experiment cases have
undefined Sharpe (`null`), with zero trades, P&L and fees; they are retained
as valid no-activity results rather than assigned an invented Sharpe.

Independent export verification is recorded in
[`export_checks.json`](export_checks.json); commit snapshot verification is
recorded in [`commit_checks.json`](commit_checks.json). The follow-up
implementation and limits are documented in
[`BUG_FIX_FOLLOWUP.md`](../../BUG_FIX_FOLLOWUP.md).

All seven pinned exports reconcile net P&L with end-minus-start equity
within 1e-8 USDT. Every scored equity point was independently reconstructed
from full-precision trades, fees and cached close prices with zero observed
error. Independently recomputed balances, returns, wins, profit factors,
drawdown and Sharpe match their reported precision. The audit verified 110
unique referenced artifacts through 469 hash checks.

Experiment exports contain summary statistics without raw trades or stitched
curves. Their 225 case identities, input geometry, provenance, cache
comparisons and finite numeric metrics were verified; independent
drawdown/Sharpe recalculation from those summary artifacts is unavailable.
Fold/CV arithmetic is covered by the deterministic regression suite.

The complete code suite passes **659 Python tests**, Python/JavaScript lint
and the deterministic parity smoke, including actual live-engine lifecycle
checks. All eight implementation commit snapshots passed `make verify`.

Reproduce into a **new, empty directory** from the repository root:

```sh
ALGO_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 python3 scripts/rerun_corrected_research.py \
  --workers 4 --baseline-ref 0c74d1b \
  --output /tmp/algo-metrics-v3-replay
```

This recomputes the original v1 comparisons as well. The v2 evidence this
run reused (see `prior_reuse` in `comparison.json`) was removed from the
tree; restore it from commit `9224627` into a directory and pass that as
`--prior-results` to reuse verified v1 results instead. Keep
the original `--baseline-ref` explicit so later commits cannot change the
comparison baseline. Existing output directories are refused to preserve
earlier evidence. Historical v2 reproduction text describes its earlier
runner checkpoint; the current runner produces metrics version 3.
