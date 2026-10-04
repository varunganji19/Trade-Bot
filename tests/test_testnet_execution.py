"""Crash/retry behavior of actual external fills, entirely against a fake."""
import time

import ccxt
import pytest

from bot import testnet as tn
from bot.journal import BookOwnedError, Journal
from config import CONFIG
from tests.test_testnet import FakeBinance, SPEC, _enter, _long


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "trading.db"))
    ex, journal = FakeBinance(), Journal()
    engine = tn.build_testnet_engine(exchange=ex, journal=journal)
    engine.quiet = True
    tn.snapshot_baseline(ex, ["BTC/USDT", "ETH/USDT"])
    return ex, journal, engine


def restart(ex, journal):
    engine = tn.build_testnet_engine(exchange=ex, journal=journal)
    engine.quiet = True
    return engine


def close(engine):
    summary = {"errors": [], "closed": []}
    engine._close(SPEC, 100, "stop loss", summary, bar_epoch=time.time(), write_equity=False)
    return summary


@pytest.mark.parametrize("leg", ["entry", "exit"])
def test_journal_failure_keeps_execution_for_restart_without_resubmission(env, monkeypatch, leg):
    ex, journal, engine = env
    if leg == "exit":
        _enter(engine, 1)
    original = journal.settle_testnet_order
    monkeypatch.setattr(journal, "settle_testnet_order", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk failed")))
    with pytest.raises(RuntimeError, match="disk failed"):
        _enter(engine, 1) if leg == "entry" else close(engine)
    assert journal.testnet_orders(pending=True)[0]["state"] == "CONFIRMED"
    assert journal.recent_trades(mode=tn.MODE)[0]["status"] == ("PENDING" if leg == "entry" else "OPEN")
    assert ex.balances["BTC"] == pytest.approx(1.999 if leg == "entry" else 1)
    assert tn.kill_state()["level"] == "entries"
    monkeypatch.setattr(journal, "settle_testnet_order", original)
    recovered = restart(ex, journal)
    assert recovered.broker.recover() == []
    assert len(ex.orders) == (1 if leg == "entry" else 2)
    assert len(journal.open_trades(mode=tn.MODE)) == (1 if leg == "entry" else 0)
    assert len(recovered.broker.positions) == (1 if leg == "entry" else 0)
    cash = recovered.broker.cash
    assert recovered.broker.recover() == []
    assert recovered.broker.cash == cash
    assert tn.kill_state()["level"] == "entries"
    assert journal.ledger_check(tn.MODE)["gap"] == 0


def test_timeout_after_fill_uses_client_id_and_not_found_does_not_resubmit(env, monkeypatch):
    ex, journal, engine = env
    create = ex.create_order
    def timeout(*a, **k):
        create(*a, **k)
        raise ccxt.RequestTimeout("accepted but response was lost")
    monkeypatch.setattr(ex, "create_order", timeout)
    with pytest.raises(ccxt.RequestTimeout):
        _enter(engine, 1)
    intent = journal.testnet_orders(pending=True)[0]
    assert intent["state"] == "UNKNOWN"
    assert intent["client_order_id"] == ex.orders["1"]["clientOrderId"]
    fetch = ex.fetch_order
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: (_ for _ in ()).throw(ccxt.OrderNotFound("temporarily absent")))
    recovered = restart(ex, journal)
    assert recovered.broker.recover()
    assert recovered.broker.execution_blocked
    with pytest.raises(tn.TestnetError, match="kill switch"):
        _enter(recovered, 1)
    assert len(ex.orders) == 1
    monkeypatch.setattr(ex, "fetch_order", fetch)
    assert recovered.broker.recover() == []
    assert recovered.broker.positions[("BTC/USDT", "1h")].qty == pytest.approx(.999)
    assert len(ex.orders) == 1
    assert tn.kill_state()["level"] == "entries"


def test_crash_before_network_call_remains_unknown_and_full_kill_is_preserved(env):
    ex, journal, engine = env
    intent = journal.prepare_testnet_entry(SPEC, _long(), 1, 100)
    journal.testnet_order_state(intent["id"], "SUBMITTING")
    tn.set_kill("all", "operator")
    assert engine.broker.recover()
    assert journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"
    assert tn.kill_state()["level"] == "all"
    assert ex.orders == {}


