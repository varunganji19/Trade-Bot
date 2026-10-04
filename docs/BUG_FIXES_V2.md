# Reviewed bug fixes, metrics version 2

The 13 reviewed findings are implemented with regression coverage. All
execution tests use temporary journals and fake exchanges. Schema migrations
are additive and idempotent; existing cash events remain immutable.

| Findings | Change | Main regression coverage |
| --- | --- | --- |
| 1, 2, 9 | Durable testnet intents and terminal settlement; restart recovery; partial inventory, precision dust and independent reconciliation | `tests/test_testnet_execution.py`, `tests/test_testnet.py` |
| 3 | Resting orders reapproved at the fill price against refreshed equity, positions, portfolio exposure, pause and cooldown; original quantity ceiling applied before caps | `tests/test_risk_replay_regressions.py`, `tests/test_backtest_regressions.py` |
| 4 | Every-cycle chronological hard-bracket replay, actual fill timestamps and persisted cursors; incomplete/unavailable held history blocks entries while available protection continues | `tests/test_risk_replay_regressions.py`, `tests/test_fill_recovery.py` |
| 5, 6 | Chronological backtest execution; every eligible candle checked; complete scored equity and terminal liquidation | `tests/test_backtest_regressions.py` |
| 7 | Fixed-capital additive fold stitching; fold transitions excluded from observed one-bar Sharpe returns | `tests/test_backtest_regressions.py` |
| 8 | CV returns and wins use full-precision net P&L and required starting capital | `tests/test_backtest_regressions.py`, `tests/test_bot.py` |
| 10 | Shared streaming gzip recovery, quarantine, locked atomic repair before append, later-member recovery and retained failed buffers | `tests/test_recorder.py` |
| 11–13 | Normalized explicit CSV booleans, numeric epoch strings/UTC timestamps, preserved zero close fee | `tests/test_validator.py` |

Recovery settles confirmed fills automatically and preserves the testnet
entry kill until an operator explicitly clears it. A full kill remains full.
UNKNOWN submissions are queried by the durable exchange/client ID and are
never submitted again because a lookup returns “not found.” A partial exit
keeps its remaining quantity and protective brackets; precision dust remains
visible in marked equity, exposure, statistics and reconciliation without
occupying a normal position slot. Trade rows retain the original quantity;
remaining quantity is explicit in restoration and position responses.

Research reports now identify `metrics_version=2`, starting capital and the
net-P&L return basis. Corrected fold returns add independent fixed-capital P&L.
Trade exports retain full precision, including sub-cent fee losses. Archived
reports are retained. The offline replay command is:

```sh
ALGO_SKIP_DOTENV=1 python3 scripts/rerun_corrected_research.py --workers 4 --output docs/research/verified_metrics_v2
```

The final comparison artifacts include identical-configuration v1/v2 runs,
archived report comparisons, exact cached input endpoints and hashes, and
source hashes. They are written separately under `docs/research/verified_metrics_v2`.
The exact pinned EURUSD cache is unavailable and is explicitly excluded;
an unrelated rolling cache is not substituted.

The completed offline rerun covers **7 available pinned windows and all 225
experiment cases**. Source hashes and 108 referenced artifact hashes were
verified, and historical reports remain unchanged. Every detailed pinned
export reconciles net trade P&L with equity; independent calculations agree
with its reported fees, returns, win rate, drawdown and Sharpe. See the
[comparison guide](research/verified_metrics_v2/README.md) for the corrected
metrics and the recorded missing input.

Additional boundary regressions cover base-asset sell commissions, CCXT's
zero-precision exception, recovered inventory lacking a stop, unavailable
held feeds, and failed scan checkpoints. The experiment test fixture uses a
stable random seed, and the dashboard lifecycle test joins its background
engine before removing its fake-feed journal.

Final verification passed through `make verify`: **565 Python tests**, Python
and JavaScript lint, and the deterministic live/backtest parity smoke. The
original baseline contained 422 passing tests.
