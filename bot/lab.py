"""Strategy Lab — pick any stock/pair, apply strategies, backtest them.

The user-facing flow (dashboard Lab tab, backed by this module):

  1. pick a BOOK      — "standard" (the forex+crypto+NSE paper book's
                        strategies and kind-aware costs) or "hft" (the
                        high-frequency book's 1m strategies + fee tiers)
  2. pick a MARKET    — kind (crypto | forex | india) + any symbol; aliases
                        normalize per kind ("BTCUSDT" -> "BTC/USDT",
                        "EURUSD" -> "EURUSD=X", "RELIANCE" -> "RELIANCE.NS",
                        "NIFTY" -> "^NSEI")
  3. pick strategies  — every strategy REGISTERED for the chosen timeframe
                        (one source of truth: STRATEGY_CLASSES), or "all"
                        for a comparison run on one fetched frame
  4. run + read       — full-cost backtest on real data; stats, equity
                        curve, trades, exit histogram; artifacts land in
                        data/results/lab_*.json

Design constraints honored from the repo's own rules:

- The lab is PURE backtest: its own PaperBroker/RiskManager per run, zero
  journal writes, zero shared state with the live engines — a lab run can
  never disturb an open paper position.
- Same costs as the books: kind-aware cost stack (crypto taker / forex
  spread / the full NSE regulatory stack) for the standard book, the HFT
  book's perp/spot tiers for the hft book.
- Honest guardrails: Yahoo's per-timeframe history caps are enforced with a
  visible note (1m forex/india = 7d, 5m/15m = 60d), lab compute caps per
  timeframe, warmup sized per book (220 bars standard, 400 on 1m/5m for the
  ema200 column).
- Runs execute on a background thread (a 365d fetch + backtest is minutes of
  IO); ONE run at a time (a second request is refused, not queued — the UI
  polls /lab/status instead).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass

from config import CONFIG, MarketSpec

# ---------------------------------------------------------------- suggestions
SUGGESTIONS: dict[str, list[str]] = {
    "crypto": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
               "ADA/USDT", "ETH/BTC", "BNB/USDT"],
    "forex": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
              "USDINR=X"],
    "india": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
              "SBIN.NS", "TATAMOTORS.NS", "^NSEI"],
}
# known quote currencies for glueless crypto input ("BTCUSDT" -> "BTC/USDT")
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "BNB", "INR")
# india aliases (index names yfinance writes with a caret)
_INDIA_ALIASES = {"NIFTY": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50": "^NSEI",
                  "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK"}


class LabError(ValueError):
    """User-input error (the API layer maps this to HTTP 422)."""


def normalize_symbol(kind: str, raw: str) -> str:
    """Per-kind alias normalization for the lab's symbol input.

    crypto: 'btcusdt'/'BTC-USDT'/'BTC/USDT' -> 'BTC/USDT' (quote inferred
            from the known list; ambiguous glueless input is refused, never
            guessed)
    forex:  'eurusd'/'EUR/USD'/'EURUSD=X' -> 'EURUSD=X'
    india:  'reliance' -> 'RELIANCE.NS'; 'RELIANCE.NS' kept; 'nifty' ->
            '^NSEI'; '^NSEI'/'^NSEBANK' kept verbatim
    """
    if not raw or not raw.strip():
        raise LabError("symbol is required")
    s = raw.strip().upper()
    if kind == "crypto":
        s = s.replace("-", "/").replace("_", "/")
        if "/" not in s:
            for q in _CRYPTO_QUOTES:
                if s.endswith(q) and len(s) > len(q):
                    s = f"{s[:-len(q)]}/{q}"
                    break
            else:
                raise LabError(
                    f"cannot split '{raw}' into BASE/QUOTE — write e.g. 'BTC/USDT' "
                    f"(known quotes: {', '.join(_CRYPTO_QUOTES)})")
        if not re.fullmatch(r"[A-Z0-9]{2,10}/[A-Z0-9]{2,10}", s):
            raise LabError(f"'{raw}' is not a valid crypto pair (BASE/QUOTE)")
        return s
    if kind == "forex":
        s = s.replace("/", "").replace("=X", "").replace("-", "")
        if not re.fullmatch(r"[A-Z]{6}", s):
            raise LabError(f"'{raw}' is not a valid forex pair (6 letters, e.g. EURUSD)")
        return f"{s}=X"
    if kind == "india":
        if s in _INDIA_ALIASES:
            return _INDIA_ALIASES[s]
        if s.startswith("^"):
            return s
        s = s.replace("-", "")
        if s.endswith(".NS"):
            return s
        if re.fullmatch(r"[A-Z0-9]{2,15}", s):
            return f"{s}.NS"
        raise LabError(f"'{raw}' is not a valid NSE symbol (e.g. RELIANCE, RELIANCE.NS, ^NSEI)")
    raise LabError(f"unknown kind '{kind}'")


# ------------------------------------------------------------- lab universe
# per-timeframe LAB compute caps (days of history the lab will run in one
# request) — 1m x 2 years would be a million-bar backtest, not a UI response
_LAB_DAYS_CAP = {"1m": 14, "5m": 60, "15m": 180, "1h": 730, "4h": 1825, "1d": 3650}
# Yahoo's own history caps per interval (forex + india ride yfinance)
_YAHOO_DAYS_CAP = {"1m": 7, "5m": 60, "15m": 60, "1h": 730, "4h": 730, "1d": 1825}
_HFT_TFS = ("1m", "5m")
_STANDARD_TFS = ("5m", "15m", "1h", "4h", "1d")


def timeframes_for(book: str, kind: str) -> list[str]:
    """Timeframes the lab offers, filtered by what the DATA source supports."""
    if book == "hft":
        tfs = list(_HFT_TFS)
    else:
        tfs = list(_STANDARD_TFS)
    if kind == "india" and book == "standard":
        # 15m india excluded (config.py: yfinance 60d cap vs the 220-bar
        # warmup — the acceptance window needs the history)
        tfs = [tf for tf in tfs if tf != "15m"]
    return tfs


def strategies_for(book: str, timeframe: str) -> list[str]:
    """Strategies REGISTERED for this timeframe (+ the ensemble blend for the
    standard book; the HFT book's ensemble votes across its three 1m
    strategies). One source of truth: the registry's preferred_timeframes."""
    from bot.strategies import STRATEGY_CLASSES
    names = sorted(name for name, cls in STRATEGY_CLASSES.items()
                   if timeframe in cls.preferred_timeframes)
    if book == "hft":
        names = [n for n in names if n.startswith("hft_")]
    return (["ensemble"] if names else []) + names