def test_unsubmitted_prepared_intent_is_abandoned_without_an_exchange_order(env):
    ex, journal, engine = env
    journal.prepare_testnet_entry(SPEC, _long(), 1, 100)
    assert engine.broker.recover() == []
    assert journal.recent_trades(mode=tn.MODE)[0]["status"] == "ABORTED"
    assert not engine.broker.execution_blocked
    assert ex.orders == {}
    assert tn.kill_state()["level"] == "entries"


def test_confirm_write_failure_can_recover_the_same_order(env, monkeypatch):
    ex, journal, engine = env
    original = journal.confirm_testnet_order
    monkeypatch.setattr(journal, "confirm_testnet_order", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("confirm failed")))
    with pytest.raises(RuntimeError):
        _enter(engine, 1)
    assert journal.testnet_orders(pending=True)[0]["state"] == "SUBMITTING"
    monkeypatch.setattr(journal, "confirm_testnet_order", original)
    assert restart(ex, journal).broker.recover() == []
    assert len(ex.orders) == 1
    assert journal.open_trades(mode=tn.MODE)[0]["qty"] == pytest.approx(.999)


def test_committed_settlement_retry_refreshes_cash_without_double_application(env, monkeypatch):
    ex, journal, engine = env
    _enter(engine, 1)
    original = journal.settle_testnet_order
    def committed_then_error(*a, **k):
        original(*a, **k)
        raise RuntimeError("after commit")
    monkeypatch.setattr(journal, "settle_testnet_order", committed_then_error)
    with pytest.raises(RuntimeError):
        close(engine)
    assert journal.testnet_orders()[-1]["state"] == "SETTLED"
    assert not engine.broker.positions
    assert engine.broker.cash == journal.recover_cash(CONFIG.paper_capital, tn.MODE)
    monkeypatch.setattr(journal, "settle_testnet_order", original)
    assert engine.broker.recover() == []
    assert len(ex.orders) == 2
    assert journal.ledger_check(tn.MODE)["gap"] == 0


def test_partial_exit_remains_managed_and_cash_settles_each_order_once(env, monkeypatch):
    ex, journal, engine = env
    _enter(engine, 1)
    create = ex.create_order
    def half(symbol, kind, side, amount, params=None):
        order = create(symbol, kind, side, amount/2 if side == "sell" else amount, params=params)
        ex.orders[order["id"]]["status"] = "canceled"
        return dict(ex.orders[order["id"]])
    monkeypatch.setattr(ex, "create_order", half)
    summary = close(engine)
    assert summary["closed"] == []
    assert summary["partial_exits"][0]["remaining_qty"] == pytest.approx(.4995)
    row = journal.open_trades(mode=tn.MODE)[0]
    assert row["qty"] == pytest.approx(.999)
    assert row["remaining_qty"] == pytest.approx(.4995)
    assert row["entry_qty"] == pytest.approx(.999)
    assert journal.stats(tn.MODE)["partial_realized_pnl"] < 0
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    cash = engine.broker.cash
    assert engine.broker.recover() == []
    assert engine.broker.cash == cash
    monkeypatch.setattr(ex, "create_order", create)
    assert close(engine)["closed"]
    assert len(ex.orders) == 3
    assert not journal.open_trades(mode=tn.MODE)
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    assert tn.reconcile(ex, journal, trip=False) == []


def test_exchange_precision_dust_is_visible_marked_and_not_a_false_mismatch(env):
    ex, journal, engine = env
    _enter(engine, 1.00001)
    assert close(engine)["closed"]
    dust = engine.broker.dust_positions[0]
    assert dust.qty == pytest.approx(0.00000999)
    assert dust.timeframe == "1h"
    assert engine.broker.equity({SPEC.symbol: 200}) == pytest.approx(engine.broker.cash+(200-dust.entry_price)*dust.qty)
    assert journal.stats(tn.MODE)["dust_quantities"][SPEC.symbol] == pytest.approx(dust.qty)
    assert tn.reconcile(ex, journal, trip=False) == []
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    tn.set_kill(None)
    _enter(engine, 1)
    assert len(ex.orders) == 3


