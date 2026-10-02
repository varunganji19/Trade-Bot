"""Binance spot TESTNET broker (bot/testnet.py), against a fake exchange:
no network, no keys. Testnet-only guards, real-fill accounting, kill
switches and reconciliation."""
from __future__ import annotations

import math
import time

import pytest

from bot import testnet as tn
from bot.journal import Journal
from bot.orchestrator import Decision
from config import CONFIG, MarketSpec

SPEC = MarketSpec("crypto", "BTC/USDT", "1h")
TESTNET_URLS = {"public": "https://testnet.binance.vision/api/v3",
                "private": "https://testnet.binance.vision/api/v3"}


class FakeBinance:
    """Fills market orders at mark x (1 +/- slip); buys pay 0.1% in the base
    asset, sells 0.1% in USDT, as Binance spot does without BNB."""

    def __init__(self, mark=100.0, slip=0.001):
        self.urls = {"api": dict(TESTNET_URLS)}
        self.mark, self.slip = mark, slip
        self.balances = {"USDT": 10_000.0, "BTC": 1.0, "ETH": 10.0}
        self.orders, self.fail = {}, 0

    def amount_to_precision(self, symbol, qty):
        return str(math.floor(qty * 1e5) / 1e5)

    def create_order(self, symbol, kind, side, amount):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("exchange unavailable")
        base, quote = symbol.split("/")
        price = self.mark * (1 + self.slip if side == "buy" else 1 - self.slip)
        if side == "buy":
            fee = {"currency": base, "cost": amount * 0.001}
            self.balances[quote] -= amount * price
            self.balances[base] += amount - fee["cost"]
        else:
            fee = {"currency": quote, "cost": amount * price * 0.001}
            self.balances[base] -= amount
            self.balances[quote] += amount * price - fee["cost"]
        oid = str(len(self.orders) + 1)
        self.orders[oid] = {"id": oid, "status": "closed", "filled": amount,
                            "average": price, "fee": fee}
        return dict(self.orders[oid])

    def fetch_order(self, oid, symbol):
        return dict(self.orders[oid])

    def fetch_balance(self):
        return {a: {"total": v} for a, v in self.balances.items()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "trading.db"))
    ex = FakeBinance()
    journal = Journal()
    engine = tn.build_testnet_engine(exchange=ex, journal=journal)
    tn.snapshot_baseline(ex, ["BTC/USDT", "ETH/USDT"])
    return ex, journal, engine


def _long(price=100.0, stop=5.0):
    return Decision(action="LONG", confidence=0.8, stop_distance=stop, target_rr=2.0,
                    price=price, strategy_name="turtle_trend", rationale="test")


def _enter(engine, qty=2.0):
    summary = {"errors": [], "opened": [], "closed": []}
    engine._fill_entry(SPEC, _long(), qty, 100.0, maker_entry=False,
                       bar_epoch=time.time() - 3600, summary=summary)
    return summary