def days_cap(kind: str, timeframe: str) -> int:
    cap = _LAB_DAYS_CAP.get(timeframe, 365)
    if kind in ("forex", "india"):
        cap = min(cap, _YAHOO_DAYS_CAP.get(timeframe, cap))
    return cap


def default_days(kind: str, timeframe: str) -> int:
    cap = days_cap(kind, timeframe)
    default = {"1m": 3, "5m": 30, "15m": 60, "1h": 180, "4h": 365, "1d": 730}.get(timeframe, 180)
    if kind == "india" and timeframe == "1h":
        default = 90          # ~630 NSE 1h bars: clears the 220-bar warmup
    return min(default, cap)


def warmup_for(book: str, timeframe: str) -> int:
    # the shared indicator builder computes ema200; on fast bars give it room
    return 400 if (book == "hft" or timeframe == "1m") else 220


# ------------------------------------------------------------------- run spec
@dataclass
class LabSpec:
    book: str                 # 'standard' | 'hft'
    kind: str                 # 'crypto' | 'forex' | 'india'
    symbol: str               # normalized (normalize_symbol)
    timeframe: str
    strategy: str             # a strategy name or 'all'
    days: int = 0             # 0 -> default_days
    start: str | None = None  # pinned window (overrides days)
    end: str | None = None
    fee_tier: str | None = None  # hft book only: 'perp' | 'spot'

    def label(self) -> str:
        return f"{self.symbol} {self.timeframe} {self.strategy} [{self.book}]"