def test_ccxt_zero_precision_exception_settles_exit_as_visible_dust(env, monkeypatch):
    ex, journal, engine = env
    original = ex.amount_to_precision

    def ccxt_precision(symbol, amount):
        value = original(symbol, amount)
        if float(value) == 0:
            raise ccxt.InvalidOrder("amount must be greater than minimum amount precision")
        return value

    monkeypatch.setattr(ex, "amount_to_precision", ccxt_precision)
    _enter(engine, 1.00001)
    assert close(engine)["closed"]
    assert engine.broker.dust_positions[0].qty == pytest.approx(.00000999)
    assert not engine.broker.execution_blocked
    assert tn.reconcile(ex, journal, trip=False) == []
    cash = engine.broker.cash
    assert engine.broker.recover() == []
    assert engine.broker.cash == cash
    assert len(ex.orders) == 2
    assert journal.ledger_check(tn.MODE)["gap"] == 0


def test_dust_only_cycle_marks_off_watchlist_inventory_without_opening_a_trade(env):
    import pandas as pd
    ex, journal, engine = env
    _enter(engine, 1.00001)
    close(engine)
    dust = engine.broker.dust_positions[0]
    engine.cfg.watchlist[:] = []
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h")-pd.Timedelta(hours=1), periods=300, freq="h")
    df = pd.DataFrame({"open": 200., "high": 201., "low": 199., "close": 200., "volume": 1.}, index=idx)
    engine.market_data.latest = lambda spec, limit=None: df
    engine.orchestrator.decide = lambda *a, **k: _long(price=200.)
    summary = engine.run_cycle()
    assert not summary["errors"]
    assert len(ex.orders) == 2
    expected = engine.broker.cash+(200-dust.entry_price)*dust.qty
    assert summary["equity"] == pytest.approx(round(expected, 2))
    assert journal.last_equity_point(tn.MODE)["equity"] == pytest.approx(expected)


def test_newly_recovered_legacy_position_without_stop_gets_protective_close(env):
    import pandas as pd
    ex, journal, engine = env
    _enter(engine, 1.)
    trade_id = journal.open_trades(mode=tn.MODE)[0]["id"]
    with journal._conn() as conn:
        conn.execute("UPDATE trades SET stop_price=NULL WHERE id=?", (trade_id,))
    engine.broker.recover()
    assert (SPEC.symbol, SPEC.timeframe) not in engine._unguarded_pending
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1.},
                         index=pd.date_range("2026-01-01", periods=80, freq="h", tz="UTC"))
    result = {"errors": [], "closed": [], "opened": [], "holds": 0}
    engine._process_market(SPEC, result, frame)
    assert result["closed"][0]["reason"] == "restored without stop"
    assert not engine.broker.positions
    assert len(ex.orders) == 2


@pytest.mark.parametrize("column", ["qty", "remaining_qty"])
def test_quantity_corruption_cannot_cancel_into_dust(env, column):
    ex, journal, engine = env
    _enter(engine, 1)
    with journal._conn() as conn:
        conn.execute(f"UPDATE trades SET {column}=42 WHERE mode='testnet'")
    problems = tn.reconcile(ex, journal)
    assert any("quantity" in p for p in problems)
    assert tn.kill_state()["level"] == "entries"


@pytest.mark.parametrize("aborted", [False, True])
def test_legacy_entry_import_restores_confirmed_holdings_without_replaying_cash(env, aborted):
    ex, journal, engine = env
    order = ex.create_order(SPEC.symbol, "market", "buy", 1)
    fee = order["fee"]["cost"]*order["average"]
    tid = journal.open_trade(SPEC.symbol, "long", .999, order["average"], 95.1, None,
                             "turtle_trend", "legacy", mode=tn.MODE,
                             entry_fee=None if aborted else fee, pending_fill=aborted)
    if aborted:
        journal.abort_trade(tid)
    before = journal.recover_cash(CONFIG.paper_capital, tn.MODE)
    tn._record_order({"id": order["id"], "trade_id": tid, "leg": "entry", "symbol": SPEC.symbol,
                      "filled": 1, "average": order["average"], "base_fee": .001,
                      "fee_quote": fee, "ts": tn._now()})
    assert engine.broker.recover() == []
    assert len(ex.orders) == 1
    assert journal.open_trades(mode=tn.MODE)[0]["qty"] == pytest.approx(.999)
    assert engine.broker.cash == pytest.approx(before-fee if aborted else before)
    assert engine.broker.recover() == []
    assert tn.kill_state()["level"] == "entries"
    assert journal.ledger_check(tn.MODE)["gap"] == 0


