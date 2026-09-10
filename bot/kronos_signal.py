"""
Kronos signal — probabilistic forecasts from a financial foundation model.

Kronos (arXiv 2508.02739, AAAI 2026, MIT license) is a decoder-only foundation
model pre-trained on K-line (OHLCV) sequences from 45+ global exchanges: a
tokenizer quantizes OHLCV into hierarchical discrete tokens, an autoregressive
Transformer forecasts them. We use `Kronos-small` (24.7M params) via the Hugging
Face Hub (NeoQuasar/Kronos-small + Kronos-Tokenizer-base).

EARNED VOTING RIGHTS (the important part):
    Kronos starts as a *tracked non-voter*. Every cycle it forecasts, we log
    direction + probability vs what actually happened, and a rolling rank-IC
    is kept. It is promoted to a 4th voter in the orchestrator only after
    `min_observations` forecasts with IC >= `ic_hurdle`; demoted if IC decays
    below the demotion floor. The model never votes on faith — the same
    evidence standard the bot applies to every other strategy.

Probabilistic use:
    `sample_count` forecast paths -> P(up), P(hit target before stop),
    dispersion. These are inputs for sizing and the vote, not trade
    instructions; raw forecasts are not alpha (the Kronos authors say so
    themselves — portfolio construction and risk control sit downstream).

Heavy deps (torch, transformers) load lazily; the bot runs fully without them.
Weights download once into the HF cache (~100MB).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd

from config import TIMEFRAME_SECONDS


@dataclass
class KronosConfig:
    model_name: str = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
    tokenizer_name: str = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
    max_context: int = 512
    sample_count: int = 30          # forecast paths (probabilistic, not point)
    temperature: float = 1.0
    top_p: float = 0.9
    # --- earned voting rights -------------------------------------------
    ic_hurdle: float = 0.02         # rolling IC needed to earn a vote
    demote_below: float = 0.0       # rolling IC that loses the vote again
    min_observations: int = 60      # forecasts before promotion is even possible
    ic_half_life: int = 100          # exp-weighted IC memory (bars of forecasts)
    evaluate_every_bars: int = 4     # live cadence: forecast every N closed bars
    track_file: str = os.path.join(os.path.dirname(__file__), "..", "data", "kronos_ic.json")


@dataclass
class KronosSignal:
    direction: str          # LONG | SHORT | FLAT
    p_up: float             # share of sampled paths closing above spot
    dispersion_pct: float | None        # std of path endpoints (%)
    expected_return_pct: float | None   # mean path endpoint (%)
    horizon_bars: int = 0
    bar_ts: str = ""        # decision bar the forecast was computed on (IC anchor)
    rationale: str = ""


class KronosICTracker:
    """Persistent, exponentially-weighted rank-IC ledger for Kronos forecasts.

    Stores (forecast score, realized forward return) pairs as they RESOLVE
    (forecast at t is scored against close[t + horizon]); IC is Spearman over
    the exp-weighted window. Survives restarts via a small JSON file."""

    def __init__(self, path: str, half_life: int = 100):
        self.path = path
        self.half_life = half_life
        self.records: list[tuple[float, float]] = []   # (score, realized_fwd)
        self._pending: list[dict] = []
        self._load()

    def _load(self):
        try:
            import json
            if os.path.exists(self.path):
                with open(self.path) as f:
                    data = json.load(f)
                self.records = [tuple(r) for r in data.get("records", [])]
                self._pending = [dict(p) for p in data.get("pending", [])]
        except Exception:
            # a torn file must not silently reset the promotion gate's memory:
            # quarantine it for inspection (the ledger starts empty and says so)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            self.records = []
            self._pending = []

    def _save(self):
        """Atomic (tmp + replace): a crash mid-write used to tear the JSON and
        _load silently reset the whole IC ledger — the promotion gate's memory."""
        import json
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"records": self.records[-2000:],
                           "pending": self._pending[-500:]}, f)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.path.exists(tmp) and os.remove(tmp)
            except OSError:
                pass

    def log_forecast(self, score: float, ts, horizon: int, market: str = ""):
        """Record a pending forecast; resolved when its horizon bar arrives.

        `market` keys the forecast to the frame that produced it — a BTC
        forecast must never be scored against whichever sibling symbol's
        closes happened to resolve first."""
        self._pending.append({"score": float(score), "ts": str(ts),
                              "horizon": int(horizon), "market": market})

    def resolve(self, closes: pd.Series, market: str = ""):
        """Match pending forecasts to their realized forward returns.

        Only forecasts logged for THIS market resolve here. Legacy pendings
        saved before market keying carry no market and resolve against
        whatever calls first (old behavior) — they drain within one horizon."""
        if not self._pending or not len(closes):
            return
        try:
            idx = pd.to_datetime(closes.index)
        except Exception:
            return
        still: list[dict] = []
        for p in self._pending:
            if p.get("market") and p["market"] != market:
                still.append(p)
                continue
            try:
                t0 = pd.Timestamp(p["ts"])
            except Exception:
                continue
            # positional resolution: bar containing t0, + horizon bars
            start = idx.searchsorted(t0, side="right") - 1
            end = start + p["horizon"]
            if start < 0 or start >= len(closes) - 1:
                still.append(p)
                continue
            if end < len(closes):
                fwd = float(closes.iloc[end]) / float(closes.iloc[start]) - 1.0
                self.records.append((p["score"], fwd))
            else:
                still.append(p)  # horizon hasn't finished forming
        self._pending = still
        self._save()

    def ic(self) -> float | None:
        """Exponentially-weighted Spearman IC of scores vs realized returns.

        An exp-decay window with half-life h has effective sample size ~2h
        recent observations, so we rank-IC over the trailing 2*half_life
        records; older forecasts barely matter."""
        if len(self.records) < 10:
            return None
        n_eff = max(10, int(2 * self.half_life))
        recs = self.records[-n_eff:]
        scores = [r[0] for r in recs]
        rets = [r[1] for r in recs]
        try:
            a = pd.Series(scores).rank().to_numpy(dtype=float)
            b = pd.Series(rets).rank().to_numpy(dtype=float)
            denom = float(a.std() * b.std())
            if denom <= 0:
                return None
            return float(((a - a.mean()) * (b - b.mean())).mean() / denom)
        except Exception:
            return None

    def n(self) -> int:
        return len(self.records)


