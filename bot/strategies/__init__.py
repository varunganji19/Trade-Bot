"""Strategy registry — the orchestrator and engine look strategies up here."""
from __future__ import annotations

from .base import BaseStrategy, Signal
from .turtle import TurtleTrend
from .meanrev import ConnorsMeanReversion
from .scalper import VWAPScalper
from .fx_regime_meanrev import FXRegimeMeanRev
from .ts_momentum import TimeSeriesMomentum

STRATEGY_CLASSES = {
    TurtleTrend.name: TurtleTrend,
    ConnorsMeanReversion.name: ConnorsMeanReversion,
    VWAPScalper.name: VWAPScalper,
    FXRegimeMeanRev.name: FXRegimeMeanRev,
    TimeSeriesMomentum.name: TimeSeriesMomentum,
}

_INSTANCE_CACHE: dict = {}   # id(params) -> (params_ref, {name: instance})


def get_strategies(params=None) -> dict:
    """Shared strategy instances (stateless, so sharing is safe).

    The cache holds a STRONG reference to the params object beside the
    instances: keyed by id(params) alone, a garbage-collected params object's
    id could be recycled by a NEW params object, which would silently receive
    the OLD object's strategy instances (strategies capture params at
    construction). Holding the ref makes id reuse impossible while cached."""
    key = id(params)
    entry = _INSTANCE_CACHE.get(key)
    if entry is None or entry[0] is not params:
        entry = (params, {name: cls(params) for name, cls in STRATEGY_CLASSES.items()})
        _INSTANCE_CACHE[key] = entry
    return entry[1]


def get_strategy(name: str, params=None) -> BaseStrategy:
    strategies = get_strategies(params)
    if name not in strategies:
        raise KeyError(f"Unknown strategy '{name}'. Known: {list(strategies)}")
    return strategies[name]


__all__ = ["BaseStrategy", "Signal", "TurtleTrend", "ConnorsMeanReversion",
           "VWAPScalper", "FXRegimeMeanRev", "TimeSeriesMomentum",
           "STRATEGY_CLASSES", "get_strategies", "get_strategy"]