def test_legacy_closed_partial_exit_is_reopened_without_a_second_sell_or_cash_event(env):
    ex, journal, engine = env
    buy = ex.create_order(SPEC.symbol, "market", "buy", 1)
    entry_fee = buy["fee"]["cost"]*buy["average"]
    tid = journal.open_trade(SPEC.symbol, "long", .999, buy["average"], 95.1, None,
                             "turtle_trend", "legacy", mode=tn.MODE, entry_fee=entry_fee)
    sell = ex.create_order(SPEC.symbol, "market", "sell", .4995)
    delta = (sell["average"]-buy["average"])*sell["filled"]-sell["fee"]["cost"]
    journal.close_trade(tid, sell["average"], delta-entry_fee, -.2, entry_fee+sell["fee"]["cost"],
                         "legacy partial", mode=tn.MODE, realized_cash_delta=delta)
    for leg, order in (("entry", buy), ("exit", sell)):
        tn._record_order({"id": order["id"], "trade_id": tid, "leg": leg, "symbol": SPEC.symbol,
                          "filled": order["filled"], "average": order["average"],
                          "base_fee": .001 if leg == "entry" else 0,
                          "fee_quote": entry_fee if leg == "entry" else sell["fee"]["cost"], "ts": tn._now()})
    before = journal.recover_cash(CONFIG.paper_capital, tn.MODE)
    assert engine.broker.recover() == []
    assert engine.broker.cash == pytest.approx(before)
    assert engine.broker.positions[(SPEC.symbol, SPEC.timeframe)].qty == pytest.approx(.4995)
    assert len(ex.orders) == 2
    assert engine.broker.recover() == []
    assert engine.broker.cash == pytest.approx(before)
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    assert tn.reconcile(ex, journal, trip=False) == []


def test_fee_details_are_recovered_from_order_executions(env, monkeypatch):
    ex, journal, engine = env
    create = ex.create_order
    def timeout(*a, **k):
        create(*a, **k)
        raise ccxt.RequestTimeout("response lost")
    monkeypatch.setattr(ex, "create_order", timeout)
    with pytest.raises(ccxt.RequestTimeout):
        _enter(engine, 1)
    fetch = ex.fetch_order
    def status_without_fee(*a, **k):
        o = fetch(*a, **k)
        o.pop("fee")
        return o
    monkeypatch.setattr(ex, "fetch_order", status_without_fee)
    monkeypatch.setattr(ex, "fetch_my_trades", lambda symbol, limit=None, params=None:
                        [{"id": "7", "order": "1", "amount": 1, "fee": ex.orders["1"]["fee"]}], raising=False)
    assert engine.broker.recover() == []
    assert journal.open_trades(mode=tn.MODE)[0]["entry_fee"] == pytest.approx(.1001)
    assert len(ex.orders) == 1


@pytest.mark.parametrize("mark", [90., 110.])
@pytest.mark.parametrize("fraction", [0.5, 1.0])
def test_sell_commission_in_base_realizes_all_depleted_inventory(env, monkeypatch, mark, fraction):
    ex, journal, engine = env
    _enter(engine, 1)
    before = engine.broker.cash
    pos = engine.broker.positions[(SPEC.symbol, SPEC.timeframe)]
    entry = pos.entry_price
    def base_fee_sell(symbol, kind, side, amount, params=None):
        depletion = amount*fraction
        base_fee = depletion*.001
        filled = depletion-base_fee
        oid = str(len(ex.orders)+1)
        ex.balances["BTC"] -= depletion
        ex.balances["USDT"] += filled*mark
        ex.orders[oid] = {"id": oid, "status": "canceled", "filled": filled, "average": mark,
                          "fee": {"currency": "BTC", "cost": base_fee},
                          "clientOrderId": params["newClientOrderId"]}
        return dict(ex.orders[oid])
    monkeypatch.setattr(ex, "create_order", base_fee_sell)
    close(engine)
    depletion = .999*fraction
    expected_delta = (mark-entry)*depletion - depletion*.001*mark
    assert engine.broker.cash == pytest.approx(before+expected_delta)
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    assert tn.reconcile(ex, journal, trip=False) == []


