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

OFFLINE SINCE 2026-09-19. Kronos is no longer wired into the live trading
loop. Three measurements ended that: it never earned a vote; one 1m forecast
costs ~41s of CPU (30 sequential paths x 60 bars) against a cycle budget of
seconds; and two books forecasting at once aborted the process on Metal
(the model auto-selects MPS, which is single-threaded). It now runs as a
research job — `main.py kronos` walks history, resolves the IC ledger and
prints the verdict; the dashboard's Evidence tab reads that ledger. The
promotion gate below is intact, so the day the ledger says Kronos earns a
vote, wiring it back in is a decision with evidence behind it rather than a
hope. (A background forecast worker lived here until that move; git history
has it.)

Heavy deps (torch, transformers) load lazily; the bot runs fully without them.
Weights download once into the HF cache (~100MB).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import pandas as pd

from config import TIMEFRAME_SECONDS


@dataclass
class KronosConfig:
    model_name: str = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
    tokenizer_name: str = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
    max_context: int = 512
    sample_count: int = 30          # forecast paths (probabilistic, not point)
    # MEASURED (Kronos-small, CPU, 2026-09-19): one forecast path costs ~0.5s
    # at horizon 24 and ~1.4s at horizon 60, and the paths run SEQUENTIALLY
    # (the vendored predictor averages a batch, which would collapse P(up)).
    # 30 paths x 60 bars = 41s per symbol — 20x the HFT book's whole cycle
    # budget. Fast books therefore sample fewer paths; P(up) from 8 paths is
    # coarser (0.125 granularity) but it is a real distribution, and the
    # forecast runs off the critical path either way (KronosForecastService).
    fast_sample_count: int = 8      # books faster than 5m
    fast_timeframe_seconds: int = 300
    temperature: float = 1.0
    top_p: float = 0.9
    # --- earned voting rights -------------------------------------------
    ic_hurdle: float = 0.02         # rolling IC needed to earn a vote
    demote_below: float = 0.0       # rolling IC that loses the vote again
    min_observations: int = 60      # forecasts before promotion is even possible
    ic_half_life: int = 100          # exp-weighted IC memory (bars of forecasts)
    evaluate_every_bars: int = 4     # live cadence: forecast every N closed bars
    track_file: str = os.path.join(os.path.dirname(__file__), "..", "data", "kronos_ic.json")


# --------------------------------------------------------------- horizon policy
# How far ahead Kronos forecasts, per book timeframe. Two hard constraints:
#
#   1. The vendored predictor is autoregressive over its own context window:
#      `KronosPredictor.generate` can emit at most `max_context` (512) steps,
#      and `predict` then builds a frame indexed by the caller's y_timestamp,
#      so ANY horizon above max_context raises
#      "Shape of passed values is (512, 6), indices imply (N, 6)".
#   2. The horizon is also what the IC ledger scores Kronos on
#      (`log_and_maybe_resolve` -> `KronosICTracker.resolve` reads
#      `sig.horizon_bars`), so it must match the book's actual holding period
#      or the model is graded on a question the book never asks.
#
# A full trading day fits comfortably on 5m and slower (288 bars at 5m, 96 at
# 15m, 24 at 1h, 1 at 1d). It does NOT fit on a 1m book: 1440 bars is both
# unproducible AND the wrong question — the HFT book's own time stops are
# 45-60 bars, so a day-ahead read would score Kronos on a move it never
# holds through. Sub-5m books therefore forecast (and are scored over) one
# hour: 60 bars at 1m, inside the predictor's reach and matched to the book.
KRONOS_FAST_TIMEFRAME_SECONDS = 300    # a book faster than 5m is "sub-5m"
KRONOS_FAST_HORIZON_SECONDS = 3600     # ...and forecasts 1h ahead
KRONOS_HORIZON_SECONDS = 86400         # every other book: ~1 day ahead


