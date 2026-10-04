# Verified report and follow-up

The report is substantially correct. Its re-entry regression and three smaller
code gaps were verified and addressed. A timer alone cannot safely resolve an
uncertain exchange order, and the backup does not prove an intentional,
completed journal reset. The [implementation plan](BUG_FIX_FOLLOWUP_PLAN.md)
records the verdict for each observation.

## Re-entry timing

The original 300-candle reproduction produced 27 trades with one-candle
exit-to-entry gaps on baseline `0c74d1b`, versus 20 trades and two-candle gaps
on the v2 working tree. The fixed implementation again produces 27 trades.

A signal/time exit still executes at the following candle's open. Its
cooldown starts at the preceding decision candle, and the following close
can make an entry decision if cooldown permits. Stop/target exits retain
their current-candle skip and cooldown. Fill timestamps and exit-at-open
precedence remain accurate.

Regression coverage includes market and maker entries, signal and time exits,
one- and three-candle cooldowns, stop/target skips, and final liquidation.
The parity smoke now runs the actual live engine for 80 candles under each
of two Scalper cooldown policies; live decision timestamps match backtest
fill timestamps shifted by one candle. It checks exact 27/16 trade counts.
An independent review also passed 20 focused regressions and that smoke.

## Uncertain exchange requests

The new [`testnet resolve` command](TESTNET.md#resolving-an-uncertain-execution)
takes the book lease, validates order identity and settles confirmed terminal
evidence once. It preserves existing entry/full halts and never sends a
replacement order. Audit records are separate from immutable intent inputs
and cash events.

An order that truly never reached the venue can be resolved through an
explicit operator assertion, with required reason/evidence, a fresh typed
not-found result, no previously observed acceptance/fills, empty account
order/trade checks and independently consistent reconciliation. Absence
alone remains insufficient. This is deliberately an operator assertion,
not an automatic inference after a time limit: Binance documents that
[timeouts/server errors can leave execution uncertain and API data sources can lag](https://developers.binance.com/en/docs/products/spot/rest-api).

Verified exchange identity and cumulative fills are persisted before active
status, missing commissions or other follow-up checks can interrupt
resolution. This also applies to greater fills observed after cancellation.
Later missing API data cannot erase that evidence, including after restart.
Independent audit reproduced an accepted order disappearing after restart;
abandonment was correctly refused, without cash/inventory/audit changes.

Seventy-two targeted fake-exchange tests pass, including settlement/recovery,
partial exits and dust, legacy imports, operator disagreement, idempotence,
observed-order disappearance, monotonic fills and migration boundaries.
The bounded independent operator audit separately passed 33 regressions.
No real testnet account was contacted.

Recovery still reloads committed position state each cycle. Compatibility
order-log exports now skip unchanged revisions; failed writes remain
eligible for retry.

## Inputs and housekeeping

Eight-digit ASCII timestamp strings explicitly mean `YYYYMMDD` in UTC;
invalid compact calendar dates are rejected. Numeric JSON values and
ordinary signed/decimal/scientific epoch strings retain the existing
seconds/milliseconds rules. ISO dates remain supported. The validator's
explicit CSV boolean parsing and zero-exit-fee fixes remain covered.
Seventy-four validator tests pass, including 14 compact-date cases.

The exact `.db-shm` and `.db-wal` backup sidecars are now ignored. They were
preserved. The backup's filename timestamp and filesystem birth time agree
on **October 3, 2026, 21:13:41 IST**. This chat began **21:35:48 IST**, with
the sidecars already present; the implementation instruction arrived later.
The reset implementation creates its backup before committing the reset
transaction. These facts support a prior backup/reset attempt, but establish
neither a successful reset nor its actor or intent. Intent remains unknown.
This investigation used metadata, reset source and captured chat history;
it did not open, reset, restore or delete the real SQLite journal.

## Corrected research

[Metrics v3 evidence](research/verified_metrics_v3/README.md) contains seven
available pinned windows and all 225 experiment cases. The exact pinned
EURUSD cache is unavailable and explicitly skipped. Historical and v2
reports were preserved; the v2 documents describe that earlier checkpoint.

Every v3 run was computed afresh with networking disabled and temporary
journals. The original comparison baseline is pinned to full revision
`0c74d1b372ffbe29894e13d9ea5800a924d6e7a9`. Previously computed v1 results
were reused only after validating their source/configuration, exact input
hashes and prior evidence. They were not reused as current v3 results.

Re-entry affects some strategies, including Turtle as well as Scalper.
Extra trades can improve or worsen P&L. BTC 4h Connors retains P&L 76.91
USDT and the corrected Sharpe 1.81 (original 14.86); correcting re-entry
does not reverse the complete-equity-curve correction.

Independent validation reconstructs every equity point in all seven pinned
exports, with zero observed discrepancy, and reconciles net P&L to equity
within 1e-8 USDT. Fees, returns, wins, drawdown and Sharpe agree at reported
precision. All 225 experiment case summaries and input provenance pass;
those summaries omit raw trade/equity data, limiting independent metric
recalculation for experiments. Four no-activity cases retain undefined
Sharpe explicitly. The saved audit records these limits and 469 checks of
110 unique referenced artifacts.

## Delivery verification

Code changes are organized in dependency order on `codex/bugfix-followup`.
Each commit snapshot is reconstructed from its staged Git tree and checked
with `make verify`, temporary journals and deterministic fake exchanges.
Shared journal/position/execution/replay interfaces stay in one coherent
commit so intermediate revisions remain runnable. Independent recorder,
validator and housekeeping changes have separate commits.

The completed commit verification records are stored beside the research
evidence. Final suite and independent export checks are recorded there.
The complete code suite passes **659 Python tests**, Python and JavaScript
lint, and strategy/engine/lifecycle parity. Eight implementation snapshots
passed independently before their commits; the final evidence is separate
from historical reports.
No push, deployment, live exchange submission or real journal reset was
performed by this work.