def test_external_order_writes_require_the_current_book_owner(env):
    from bot.journal import BookOwnedError
    ex, journal, engine = env
    token = journal.claim_book(tn.MODE)
    try:
        with pytest.raises(BookOwnedError):
            _enter(engine, 1)
        assert not ex.orders
        result = engine.broker.execute_entry(SPEC, _long(), 1, 100, owner_token=token)
        assert result.state == "SETTLED"
    finally:
        journal.release_book(tn.MODE, token)


def test_pending_execution_survives_lease_reclaim_and_refuses_account_reset(env, tmp_path):
    ex, journal, engine = env
    intent = journal.prepare_testnet_entry(SPEC, _long(), 1, 100)
    journal.testnet_order_state(intent["id"], "SUBMITTING")
    token = journal.claim_book(tn.MODE)
    journal.release_book(tn.MODE, token)
    with pytest.raises(ValueError, match="unresolved"):
        journal.reset_account(CONFIG.paper_capital, str(tmp_path/"backup.db"), tn.MODE)
    assert journal.testnet_orders(pending=True)[0]["client_order_id"] == intent["client_order_id"]
    assert not ex.orders


def _absent_intent(env, monkeypatch, leg="entry"):
    ex, journal, engine = env
    if leg == "exit":
        _enter(engine, 1)
        pos = next(iter(engine.broker.positions.values()))
        intent = journal.prepare_testnet_exit(pos.trade_id, pos.qty, 100, "stop loss")
    else:
        intent = journal.prepare_testnet_entry(SPEC, _long(), 1, 100)
    journal.testnet_order_state(intent["id"], "UNKNOWN")
    for name in ("fetch_open_orders", "fetch_orders", "fetch_my_trades"):
        monkeypatch.setattr(ex, name, lambda *a, **k: [], raising=False)
    return intent


def _resolve(env, intent, **kwargs):
    _, journal, engine = env
    token = journal.claim_book(tn.MODE)
    try:
        return engine.broker.resolve(intent["id"], owner_token=token, **kwargs)
    finally:
        journal.release_book(tn.MODE, token)


@pytest.mark.parametrize("kwargs", [
    {}, {"reason": "checked", "evidence": "incident"},
    {"confirm_never_accepted": True},
    {"confirm_never_accepted": True, "reason": "checked"},
    {"confirm_never_accepted": True, "evidence": "incident"},
    {"confirm_never_accepted": True, "reason": " ", "evidence": "incident"},
])
def test_operator_cannot_abandon_without_explicit_complete_attestation(env, monkeypatch, kwargs):
    intent = _absent_intent(env, monkeypatch)
    with pytest.raises(tn.TestnetError):
        _resolve(env, intent, **kwargs)
    assert env[1].testnet_orders(pending=True)[0]["state"] == "UNKNOWN"
    assert env[1].testnet_resolution_audits() == []
    assert env[0].orders == {}


@pytest.mark.parametrize("leg", ["entry", "exit"])
def test_operator_abandonment_is_audited_once_and_changes_no_cash_or_inventory(env, monkeypatch, leg):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch, leg)
    before = journal.testnet_book(CONFIG.paper_capital)
    with journal._conn() as conn:
        events = [tuple(r) for r in conn.execute("SELECT * FROM cash_events")]
    tn.set_kill("all", "operator full halt")
    kill = tn.kill_state()
    args = dict(confirm_never_accepted=True, reason="independently checked", evidence="incident-42")
    assert _resolve(env, intent, **args).state == "REJECTED"
    assert journal.testnet_book(CONFIG.paper_capital) == before
    with journal._conn() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM cash_events")] == events
    audits = journal.testnet_resolution_audits()
    assert len(audits) == 1 and audits[0]["reason"] == args["reason"]
    assert audits[0]["evidence"] == args["evidence"] and audits[0]["ts"]
    assert "OrderNotFound" in audits[0]["checks"]
    assert tn.kill_state() == kill
    engine = restart(ex, Journal())
    assert _resolve((ex, engine.journal, engine), intent, **args).state == "REJECTED"
    assert len(engine.journal.testnet_resolution_audits()) == 1
    assert engine.broker.recover() == []
    assert engine.broker.cash == before["cash"]
    assert journal.testnet_orders()[-1]["payload"] == intent["payload"]
    if leg == "exit":
        assert next(iter(engine.broker.positions.values())).stop is not None
    else:
        assert journal.testnet_trade(intent["trade_id"])["status"] == "ABORTED"


