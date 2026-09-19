# Project audit and implemented plan — 2026-09-18, second pass 2026-09-19

The first pass fixed account-reset isolation, reset atomicity, position
sizing, and broker/journal divergence after persistence errors, and listed
four priorities it had not implemented. The second pass re-verified every
claim in this document against the source and closed those four: durable
risk state and event-ordered cash recovery across restart, database-level
ownership of each book across processes, and a reproducible locked
environment that CI actually exercises. One finding the first pass reported
as fixed was not fully fixed — see the second-pass table.

## Scope and evidence

Reviewed the current working tree, including the substantial changes already
present when this audit began. Those edits were preserved; this pass did not
commit, reset, or stage them. The implementation changes are in
`bot/dashboard.py`, `bot/journal.py`, `bot/engine.py`, `bot/risk.py`,
`tests/test_bot.py`, and the new `tests/test_audit_regressions.py`.

Used the codebase-memory graph for architecture, symbol discovery, and
bidirectional traces, then checked the relevant source. The initial full
index generation was `2026-09-18T15:39:31Z`, project
`Users-varun-Downloads-Varun-Algo`. Coverage reported no recorded issue for
the selected source paths. Reported gaps within `bot/` and `tests/` were
generated cache directories. This is best-effort coverage, not a proof that
every runtime path is represented. For example, dynamic risk calls required
source verification beyond the graph's direct caller results.

The main review covered broker accounting, risk sizing, engine persistence and
restart paths, journal transactions, dashboard account/lifecycle operations,
data-cache behavior, and CI/test configuration. Backtest ordering, validation,
and Strategy Lab limits received targeted source checks; the existing strategy,
causality, and accounting tests were executed. Vendored model/JavaScript code,
live exchange behavior, pretrained weights, dependency vulnerability feeds,
and historical profitability were not independently audited.

Baseline: **187 passed, 4 failed**; lint passed. The four failures were in
exchange cooldown and disk-cache tests. Local Python is **3.14.1**; the CI
matrix's Python 3.11–3.13 environments were not run locally.

## Findings fixed in the first pass

| Priority | Finding and evidence | Implemented behavior |
| --- | --- | --- |
| P1 | `api_account_reset` deleted every row in the shared database after stopping only the standard engine. HFT positions, equity, decisions, and transactions could disappear while that engine continued running. | Reset deletes only `mode='paper'` rows. HFT/demo data and shared chat remain. Dashboard reset text now states this scope. |
| P1 | Stop cleared `_engine` before its thread finished. A second reset saw `not_running` and could delete state during an in-flight cycle. Engine construction could also overlap reset. | Stop reports the remaining thread/construction state. Reset rechecks both under the lifecycle lock and holds that lock through its transaction. Startup retains its construction marker until thread publication. |
| P1 | Reset used a best-effort WAL checkpoint plus file copy, then committed deletion, fresh equity, and the reset ledger separately. Backup or insert failure could leave an incomplete account. | `Journal.reset_account` reserves the SQLite writer, makes and checks a SQLite backup through a separate reader, then deletes and inserts the new balance/ledger in one transaction. Failure leaves the original account intact. |
| P1 | `_fill_entry` aborted the journal row when `record_fill` failed, but retained the simulated position and fee. `_close` removed the position and settled cash before a failing journal close. | Entry persistence failure restores cash/fee/P&L totals and removes the simulated fill. Exit persistence failure restores the position and totals so its exit can be retried. Success bookkeeping follows the journal commit. *Incomplete:* the triangular arb settled outside this pattern — see the second-pass table.* |
| P1 | `size_position` used nearest rounding despite promising to round down. At 10,000 equity, 1,600 price and a 60 stop, it returned 1.5625 rounded to 2 shares: 3,200 notional against a 2,500 cap and 120 stop risk against a 100 budget. | Whole-unit and six-decimal quantities round down. Invalid numeric sizing inputs return zero. Approval rejects non-finite equity, confidence, target, and gross exposure rather than letting NaN comparisons bypass gates. |
| P2 | Four baseline tests assumed unprefixed cache filenames or bypassing an explicitly selected exchange's cooldown. A separate sizing test explicitly expected upward rounding. | Updated fixtures/assertions to preserve market-kind cache isolation, require cooldown expiry before a probe, and expect downward sizing. Production cache/cooldown behavior was retained. |

## Plan chosen after the first pass

1. Protect account reset against cross-book deletion, unfinished engine work,
   incomplete backups, and partial reset transactions. **Implemented.**
2. Enforce finite risk inputs and downward quantity sizing. **Implemented.**
3. Keep the paper broker and journal aligned when fill/close writes fail.
   **Implemented for recoverable exceptions in the running process.**
