"""Strategy validator: is a backtest's trade list evidence of an edge?
(roadmap V3 — an overfitting check for strategies people already have)

Input is the trade list a backtest produced, in one of three forms:

  * a freqtrade backtest export (`backtest-result-*.json`, or the `.zip`
    newer versions write) — only its documented output format is read;
  * a CSV of trades (open and close time and net P&L per trade; fees and
    notional optional) with lenient column names;
  * this repo's own `main.py backtest --json` output.

Every check works from the trades alone, so the validator never needs the
strategy's code; the price of that is that it cannot re-optimise anything,
which is why "stability" here means consistency across time, not a refit.

  intervals     90% block-bootstrap interval on profit factor and per-trade
                Sharpe (bot/evidence_stats.py, the gate's own statistics)
  stability     profit factor in each chronological quarter of the period
  costs         profit factor with fees x0.5, x1 and x2 (needs fees)
  selection     Deflated Sharpe for the number of variants tried (--trials;
                only the user knows it, and 1 is the most generous answer)
  PBO           probability of backtest overfitting across the strategies
                in one file, when it holds two or more on the same period
  regimes       trades and profit factor per market regime, from daily bars

The verdict uses the bar this repo holds its own strategies to (rule v2):
"robust" needs every check to pass AND to have been run; a check that could
not run (no fees, no price data) caps the verdict at "fragile". The point is
to say "not proven" plainly, not to grade generously.
"""
from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from bot.evidence_stats import (REGIMES, profit_factor, regime_at, sharpe_per_trade,
                                trade_evidence)

ROBUST, FRAGILE, OVERFIT = "robust", "fragile", "likely overfit"

MIN_TRADES = 100          # the gate's v2 floor
MIN_PER_REGIME = 10
MIN_REGIMES = 3
SEGMENTS = 4              # chronological quarters for the stability check
MIN_SEGMENT_TRADES = 5
FEE_MULTS = (0.5, 1.0, 2.0)
DSR_PASS, DSR_FAIL = 0.95, 0.5
PBO_BLOCKS = 16           # time blocks the PBO paths are cut into

_OPEN = ("open_date", "open_time", "entry_time", "entry_ts", "opened_ts", "open",
         "entry_date")
_CLOSE = ("close_date", "close_time", "exit_time", "exit_ts", "closed_ts", "close",
          "exit_date")
_PNL = ("profit_abs", "pnl", "net_pnl", "profit", "pnl_abs")
_FEE = ("fees", "fee", "fee_abs", "commission")
_NOTIONAL = ("stake_amount", "notional", "stake")
_PAIR = ("pair", "symbol", "market")


class ValidatorError(ValueError):
    """The input cannot be read as a trade list."""


# --------------------------------------------------------------------- input
def _pick(row: dict, names: tuple):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _ts(v) -> pd.Timestamp:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return pd.Timestamp(int(v), unit="ms" if v > 1e11 else "s", tz="UTC")
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _normalise(row: dict) -> dict:
    """One trade in the validator's terms; raises on a row it cannot use."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    opened, closed, pnl = (_pick(lower, _OPEN), _pick(lower, _CLOSE),
                           _float(_pick(lower, _PNL)))
    if opened is None or closed is None or pnl is None:
        raise ValidatorError("each trade needs an open time, a close time and a net P&L "
                             f"(columns like {_OPEN[0]}, {_CLOSE[0]}, {_PNL[0]}); got "
                             f"{sorted(lower)}")
    fee = _float(_pick(lower, _FEE))
    if fee is None and _float(lower.get("fee_open")) is not None:
        # freqtrade: fee rates per leg on the traded value of each leg
        amount = _float(lower.get("amount")) or 0.0
        fee = (amount * (_float(lower.get("open_rate")) or 0.0) * _float(lower["fee_open"])
               + amount * (_float(lower.get("close_rate")) or 0.0)
               * (_float(lower.get("fee_close")) or _float(lower["fee_open"])))
    notional = _float(_pick(lower, _NOTIONAL))
    if notional is None and _float(lower.get("qty")) and _float(lower.get("entry_price")):
        notional = abs(_float(lower["qty"]) * _float(lower["entry_price"]))
    return {"open": _ts(opened), "close": _ts(closed), "pnl": pnl,
            "fee": None if fee is None else abs(fee), "notional": notional,
            "pair": _pick(lower, _PAIR)}


def _from_rows(rows: list[dict]) -> list[dict]:
    trades = [_normalise(r) for r in rows if not r.get("is_open")]
    trades.sort(key=lambda t: (t["close"], t["open"]))
    return trades


def _freqtrade(doc: dict) -> dict:
    out = {}
    for name, res in (doc.get("strategy") or {}).items():
        out[name] = _from_rows(res.get("trades") or [])
    return out


def _json_doc(doc) -> tuple[str, dict]:
    if isinstance(doc, dict) and isinstance(doc.get("strategy"), dict):
        return "freqtrade", _freqtrade(doc)
    if isinstance(doc, dict) and isinstance(doc.get("trades"), list):
        name = (doc.get("stats") or doc.get("aggregate") or {}).get("strategy") or "strategy"
        return "trade list (JSON)", {str(name): _from_rows(doc["trades"])}
    if isinstance(doc, list):
        return "trade list (JSON)", {"strategy": _from_rows(doc)}
    raise ValidatorError("JSON is neither a freqtrade backtest export nor a trade list")


def load_trades(path: str | Path) -> tuple[str, dict]:
    """(source description, {strategy name: trades sorted by close time})."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                if member.endswith(".json") and not member.endswith(("_config.json",)):
                    doc = json.loads(zf.read(member))
                    if isinstance(doc, dict) and "strategy" in doc:
                        return "freqtrade", _freqtrade(doc)
        raise ValidatorError(f"{path.name}: no freqtrade backtest result inside the zip")
    text = path.read_text()
    if path.suffix == ".json" or text.lstrip()[:1] in "[{":
        return _json_doc(json.loads(text))
    rows = list(csv.DictReader(io.StringIO(text)))
    return "trade list (CSV)", {path.stem: _from_rows(rows)}


