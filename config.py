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


# ALGO_SKIP_DOTENV: the test suite sets it so a developer's local .env (an
# API key, a dashboard token) cannot change a test result — see tests/conftest
if os.environ.get("ALGO_SKIP_DOTENV") != "1":
    _load_dotenv()


# WHERE EVERY SETTING CAME FROM. The env vars that steer this bot are read
# in a dozen places and a wrong one fails SILENTLY — the .env loader bug
# (auth was off because DASHBOARD_TOKEN never reached the process) and the
# fee-tier default are both in docs/archive/HISTORY.md. Each read is recorded here so
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


from bot.params import StrategyParams  # noqa: E402,F401  (re-exported)


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
# the fast book's floor on a declared take-profit (in R): its brackets invert
# the swing ratio, so it carries its own floor (bot/hft.build_hft_config) and
# strategies refuse a nearer target themselves instead of being vetoed
HFT_MIN_TARGET_RR = 0.3

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
    # the standard book's universe: crypto + forex, priced in US dollars
    # 1h: turtle trend + ensemble
    MarketSpec("crypto", "BTC/USDT", "1h", "Bitcoin"),
    MarketSpec("crypto", "ETH/USDT", "1h", "Ethereum"),
    MarketSpec("crypto", "SOL/USDT", "1h", "Solana"),
    # 15m: VWAP scalper (5m measured cost-negative -> 15m, see docs/archive/BACKTESTS.md)
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
    """Cross-symbol capital allocation (skfolio-backed, see docs/archive/RESEARCH.md §2.5).

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
    """The FAST paper book (bot/hft/, docs/archive/HFT.md): a SECOND paper account
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