@pytest.mark.parametrize("leg", ["entry", "exit"])
@pytest.mark.parametrize("full_halt", [False, True])
def test_operator_resolution_settles_later_found_fill_even_with_abandon_flag(env, monkeypatch, leg, full_halt):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch, leg)
    side = "buy" if leg == "entry" else "sell"
    order = ex.create_order(SPEC.symbol, "market", side, intent["requested"],
                            params={"newClientOrderId": intent["client_order_id"]})
    ex.orders[order["id"]].update(symbol=SPEC.symbol, side=side)
    if full_halt:
        tn.set_kill("all", "operator full halt")
    kill = tn.kill_state()
    result = _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    assert result.state == "SETTLED"
    assert journal.testnet_resolution_audits() == []
    cash = engine.broker.cash
    assert _resolve(env, intent).state == "SETTLED"
    assert engine.broker.cash == cash
    assert len(ex.orders) == (1 if leg == "entry" else 2)
    assert journal.ledger_check(tn.MODE)["gap"] == 0
    if full_halt:
        assert tn.kill_state() == kill
    else:
        assert tn.kill_state()["level"] == "entries"


@pytest.mark.parametrize("field,value", [
    ("clientOrderId", "other-client"), ("symbol", "ETH/USDT"), ("side", "sell"),
    ("id", None), ("filled", None), ("filled", float("nan")), ("status", "open"),
])
def test_operator_resolution_refuses_ambiguous_or_mismatched_order(env, monkeypatch, field, value):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    order = {"id": "42", "symbol": SPEC.symbol, "side": "buy",
             "clientOrderId": intent["client_order_id"], "filled": 1, "status": "closed"}
    order[field] = value
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(order))
    with pytest.raises(tn.TestnetError):
        _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    assert journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"
    assert journal.testnet_resolution_audits() == []
    assert ex.orders == {}


@pytest.mark.parametrize("endpoint", ["fetch_open_orders", "fetch_orders", "fetch_my_trades"])
def test_operator_abandonment_refuses_recent_order_or_fill_evidence(env, monkeypatch, endpoint):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    monkeypatch.setattr(ex, endpoint, lambda *a, **k: [{"id": "ambiguous"}])
    with pytest.raises(tn.TestnetError, match="orders/fills"):
        _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    assert journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"


def test_operator_abandonment_refuses_failed_checks_and_unrelated_reconciliation_gap(env, monkeypatch):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    monkeypatch.setattr(ex, "fetch_orders", lambda *a, **k: (_ for _ in ()).throw(ccxt.RequestTimeout("unavailable")))
    with pytest.raises(ccxt.RequestTimeout):
        _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    monkeypatch.setattr(ex, "fetch_orders", lambda *a, **k: [])
    ex.balances["BTC"] += .25
    with pytest.raises(tn.TestnetError, match="reconciliation mismatch"):
        _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    assert journal.testnet_resolution_audits() == []


def test_operator_resolution_requires_a_live_matching_lease(env, monkeypatch):
    _, journal, engine = env
    intent = _absent_intent(env, monkeypatch)
    with pytest.raises(ValueError, match="lease"):
        engine.broker.resolve(intent["id"])
    with pytest.raises(BookOwnedError, match="lease was lost"):
        engine.broker.resolve(intent["id"], owner_token="expired")
    assert journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"


def test_abandoned_exit_allows_a_protective_exit_while_entries_stay_halted(env, monkeypatch):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch, "exit")
    assert _resolve(env, intent, confirm_never_accepted=True,
                    reason="checked", evidence="incident").state == "REJECTED"
    assert tn.kill_state()["level"] == "entries"
    assert close(engine)["closed"]
    assert len(ex.orders) == 2
    assert not engine.broker.positions
    assert journal.ledger_check(tn.MODE)["gap"] == 0


