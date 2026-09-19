"""
Central configuration for the AI trading bot.

All tunables live here. Environment variables override defaults so the same
code can move from paper trading to a real broker later without edits.
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _load_dotenv(path: str | None = None) -> None:
    """Import-time .env load so DIRECT entry points (e.g. `uvicorn
    bot.dashboard:app`, which never goes through main.py's loader) still pick
    up DASHBOARD_TOKEN/BOT_DB_PATH. Existing process env always wins; a
    missing file is the normal case. Same parsing rules as main.py: leading
    `export ` stripped, ` #` comments stripped outside quotes."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = _strip_inline_comment(value.strip())
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # no .env is the normal case


def _strip_inline_comment(s: str) -> str:
    """Cut a ` #` comment outside quotes (a `#` inside '...'/"..." stays)."""
    in_single = in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double \
                and i > 0 and s[i - 1] in (" ", "\t"):
            return s[:i].rstrip()
    return s


_load_dotenv()


# WHERE EVERY SETTING CAME FROM. The env vars that steer this bot are read
# in a dozen places and a wrong one fails SILENTLY — the .env loader bug
# (auth was off because DASHBOARD_TOKEN never reached the process) and the
# fee-tier default are both in HISTORY.md. Each read is recorded here so
# `python3 main.py config` can print the effective value AND its source.
ENV_PROVENANCE: dict[str, dict] = {}


def _record(name: str, default, value, ok: bool = True) -> None:
    ENV_PROVENANCE[name] = {
        "value": value, "default": default,
        "source": "env" if name in os.environ else "default",
        "raw": os.environ.get(name), "ok": ok,
    }


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        warnings.warn(f"[config] {name} unreadable — using default {default}")
        _record(name, default, default, ok=False)
        return default
    _record(name, default, value)
    return value


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        warnings.warn(f"[config] {name} unreadable — using default {default}")
        _record(name, default, default, ok=False)
        return default
    _record(name, default, value)
    return value


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    _record(name, default, value)
    return value


def utc_now() -> str:
    """Canonical timestamp for journal/engine writes (ISO-UTC, seconds).
    One shared clock: the engine and the journal used to define identical
    private copies."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01        # 1% of equity risked per trade
    max_position_pct: float = 0.25      # max notional per position (25% of equity)
    max_open_positions: int = 4
    # gross-notional leverage cap: total open notional (all books, marked) plus
    # the new entry's pre-fill notional may not exceed this multiple of equity.
    # The ~1x bound used to be only IMPLICIT (25% x 4 positions); making it an
    # explicit gate enforces it across mixed timeframes/books where the per-
    # position and per-symbol caps cannot see the whole picture (audit Fix
    # 2.2-lite: 4 books x 25% can stack to 1x in the live engine, and nothing
    # bounded the total if the per-position cap was ever raised).
    max_gross_leverage: float = 1.0
    # CORRELATED-CLUSTER CAP. max_gross_leverage bounds the WHOLE book, and
    # max_position_pct bounds one position — but four "independent" positions
    # in BTC, ETH, SOL and their crosses are one bet wearing four hats, and
    # nothing above noticed that. This caps the gross notional of any single
    # correlated family (see bot.risk.correlation_cluster).
    # NOTE FOR THE OPERATOR: 0.6 x equity allows roughly two full-size
    # positions in one family. It is a RISK POLICY number, not a measurement
    # — raise it to trade a family harder, lower it to force diversification.
    max_cluster_leverage: float = 0.6
    daily_loss_kill_switch: float = 0.03  # stop opening trades after -3% day
    min_confidence: float = 0.55        # orchestrator confidence floor for entries
    # R-distance sanity gate (the old dead max_r_per_trade knob promised it):
    # a stop wider than max_r_per_trade of the entry price means ATR exploded
    # — refuse rather than size into a vol regime the exits can't manage
    max_r_per_trade: float = 0.10      # stop may sit at most 10% of entry price
    # reward floor: when a strategy DOES declare a fixed target, sub-1.2R
    # trades don't pay for their own risk (signal-exit strategies pass None
    # and skip the gate)
    min_rr_per_trade: float = 1.2      # declared targets must be ≥ 1.2R
    cooldown_bars_after_stop: int = 3   # bars to wait on a symbol after stop-out
    # drawdown throttle (research roadmap: auto-shrink size in drawdowns so a
    # bad regime can't compound losses at full risk; scales size only — the
    # kill switch remains the only full stop)
    drawdown_half_risk_at: float = 0.10   # DD ≥ 10% → new trades risk half
    drawdown_quarter_risk_at: float = 0.20  # DD ≥ 20% → new trades risk a quarter


@dataclass
class CostConfig:
    # crypto (Binance-like taker fees) and forex (spread modeled as cost).
    # Market legs (entries, stop/manual/signal exits) pay the taker fee plus
    # adverse slippage. A bracket take-profit is a RESTING LIMIT: it fills at
    # its level (or the better open on a gap) and pays no slippage — the fee
    # earned for providing liquidity. Crypto maker is deliberately equal to
    # taker (Binance base tier charges both 0.10%) so the only modeled maker
    # benefit is the eliminated slippage, not a fee-tier assumption. Forex
    # maker halves the modeled spread cost: resting inside the book instead
    # of crossing it.
    #
    fee_crypto: float = 0.001
    fee_forex: float = 0.0002
    maker_fee_crypto: float = 0.001
    maker_fee_forex: float = 0.0001
    slippage_crypto: float = 0.0005
    slippage_forex: float = 0.0001
    maker_pricing: bool = _env_int("MAKER_PRICING", 1) == 1  # kill-switch for the maker model

    def fee(self, kind: str, maker: bool = False, side: str | None = None) -> float:
        """Per-leg cost rate for `kind` ('crypto' | 'forex'). `side` is kept
        in the signature (call sites pass it) but no longer changes the rate:
        it existed for India's buy-side stamp duty, and the India universe
        was removed on 2026-09-19 (zero trades, zero decisions, ever)."""
        if maker:
            return self.maker_fee_crypto if kind == "crypto" else self.maker_fee_forex
        return self.fee_crypto if kind == "crypto" else self.fee_forex

    def slippage(self, kind: str, maker: bool = False) -> float:
        if maker:
            return 0.0     # a resting limit fills at its own level; nothing is crossed
        return self.slippage_crypto if kind == "crypto" else self.slippage_forex


@dataclass
class StrategyParams:
    # Turtle trend following (Donchian breakout)
    turtle_entry_period: int = 20
    turtle_exit_period: int = 10
    turtle_atr_period: int = 14
    turtle_stop_atr: float = 2.0
    turtle_adx_min: float = 20.0        # volatility/trend regime filter (see RESEARCH.md §2.1)

    # Connors RSI-2 mean reversion (documented on daily bars -> we trade 4h, the
    # closest tradable analogue; on 1h the per-trade edge < costs, see BACKTESTS.md)
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
    # A/B + walk-forward in BACKTESTS.md Round 6: 3 of 4 cells positive,
    # walk-forward positive on BOTH symbols; binds rarely (~2 entries/yr).
    mr_halflife_max: float = 12.0

    # VWAP scalper (ORB-inspired + volume confirmation)
    # Cost study on real data (BACKTESTS.md): 5m crypto taker-fee round trip
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
    # negative across 60/90/180d and walk-forward (BACKTESTS.md), so it ships
    # OFF: 0.0 auto-passes. Raise it (e.g. 1.10) to experiment; NaN (no volume
    # data / fresh slots) always passes either way — forex stays ungated.
    scalper_rvol_min: float = 0.0
    scalper_break_even_rr: float = 1.0  # move stop to breakeven after 1R
    scalper_min_confidence: float = 0.62    # low-conf entries measured as noise
    scalper_short_min_confidence: float = 1.01  # shorts disabled: every short bucket lost money in testing (BACKTESTS.md)
    scalper_trend_filter: bool = True   # longs need close > EMA200, shorts close < EMA200
    scalper_adx_min: float = 25.0       # entries below ADX 25 measured as negative-edge
    scalper_target_rr: float | None = None  # no fixed TP: caps measured at avg_win $12 vs $19 free
    # buffered VWAP exit: a single bar close across VWAP is noise; require
    # 2 consecutive closes beyond VWAP - 0.25 ATR (see BACKTESTS.md cost study)
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
    # HFT.md). One contiguous block appended at the END of the dataclass; never
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
    # Avellaneda-Stoikov-inspired maker (gamma-sigma quote width + drift skew)
    hft_mm_gamma: float = 0.1          # risk aversion (A-S notation)
    hft_mm_sigma_window: int = 60      # log-return std window (bars)
    hft_mm_width_frac_atr: float = 0.5 # quote half-width = this x ATR (A-S width scales with sigma)
    hft_mm_min_width_bps: float = 2.0  # half-width floor (bp of price)
    hft_mm_max_width_bps: float = 15.0
    hft_mm_target_rr: float = 0.34     # target ~ one half-width against a 3-half-width stop (MM brackets INVERT the swing ratio)
    hft_mm_stop_widths: float = 3.0    # hard stop at 3 half-widths (inventory blowup guard)
    hft_mm_time_stop: int = 12         # bars (~1h)
    # ---- cost floors (bp of price), DERIVED from the book's fee tier by
    # bot/hft.build_hft_config — never hand-set per strategy.
    # WHY THIS EXISTS: RiskManager.approve refuses any stop tighter than the
    # modeled taker round trip ("tiny stop (dust)"), and it is right to: a
    # win that cannot pay its own fees is dust. The 1m strategies used to
    # size stops off raw ATR with no reference to that number, so on a quiet
    # 1m tape (BTC ATR ~5-8bp vs a 16bp perp round trip) EVERY entry decision
    # was journaled and then vetoed — the HFT book produced 15 entry
    # decisions and 0 trades. A strategy that cannot clear costs must say so
    # itself instead of emitting a signal the risk manager has to kill.
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


@dataclass
class MarketSpec:
    """A tradable market. kind is 'crypto' or 'forex'.

    The 'india' kind (NSE cash equities + indices) was removed on 2026-09-19:
    zero trades and zero decisions in the whole journal, against a session
    calendar, a per-side regulatory cost stack, a currency-isolation rule and
    a market-mode toggle threaded through every module. Git history has it."""
    kind: str            # 'crypto' | 'forex'
    symbol: str          # ccxt style 'BTC/USDT', yfinance 'EURUSD=X' forex
    timeframe: str       # '5m', '15m', '1h', '4h', '1d'
    display: str = ""    # pretty name for dashboard

    def __post_init__(self):
        if not self.display:
            self.display = self.symbol.replace("=X", "")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol, "timeframe": self.timeframe,
                "display": self.display}


# FAST BOOK universe (bot/hft/): the separate intraday paper account.
# USD-only single-currency accounting (crypto + forex).
#
# WHY 5m AND NOT 1m (decided 2026-09-19, on measurement):
# the book ran 1m bars for one reason — speed — and 1m is where the cost wall
# wins. The modeled taker round trip is 16bp (perp tier) against a BTC 1m ATR
# of 5-8bp: a 1-ATR stop cannot pay for its own round trip, so every entry
# either got vetoed as dust or had to quote so wide it never filled. At 5m
# the same ATR is ~3-5x larger and clears the round trip honestly. The
# journal identifier stays 'hft' so the existing record keeps resolving; the
# book is an INTRADAY book, and the docs now say so rather than claiming a
# latency edge a polled OHLCV feed cannot have.
HFT_WATCHLIST: list[MarketSpec] = [
    # crypto 5m (ccxt public endpoints, 24/7)
    MarketSpec("crypto", "BTC/USDT", "5m", "Bitcoin fast"),
    MarketSpec("crypto", "ETH/USDT", "5m", "Ethereum fast"),
    MarketSpec("crypto", "SOL/USDT", "5m", "Solana fast"),
    MarketSpec("crypto", "ETH/BTC", "5m", "ETH/BTC cross"),
    # forex 5m (yfinance: 60d of history per request)
    MarketSpec("forex", "EURUSD=X", "5m", "EUR/USD fast"),
]


def infer_kind(symbol: str) -> str:
    """'forex' for yfinance-style XXXXXX=X, else 'crypto' (ccxt BASE/QUOTE).
    One shared inference — call sites used to disagree on the fallback for
    malformed symbols, and kind drives the cost model (crypto taker fees vs
    the forex spread), a 5x fee/slippage difference in both directions."""
    if "=" in symbol:
        return "forex"
    return "crypto"


def parse_utc(ts: str | None) -> "datetime | None":
    """ISO journal timestamp -> timezone-aware UTC datetime; naive rows read as
    UTC (the journal only ever writes UTC), blank/unparseable input -> None.
    One shared parser: risk and broker each used to hand-roll this with
    different fallbacks."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


DEFAULT_WATCHLIST: list[MarketSpec] = [
    # FOREX-mode universe (crypto + forex): the active book while the persisted
    # market mode (see MARKET_MODE below) is "forex" — the default. The India
    # universe lives in SPECS_INDIA; the two lists never merge.
    # 1h: turtle trend + ensemble
    MarketSpec("crypto", "BTC/USDT", "1h", "Bitcoin"),
    MarketSpec("crypto", "ETH/USDT", "1h", "Ethereum"),
    MarketSpec("crypto", "SOL/USDT", "1h", "Solana"),
    # 15m: VWAP scalper (5m measured cost-negative -> 15m, see BACKTESTS.md)
    MarketSpec("crypto", "BTC/USDT", "15m", "Bitcoin (scalp)"),
    MarketSpec("crypto", "ETH/USDT", "15m", "Ethereum (scalp)"),
    # 4h: Connors mean reversion (daily-bar strategy analogue)
    MarketSpec("crypto", "BTC/USDT", "4h", "Bitcoin (mean-rev)"),
    MarketSpec("crypto", "ETH/USDT", "4h", "Ethereum (mean-rev)"),
    # forex 1h
    MarketSpec("forex", "EURUSD=X", "1h", "EUR/USD"),
    MarketSpec("forex", "GBPUSD=X", "1h", "GBP/USD"),
]

@dataclass
class LLMConfig:
    provider: str = "none"   # 'openai' | 'anthropic' | 'none' (auto-detected from env)
    model: str = ""
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        if os.environ.get("OPENAI_API_KEY"):
            return cls("openai", _env_str("LLM_MODEL", "gpt-4o-mini"))
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls("anthropic", _env_str("LLM_MODEL", "claude-3-5-sonnet-latest"))
        return cls("none", "")


@dataclass
class PortfolioConfig:
    """Cross-symbol capital allocation (skfolio-backed, see RESEARCH.md §2.5).

    ENV TIMING (deliberate, do not "fix" piecemeal): every _env_* default in
    this file is read ONCE at import time — a process that changes
    PAPER_CAPITAL/BOT_DB_PATH/... mid-run keeps the import-time values (the
    documented way to change them is restart with new env). The two EXCEPTIONS
    are call-time by design and live elsewhere:
      - CACHE_FRESHNESS_HOURS (bot/data.py _cache_freshness_hours): one-off
        `CACHE_FRESHNESS_HOURS=0 ...` force-refetch must work per-invocation;
      - HFT_FEE_TIER (bot/hft/__init__.py hft_fee_tier): the harness builds
        both tiers in ONE process, so the tier must resolve per call.

    Capital is divided across CONCURRENT OPEN positions by risk budget:
    inverse-vol uses only each symbol's realized volatility (no return
    forecasts to overfit), max_sym_weight prevents the optimizer from
    concentrating everything in the currently calmest asset.
    """
    enabled: bool = _env_int("PORTFOLIO_ALLOC", 1) == 1
    method: str = _env_str("PORTFOLIO_METHOD", "inverse_vol")  # inverse_vol | hrp | equal
    lookback_bars: int = 200        # bars of returns for the allocator
    max_sym_weight: float = 0.40    # cap any single symbol's share of risk budget
    min_sym_weight: float = 0.10    # floor so a queued entry is never starved


@dataclass
class HFTConfig:
    """The FAST paper book (bot/hft/, HFT.md): a SECOND paper account
    trading 5m bars — same broker/risk/journal machinery as the
    standard book, separate capital, universe, cadence, and journal rows
    (mode='hft'; every journal read already filters on mode, so the whole
    HFT trade history is one filtered query)."""
    enabled: bool = _env_int("HFT_ENABLED", 1) == 1
    paper_capital: float = _env_float("HFT_PAPER_CAPITAL", 10_000.0)
    # 10s default poll against 5m bars: the new-bar gate means most cycles
    # are no-ops, so this only sets how quickly a CLOSED bar is acted on
    # (worst case ~10s of a 300s bar). The 2s setting the 1m book used bought
    # latency that a polled OHLCV feed cannot actually deliver on.
    live_interval_seconds: int = _env_int("HFT_INTERVAL", 10)
    lookback_bars: int = 400
    risk_per_trade: float = 0.005          # 0.5% per trade — faster book, tighter risk
    daily_loss_kill_switch: float = 0.02   # -2% day halts new HFT entries
    max_open_positions: int = 4
    min_confidence: float = 0.55
    # maker-fill realism: a resting limit must be PENETRATED by this many bps
    # before it fills (approximates queue priority — a touch is not a fill).
    # 0.0 = touch fills (optimistic); raise for an honest adverse-selection sim.
    maker_penetration_bps: float = _env_float("HFT_PENETRATION_BPS", 0.0)
    limit_wait_bars: int = 5               # unfilled maker entries expire after N bars


@dataclass
class Config:
    paper_capital: float = _env_float("PAPER_CAPITAL", 10_000.0)
    db_path: str = _env_str("BOT_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trading.db"))
    data_cache_dir: str = _env_str("BOT_CACHE_DIR", "") or _env_str("DATA_CACHE_DIR", "") or \
        os.path.join(os.path.dirname(__file__), "data", "cache")

    live_interval_seconds: int = _env_int("LIVE_INTERVAL", 60)
    lookback_bars: int = 400          # candles fetched per market per cycle

    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    params: StrategyParams = field(default_factory=StrategyParams)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    llm: LLMConfig = field(default_factory=LLMConfig.from_env)
    hft: HFTConfig = field(default_factory=HFTConfig)

    watchlist: list = field(default_factory=lambda: list(DEFAULT_WATCHLIST))


    def __post_init__(self):
        # Single-journal-dir rule: every derived state file (cache, watchlist,
        # mode, kronos ledger) lives next to CONFIG.db_path's dir, so a
        # BOT_DB_PATH override (or a test tmp dir) moves the WHOLE state, not
        # just the journal. Explicit overrides win: BOT_CACHE_DIR/DATA_CACHE_DIR
        # for the parquet cache (read above); WATCHLIST_PATH below keeps a
        # test-installed custom path, else follows db_path too.
        if not os.environ.get("BOT_CACHE_DIR") and not os.environ.get("DATA_CACHE_DIR"):
            self.data_cache_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.db_path)), "cache")


