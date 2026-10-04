"""Bounded fill-time risk regressions; cycle coverage follows in the durable-book change."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bot.risk import RiskManager
from config import CONFIG, MarketSpec


def decision(**changes):
    values = dict(action="LONG", price=100., stop_distance=5., confidence=1.,
                  target_rr=2., strategy_name="turtle_trend", rationale="regression")
    values.update(changes)
    return SimpleNamespace(**values)



@pytest.mark.parametrize("ceiling", [1., 10.])
def test_quantity_ceiling_applied_before_exposure_checks(ceiling):
    cfg = deepcopy(CONFIG)
    cfg.risk.max_gross_leverage = .2
    cfg.risk.max_cluster_leverage = .2
    rm = RiskManager(cfg)
    approval = rm.approve(decision(), MarketSpec("crypto", "TEST/USDT", "5m"),
                          10_000., 0, False, open_gross_notional=500.,
                          cluster_gross_notional=500., qty_ceiling=ceiling)
    assert approval.approved
    assert approval.qty == ceiling



@pytest.mark.parametrize("ceiling", [0., -1., float("nan"), float("inf"), .01])
def test_untradable_quantity_ceiling_rejected(ceiling):
    approval = RiskManager(deepcopy(CONFIG)).approve(
        decision(), MarketSpec("crypto", "TEST/USDT", "5m"), 10_000., 0, False,
        qty_ceiling=ceiling)
    assert not approval.approved