def kronos_horizon(timeframe: str, max_context: int = KronosConfig.max_context) -> int:
    """Forecast horizon in bars for `timeframe` (see the policy note above).

    Raises ValueError when the resulting horizon exceeds what the predictor
    can actually generate — a policy bug, surfaced at config time by
    `validate_horizon_policy` rather than once per cycle forever."""
    step = TIMEFRAME_SECONDS[timeframe]
    ahead = (KRONOS_FAST_HORIZON_SECONDS if step < KRONOS_FAST_TIMEFRAME_SECONDS
             else KRONOS_HORIZON_SECONDS)
    bars = max(1, ahead // step)
    if bars > max_context:
        raise ValueError(
            f"Kronos horizon for {timeframe} is {bars} bars, above the "
            f"predictor's max_context ({max_context}) — it can generate at "
            f"most {max_context} steps. Shorten the horizon policy for this "
            f"timeframe (see KRONOS_FAST_HORIZON_SECONDS in bot/kronos_signal.py).")
    return bars


def validate_horizon_policy(max_context: int = KronosConfig.max_context) -> dict[str, int]:
    """Assert every supported timeframe maps to a producible horizon.

    Called once when the engine attaches Kronos: an over-long horizon used to
    fail deep inside `evaluate`, which swallows exceptions into `last_error`,
    making Kronos a permanent silent no-op on the 1m book. Now it raises
    before the first cycle."""
    return {tf: kronos_horizon(tf, max_context=max_context) for tf in TIMEFRAME_SECONDS}


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
        # the forecast worker appends records while the engine thread resolves
        # them (and both save): every mutation below takes this lock
        self._lock = threading.RLock()
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
        with self._lock:
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
        with self._lock:
            still: list[dict] = []
            for p in list(self._pending):
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


# ONE model per process, and ONE inference at a time.
#
# Each TradingEngine used to build its own KronosSignalEngine, so running the
# standard and HFT books together loaded the 25M-param model TWICE (~1GB RSS)
# and ran two CPU-saturating inferences concurrently — torch already uses
# every core per call, so the second one only steals from the first. Both
# books then stalled at cycle 0 with the machine pegged, which is what
# "starting both engines crashes it" actually was.
# On Apple Silicon the vendored predictor auto-selects the MPS (Metal)
# backend, and Metal command buffers cannot be driven from two threads: the
# second one aborts the PROCESS, not the thread —
#   "failed assertion _status < MTLCommandBufferStatusCommitted
#    at line 323 in -[IOGPUMetalCommandBuffer setCurrentCommandEncoder:]"
# — which is exactly what "starting both engines just crashes it" was. Every
# model touch (load AND inference) therefore happens under ONE lock, on ONE
# worker thread (see KronosForecastService / forecast_service).
_SHARED: dict = {}                    # (model_name, tokenizer_name, max_context) -> predictor
_SHARED_LOCK = threading.RLock()      # guards the dict AND the load itself
_INFERENCE_LOCK = _SHARED_LOCK        # one lock for every model touch


class KronosPredictorLazy:
    """Lazily-loaded Kronos stack (torch/transformers + HF weights).

    Falls back to None when unavailable so the orchestrator path never breaks:
    a missing model degrades to 'no Kronos signal', never an engine crash."""

    def __init__(self, cfg: "KronosConfig | None" = None):
        self.cfg = cfg or KronosConfig()
        self._predictor = None
        self._failed = False
        self._failed_at: float | None = None  # monotonic ts of last load failure
        self._fail_count = 0                  # consecutive failures (backoff base)

    # P0: the old boolean latch never retried — one transient failure
    # (cold HF cache, venue WiFi) disabled Kronos for the whole process.
    # Retry with exponential backoff: 60s, 120s, 240s ... capped at 1h.
    RETRY_BASE_SEC = 60.0
    RETRY_MAX_SEC = 3600.0

    def _retry_due(self) -> bool:
        """True when a (re)load attempt is currently allowed: always on a
        clean slate, otherwise only once the backoff window since the last
        failure has elapsed (a newly vendored weights dir / restored network
        is picked up on the next due attempt, not never)."""
        if not self._failed:
            return True
        backoff = min(self.RETRY_MAX_SEC,
                      self.RETRY_BASE_SEC * (2 ** max(0, self._fail_count - 1)))
        return (time.monotonic() - (self._failed_at or 0.0)) >= backoff

    def _key(self):
        return (self.cfg.model_name, self.cfg.tokenizer_name, self.cfg.max_context)

    def _ensure(self):
        if self._predictor is not None:
            return self._predictor
        if self._failed and not self._retry_due():
            return None
        # the LOAD runs under the same lock as inference: two engines loading
        # concurrently moved two models onto Metal and aborted the process
        with _SHARED_LOCK:
            shared = _SHARED.get(self._key())
            if shared is not None:
                # another book already paid for this load — inference is
                # stateless, so the model is shared (see the note above)
                self._predictor = shared
                self._failed = False
                self._failed_at = None
                self._fail_count = 0
                return shared
            return self._load_locked()

    def _load_locked(self):
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
            _SHARED[self._key()] = self._predictor
            self._failed = False
            self._failed_at = None
            self._fail_count = 0
        except Exception:
            self._failed = True
            self._failed_at = time.monotonic()
            self._fail_count += 1
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
        if self._failed and not self._retry_due():
            return False
        if self._predictor is not None:
            return True
        with _SHARED_LOCK:
            if _SHARED.get(self._key()) is not None:
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

    def sample_budget(self, timeframe: str | None) -> int:
        """Forecast paths to sample for `timeframe`.

        The paths run sequentially (the vendored predictor averages a batch),
        so this is a linear cost dial: 30 paths x 60 bars measured 41s on CPU.
        Sub-5m books sample fewer — the forecast still runs off the trading
        cycle (KronosForecastService), but a 41s job per symbol per 4 bars
        would keep a core busy permanently for a model that has not yet
        earned a vote."""
        if timeframe is not None:
            try:
                if TIMEFRAME_SECONDS[timeframe] < self.cfg.fast_timeframe_seconds:
                    return max(1, self.cfg.fast_sample_count)
            except KeyError:
                pass
        return max(1, self.cfg.sample_count)

    # ------------------------------------------------------------- forecast
    def evaluate(self, df: pd.DataFrame, horizon: int = 24,
                 timeframe: str | None = None) -> KronosSignal | None:
        # Deliberately OUTSIDE the try below: everything in there degrades to
        # "no signal this cycle", but a horizon the predictor cannot generate
        # is a caller bug that would otherwise hide as a per-cycle last_error
        # forever (it did, on the 1m book).
        if horizon > self.cfg.max_context:
            raise ValueError(
                f"horizon={horizon} exceeds the predictor's max_context "
                f"({self.cfg.max_context}); it can generate at most "
                f"{self.cfg.max_context} steps")
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
            # vendored model (out of scope; see HISTORY.md correction #3).
            preds = []
            sample_count = max(1, self.sample_budget(timeframe))
            # ONE inference at a time process-wide: torch saturates every
            # core per call, so two books forecasting concurrently just
            # thrash (see _INFERENCE_LOCK)
            for _ in range(sample_count):
                with _INFERENCE_LOCK:
                    pred = p.predict(df=x.copy(), x_timestamp=x_ts.copy(),
                                     y_timestamp=y_ts.copy(),
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


