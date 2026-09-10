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

_INSTANCE_CACHE: dict = {}


def get_strategies(params=None) -> dict:
    """Shared strategy instances (stateless, so sharing is safe)."""
    key = id(params)
    if key not in _INSTANCE_CACHE:
        _INSTANCE_CACHE[key] = {name: cls(params) for name, cls in STRATEGY_CLASSES.items()}
    return _INSTANCE_CACHE[key]


def get_strategy(name: str, params=None) -> BaseStrategy:
    strategies = get_strategies(params)
    if name not in strategies:
        raise KeyError(f"Unknown strategy '{name}'. Known: {list(strategies)}")
    return strategies[name]


__all__ = ["BaseStrategy", "Signal", "TurtleTrend", "ConnorsMeanReversion",
           "VWAPScalper", "FXRegimeMeanRev", "TimeSeriesMomentum",
           "STRATEGY_CLASSES", "get_strategies", "get_strategy"]