def load_daily(path: str | Path) -> pd.DataFrame:
    """Daily OHLC from a CSV with date/open/high/low/close columns."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    date_col = next((c for c in ("date", "timestamp", "time", "datetime") if c in df), None)
    if date_col is None or not {"high", "low", "close"} <= set(df.columns):
        raise ValidatorError("price CSV needs date, high, low and close columns")
    df.index = pd.DatetimeIndex([_ts(v) for v in df[date_col]])
    return df[["open", "high", "low", "close"]
              if "open" in df else ["high", "low", "close"]].astype(float).sort_index()


def regime_daily(by_strategy: dict, symbol: str) -> pd.DataFrame:
    """Daily bars of `symbol` covering every trade plus the 200+ days the
    regime labels need before the first one (fetched, then cached)."""
    import datetime as dt
    from bot.data import fetch_history
    from config import MarketSpec, infer_kind
    first = min((ts[0]["open"] for ts in by_strategy.values() if ts), default=None)
    days = 260 + ((dt.datetime.now(dt.timezone.utc) - first).days if first else 0)
    return fetch_history(MarketSpec(infer_kind(symbol), symbol, "1d", symbol), days=days)


# ------------------------------------------------------------------- checks
def _pf(pnls) -> float | None:
    v = profit_factor(pnls)
    return None if v is None else round(v, 3)


def stability(trades: list[dict]) -> dict:
    """Profit factor in each chronological quarter of the traded period."""
    if not trades:
        return {"segments": [], "profitable": 0, "measured": 0}
    t0, t1 = trades[0]["close"], trades[-1]["close"]
    span = (t1 - t0) / SEGMENTS
    segs = []
    for k in range(SEGMENTS):
        lo, hi = t0 + span * k, t0 + span * (k + 1)
        pnls = [t["pnl"] for t in trades
                if lo <= t["close"] and (t["close"] < hi or k == SEGMENTS - 1)]
        segs.append({"from": lo.date().isoformat(), "to": hi.date().isoformat(),
                     "trades": len(pnls), "pf": _pf(pnls),
                     "measured": len(pnls) >= MIN_SEGMENT_TRADES})
    measured = [s for s in segs if s["measured"]]
    return {"segments": segs, "measured": len(measured),
            "profitable": sum(1 for s in measured if (s["pf"] or 0) >= 1.0)}


def cost_sensitivity(trades: list[dict]) -> dict:
    """Profit factor with each trade's fees scaled. Net P&L already paid fees
    once, so fees x m moves each trade by (1 - m) x its fee."""
    if not trades or any(t["fee"] is None for t in trades):
        return {"available": False,
                "why": "the trade list has no per-trade fees (add a fees column)"}
    rows = []
    for m in FEE_MULTS:
        pnls = [t["pnl"] + (1.0 - m) * t["fee"] for t in trades]
        rows.append({"fee_mult": m, "pf": _pf(pnls), "net": round(sum(pnls), 2)})
    return {"available": True, "rows": rows,
            "fees_total": round(sum(t["fee"] for t in trades), 2)}


def deflated_sharpe(pnls, trials: int) -> dict:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado 2014) on per-trade
    returns: the probability that the true Sharpe is above the best Sharpe
    `trials` worthless variants would show by luck. trials=1 is the
    Probabilistic Sharpe ratio against zero."""
    from bot.validation import _norm_inv_cdf, _norm_sf
    a = np.asarray(pnls, dtype=float)
    n = a.size
    sr = sharpe_per_trade(a)
    if sr is None or n < 10:
        return {"dsr": None, "why": "needs 10+ trades with varying P&L"}
    trials = max(1, int(trials))
    if trials == 1:
        sr0 = 0.0
    else:
        g = 0.5772156649015329
        e_max = ((1 - g) * _norm_inv_cdf(1 - 1 / trials)
                 + g * _norm_inv_cdf(1 - 1 / (trials * math.e)))
        sr0 = e_max / math.sqrt(n - 1)        # null: each trial's SR has var 1/(n-1)
    s = pd.Series(a)
    skew, kurt = float(s.skew()), float(s.kurt()) + 3.0
    var = (1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / (n - 1)
    if var <= 0:
        return {"dsr": None, "why": "degenerate return distribution"}
    dsr = 1.0 - _norm_sf((sr - sr0) / math.sqrt(var))
    return {"dsr": round(dsr, 3), "sharpe_per_trade": round(sr, 4),
            "trials": trials, "sr0": round(sr0, 4)}


def pbo(family: dict[str, list[dict]]) -> dict:
    """PBO across the strategies in one file, on PBO_BLOCKS equal time
    blocks of their shared period (bot/validation.py: pbo_cscv)."""
    from bot.validation import pbo_cscv
    family = {n: ts for n, ts in family.items() if ts}
    if len(family) < 2:
        return {"pbo": None, "why": "needs two or more strategies run on the same period "
                                    "(a freqtrade file with several strategies)"}
    t0 = min(ts[0]["close"] for ts in family.values())
    t1 = max(ts[-1]["close"] for ts in family.values())
    if t1 <= t0:
        return {"pbo": None, "why": "the strategies share no period"}
    width = (t1 - t0) / PBO_BLOCKS
    paths = {}
    for name, ts in family.items():
        sums = [0.0] * PBO_BLOCKS
        for t in ts:
            sums[min(PBO_BLOCKS - 1, int((t["close"] - t0) / width))] += t["pnl"]
        paths[name] = sums
    res = pbo_cscv(paths)
    return {"pbo": res.get("pbo"), "why": res.get("reason") or res.get("verdict"),
            "configs": len(paths)}


def regimes(trades: list[dict], daily: pd.DataFrame | None) -> dict:
    """Trades and profit factor per market regime on the day each opened."""
    if daily is None:
        return {"available": False,
                "why": "no daily prices (pass --regime-market or --prices)"}
    from bot.evidence_stats import label_regimes
    labels = label_regimes(daily)
    by: dict[str, list[float]] = {r: [] for r in REGIMES}
    unlabelled = 0
    for t in trades:
        r = regime_at(labels, t["open"])
        if r is None:
            unlabelled += 1
        else:
            by[r].append(t["pnl"])
    rows = [{"regime": r, "trades": len(p), "pf": _pf(p)} for r, p in by.items()]
    return {"available": True, "rows": rows, "unlabelled": unlabelled,
            "covered": sum(1 for row in rows if row["trades"] >= MIN_PER_REGIME)}


# ------------------------------------------------------------------ verdict
def verdict(r: dict) -> tuple[str, list[str], list[str]]:
    """(label, reasons it is not robust, checks that passed)."""
    ev, fails, passes, overfit = r["intervals"], [], [], []
    n, lo, hi = ev["trades"], ev["pf_lo"], ev["pf_hi"]
    if hi is not None and hi < 1.0:
        overfit.append(f"even the top of the profit-factor interval ({hi:.2f}) is below 1.0: "
                       "the backtest itself shows no edge")
    elif ev["pf"] is not None and ev["pf"] < 1.0:
        overfit.append(f"no edge: it loses money in its own backtest (PF {ev['pf']:.2f}), "
                       "which is the optimistic case")
    if n < MIN_TRADES:
        fails.append(f"{n} trades; {MIN_TRADES} are needed before an interval means much")
    if lo is None or lo < 1.0:
        fails.append("the pessimistic end of the profit-factor interval is "
                     + (f"{lo:.2f}, below 1.0" if lo is not None else "unmeasurable"))
    else:
        passes.append(f"profit-factor interval {lo:.2f}–{hi:.2f} stays above 1.0")
    st = r["stability"]
    if st["measured"] < SEGMENTS or st["profitable"] < SEGMENTS - 1:
        fails.append(f"profitable in {st['profitable']} of {st['measured']} measurable "
                     f"quarters; {SEGMENTS - 1} of {SEGMENTS} are needed")
    else:
        passes.append(f"profitable in {st['profitable']} of {SEGMENTS} quarters")
    costs = r["costs"]
    if not costs["available"]:
        fails.append(f"cost sensitivity not checked: {costs['why']}")
    else:
        doubled = next(row for row in costs["rows"] if row["fee_mult"] == 2.0)
        if doubled["pf"] is None or doubled["pf"] < 1.0:
            fails.append(f"with fees doubled the profit factor is {doubled['pf']}")
        else:
            passes.append(f"survives doubled fees (PF {doubled['pf']:.2f})")
    ds = r["deflated_sharpe"]
    if ds["dsr"] is None:
        fails.append(f"Deflated Sharpe not measurable: {ds['why']}")
    else:
        what = (f"Deflated Sharpe {ds['dsr']:.2f} for {ds['trials']} variant(s) tried")
        if ds["sharpe_per_trade"] <= 0:
            fails.append(f"{what}: there is no positive Sharpe to deflate")
        elif ds["trials"] > 1 and ds["dsr"] < DSR_FAIL:
            overfit.append(f"{what}: the result is what trying that many variants "
                           "produces by luck")
        elif ds["dsr"] < DSR_PASS:
            fails.append(f"{what}; {DSR_PASS} is needed")
        else:
            passes.append(what)
    if r["trials_given"] is None:
        fails.append("number of variants tried not given (--trials): 1 was assumed, "
                     "the most generous case")
    # PBO judges the family the strategy was picked from, so it can sink a
    # strategy but is not listed as one of its own strengths
    pb = r["pbo"]
    if pb["pbo"] is not None and pb["pbo"] >= 0.5:
        overfit.append(f"PBO {pb['pbo']:.2f}: picking the best of the strategies in this "
                       "file does worse than a coin flip out of sample")
    rg = r["regimes"]
    if not rg["available"]:
        fails.append(f"regime coverage not checked: {rg['why']}")
    elif rg["covered"] < MIN_REGIMES:
        fails.append(f"{MIN_PER_REGIME}+ trades in {rg['covered']} of {MIN_REGIMES} "
                     "market regimes")
    else:
        passes.append(f"traded in all {MIN_REGIMES} market regimes")
    if overfit:
        return OVERFIT, overfit + fails, passes
    return (ROBUST if not fails else FRAGILE), fails, passes


def analyse(trades: list[dict], *, trials: int | None = None,
            daily: pd.DataFrame | None = None, family: dict | None = None) -> dict:
    """Every check on one strategy's trades. trials=None means the user did
    not say how many variants they tried: 1 is assumed, and the verdict
    cannot be robust."""
    pnls = [t["pnl"] for t in trades]
    r = {
        "trades": len(trades),
        "trials_given": trials,
        "period": ([trades[0]["open"].date().isoformat(), trades[-1]["close"].date().isoformat()]
                   if trades else None),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(100.0 * sum(p > 0 for p in pnls) / len(pnls), 1) if pnls else None,
        "intervals": trade_evidence(pnls),
        "stability": stability(trades),
        "costs": cost_sensitivity(trades),
        "deflated_sharpe": deflated_sharpe(pnls, trials or 1),
        "pbo": pbo(family or {}),
        "regimes": regimes(trades, daily),
    }
    r["verdict"], r["reasons"], r["passed"] = verdict(r)
    return r


def validate(path: str | Path, *, trials: int | None = None,
             daily: pd.DataFrame | None = None, strategy: str | None = None) -> dict:
    source, by_strategy = load_trades(path)
    if strategy:
        if strategy not in by_strategy:
            raise ValidatorError(f"no strategy {strategy!r} in the file "
                                 f"(it has {', '.join(by_strategy)})")
        names = [strategy]
    else:
        names = list(by_strategy)
    if not any(by_strategy[n] for n in names):
        raise ValidatorError("the file holds no closed trades")
    return {"input": Path(path).name, "source": source, "trials": trials,
            "strategies": {n: analyse(by_strategy[n], trials=trials, daily=daily,
                                      family=by_strategy) for n in names}}


# ------------------------------------------------------------------- report
def _f(v, spec=".2f", none="—"):
    return none if v is None else format(v, spec)


def render_markdown(report: dict) -> str:
    out = [f"# Strategy validation: {report['input']}", "",
           f"Source: {report['source']}. Variants tried (--trials): "
           + (str(report["trials"]) if report["trials"] else "not given, 1 assumed") + ".",
           ""]
    pb = next(iter(report["strategies"].values()))["pbo"]
    out += ["Probability of backtest overfitting (PBO) across the strategies in the file: "
            + (f"**{pb['pbo']:.2f}** over {pb['configs']} strategies "
               "(0.5 or more: picking the best of them is worse than a coin flip)."
               if pb["pbo"] is not None else f"not measured; {pb['why']}."), ""]
    for name, r in report["strategies"].items():
        ev = r["intervals"]
        out += [f"## {name}: **{r['verdict'].upper()}**", "",
                f"{r['trades']} closed trades"
                + (f", {r['period'][0]} to {r['period'][1]}" if r["period"] else "")
                + f"; net P&L {r['net_pnl']:+,.2f}; win rate {_f(r['win_rate'], '.1f')}%.", ""]
        if r["reasons"]:
            out += ["Why it is not robust:", ""] + [f"- {x}" for x in r["reasons"]] + [""]
        if r["passed"]:
            out += ["What held up:", ""] + [f"- {x}" for x in r["passed"]] + [""]
        out += ["| Check | Result |", "|---|---|",
                f"| Profit factor (90% interval) | {_f(ev['pf'])} ({_f(ev['pf_lo'])}–"
                f"{_f(ev['pf_hi'])}) |",
                f"| Sharpe per trade (90% interval) | {_f(ev['sharpe'], '.3f')} "
                f"({_f(ev['sharpe_lo'], '.3f')}–{_f(ev['sharpe_hi'], '.3f')}) |"]
        ds = r["deflated_sharpe"]
        out.append(f"| Deflated Sharpe ({ds.get('trials', 1)} variants) | "
                   + (_f(ds["dsr"]) if ds["dsr"] is not None else ds["why"]) + " |")
        out += ["", "Stability over time (profit factor per quarter):", "",
                "| Period | Trades | PF |", "|---|---:|---:|"]
        out += [f"| {s['from']} → {s['to']} | {s['trades']} | "
                f"{_f(s['pf']) if s['measured'] else 'too few'} |"
                for s in r["stability"]["segments"]]
        c = r["costs"]
        out += ["", "Cost sensitivity:", ""]
        if c["available"]:
            out += ["| Fees | PF | Net P&L |", "|---|---:|---:|"]
            out += [f"| ×{row['fee_mult']:g} | {_f(row['pf'])} | {row['net']:+,.2f} |"
                    for row in c["rows"]]
        else:
            out.append(f"Not checked: {c['why']}.")
        g = r["regimes"]
        out += ["", "Market regimes (on the day each trade opened):", ""]
        if g["available"]:
            out += ["| Regime | Trades | PF |", "|---|---:|---:|"]
            out += [f"| {row['regime']} | {row['trades']} | {_f(row['pf'])} |"
                    for row in g["rows"]]
            if g["unlabelled"]:
                out.append(f"\n{g['unlabelled']} trades opened before the price history "
                           "could label a regime (200 daily bars are needed).")
        else:
            out.append(f"Not checked: {g['why']}.")
        out.append("")
    out += ["---", "",
            "*Robust* means every check passed and every check ran: at least "
            f"{MIN_TRADES} trades, the pessimistic end of the profit-factor interval at "
            "or above 1.0, profitable in 3 of 4 quarters, still profitable with fees "
            f"doubled, Deflated Sharpe ≥ {DSR_PASS} for the variants tried, PBO below "
            f"0.5 when measurable, and {MIN_PER_REGIME}+ trades in each of "
            f"{MIN_REGIMES} market regimes. *Likely overfit* means the evidence points "
            "the other way. Anything else is *fragile*: not disproven, not proven. "
            "This is a statistical check of a backtest, not investment advice.", ""]
    return "\n".join(out)