4. Repair obsolete test assumptions, add targeted regressions, and run the
   full suite, lint, dependency checks, and CLI smoke checks. **Implemented.**

The regressions use temporary databases and mocked/local inputs. They include
real SQLite trigger failures to verify transaction rollback, uncheckpointed
WAL backup content, mode isolation, repeat reset attempts, numeric boundaries,
entry rollback, exit retry, and the actual ASGI bearer-token middleware stack.
No operator account reset or trading-engine start was performed.

## Second pass — 2026-09-19

Every finding in the table above was re-verified against the source rather
than against the claim. Rows 1, 2, 3, 5 and 6 hold as written. Row 4 did not:
its two named paths (`_fill_entry`, `_close`) are correct, but the audit's
wider framing — no remaining path mutating broker state without rollback —
was untrue for the triangular arb. The four priorities listed as remaining
have since been implemented; the two P1s were already done when this pass
began and are re-verified below, the two P2s were completed in this pass.

| Priority | Finding and evidence | Implemented behavior |
| --- | --- | --- |
| P1 | `_settle_tri_pending` journaled the arb with neither `entry_fee` nor `realized_cash_delta`, so `close_trade` fell back to `pnl + fees/2` while the broker moved by `pnl` alone. On a 1,000 notional at 30bp the ledger recorded +2.25 more than the broker held; a restart before the next checkpoint replayed the inflated number. Writing the row open-then-close also left a window where process death stranded an `OPEN` TRI-ETH row — a symbol in no watchlist, so nothing could ever mark or close it, and every later restart restored the ghost. | New `Journal.record_round_trip` writes the CLOSED row, its single cash event, the Account-tab row and the equity anchor in ONE transaction, with the exact cash delta separated from the rounded display P&L. The broker moves only after that commit, so a failure anywhere leaves ledger and broker on the pre-arb number and nothing half-written. |
| P2 | Ownership of a book was an in-process variable. The dashboard's guards read `_engine`/`_engine_thread`, all `None` when the engine runs as a separate `main.py run` process, so a deposit committed while that engine held cash in memory was erased by its next checkpoint — reproduced as a silently vanished $500 whose `transactions` row survived. | A `book_owner` row per book is the lease. `claim_book`/`heartbeat_book`/`release_book` take it under `BEGIN IMMEDIATE`; the engine claims it in `own_book()` when it starts trading (CLI loop, single cycle, and both dashboard threads) and its checkpoints and closes carry the token, which is re-checked inside the same transaction. Deposits, withdrawals, resets, the market switch's offline force-close and a second engine start are refused with 409 while any process owns the book. A dead owner's pid frees it at once; a silent one expires after 15 minutes. |
| P2 | `adjust_account` and `reset_account` were the only writers without `@_retry_busy`, and `_adjust_account` mapped only `ValueError`, so cross-process contention surfaced as a 500. | Both retry, and the dashboard maps a surviving busy error to 503 and an ownership refusal to 409. |
| P2 | The `locked` CI job installed only `requirements.lock` and then ran `ruff`, which the lock does not contain — the job failed on every run. It also ran Python 3.12 against a lock compiled for 3.14, so the pinned set was not the resolved set. | The job pins `ruff==0.16.5` alongside the lock, runs the lock's own interpreter, and adds `pip check`. `make lock` records the command that reproduces the file. |

The ownership work was then reviewed against its own failure modes, which
found five more defects in it; all are fixed and covered by regressions:

- The lease ANDed the owner's pid with heartbeat freshness, so an engine on
  an interval longer than the 15-minute TTL — or a laptop that slept — lost
  its own book: its checkpoint was rejected, the dashboard's guard let a
  deposit through, and another process could claim the book. On the owning
  host the pid alone now decides; the heartbeat only speaks for a host whose
  pids cannot be probed.
- `record_round_trip` writes an equity anchor but took no lease, leaving the
  one unguarded path into the state the feature protects.
- `@_retry_busy` on `adjust_account` replayed `on_adjust`, which shifts the
  caller's in-memory risk baselines before the commit: a lost writer race
  applied the delta twice in memory against one row in the database. Only
  the callback-free form retries now.
- A failed thread start in `_spawn_hft_engine` published the engine and
  leaked the lease to a live pid, making the HFT book permanently
  unstartable and unresettable. It now mirrors the paper path's rollback.
- The arb anchored equity on `broker.equity({})`, which marks every open
  position at its entry price — a zero-unrealized point that stayed in the
  curve and in `stats()`'s drawdown scan. It uses the cycle's marks.