def _validate(spec: LabSpec) -> LabSpec:
    if spec.book not in ("standard", "hft"):
        raise LabError("book must be 'standard' or 'hft'")
    if spec.kind not in ("crypto", "forex", "india"):
        raise LabError("kind must be crypto, forex or india")
    spec.symbol = normalize_symbol(spec.kind, spec.symbol)
    if spec.timeframe not in timeframes_for(spec.book, spec.kind):
        raise LabError(
            f"timeframe {spec.timeframe!r} not offered for {spec.book}/{spec.kind} "
            f"(options: {', '.join(timeframes_for(spec.book, spec.kind))})")
    if spec.strategy != "all" and spec.strategy not in strategies_for(spec.book, spec.timeframe):
        raise LabError(
            f"strategy {spec.strategy!r} not registered for {spec.timeframe} "
            f"(options: {', '.join(strategies_for(spec.book, spec.timeframe))})")
    if spec.fee_tier is not None and spec.fee_tier not in ("perp", "spot"):
        raise LabError("fee_tier must be perp or spot")
    if spec.book == "hft" and spec.fee_tier is None:
        from bot.hft import hft_fee_tier
        spec.fee_tier = hft_fee_tier()
    if spec.book == "standard":
        spec.fee_tier = None
    if spec.start and spec.end:
        spec.days = 0
    else:
        cap = days_cap(spec.kind, spec.timeframe)
        spec.days = spec.days or default_days(spec.kind, spec.timeframe)
        if spec.days > cap:
            raise LabError(
                f"days={spec.days} exceeds the {timeframe_cap_note(spec.kind, spec.timeframe, cap)}")
    return spec


def timeframe_cap_note(kind: str, timeframe: str, cap: int) -> str:
    why = "lab compute cap"
    if kind in ("forex", "india") and timeframe in _YAHOO_DAYS_CAP and \
            _YAHOO_DAYS_CAP[timeframe] <= _LAB_DAYS_CAP.get(timeframe, 10**9):
        why = "yfinance history cap"
    return f"{cap}d cap for {timeframe} ({why})"


