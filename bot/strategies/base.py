"""Strategy interface + Signal model.

Strategies are STATELESS over an indicator-enriched DataFrame: `evaluate(df, i)`
decides entries at the last closed bar `i`, and `check_exit(df, i, position)`
manages exits for an open position. Because they only read indicator columns,
the exact same code path runs in the backtester and the live engine — no
look-ahead, no drift between paper and backtest.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    strategy: str
    action: str            # 'LONG' | 'SHORT' | 'FLAT'
    confidence: float      # 0..1
    stop_distance: float | None = None   # price units (ATR-based)
    target_rr: float | None = None       # take-profit as R-multiple of stop
    rationale: str = ""
    meta: dict = field(default_factory=dict)


class BaseStrategy:
    name: str = "base"
    preferred_timeframes: tuple = ("1h",)
    enabled: bool = True  # explicit kill-switch; False excludes from vote regardless of REGIME_WEIGHTS

    def __init__(self, params=None):
        from config import StrategyParams
        self.p = params or StrategyParams()

    def evaluate(self, df, i: int) -> Signal:
        raise NotImplementedError

    def check_exit(self, df, i: int, position) -> tuple[str | None, float | None]:
        """Return (exit_reason | None, new_stop | None)."""
        return None, None

    # ---- shared helpers -------------------------------------------------
    @staticmethod
    def _at(df, col: str, i: int, shift: int = 0):
        """Value of column at bar (i - shift); NaN-safe."""
        j = i - shift
        if j < 0:
            return float("nan")
        v = df[col].iloc[j]
        return float(v) if v is not None else float("nan")

    @staticmethod
    def _ok(v) -> bool:
        import math
        return v is not None and not (isinstance(v, float) and math.isnan(v))

    def _clip_conf(self, value: float) -> float:
        return float(max(0.30, min(0.90, value)))