# --------------------------------------------------------------- testnet only
def test_only_testnet_keys_are_read(monkeypatch):
    monkeypatch.delenv(tn.KEY_ENV, raising=False)
    monkeypatch.delenv(tn.SECRET_ENV, raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "real-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "real-secret")
    with pytest.raises(tn.NotTestnetError, match=tn.KEY_ENV):
        tn.make_exchange()


def test_the_real_client_is_built_in_sandbox_and_checked(monkeypatch):
    monkeypatch.setenv(tn.KEY_ENV, "k")
    monkeypatch.setenv(tn.SECRET_ENV, "s")
    ex = tn.make_exchange()                  # no network on construction
    assert all("testnet.binance.vision" in u for u in ex.urls["api"].values()
               if isinstance(u, str) and "/api/" in u)


def test_a_production_client_is_refused():
    import ccxt
    with pytest.raises(tn.NotTestnetError):
        tn.assert_testnet(ccxt.binance())
    fake = FakeBinance()
    fake.urls["api"]["private"] = "https://api.binance.com/api/v3"
    with pytest.raises(tn.NotTestnetError):
        tn.TestnetBroker(fake)


# ------------------------------------------------------------- real fills
def test_the_book_records_what_the_exchange_did(env):
    ex, journal, engine = env
    _enter(engine)
    row = journal.open_trades(mode=tn.MODE)[0]
    assert row["entry_price"] == pytest.approx(100.1)            # the exchange's fill
    assert row["qty"] == pytest.approx(1.998)                    # 2 minus the BTC fee
    assert row["entry_fee"] == pytest.approx(0.002 * 100.1)      # fee valued in USDT
    assert row["stop_price"] == pytest.approx(95.1)              # bracket from the fill
    order = tn.recorded_orders()[0]
    assert (order["leg"], order["side"], order["trade_id"]) == ("entry", "buy", row["id"])

    summary = {"errors": [], "opened": [], "closed": []}
    engine._close(SPEC, 95.1, "stop loss", summary, bar_epoch=time.time(), write_equity=False)
    closed = journal.recent_trades(mode=tn.MODE)[0]
    assert closed["exit_price"] == pytest.approx(99.9)           # market exit, not the level
    gross = (99.9 - 100.1) * 1.998
    assert closed["pnl"] == pytest.approx(gross - 1.998 * 99.9 * 0.001 - 0.002 * 100.1, abs=0.01)
    assert tn.reconcile(ex, journal) == []


def test_the_spot_testnet_never_shorts(env):
    ex, _, engine = env
    with pytest.raises(tn.TestnetError, match="cannot short"):
        engine.broker.open_position(SPEC, Decision(action="SHORT", stop_distance=5.0, price=100),
                                    1.0, 100.0, trade_id=1)
    assert ex.orders == {}


# ------------------------------------------------------------ kill switches
def test_the_entry_kill_blocks_entries_but_lets_stops_out(env):
    ex, journal, engine = env
    _enter(engine)
    tn.set_kill("entries", "test")
    with pytest.raises(tn.TestnetError, match="kill switch"):
        engine.broker.open_position(MarketSpec("crypto", "ETH/USDT", "1h"), _long(), 1.0,
                                    100.0, trade_id=99)
    engine._close(SPEC, 95.0, "stop loss", {"errors": [], "closed": []},
                  bar_epoch=time.time(), write_equity=False)
    assert journal.open_trades(mode=tn.MODE) == []


def test_the_full_kill_blocks_every_order(env):
    ex, journal, engine = env
    _enter(engine)
    tn.set_kill("all", "test")
    n = len(ex.orders)
    with pytest.raises(tn.TestnetError, match="kill switch"):
        engine._close(SPEC, 95.0, "stop loss", {"errors": [], "closed": []},
                      bar_epoch=time.time(), write_equity=False)
    assert len(ex.orders) == n                        # nothing was sent...
    assert len(journal.open_trades(mode=tn.MODE)) == 1   # ...and the position is kept


def test_caps_refuse_large_or_too_many_orders(env, monkeypatch):
    _, _, engine = env
    with pytest.raises(tn.TestnetError, match="exceeds the cap"):
        engine.broker.open_position(SPEC, _long(), 50.0, 100.0, trade_id=1)
    monkeypatch.setattr(tn, "MAX_ORDERS_PER_DAY", 1)
    engine.broker.open_position(SPEC, _long(), 1.0, 100.0, trade_id=1)
    with pytest.raises(tn.TestnetError, match="daily order cap"):
        engine.broker.open_position(MarketSpec("crypto", "ETH/USDT", "1h"), _long(), 1.0,
                                    100.0, trade_id=2)


def test_repeated_order_failures_trip_the_kill(env):
    ex, _, engine = env
    ex.fail = tn.MAX_CONSECUTIVE_FAILURES
    for _ in range(tn.MAX_CONSECUTIVE_FAILURES):
        with pytest.raises(RuntimeError):
            engine.broker.open_position(SPEC, _long(), 1.0, 100.0, trade_id=1)
    assert tn.kill_state()["level"] == "entries"
    assert "consecutive order failures" in tn.kill_state()["reason"]


# ----------------------------------------------------------- reconciliation
def test_a_journal_that_disagrees_with_the_exchange_trips_the_kill(env):
    ex, journal, engine = env
    _enter(engine)
    with journal._conn() as conn:
        conn.execute("UPDATE trades SET entry_price=90 WHERE mode='testnet'")
    problems = tn.reconcile(ex, journal)
    assert any("journal price 90" in p for p in problems)
    assert tn.kill_state()["level"] == "entries"


def test_holdings_that_moved_outside_the_book_are_named(env):
    ex, journal, engine = env
    _enter(engine)
    ex.balances["BTC"] -= 0.5                         # sold by hand on the exchange
    problems = tn.reconcile(ex, journal, trip=False)
    assert any(p.startswith("BTC: exchange holds") for p in problems)
    assert tn.kill_state()["level"] is None           # trip=False only reports


def test_the_testnet_book_is_not_counted_against_the_paper_books(env):
    _, journal, engine = env
    journal.open_trade("ETH/USDT", "long", 10.0, 1000.0, 950.0, None, "x", "x", mode="paper")
    assert engine._cross_book_gross_notional() == 0.0
    assert [s.symbol for s in engine.cfg.watchlist] == \
        [s.symbol for s in CONFIG.watchlist if s.kind == "crypto"]


def test_one_rejected_exit_does_not_stop_the_other_markets(env):
    """A full cycle: both positions breach their stop on the last bar; the
    exchange rejects the BTC exit, and ETH must still be stopped out."""
    import numpy as np
    import pandas as pd
    ex, journal, engine = env
    eth = MarketSpec("crypto", "ETH/USDT", "1h")
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1),
                        periods=300, freq="h")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
                       "volume": 1.0}, index=idx)
    df.iloc[-1, df.columns.get_loc("low")] = 80.0          # through every stop
    entry_epoch = float(idx[-3].timestamp())
    for spec in (SPEC, eth):
        engine._fill_entry(spec, _long(), 1.0, 100.0, maker_entry=False,
                           bar_epoch=entry_epoch, summary={"errors": [], "opened": []})
    engine.cfg.watchlist[:] = [SPEC, eth]
    engine.market_data.latest = lambda spec, limit=None: df

    real = ex.create_order
    def reject_btc(symbol, *a, **k):
        if symbol == "BTC/USDT":
            raise RuntimeError("rejected")
        return real(symbol, *a, **k)
    ex.create_order = reject_btc
    summary = engine.run_cycle()

    assert any(e.startswith("BTC/USDT 1h: RuntimeError") for e in summary["errors"])
    assert [t["symbol"] for t in journal.open_trades(mode=tn.MODE)] == ["BTC/USDT"]
    assert ("BTC/USDT", "1h") in engine.broker.positions
    assert np.isclose(journal.recent_trades(mode=tn.MODE)[0]["exit_price"], 99.9)
