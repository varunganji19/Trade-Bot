"""Fill-candle protection and retryable recovery exits."""
from copy import deepcopy
from types import SimpleNamespace
import sqlite3

import pandas as pd
import pytest

from bot.engine import TradingEngine
from bot.journal import Journal
from config import CONFIG, MarketSpec


def engine_frame(tmp_path):
    cfg = deepcopy(CONFIG)
    cfg.db_path = str(tmp_path / 'book.db')
    spec = MarketSpec('crypto', 'TEST/USDT', '5m')
    cfg.watchlist = [spec]
    cfg.portfolio.enabled = False
    journal = Journal(cfg.db_path)
    engine = TradingEngine(cfg=cfg, mode='hft', quiet=True, journal=journal)
    frame = pd.DataFrame(dict(open=[100.] * 80, high=[101.] * 80,
                              low=[99.] * 80, close=[100.] * 80,
                              volume=[100.] * 80),
                         index=pd.date_range('2026-01-01', periods=80, freq='5min', tz='UTC'))
    engine.market_data.latest = lambda spec: frame
    return engine, journal, spec, frame


@pytest.mark.parametrize('retry_close', [False, True])
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('breach', ['stop loss', 'take profit', None])
def test_maker_fill_checks_stop_on_fill_candle(tmp_path, side, breach, retry_close):
    eng, journal, spec, frame = engine_frame(tmp_path)
    if breach:
        frame.loc[frame.index[-1], 'low' if (side == 'LONG') == (breach == 'stop loss') else 'high'] = (
            89. if (side == 'LONG') == (breach == 'stop loss') else 111.)
    decision = SimpleNamespace(action=side, price=100., stop_distance=5., confidence=1.,
                               target_rr=2., strategy_name='hft_micro_breakout',
                               rationale='fill regression')
    key = (spec.symbol, spec.timeframe)
    eng._pending[key] = dict(decision=decision, qty=1., limit=100., side=side,
                            waited=0, decision_bar_ts=frame.index[-2].timestamp())
    original_close = journal.close_trade
    if retry_close and breach:
        def fail_close(*args, **kwargs):
            raise sqlite3.OperationalError('injected fill exit failure')
        journal.close_trade = fail_close
    result = eng.run_cycle()
    assert len(result['opened']) == 1
    if retry_close and breach:
        assert result['errors']
        assert key in eng.broker.positions
        assert journal.recent_trades()[0]['status'] == 'OPEN'
        journal.close_trade = original_close
        result = eng.run_cycle()
        assert not result['opened']
    assert not result['errors']
    assert bool(result['closed']) == bool(breach)
    assert (key in eng.broker.positions) != bool(breach)
    assert journal.recent_trades()[0]['status'] == ('CLOSED' if breach else 'OPEN')
    if breach:
        assert result['closed'][0]['reason'] == breach


def test_stopless_restore_retries_failed_close(tmp_path):
    eng, journal, spec, frame = engine_frame(tmp_path)
    journal.open_trade(spec.symbol, 'long', 1., 100., None, None,
                       'turtle_trend', 'legacy', mode='hft', timeframe=spec.timeframe)
    eng._restore_positions()
    key = (spec.symbol, spec.timeframe)
    original = journal.close_trade
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError('injected close failure')
        return original(*args, **kwargs)

    journal.close_trade = fail_once
    cash = eng.broker.cash
    first = eng.run_cycle()
    assert first['errors']
    assert key in eng._unguarded_pending
    assert key in eng.broker.positions
    assert eng.broker.cash == cash
    second = eng.run_cycle()
    assert not second['errors']
    assert calls == 2
    assert second['closed'][0]['reason'] == 'restored without stop'
    assert key not in eng._unguarded_pending
    assert key not in eng.broker.positions
    assert journal.recent_trades()[0]['status'] == 'CLOSED'
