# Self-check: ten questions a mentor is likely to ask

*Roadmap M6. Each question has a short answer and the code and test that
back it, so the answer can be shown rather than asserted. Open the file,
read the function, run the test. If you can explain each one without these
notes, you own the system.*

Run any single test with `python3 -m pytest tests/<file>.py -k <name> -q`.

---

### 1. Does it make money?

No. Under the platform's own rule nothing may trade: no strategy's 90%
profit-factor interval clears 1.0 after fees on two years of data.

- Verdicts: `experiments/standard_gate.results.json`, `experiments/fast_gate.results.json`
- Rule: `verdicts_from_evidence` in `bot/promotion.py`
- Test: `test_rule_v2_promotes_only_proven_strategies` (tests/test_experiments.py)

### 2. How do you know the backtest does not look into the future?

Decisions use closed bars only, and that is tested by truncation: cutting the
data at bar *i* must not change the decision at bar *i*.

- Code: `Backtester.run` in `bot/backtest.py` (decide on bar *i*, fill at *i+1*'s open);
  the forming bar is dropped in `bot/data/validate.py`
- Checks: `scripts/parity_smoke.py` ("causality"), run by `make verify`
- Tests: `test_strategies_never_read_future`, `test_donchian_no_lookahead` (tests/test_bot.py),
  `test_regime_label_is_causal` (tests/test_evidence_stats.py)

### 3. Is the live bot running the same code as the backtest?

Yes. Both drive the same `Orchestrator`, `RiskManager` and `PaperBroker`;
the LLM and news feed were removed from the decision path because a
backtest could not replay them.

- Code: `bot/engine.py` and `bot/positions.py` (live), `bot/backtest.py` (backtest)
- Check: `scripts/parity_smoke.py` ("decision: engine orchestrator == backtest orchestrator")

### 4. How are trading costs modelled?

Fees on both legs; market orders pay taker fee plus adverse slippage;
a take-profit is a resting limit (maker fee, no slippage); stops gapped
through fill at the open, never at the better level. A stop tighter than
the round-trip cost is refused.

- Code: `CostConfig` in `config.py`, `PaperBroker.scan_bar_exits` in `bot/broker.py`,
  the dust gate in `RiskManager.approve` (`bot/risk.py`)
- Tests: `test_broker_gap_through_stop_fills_at_open`,
  `test_take_profit_gap_fill_is_favorable_and_maker_priced`

### 5. You tried many variants. Isn't the best one just luck?

That is what the experiment registry and the Deflated Sharpe ratio are for.
Experiments are declared in git before they run, every variant is recorded,
and the Deflated Sharpe reads the trial count from the registry.

- Code: `bot/experiments.py` (`load_declaration`, `trials_for`),
  `deflated_sharpe` and `pbo_cscv` in `bot/validation.py`
- Tests: `test_uncommitted_declaration_is_refused`, `test_a_trial_is_a_distinct_configuration`,
  `test_deflated_sharpe_quantifies_selection`, `test_pbo_cscv_over_config_family`

### 6. Why should anyone trust the promotion gate?

It votes only on out-of-sample trades, asks for the *pessimistic* end of a
block-bootstrap interval, needs 100+ trades over three market regimes, and
refuses to publish partial evidence.

- Code: `bot/evidence_stats.py` (`bootstrap_interval`, `label_regimes`), `bot/promotion.py`
- Tests: `test_bootstrap_is_deterministic_and_brackets_the_point`,
  `test_gate_refuses_to_publish_partial_evidence`,
  `test_orchestrator_silences_probation_under_rule_v2`

### 7. What stops one bad day from emptying the account?

A chain of vetoes before every entry: 1% risk per trade, at most 25% of
equity per position and 1.0× gross across both books, a cap on correlated
families, a −3% daily kill switch, and a drawdown throttle that halves risk
at −10% and quarters it at −20%. Risk state survives restarts.

- Code: `RiskManager.approve`, `size_position`, `note_equity` in `bot/risk.py`
- Tests: `test_risk_daily_kill_switch`, `test_drawdown_throttle_scales_risk_in_drawdown`,
  tests/test_risk_state_persistence.py

### 8. What happens when a strategy stops working?

The drift monitor compares a promoted strategy's live results with its
expected range every week; three weeks below it and it is demoted
automatically, and re-running the evidence cannot hand the vote back.

- Code: `check` in `bot/drift.py`, the overlay in `promotion.load_verdicts`
- Test: `test_three_weeks_below_the_range_demotes_and_the_gate_obeys` (tests/test_drift.py)

### 9. What if the process crashes in the middle of a trade?

The journal is written first: the trade row exists before the simulated
fill and is completed or aborted with it, so no unbracketed position
survives. On restart, cash is rebuilt from the last equity anchor plus the
cash events after it, and every write requires the book's lease so a second
engine cannot fork the account.

- Code: `_fill_entry` in `bot/positions.py`, `recover_cash` and `claim_book` in `bot/journal/ledger.py`
- Tests: `test_engine_aborts_journal_row_when_fill_fails`,
  `test_engine_crash_window_cash_reconciliation_is_exact`, tests/test_book_ownership.py

### 10. Do the numbers on the dashboard actually add up?

Every poll reconciles each book: cash must equal starting capital plus net
deposits plus realized P&L minus open entry fees. A gap shows a banner that
names each term; it is never averaged away.

- Code: `ledger_check` in `bot/journal/ledger.py`, the banner in `bot/static/app.js`
- Tests: `test_a_mismatched_journal_raises_the_banner`, `test_a_consistent_book_reconciles`
  (tests/test_ledger.py)

---

Related: [ARCHITECTURE.md](ARCHITECTURE.md) draws the decision path, risk
gates, promotion gate and fill model; [METHODOLOGY.md](METHODOLOGY.md)
states every rule; [RESULTS.md](RESULTS.md) lists what was accepted and
rejected.
