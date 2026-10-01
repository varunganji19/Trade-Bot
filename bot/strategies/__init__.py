"""Strategy registry — the orchestrator and engine look strategies up here."""
from __future__ import annotations

from .base import BaseStrategy, Signal
from .turtle import TurtleTrend
from .meanrev import ConnorsMeanReversion
from .scalper import VWAPScalper
from .fx_regime_meanrev import FXRegimeMeanRev
from .ts_momentum import TimeSeriesMomentum
from .hft import (HFTMicroBreakout, HFTExhaustionFade, HFTMarketMaker,
                  HFTOFIMomentum, HFTCrossReversion, HFTFundingReversion,
                  HFTTakerFlow)

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
    HFTCrossReversion.name: HFTCrossReversion,
    HFTFundingReversion.name: HFTFundingReversion,
    HFTTakerFlow.name: HFTTakerFlow,
}

HFT_STRATEGY_NAMES = ("hft_micro_breakout", "hft_exhaustion_fade",
                      "hft_market_maker", "hft_ofi_momentum",
                      "hft_cross_reversion", "hft_funding_reversion",
                      "hft_taker_flow")

# CANDIDATES: registered, backtestable and available in the Lab and the
# battery, but NEVER voting in the live ensemble, whatever a gate measures:
# graduating one is a deliberate code change. Each carries the reason, which
# the dashboard shows in place of the gate's status word — a candidate's
# gate result can rest on assumptions the gate cannot check (the market
# maker below), and must not read as an edge.
CANDIDATE_NOTES = {
    "hft_ofi_momentum": "candidate — its order flow is a CLV x volume guess, not "
                        "real taker flow; never votes (docs/RESULTS.md §1)",
    "hft_market_maker": "candidate, not an edge — profitable only with touch fills; "
                        "loses (PF 0.96) once quotes must trade through by 5 bp, "
                        "and candles cannot show queue position; never votes "
                        "(experiments/market_maker_fill_model)",
    "hft_cross_reversion": "candidate — loses before fees on most pairs; never votes "
                           "(docs/RESULTS.md §1)",
    "hft_funding_reversion": "candidate — the live engine has no funding data, and it "
                             "loses before fees; never votes (docs/RESULTS.md §1)",
    "hft_taker_flow": "candidate — real Binance taker flow, which the live engine "
                      "does not fetch; never votes (experiments/taker_flow)",
}
CANDIDATE_STRATEGIES = tuple(CANDIDATE_NOTES)

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
           "HFTOFIMomentum", "HFTCrossReversion", "HFTFundingReversion", "HFTTakerFlow",
           "HFT_STRATEGY_NAMES", "CANDIDATE_STRATEGIES", "CANDIDATE_NOTES",
           "STRATEGY_CLASSES", "get_strategies", "get_strategy"]
