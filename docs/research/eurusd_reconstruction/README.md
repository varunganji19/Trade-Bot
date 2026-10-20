# EUR/USD window reconstructed and replayed — 2026-10-20

The exact archived `EURUSD_1h_2026-01-04_2026-09-04.parquet` file remains
unavailable. Neither Git history nor the local code backup contains it,
and no original pinned EUR/USD result file survives. This evidence does
**not** claim recovery of that file or its byte identity.

The requested `[2026-01-04, 2026-09-04)` window was instead reconstructed
from three separately saved Yahoo snapshots. All **4,107 hourly OHLCV rows
agree exactly**, including their timestamps. The first available trading bar
is January 5 at 00:00 UTC and the last September 3 at 23:00 UTC. No values
were interpolated, fetched or substituted. All source files retain their
original hashes. The runtime pinned-cache loader is unchanged: it still
refuses to silently substitute a differently named rolling cache.

[checks.json](checks.json) records all three source hashes, the reconstructed
content hash, configuration, source hashes and the full original baseline
revision. Current and baseline backtests ran offline with temporary journals;
the current replay was repeated and produced identical trades and equity.
The existing metrics-v3 files remain unchanged.

| Current metrics-v3 result | Value |
|---|---:|
| Trades | 98 |
| Net P&L | -129.24 |
| Fees | 93.93 |
| End equity, from 10,000 | 9,870.76 |
| Return | -1.29% |
| Profit factor | 0.48 |
| Drawdown | -1.33% |
| Sharpe | -3.37 |

The [full export](EURUSD_1h_fxmr.reconstructed.json) contains every trade
and scored equity point. An independent calculation checked both fee legs,
net P&L, return, drawdown, Sharpe and all **3,887 equity points**; maximum
equity discrepancy was `9.094947017729282e-12`. It confirms a losing result
under this configuration, not a promoted strategy.

Seven regression tests reject disagreeing prices, missing/duplicate bars,
naive timestamps, non-finite values, empty windows and duplicate source
paths. To repeat into a new directory:

```sh
ALGO_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 python3 scripts/recover_eurusd_replay.py \
  --output /tmp/algo-eurusd-reconstruction
```

The runner requires the three pinned source hashes, checks agreement before
replay, and refuses an existing output directory. The historical exact-file
comparison remains seven of eight; this is a separately documented recovery
of the missing date window from corroborating snapshots.