# ------------------------------------------------------------------- the run
def run_lab(spec: LabSpec) -> dict:
    """Synchronous lab backtest: fetch once, run the strategy (or every
    registered strategy for a comparison), return the full result payload.
    Raises LabError on bad input, RuntimeError on data failures."""
    spec = _validate(spec)
    if spec.book == "hft":
        from bot.hft import build_hft_config
        cfg = build_hft_config(fee_tier=spec.fee_tier)
    else:
        cfg = CONFIG

    from bot.backtest import Backtester
    from bot.data import fetch_history

    kind_spec = MarketSpec(spec.kind, spec.symbol, spec.timeframe)
    t0 = time.time()
    df = fetch_history(kind_spec, days=spec.days or None,
                       start=spec.start, end=spec.end)
    warmup = warmup_for(spec.book, spec.timeframe)
    if len(df) < warmup + 10:
        raise LabError(
            f"only {len(df)} bars returned for {spec.symbol} {spec.timeframe} — "
            f"need >= {warmup + 10} (raise the window or pick a longer timeframe)")
    fetch_s = round(time.time() - t0, 1)

    bt = Backtester(cfg)
    strategies = (strategies_for(spec.book, spec.timeframe)
                  if spec.strategy == "all" else [spec.strategy])
    runs, comparison = [], []
    for name in strategies:
        t1 = time.time()
        res = bt.run(kind_spec, df, strategy=None if name == "ensemble" else name,
                     warmup_bars=warmup)
        stats = res.stats()
        stats["strategy"] = name
        stats["runtime_s"] = round(time.time() - t1, 1)
        runs.append((name, res, stats))
        comparison.append({k: stats[k] for k in
                           ("strategy", "return_pct", "total_pnl", "trades",
                            "win_rate_pct", "profit_factor", "max_drawdown_pct",
                            "sharpe", "fees")})
        if spec.strategy == "all":
            bt = Backtester(cfg)   # fresh engine state per comparison cell

    best = max(runs, key=lambda r: r[2]["total_pnl"])
    costs = cfg.costs
    c = costs.fee(spec.kind) + costs.slippage(spec.kind)
    payload = {
        "spec": {"book": spec.book, "kind": spec.kind, "symbol": spec.symbol,
                 "timeframe": spec.timeframe, "strategy": spec.strategy,
                 "days": spec.days, "start": spec.start, "end": spec.end,
                 "fee_tier": spec.fee_tier, "warmup_bars": warmup},
        "bars": len(df),
        "window": {"first": str(df.index[0]), "last": str(df.index[-1])},
        "fetch_s": fetch_s,
        "taker_round_trip_bps": round(c * 2 * 1e4, 1),
        "stats": best[2],
        "trades": best[1].trades[-200:],
        "equity_curve": best[1].equity_curve,
        "exit_reasons": _exit_histogram(best[1].trades),
        "comparison": comparison if spec.strategy == "all" else None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_artifact(payload)
    return payload


def _exit_histogram(trades: list) -> dict:
    hist: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason") or "?"
        hist[r] = hist.get(r, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def _write_artifact(payload: dict) -> str:
    try:
        s = payload["spec"]
        name = f"lab_{s['book']}_{s['symbol'].replace('/', '').replace('=X', '')}_" \
               f"{s['timeframe']}_{s['strategy'].replace(' ', '')}_" \
               f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        path = os.path.join("data", "results", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=1, default=str)
        return path
    except Exception:
        return ""


# --------------------------------------------------------- async run manager
# ONE lab run at a time (a second POST is refused, not queued). State lives on
# the module so the UI polls it and tests can drive it hermetically.
_lab_lock = threading.Lock()
_lab_thread: threading.Thread | None = None
_lab_state: dict = {"status": "idle", "note": "", "result": None,
                    "error": None, "started_at": None, "finished_at": None}


def start_lab_run(params: dict) -> dict:
    """Validate + spawn the run on a worker thread. Returns the API response.
    Raises LabError for bad input (422); refuses a second concurrent run."""
    global _lab_thread
    spec = _validate(LabSpec(
        book=params.get("book", "standard"),
        kind=params.get("kind", "crypto"),
        symbol=params.get("symbol", ""),
        timeframe=params.get("timeframe", "1h"),
        strategy=params.get("strategy", "ensemble"),
        days=int(params.get("days") or 0) or 0,
        start=params.get("start") or None,
        end=params.get("end") or None,
        fee_tier=params.get("fee_tier") or None,
    ))
    with _lab_lock:
        if _lab_thread is not None and _lab_thread.is_alive():
            raise LabError("a lab run is already in progress — wait for it (poll /api/lab/status)")
        _lab_state.update(status="running", note=f"fetching {spec.symbol} {spec.timeframe}…",
                          result=None, error=None,
                          started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          finished_at=None)

    def _worker(s: LabSpec):
        global _lab_thread
        try:
            result = run_lab(s)
            with _lab_lock:
                _lab_state.update(status="done", note=result["window"]["first"] + " → "
                                  + result["window"]["last"],
                                  result=result,
                                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        except Exception as exc:
            with _lab_lock:
                _lab_state.update(status="error",
                                  error=f"{type(exc).__name__}: {exc}",
                                  note="run failed",
                                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            if not isinstance(exc, LabError):
                traceback.print_exc()
        finally:
            _lab_thread = None

    _lab_thread = threading.Thread(target=_worker, args=(spec,), daemon=True)
    _lab_thread.start()
    return {"status": "started", "spec": {"book": spec.book, "kind": spec.kind,
                                          "symbol": spec.symbol,
                                          "timeframe": spec.timeframe,
                                          "strategy": spec.strategy}}


def lab_status() -> dict:
    with _lab_lock:
        return dict(_lab_state)


def lab_running() -> bool:
    return _lab_thread is not None and _lab_thread.is_alive()