class KronosPredictorLazy:
    """Lazily-loaded Kronos stack (torch/transformers + HF weights).

    Falls back to None when unavailable so the orchestrator path never breaks:
    a missing model degrades to 'no Kronos signal', never an engine crash."""

    def __init__(self, cfg: "KronosConfig | None" = None):
        self.cfg = cfg or KronosConfig()
        self._predictor = None
        self._failed = False

    def _ensure(self):
        if self._predictor is not None or self._failed:
            return self._predictor
        try:
            import sys
            model_root = os.path.join(os.path.dirname(__file__), "..", "models", "kronos")
            if model_root not in sys.path:
                sys.path.insert(0, model_root)
            if not os.path.isdir(os.path.join(model_root, "model")):
                raise RuntimeError(
                    "Kronos source not vendored at models/kronos "
                    "(git clone https://github.com/shiyu-coder/Kronos -> models/kronos)")
            # pyrefly: ignore [missing-import]
            from model import Kronos, KronosTokenizer, KronosPredictor
            import model.kronos as _kronos_mod
            # the vendored model draws a tqdm progress bar per forecast — pure
            # noise in server logs and on a projector; a plain range is the
            # identical loop without the terminal churn
            _kronos_mod.trange = range
            tok = KronosTokenizer.from_pretrained(self.cfg.tokenizer_name)
            model = Kronos.from_pretrained(self.cfg.model_name)
            self._predictor = KronosPredictor(model, tok, max_context=self.cfg.max_context)
        except Exception:
            self._failed = True
            return None
        return self._predictor

    def _probe(self) -> bool:
        """Cheap availability check: vendored source + importable heavy deps.
        Deliberately does NOT call _ensure() — from_pretrained downloads ~100MB
        of weights, and `available` runs inside the dashboard's start-HTTP
        request (TradingEngine.__init__): the request used to hang for the
        whole download on cold venue WiFi with a dead-looking Start button.
        The load itself happens on the FIRST evaluate() call, which runs in
        the engine thread."""
        if self._failed:
            return False
        if self._predictor is not None:
            return True
        try:
            model_root = os.path.join(os.path.dirname(__file__), "..", "models", "kronos")
            if not os.path.isdir(os.path.join(model_root, "model")):
                return False
            # configured model/tokenizer must be a Hub id (org/name, exactly one
            # slash) or an existing local path — "tests/fixtures/missing_model"
            # is neither, so the probe rejects it WITHOUT paying a load
            import re as _re
            for name in (self.cfg.model_name, self.cfg.tokenizer_name):
                if not os.path.exists(name) and not _re.fullmatch(
                        r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", name):
                    return False
            import torch            # noqa: F401  (probe only: import, no load)
            import transformers     # noqa: F401
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._probe()


def _future_index(last_ts, horizon: int, timeframe: str) -> pd.DatetimeIndex:
    """`horizon` future bar stamps spaced TIMEFRAME_SECONDS[timeframe] apart,
    starting STRICTLY after `last_ts`, tz-aware UTC.

    Pure pandas — unit-testable without the model. This replaces the
    infer_freq-based stamping on irregular books (forex weekend gaps, exchange
    outages, cached frames with holes): there infer_freq returns None and the
    old "1h" fallback stamped a 15m book's 24-step forecast across 24 wrong
    hours (and a 4h book's across 24 instead of 96). The spacing comes from
    the caller's ACTUAL timeframe, not from the (possibly gapped) index."""
    step = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    last = pd.Timestamp(last_ts)
    if last.tzinfo is not None:
        last = last.tz_convert("UTC")
    else:
        last = last.tz_localize("UTC")
    return pd.date_range(start=last + step, periods=horizon, freq=step, tz="UTC")


class KronosSignalEngine:
    """The bot-facing interface: forecast -> probabilistic signal + IC ledger.

    This engine NEVER trades by itself. It emits a KronosSignal, logs it for
    the IC tracker, and exposes `promoted()` — whether the model has EARNED a
    vote in the orchestrator under the config hurdles."""

    def __init__(self, cfg: "KronosConfig | None" = None):
        self.cfg = cfg or KronosConfig()
        self.predictor = KronosPredictorLazy(self.cfg)
        self.tracker = KronosICTracker(self.cfg.track_file, half_life=self.cfg.ic_half_life)
        self.last_error: str | None = None   # set by evaluate(); never silently swallow

    # ------------------------------------------------------------- forecast
    def evaluate(self, df: pd.DataFrame, horizon: int = 24,
                 timeframe: str | None = None) -> KronosSignal | None:
        p = self.predictor._ensure()
        if p is None or df is None or len(df) < 30:
            return None
        try:
            x = df.tail(self.cfg.max_context)
            cols = [c for c in ("open", "high", "low", "close", "volume") if c in x]
            x = x[cols]
            x_ts = pd.Series(x.index)
            # future stamps: the timeframe (when the caller knows it — the
            # engine always does) keeps irregular books honest; infer_freq is
            # the legacy fallback for callers without one (main.py cmd_kronos)
            if timeframe is not None:
                fut_idx = _future_index(x.index[-1], horizon, timeframe)
            else:
                freq = pd.infer_freq(df.index) or "1h"
                fut_idx = pd.date_range(x.index[-1], periods=horizon + 1,
                                        freq=freq, tz="UTC")[1:]
            y_ts = pd.Series(fut_idx)

            # SEQUENTIAL single-sample calls on purpose: the vendored
            # KronosPredictor averages the sample dimension before returning
            # (kronos.py auto_regressive_inference ends with
            # np.mean(preds, axis=1)), so one batched call with
            # sample_count=30 yields ONE mean path — P(up) would collapse to
            # exactly 0/1 and dispersion to 0. Batching requires patching the
            # vendored model (out of scope; see FLAW_VALIDATION correction #3).
            preds = []
            sample_count = max(1, self.cfg.sample_count)
            for _ in range(sample_count):
                pred = p.predict(df=x.copy(), x_timestamp=x_ts.copy(), y_timestamp=y_ts.copy(),
                                 pred_len=horizon, T=self.cfg.temperature,
                                 top_p=self.cfg.top_p, sample_count=1)
                if pred is not None and len(pred):
                    preds.append(pred)
            if not preds:
                return None

            spot = float(df["close"].iloc[-1])
            ends = [float(pr["close"].iloc[-1]) for pr in preds if "close" in pr]
            if not ends:
                return None
            p_up = sum(1 for e in ends if e > spot) / len(ends)
            exp_ret = (sum(ends) / len(ends) / spot - 1.0) * 100.0
            disp = float(pd.Series(ends).std(ddof=0) if len(ends) > 1 else 0.0) / spot * 100.0

            direction = "FLAT"
            if p_up >= 0.60:
                direction = "LONG"
            elif p_up <= 0.40:
                direction = "SHORT"

            sig = KronosSignal(
                direction=direction, p_up=round(p_up, 3),
                dispersion_pct=round(disp, 3), expected_return_pct=round(exp_ret, 3),
                horizon_bars=horizon, bar_ts=str(df.index[-1]),
                rationale=(f"Kronos {self.cfg.model_name.split('/')[-1]}: {len(ends)} sampled "
                           f"{horizon}-bar paths, P(up)={p_up:.0%}, E[ret]={exp_ret:+.2f}%, "
                           f"dispersion {disp:.2f}%"),
            )
            return sig
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    # ------------------------------------------------------------- promotion
    def promoted(self) -> bool:
        """Kronos has EARNED an orchestrator vote (with hysteresis):
        promote at IC >= ic_hurdle after min_observations resolved forecasts,
        keep the vote while IC >= demote_below. The latch makes the gate
        history-dependent: a promoted model isn't demoted by a single
        sub-hurdle reading, only by genuine IC decay."""
        ic = self.tracker.ic()
        n = self.tracker.n()
        if n < self.cfg.min_observations or ic is None:
            self._promoted = False
            return False
        if getattr(self, "_promoted", False):
            keep = ic >= self.cfg.demote_below
        else:
            keep = ic >= self.cfg.ic_hurdle
        self._promoted = keep
        return keep

    # ------------------------------------------------------------- scoring
    def log_and_maybe_resolve(self, df: pd.DataFrame, sig: KronosSignal,
                              market: str = ""):
        """Record the forecast for later IC scoring (anchored to ITS decision
        bar, keyed to ITS market), and resolve pending forecasts of THIS
        market whose horizon bars have since closed. FLAT forecasts carry no
        information and are not scored."""
        if sig is None:
            return
        self.tracker.resolve(df["close"], market=market)
        if sig.direction == "FLAT":
            return
        # signed conviction in [-1, 1]: strong up-read -> +, strong down-read -> -
        score = 2.0 * sig.p_up - 1.0
        self.tracker.log_forecast(score, sig.bar_ts or str(df.index[-1]),
                                  sig.horizon_bars, market=market)