CONFIG = Config()

TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "data", "watchlist.json")
_DEFAULT_WATCHLIST_PATH = os.path.abspath(WATCHLIST_PATH)
MAX_WATCHLIST_SPECS = 12
# 1m is deliberately NOT here: no strategy trades it since the fast book
# moved to 5m (a 16bp round trip against a 5-8bp 1m ATR is unpayable), and an
# offerable timeframe that no strategy owns is a trap — it produces a
# watchlist spec that can only ever HOLD. TIMEFRAME_SECONDS keeps 1m so
# historical rows, caches and ad-hoc research frames still resolve.
VALID_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
VALID_KINDS = ("crypto", "forex")


def db_dir() -> str:
    """Directory holding the ACTIVE journal — every derived state file
    (watchlist, mode, kronos ledger) resolves under this at CALL time so a
    BOT_DB_PATH override or test tmp dir moves the whole state together."""
    return os.path.dirname(os.path.abspath(CONFIG.db_path))


def cache_dir() -> str:
    """Resolved parquet-cache dir: the explicit CONFIG.data_cache_dir
    (import-time BOT_CACHE_DIR/DATA_CACHE_DIR override or a test-installed
    path) wins, else the journal dir's `cache/` (the __post_init__ rule)."""
    return CONFIG.data_cache_dir