def test_operator_resolution_rejects_confirmed_zero_fill_without_attestation(env, monkeypatch):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch)
    order = {"id": "42", "symbol": SPEC.symbol, "side": "buy",
             "clientOrderId": intent["client_order_id"], "filled": 0, "status": "rejected"}
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(order))
    before = engine.broker.cash
    assert _resolve(env, intent).state == "REJECTED"
    assert journal.testnet_resolution_audits() == []
    assert engine.broker.cash == before
    assert not engine.broker.positions


def test_operator_resolution_uses_verified_exchange_id_fallback(env, monkeypatch):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    order = ex.create_order(SPEC.symbol, "market", "buy", 1,
                            params={"newClientOrderId": intent["client_order_id"]})
    ex.orders[order["id"]].update(symbol=SPEC.symbol, side="buy")
    fetch = ex.fetch_order
    calls = []
    def lookup(oid, *a, **k):
        calls.append(oid)
        if oid is None:
            raise ccxt.OrderNotFound("client ID temporarily absent")
        return fetch(oid, *a, **k)
    monkeypatch.setattr(ex, "fetch_order", lookup)
    assert _resolve(env, intent, order_id=order["id"]).state == "SETTLED"
    assert calls == [None, order["id"]]
    assert len(ex.orders) == 1
    assert journal.testnet_resolution_audits() == []


def test_found_order_with_unavailable_fee_evidence_cannot_be_abandoned(env, monkeypatch):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    order = {"id": "42", "symbol": SPEC.symbol, "side": "buy", "average": 100,
             "clientOrderId": intent["client_order_id"], "filled": 1, "status": "closed"}
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(order))
    monkeypatch.setattr(ex, "fetch_my_trades", lambda *a, **k: (_ for _ in ()).throw(ccxt.OrderNotFound("fee details unavailable")))
    with pytest.raises(ccxt.OrderNotFound):
        _resolve(env, intent, confirm_never_accepted=True, reason="checked", evidence="incident")
    assert journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"
    assert journal.testnet_resolution_audits() == []


@pytest.mark.parametrize("status,filled", [("open", 0), ("closed", 1)])
def test_observed_order_cannot_be_abandoned_after_disappearing_and_restart(env, monkeypatch, status, filled):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch)
    order = {"id": "42", "symbol": SPEC.symbol, "side": "buy", "average": 100,
             "clientOrderId": intent["client_order_id"], "filled": filled, "status": status}
    before = journal.testnet_book(CONFIG.paper_capital)
    tn.set_kill("all", "operator full halt")
    kill = tn.kill_state()
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(order))
    if status == "closed":
        monkeypatch.setattr(ex, "fetch_my_trades", lambda *a, **k: [{"id": "1", "order": "42", "amount": filled}])
    with pytest.raises(tn.TestnetError, match="active|commission"):
        _resolve(env, intent)
    observed = journal.testnet_orders(pending=True)[0]
    assert observed["exchange_order_id"] == "42"
    assert observed["observed_filled"] == filled
    assert observed["observed_status"] == status and observed["observed_ts"]
    assert observed["snapshot"] is None and observed["state"] == "UNKNOWN"
    assert observed["payload"] == intent["payload"]
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: (_ for _ in ()).throw(ccxt.OrderNotFound("disappeared")))
    monkeypatch.setattr(ex, "fetch_my_trades", lambda *a, **k: [])
    engine = restart(ex, Journal())
    with pytest.raises(tn.TestnetError, match="known order identity or fills"):
        _resolve((ex, engine.journal, engine), intent, confirm_never_accepted=True,
                 reason="empty recent history", evidence="incident")
    assert engine.journal.testnet_orders(pending=True)[0]["state"] == "UNKNOWN"
    assert engine.journal.testnet_resolution_audits() == []
    assert engine.journal.testnet_book(CONFIG.paper_capital) == before
    assert tn.kill_state() == kill