Smaller ones from the same review: `owner_token` now distinguishes "mine",
`NO_OWNER` ("nobody's") and None ("not a trading write"), so the market
switch's offline force-close asserts an unowned book inside each close's own
transaction instead of checking beforehand; `close_trade` guards the book its
row belongs to rather than the caller's argument; a lease lost mid-cycle is
re-raised instead of being filed as a degraded cycle; `BookOwnedError` no
longer subclasses `RuntimeError` (a handler for close failures was reporting
it as "retry from the Portfolio tab"); the CLI prints the refusal and exits 1
instead of a traceback; and a failing auto-resume no longer aborts uvicorn
startup, which would leave no UI to explain itself.

### P1 — Persist risk state across restart. **Implemented.**

`RiskManager` takes `state_db_path`/`mode` and restores in its constructor,
before `_restore_positions` and before any cycle can approve an entry.
Verified against the acceptance criteria: a same-day restart keeps a latched
halt after equity recovers; a UTC rollover clears only the daily controls
while `peak_equity`, `dd_risk_scale` and cooldowns survive; `adjust_cash_flow`
shifts both baselines without erasing the trading loss; `risk_state` is keyed
by mode, so the books are independent; backtests construct a memory-only
manager and write nothing. An unreadable checkpoint is preserved for
diagnosis and vetoes new entries rather than being overwritten with defaults.

### P1 — Make cash recovery independent of timestamp windows. **Implemented.**

`_recover_cash` anchors on the last equity row's `cash_event_id` and sums
`cash_events` by id, so clock rollback and same-second events are immaterial.
The audit's own reproduction now returns 9,999.89995 before and after
restart, stable across repeats, separately for paper and HFT, with entry
fees on still-open trades included. Databases written before `cash_events`
existed keep the old timestamp fallback for their untracked legs and switch
to exact cursors at the next checkpoint; the one case that cannot be
reconstructed — a legacy event in the same second as its anchor — is
reported through `Journal.last_error` rather than guessed at.

### P2 — Complete account-operation and replay persistence. **Implemented.**

`adjust_account` recovers the balance inside its own `BEGIN IMMEDIATE`, so
the engine-off read/compute/write is one unit against a SQLite RESERVED
lock — effective across `Journal` instances and across processes — and the
cash event, equity checkpoint, typed ledger row and risk baselines commit
together. The engine-on path snapshots and restores the risk fields its
callback mutates and moves broker cash only after the commit. Ownership of a
standalone CLI engine is now enforced at the database level (above).
`decision_bar_ts` is written on every live fill path, including resting
maker/limit fills and the HFT book, and recovery keys the replay window and
the bars-held clock off it. A parity test drives the same bars continuously
and across a restart and asserts an identical exit bar, exit reason and
bars-held trace.

### P2 — Make dependency reproducibility claims precise. **Implemented.**

`requirements.lock` is a complete `pip-compile` resolution of the direct and
transitive core/dev environment, exercised by a dedicated `locked` CI job
while the Python matrix keeps installing the floor file for upgrade drift.
The job's breakage described above is fixed. `pip check` reports no broken
requirements; that is a consistency check, not a vulnerability verdict.

## Remaining priorities

None from this audit's list. Two limits are disclosed rather than fixed:

- A legacy database's same-second event and anchor cannot be ordered after
  the fact. Recovery reports the ambiguity instead of inventing a number.
- Cross-host ownership rests on the heartbeat alone (a 15-minute lease),
  because a remote pid cannot be probed. Sharing one SQLite file across
  hosts was never a supported deployment.

## Validation

Second pass, on the current tree:

- `python3 -m pytest tests/ -q`: **278 passed** (258 before this pass, plus
  16 ownership cases, 3 arb-settlement cases and the restart parity case).
  One Starlette/httpx TestClient deprecation warning.
- Each new regression was confirmed to FAIL against the pre-fix behavior
  before being kept, by reverting the fix and re-running it: the parity test
  against a restore that ignores `decision_bar_ts`, the arb tests against the
  `pnl + fees/2` fallback and the `equity({})` anchor, and the lease tests
  against the stale-heartbeat rule, the unguarded round trip and the blind
  retry.
- `python3 -m ruff check bot main.py config.py run_battery.py tests scripts`:
  **passed**.
- `git diff --check`: **passed**.
- All **16 CLI subcommand `--help` checks passed**.
- `python3 -m pip check`: **no broken requirements found**.
- `requirements.lock` was re-resolved into a scratch file to confirm it is a
  genuine, installable `pip-compile` output; the committed pins were kept
  rather than bumped, since upgrading pytest/skfolio is not an audit fix.

Historical backtest reports were not regenerated. Flooring quantities can
change future backtest results, especially for whole-share positions; the
intended behavioral change is adherence to the configured sizing budget.
Live exchange behavior, vendored model code, pretrained weights, dependency
vulnerability feeds and historical profitability remain outside this audit.