def watchlist_path() -> str:
    """Resolved watchlist.json: an explicitly customized WATCHLIST_PATH (tests
    install one) wins; otherwise the journal dir's `watchlist.json` — the
    split-brain fix (the old module constant always pointed at the repo's
    data/ even when BOT_DB_PATH moved the journal elsewhere)."""
    if os.path.abspath(WATCHLIST_PATH) != _DEFAULT_WATCHLIST_PATH:
        return WATCHLIST_PATH
    return os.path.join(db_dir(), "watchlist.json")


def kronos_track_path() -> str:
    """Resolved kronos_ic.json under the journal dir (STRAT agent: point
    KronosSignalEngine's default track_file here so BOT_DB_PATH overrides
    stop leaking the IC ledger into the repo's data/)."""
    return os.path.join(db_dir(), "kronos_ic.json")

def _watchlist_to_dicts(specs: list) -> list:
    return [{"kind": s.kind, "symbol": s.symbol, "timeframe": s.timeframe,
             "display": s.display} for s in specs]


def save_watchlist(specs: list, path: str | None = None) -> bool:
    """Persist the watchlist to data/watchlist.json. Atomic (temp + replace) so
    a crash mid-write can't leave a torn JSON that the loader silently falls
    back from — returning the user's market list to a stale/default state.
    Returns False (never raises) when the write fails; callers surface it."""
    import json
    path = path or watchlist_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump({"specs": _watchlist_to_dicts(specs)}, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        return False


def apply_saved_watchlist(path: str | None = None) -> list:
    """Load data/watchlist.json into CONFIG.watchlist if it exists.

    Mutates CONFIG.watchlist IN PLACE ([:] =) so a running engine that shares
    the CONFIG singleton picks the change up on its next cycle. Never raises —
    a corrupt file is renamed aside (not silently ignored: the bot must not
    quietly trade a different market list than the user configured) and the
    default watchlist is used.
    """
    path = path or watchlist_path()
    if os.path.exists(path):
        try:
            import json
            with open(path) as fh:
                payload = json.load(fh)
            specs = [MarketSpec(s["kind"], s["symbol"], s["timeframe"],
                                s.get("display") or "")
                     for s in payload.get("specs", [])]
            # a saved spec whose kind/timeframe the bot no longer trades can
            # only ever HOLD (its fetch path is gone). Drop those and say so,
            # rather than loading a watchlist the engine cannot act on — the
            # india kind and the 1m timeframe were both retired 2026-09-19.
            live = [sp for sp in specs
                    if sp.kind in VALID_KINDS and sp.timeframe in VALID_TIMEFRAMES]
            dropped = [sp for sp in specs if sp not in live]
            if dropped:
                print(f"[config] dropped {len(dropped)} watchlist spec(s) the bot "
                      f"no longer trades: "
                      f"{', '.join(f'{sp.kind}:{sp.symbol} {sp.timeframe}' for sp in dropped)}")
            if live:
                CONFIG.watchlist[:] = live
            elif specs:
                print("[config] every saved watchlist spec was retired — "
                      "falling back to the default watchlist")
                save_watchlist(CONFIG.watchlist, path)
        except Exception:
            try:    # keep the corrupt file for inspection, out of the load path
                os.replace(path, f"{path}.corrupt.{int(datetime.now(timezone.utc).timestamp())}")
            except OSError:
                pass
            print("[config] watchlist.json unreadable — moved to .corrupt.<epoch>, "
                  "using the default watchlist")
    else:
        save_watchlist(CONFIG.watchlist, path)
    return CONFIG.watchlist


def bars_per_year(timeframe: str, kind: str = "crypto") -> float:
    """Bars/year for Sharpe annualization. Crypto trades 24/7; Yahoo forex
    trades ~24x5 (weekend gaps), so the 24/7 count overstated forex Sharpe
    magnitudes ~18% — kind='forex' scales the count by 5/7."""
    bars = (365.0 * 86400.0) / TIMEFRAME_SECONDS[timeframe]
    return bars * (5.0 / 7.0) if kind == "forex" else bars
