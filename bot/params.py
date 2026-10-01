"""Strategy parameters: every tunable the strategies read, with the measured
reason each value is what it is. Re-exported by config (`from config import
StrategyParams`), and kept apart so config.py stays about the environment,
costs, risk and markets."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyParams:
    # Turtle trend following (Donchian breakout)
    turtle_entry_period: int = 20
    turtle_exit_period: int = 10
    turtle_atr_period: int = 14
    turtle_stop_atr: float = 2.0
    turtle_adx_min: float = 20.0        # volatility/trend regime filter (see docs/archive/RESEARCH.md §2.1)

    # Connors RSI-2 mean reversion (documented on daily bars -> we trade 4h, the
    # closest tradable analogue; on 1h the per-trade edge < costs, see docs/archive/BACKTESTS.md)
    mr_rsi_period: int = 2
    mr_rsi_buy_below: float = 5.0     # tightened from 10: fewer, deeper pullbacks only
    mr_rsi_sell_above: float = 95.0
    mr_exit_long_rsi: float = 65.0
    mr_exit_short_rsi: float = 35.0
    mr_trend_ema: int = 200
    mr_exit_ema: int = 5
    mr_stop_atr: float = 3.0
    mr_time_stop_bars: int = 12       # 12 x 4h = 2 days; edge decays fast
    # Chan half-life gate (AR(1)/OU time scale of mean reversion — the one
    # ARIMA-family tool that survived measurement). The deviation
    # log(close/EMA20) is fit to x_t = c + phi*x_{t-1} + e_t over a rolling 100
    # bars; half-life = -ln(2)/(phi-1) bars = how fast pullbacks have actually
    # been reverting. Entries are refused when that half-life exceeds the
    # strategy's own 12-bar time-stop horizon (NaN auto-passes like every
    # gate; the threshold does the refusing — finite windows bias a true
    # random walk to ~window/5 bars, so inf/explosive is rare). Measured
    # A/B + walk-forward in docs/archive/BACKTESTS.md Round 6: 3 of 4 cells positive,
    # walk-forward positive on BOTH symbols; binds rarely (~2 entries/yr).
    mr_halflife_max: float = 12.0

    # VWAP scalper (ORB-inspired + volume confirmation)
    # Cost study on real data (docs/archive/BACKTESTS.md): 5m crypto taker-fee round trip
    # (~0.3%) exceeds the measured 12-bar forward edge, so the scalper trades
    # 15m, long-biased with the EMA200 trend (shorts need high conviction).
    scalper_ema_fast: int = 9
    scalper_ema_slow: int = 21
    scalper_stop_atr: float = 2.0      # wide enough that 1-bar noise can't stop the trade
    scalper_time_stop_bars: int = 12    # matches the measured 12-bar edge horizon
    scalper_range_period: int = 12      # rolling "opening range" analog
    scalper_vol_ratio_min: float = 1.15
    # Time-of-day RVOL gate (Zarattini-Barbon-Aziz 2024 "Stocks in Play": on
    # US-equity OPENING RANGES the same ORB rules went from Sharpe 0.48 to 2.81
    # trading only names unusually active vs their own time-of-day norm).
    # Our every-bar 24/7-crypto adaptation measured NEUTRAL-to-slightly-
    # negative across 60/90/180d and walk-forward (docs/archive/BACKTESTS.md), so it ships
    # OFF: 0.0 auto-passes. Raise it (e.g. 1.10) to experiment; NaN (no volume
    # data / fresh slots) always passes either way — forex stays ungated.
    scalper_rvol_min: float = 0.0
    scalper_break_even_rr: float = 1.0  # move stop to breakeven after 1R
    scalper_min_confidence: float = 0.62    # low-conf entries measured as noise
    scalper_short_min_confidence: float = 1.01  # shorts disabled: every short bucket lost money in testing (docs/archive/BACKTESTS.md)
    scalper_trend_filter: bool = True   # longs need close > EMA200, shorts close < EMA200
    scalper_adx_min: float = 25.0       # entries below ADX 25 measured as negative-edge
    scalper_target_rr: float | None = None  # no fixed TP: caps measured at avg_win $12 vs $19 free
    # buffered VWAP exit: a single bar close across VWAP is noise; require
    # 2 consecutive closes beyond VWAP - 0.25 ATR (see docs/archive/BACKTESTS.md cost study)
    scalper_vwap_buffer_atr: float = 0.25
    scalper_exit_confirm_bars: int = 2
    # generic cooldown after ANY scalper exit (not just stops) so one bad setup
    # can't immediately re-trigger and churn fees
    scalper_cooldown_bars: int = 12

    # FX regime-conditioned mean reversion (bot/strategies/fx_regime_meanrev.py,
    # 1h bars — Milestone C2, grounded in SSRN 6087107). One contiguous block
    # appended at the END of the dataclass; never reordered (merge rule).
    # z-score of log(close/EMA20) over fxmr_z_window bars is the deviation.
    fxmr_z_window: int = 100         # rolling window for the deviation z-score's mean/sigma
    fxmr_z_entry: float = 2.0        # |z| beyond this = stretched far enough from the mean to fade
    fxmr_z_exit: float = 0.5         # z crossing back through this toward the mean = snapback complete
    fxmr_halflife_max: float = 12.0  # AR(1) half-life of the deviation must be <= this (reversion in horizon)
    fxmr_stop_atr: float = 1.5       # hard stop in ATRs (intraday FX vol scale)
    fxmr_target_rr: float = 1.5      # declared fixed R-target (reward floor needs >= 1.2R; see module docstring)
    fxmr_time_stop_bars: int = 24    # 24 x 1h = ~1 trading day; intraday reversion must not become a position trade
    fxmr_min_atr_pct: float = 0.0002 # ATR >= 0.02% of price: dead-flat markets give the spread the whole edge

    # India time-series momentum (bot/strategies/ts_momentum.py, 1h/4h bars —
    # Milestone C1, grounded in SSRN 3345280/3510433/4587697). One contiguous
    # block appended at the END of the dataclass; never reordered (merge rule).
    # Defaults are the papers' plain reading — no tuning was done.
    tsmom_lookback: int = 240        # trailing-return window: 240 1h bars ~ 10 months of NSE sessions (papers' 6-12 month momentum horizon)
    tsmom_min_ret: float = 0.08      # +8% over the lookback: the decile portfolio's top cut as a single-symbol absolute gate (SSRN 3345280)
    tsmom_52w_bars: int = 2450       # 1-year rolling-high window: ~2450 1h bars ~ 245 NSE sessions (SSRN 4587697)
    tsmom_52w_prox: float = 0.10     # close within 10% of the 1-year high: anchor proximity (SSRN 4587697)
    tsmom_stop_atr: float = 2.5      # wide 2.5-ATR stop: a slow 10-month-horizon strategy must not be noise-stopped
    tsmom_exit_ret: float = 0.0      # exit line: trailing return < 0 = the momentum regime flipped non-positive (signal exit, the papers' monthly re-rank analogue)

    # ---- HFT book (bot/strategies/hft.py, 1m bars — grounding + fee math in
    # docs/archive/HFT.md). One contiguous block appended at the END of the dataclass; never
    # reordered (merge rule). These run ONLY on the separate high-frequency
    # paper book (mode='hft'), never on the standard watchlists.
    # micro-breakout (Zarattini & Aziz ORB analogue at a 5m cadence, taker).
    # BAR counts below are 5m bars: the book moved 1m -> 5m on 2026-09-19 and
    # every bar-denominated window was re-scaled to keep its wall-clock
    # meaning (30 x 1m = 6 x 5m), so the research horizons still hold.
    hft_bo_range: int = 12             # rolling range bars (~1h: the "opening range" analogue)
    hft_bo_atr_min_pct: float = 0.0008 # ATR >= 0.08% of price (the cost floor raises this when the fee tier demands)
    hft_bo_stop_atr: float = 1.0
    hft_bo_target_rr: float = 2.0      # the papers' 2R take-profit
    hft_bo_time_stop: int = 12         # bars (~1h)
    hft_bo_vol_ratio_min: float = 1.3  # volume confirmation vs 20-bar mean
    # exhaustion fade (Carver's 4-8 min mean-reversion horizon + capitulation
    # volume spike; MAKER entry — the gross edge is a few bp)
    hft_fade_z_entry: float = 2.5      # |z of log(close/ema50)|, rolling 100-bar sigma
    hft_fade_vol_spike: float = 3.0    # vol_ratio (vol vs 20-bar mean) must exceed this
    hft_fade_clv_max: float = -0.8     # close in the extreme tail of the bar's range
    hft_fade_stop_atr: float = 2.0
    hft_fade_time_stop: int = 9        # bars (~45 min: inside the documented MR half-life band)
    # exit mode (experiment, docs/archive/HFT_TRADE_FREQUENCY.md). None: exit with a
    # market order once price is back at the mean (taker, the live default).
    # A number: rest a take-profit at the entry-time mean, this many bp
    # beyond it so price must trade THROUGH the level to fill (a touch is not
    # a fill) — maker fee, no slippage; stops and the time stop stay taker.
    hft_fade_limit_exit_bps: float | None = None
    # Avellaneda-Stoikov-inspired maker (gamma-sigma quote width + drift skew)
    hft_mm_gamma: float = 0.1          # risk aversion (A-S notation)
    hft_mm_width_frac_atr: float = 0.5 # quote half-width = this x ATR (A-S width scales with sigma)
    hft_mm_min_width_bps: float = 2.0  # half-width floor (bp of price)
    hft_mm_max_width_bps: float = 15.0
    hft_mm_target_rr: float = 0.34     # target ~ one half-width against a 3-half-width stop (MM brackets INVERT the swing ratio)
    hft_mm_stop_widths: float = 3.0    # hard stop at 3 half-widths (inventory blowup guard)
    hft_mm_time_stop: int = 12         # bars (~1h)
    # ---- cost floors (bp of price), DERIVED from the book's fee tier by
    # bot/hft.build_hft_config — never hand-set per strategy.
    # WHY: RiskManager.approve refuses any stop tighter than the modeled
    # taker round trip ("tiny stop (dust)") — a win that cannot pay its own
    # fees is dust. A strategy that sizes stops off raw ATR without this
    # number emits signals the risk manager then kills, so on a quiet tape
    # (BTC ATR ~5-8bp vs a 16bp perp round trip) every decision is vetoed and
    # nothing says why. Each strategy refuses an unpayable setup itself.
    hft_cost_floor_bps: float = 16.0        # taker in + taker out (perp tier default)
    hft_maker_cost_floor_bps: float = 10.0  # maker in + taker out (resting-entry strategies)
    hft_cost_buffer: float = 1.25           # required edge over the floor (x)
    # order-flow imbalance momentum (Cont, Kukanov & Stoikov 2014: OFI is
    # near-linearly related to short-horizon price change). OHLCV proxy:
    # signed volume = CLV x volume, summed over a short window, z-scored.
    hft_ofi_window: int = 3            # bars of signed volume in the imbalance sum (~15 min)
    hft_ofi_z_entry: float = 1.5       # |z| of the imbalance to act on
    hft_ofi_stop_atr: float = 1.0
    hft_ofi_target_rr: float = 1.5
    hft_ofi_time_stop: int = 3         # bars (~15 min: the documented OFI horizon is minutes)
    # cross-pair spread reversion (candidate): a cross like ETH/BTC IS the
    # price ratio of two co-moving coins, so fading its stretches is a pairs
    # trade in one instrument — gated on the spread actually reverting
    hft_xr_z_entry: float = 2.0         # |z of log(close/ema50)|, rolling 100-bar sigma
    hft_xr_halflife_window: int = 200   # bars in the AR(1) half-life fit (~17h)
    hft_xr_halflife_max: float = 24.0   # bars (2h): reversion must happen inside the hold
    hft_xr_stop_atr: float = 2.5
    hft_xr_time_stop: int = 36          # bars (3h)
    # funding-rate extremes (candidate): a crowded side of the perpetual
    # (funding far from its own recent norm) fading once price stretches
    # with the crowd and the bar turns against it
    hft_fund_z_entry: float = 2.0       # |z of the funding rate| vs its last 90 prints
    hft_fund_price_z: float = 1.0       # price stretched with the crowd (z of log(close/ema50))
    hft_fund_stop_atr: float = 2.0
    hft_fund_target_rr: float = 2.0
    hft_fund_time_stop: int = 36        # bars (3h)
