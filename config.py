"""
Central configuration for the AI trading bot.

All tunables live here. Environment variables override defaults so the same
code can move from paper trading to a real broker later without edits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


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
    fee_crypto: float = 0.001
    fee_forex: float = 0.0002
    maker_fee_crypto: float = 0.001
    maker_fee_forex: float = 0.0001
    slippage_crypto: float = 0.0005
    slippage_forex: float = 0.0001
    maker_pricing: bool = _env_int("MAKER_PRICING", 1) == 1  # kill-switch for the maker model

    def fee(self, kind: str, maker: bool = False) -> float:
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


@dataclass
class MarketSpec:
    """A tradable market. kind is 'crypto' or 'forex'."""
    kind: str            # 'crypto' | 'forex'
    symbol: str          # ccxt style 'BTC/USDT' for crypto, yfinance style 'EURUSD=X' for forex
    timeframe: str       # '5m', '15m', '1h', '4h', '1d'
    display: str = ""    # pretty name for dashboard

    def __post_init__(self):
        if not self.display:
            self.display = self.symbol.replace("=X", "")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol, "timeframe": self.timeframe,
                "display": self.display}


def infer_kind(symbol: str) -> str:
    """'forex' for yfinance-style XXXXXX=X, else 'crypto' (ccxt BASE/QUOTE).
    One shared inference — call sites used to disagree on the fallback for
    malformed symbols, and kind drives a 5x fee/slippage difference."""
    return "forex" if "=" in symbol else "crypto"


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
class Config:
    paper_capital: float = _env_float("PAPER_CAPITAL", 10_000.0)
    db_path: str = _env_str("BOT_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trading.db"))
    data_cache_dir: str = os.path.join(os.path.dirname(__file__), "data", "cache")

    live_interval_seconds: int = _env_int("LIVE_INTERVAL", 60)
    lookback_bars: int = 400          # candles fetched per market per cycle

    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    params: StrategyParams = field(default_factory=StrategyParams)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    llm: LLMConfig = field(default_factory=LLMConfig.from_env)

    watchlist: list = field(default_factory=lambda: list(DEFAULT_WATCHLIST))

    news_feeds: list = field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://www.fxstreet.com/rss/news",
    ])
    news_max_items: int = 15
    news_ttl_seconds: int = 600


CONFIG = Config()

TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "data", "watchlist.json")
MAX_WATCHLIST_SPECS = 12
VALID_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
VALID_KINDS = ("crypto", "forex")


def _watchlist_to_dicts(specs: list) -> list:
    return [{"kind": s.kind, "symbol": s.symbol, "timeframe": s.timeframe,
             "display": s.display} for s in specs]


def save_watchlist(specs: list, path: str | None = None) -> bool:
    """Persist the watchlist to data/watchlist.json. Atomic (temp + replace) so
    a crash mid-write can't leave a torn JSON that the loader silently falls
    back from — returning the user's market list to a stale/default state.
    Returns False (never raises) when the write fails; callers surface it."""
    import json
    path = path or WATCHLIST_PATH
    tmp = f"{path}.tmp"
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
    path = path or WATCHLIST_PATH
    if os.path.exists(path):
        try:
            import json
            with open(path) as fh:
                payload = json.load(fh)
            specs = [MarketSpec(s["kind"], s["symbol"], s["timeframe"],
                                s.get("display") or "")
                     for s in payload.get("specs", [])]
            if specs:
                CONFIG.watchlist[:] = specs
        except Exception:
            try:    # keep the corrupt file for inspection, out of the load path
                os.replace(path, f"{path}.corrupt")
            except OSError:
                pass
            print("[config] watchlist.json unreadable — moved to .corrupt, "
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