def test_observed_cumulative_fills_and_identity_cannot_regress(env, monkeypatch):
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    token = journal.claim_book(tn.MODE)
    try:
        journal.observe_testnet_order(intent["id"], "42", .5, "open", owner_token=token)
        with pytest.raises(ValueError, match="cannot decrease"):
            journal.observe_testnet_order(intent["id"], "42", .25, "open", owner_token=token)
        with pytest.raises(ValueError, match="ID cannot change"):
            journal.observe_testnet_order(intent["id"], "43", .75, "closed", owner_token=token)
        journal.observe_testnet_order(intent["id"], "42", None, "open", owner_token=token)
    finally:
        journal.release_book(tn.MODE, token)
    observed = journal.testnet_orders(pending=True)[0]
    assert observed["exchange_order_id"] == "42" and observed["observed_filled"] == .5
    order = {"id": "42", "symbol": SPEC.symbol, "side": "buy", "average": 100,
             "clientOrderId": intent["client_order_id"], "filled": 0, "status": "canceled"}
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(order))
    with pytest.raises(tn.TestnetError, match="below.*known fills"):
        _resolve(env, intent)
    assert journal.testnet_orders(pending=True)[0]["observed_filled"] == .5


def test_order_observation_columns_migrate_additively_and_idempotently(env, monkeypatch):
    _, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    with journal._conn() as conn:
        for column in ("observed_filled", "observed_status", "observed_ts"):
            conn.execute(f"ALTER TABLE testnet_execution_orders DROP COLUMN {column}")
    for _ in range(2):
        restored = Journal().testnet_orders(pending=True)[0]
        assert restored["id"] == intent["id"] and restored["payload"] == intent["payload"]
        assert restored["observed_filled"] == 0
        assert restored["observed_status"] is None and restored["observed_ts"] is None
        assert restored["state"] == "UNKNOWN"
    assert journal.recover_cash(CONFIG.paper_capital, tn.MODE) == CONFIG.paper_capital


def test_refetched_terminal_fills_are_observed_before_missing_commissions(env, monkeypatch):
    ex, journal, engine = env
    intent = _absent_intent(env, monkeypatch)
    original = {"id": "42", "filled": .25, "status": "open"}
    terminal = {"id": "42", "filled": .75, "status": "canceled", "average": 100}
    monkeypatch.setattr(ex, "fetch_order", lambda *a, **k: dict(terminal))
    with pytest.raises(tn.TestnetError, match="quantities do not yet agree"):
        engine.broker._consume(intent, original, None)
    observed = journal.testnet_orders(pending=True)[0]
    assert observed["exchange_order_id"] == "42" and observed["observed_filled"] == .75
    assert observed["snapshot"] is None
    assert engine.broker.cash == CONFIG.paper_capital


def test_recovery_does_not_rewrite_unchanged_export_and_retries_failed_export(env, monkeypatch):
    _, _, engine = env
    replace = tn.os.replace
    writes = []
    def spy(src, dst):
        if dst == tn.orders_path():
            writes.append(dst)
        return replace(src, dst)
    monkeypatch.setattr(tn.os, "replace", spy)
    assert engine.broker.recover() == []
    assert engine.broker.recover() == []
    assert len(writes) == 1
    _enter(engine, 1)
    assert len(writes) == 2
    assert engine.broker.recover() == []
    assert len(writes) == 2
    other = restart(env[0], env[1])
    def fail(src, dst):
        if dst == tn.orders_path():
            raise OSError("export disk failure")
        return replace(src, dst)
    monkeypatch.setattr(tn.os, "replace", fail)
    assert "export disk failure" in other.broker.recover()[0]
    monkeypatch.setattr(tn.os, "replace", spy)
    assert other.broker.recover() == []
    assert len(writes) == 3
    assert other.broker.recover() == []
    assert len(writes) == 3


def test_resolve_cli_uses_the_lease_and_reports_operator_result(env, monkeypatch, capsys):
    from bot.cli.operate import cmd_testnet
    from main import build_parser
    ex, journal, _ = env
    intent = _absent_intent(env, monkeypatch)
    monkeypatch.setattr(tn, "make_exchange", lambda: ex)
    args = build_parser().parse_args(["testnet", "resolve", "--intent-id", str(intent["id"]),
                                     "--confirm-never-accepted", "--reason", "checked", "--evidence", "incident"])
    cmd_testnet(args)
    assert "REJECTED" in capsys.readouterr().out
    assert journal.book_owner(tn.MODE) is None
    assert len(journal.testnet_resolution_audits()) == 1
