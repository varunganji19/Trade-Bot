"""HFT book — the separate high-frequency PAPER account.

The standard book (mode='paper') trades 1h/4h/15m specs on the market-mode
universe (forex+crypto vs India). This package defines a SECOND book,
mode='hft', trading 1m bars (crypto + forex) with its own capital, risk
dials, fee tier, strategies (bot/strategies/hft.py) and dashboard page.
Everything below the decision layer is SHARED — PaperBroker, RiskManager,
Orchestrator, journal — so HFT paper trades and HFT backtests run the same
code the standard book runs (live/backtest parity by construction).

Fee tier: 1m strategies live or die on the fee schedule. The default
'HFT_FEE_TIER=perp' models a perpetuals-style tier (taker 5bp, maker 2bp,
slippage 3bp) because spot base-tier maker=taker=10bp makes every 1m strategy
dead on arrival — a measured finding, not an assumption (HFT.md). Set
HFT_FEE_TIER=spot to run the same strategies on the spot tier and see it.
"""
from __future__ import annotations

from dataclasses import replace

from config import CONFIG, HFT_WATCHLIST, Config

__all__ = ["build_hft_config", "build_hft_engine", "HFT_WATCHLIST"]


# perp-style fee tier (the HFT book's default cost model, see module docstring)
_PERP_FEE_OVERRIDES = {"fee_crypto": 0.0005, "maker_fee_crypto": 0.0002,
                       "slippage_crypto": 0.0003}


def hft_fee_tier() -> str:
    # read through config's helper so `main.py config` can report it with a
    # source — the tier silently decides whether any 1m/5m strategy can clear
    # its own costs, which makes it the last setting that should be invisible
    from config import _env_str
    tier = (_env_str("HFT_FEE_TIER", "perp") or "perp").strip().lower()
    return tier if tier in ("perp", "spot") else "perp"


def _cost_floor_params(costs, params):
    """Derive the 1m strategies' cost floors from the book's ACTUAL fee tier.

    RiskManager.approve refuses any stop tighter than the modeled taker round
    trip, so a strategy whose stop is below it can only ever produce vetoed
    decisions (measured: 15 entry decisions, 0 trades on the live 1m book).
    The floors live in StrategyParams so both the live engine and the
    backtester read the same numbers through cfg.params — parity by
    construction, and switching HFT_FEE_TIER moves every floor together."""
    from dataclasses import replace as _replace
    # crypto leg is the binding case (the widest round trip in the universe);
    # forex 1m legs are cheaper and clear the same floor comfortably
    taker_rt = (costs.fee("crypto") + costs.slippage("crypto")) * 2.0 * 1e4
    maker_rt = (costs.fee("crypto", maker=True) + costs.slippage("crypto", maker=True)
                + costs.fee("crypto") + costs.slippage("crypto")) * 1e4
    return _replace(params,
                    hft_cost_floor_bps=round(taker_rt, 2),
                    hft_maker_cost_floor_bps=round(maker_rt, 2))


def build_hft_config(fee_tier: str | None = None) -> Config:
    """A Config for the HFT book: HFT_WATCHLIST universe, HFT capital/cadence,
    tightened risk dials, and the book's own fee schedule. Built via
    dataclasses.replace so the standard book's CONFIG singleton is never
    touched (the dashboard hot-swaps CONFIG.watchlist in place)."""
    h = CONFIG.hft
    tier = fee_tier or hft_fee_tier()
    costs = CONFIG.costs if tier == "spot" else replace(CONFIG.costs, **_PERP_FEE_OVERRIDES)
    # the MM bracket inverts the swing-trade reward ratio (small target vs
    # wider inventory stop) — the HFT book carries its own floor (HFT.md)
    risk = replace(CONFIG.risk,
                   risk_per_trade=h.risk_per_trade,
                   daily_loss_kill_switch=h.daily_loss_kill_switch,
                   max_open_positions=h.max_open_positions,
                   min_confidence=h.min_confidence,
                   min_rr_per_trade=0.3)
    return replace(CONFIG,
                   params=_cost_floor_params(costs, CONFIG.params),
                   watchlist=list(HFT_WATCHLIST),
                   paper_capital=h.paper_capital,
                   live_interval_seconds=h.live_interval_seconds,
                   lookback_bars=h.lookback_bars,
                   costs=costs,
                   risk=risk)


def build_hft_engine(journal=None, quiet: bool = False):
    """A TradingEngine wired for the HFT book: mode='hft' tags every journal
    row (trades/decisions/equity) so the whole HFT record is one filtered
    query away, and the mode-filtered open_trades() restore keeps the two
    books' positions apart."""
    from bot.engine import TradingEngine
    return TradingEngine(cfg=build_hft_config(), mode="hft",
                         journal=journal, quiet=quiet)
