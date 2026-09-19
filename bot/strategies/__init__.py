"""Strategy registry — the orchestrator and engine look strategies up here."""
from __future__ import annotations

from .base import BaseStrategy, Signal
from .turtle import TurtleTrend
from .meanrev import ConnorsMeanReversion
from .scalper import VWAPScalper
from .fx_regime_meanrev import FXRegimeMeanRev
from .ts_momentum import TimeSeriesMomentum
from .hft import (HFTMicroBreakout, HFTExhaustionFade, HFTMarketMaker,
                  HFTOFIMomentum)

STRATEGY_CLASSES = {
    TurtleTrend.name: TurtleTrend,
    ConnorsMeanReversion.name: ConnorsMeanReversion,
    VWAPScalper.name: VWAPScalper,
    FXRegimeMeanRev.name: FXRegimeMeanRev,
    TimeSeriesMomentum.name: TimeSeriesMomentum,
    HFTMicroBreakout.name: HFTMicroBreakout,
    HFTExhaustionFade.name: HFTExhaustionFade,
    HFTMarketMaker.name: HFTMarketMaker,
    HFTOFIMomentum.name: HFTOFIMomentum,
}

HFT_STRATEGY_NAMES = ("hft_micro_breakout", "hft_exhaustion_fade",
                      "hft_market_maker", "hft_ofi_momentum")

# CANDIDATES: registered, backtestable and available in the Lab and the
# battery, but NOT voting in the live ensemble — the same evidence standard
# the bot applies to Kronos (earn the vote, never vote on faith). A candidate
# graduates by beating the incumbents in the harness, which is a measurement,
# not an opinion.
#   hft_ofi_momentum — measured 2026-09-19 (3d, perp tier): PF 0.32 (BTC) /
#   0.68 (ETH), 7-12 trades, negative return on both. It does not beat the
#   incumbents it was written to replace, so it does not get a vote.
CANDIDATE_STRATEGIES = ("hft_ofi_momentum",)

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
           "HFTMicroBreakout", "HFTExhaustionFade", "HFTMarketMaker",
           "HFTOFIMomentum",
           "HFT_STRATEGY_NAMES", "CANDIDATE_STRATEGIES",
           "STRATEGY_CLASSES", "get_strategies", "get_strategy"]
