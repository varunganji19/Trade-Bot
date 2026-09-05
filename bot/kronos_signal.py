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
from dataclasses import dataclass, field

import pandas as pd


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
    p_target_before_stop: float | None  # path share touching +1R stop-dist before -1R
    dispersion_pct: float | None        # std of path endpoints (%)
    expected_return_pct: float | None   # mean path endpoint (%)
    horizon_bars: int = 0
    bar_ts: str = ""        # decision bar the forecast was computed on (IC anchor)
    rationale: str = ""
    meta: dict = field(default_factory=dict)


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
            self.records = []
            self._pending = []

    def _save(self):
        try:
            import json
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump({"records": self.records[-2000:],
                           "pending": self._pending[-500:]}, f)
        except Exception:
            pass

    def log_forecast(self, score: float, ts, horizon: int, expected_price: float | None = None):
        """Record a pending forecast; resolved when its horizon bar arrives."""
        self._pending.append({"score": float(score), "ts": str(ts),
                              "horizon": int(horizon), "expected": expected_price})

    def resolve(self, closes: pd.Series):
        """Match pending forecasts to their realized forward returns."""
        if not self._pending or not len(closes):
            return
        try:
            idx = pd.to_datetime(closes.index)
        except Exception:
            return
        still: list[dict] = []
        for p in self._pending:
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
            tok = KronosTokenizer.from_pretrained(self.cfg.tokenizer_name)
            model = Kronos.from_pretrained(self.cfg.model_name)
            self._predictor = KronosPredictor(model, tok, max_context=self.cfg.max_context)
        except Exception:
            self._failed = True
            return None
        return self._predictor

    @property
    def available(self) -> bool:
        return self._ensure() is not None


class KronosSignalEngine:
    """The bot-facing interface: forecast -> probabilistic signal + IC ledger.

    This engine NEVER trades by itself. It emits a KronosSignal, logs it for
    the IC tracker, and exposes `promoted()` — whether the model has EARNED a
    vote in the orchestrator under the config hurdles."""

    def __init__(self, cfg: "KronosConfig | None" = None):
        self.cfg = cfg or KronosConfig()
        self.predictor = KronosPredictorLazy(self.cfg)
        self.tracker = KronosICTracker(self.cfg.track_file, half_life=self.cfg.ic_half_life)

    # ------------------------------------------------------------- forecast
    def evaluate(self, df: pd.DataFrame, horizon: int = 24,
                stop_distance: float | None = None) -> KronosSignal | None:
        p = self.predictor._ensure()
        if p is None or df is None or len(df) < 30:
            return None
        try:
            x = df.tail(self.cfg.max_context)
            cols = [c for c in ("open", "high", "low", "close", "volume") if c in x]
            x = x[cols]
            x_ts = pd.Series(x.index)
            freq = pd.infer_freq(df.index) or "1h"
            fut_idx = pd.date_range(x.index[-1], periods=horizon + 1,
                                    freq=freq, tz="UTC")[1:]
            y_ts = pd.Series(fut_idx)

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

            # P(touch +1R before -1R) across paths (for TP/SL bracket quality)
            p_tbs = None
            if stop_distance and stop_distance > 0:
                hits_up = 0
                for pr in preds:
                    path = pr["close"] if "close" in pr else None
                    if path is None:
                        continue
                    up, dn = spot + stop_distance, spot - stop_distance
                    hit_up = hit_dn = False
                    for v in path:
                        if v >= up:
                            hit_up = True
                            break
                        if v <= dn:
                            hit_dn = True
                            break
                    if hit_up and not hit_dn:
                        hits_up += 1
                p_tbs = hits_up / len(preds)

            direction = "FLAT"
            if p_up >= 0.60:
                direction = "LONG"
            elif p_up <= 0.40:
                direction = "SHORT"

            sig = KronosSignal(
                direction=direction, p_up=round(p_up, 3), p_target_before_stop=(
                    round(p_tbs, 3) if p_tbs is not None else None),
                dispersion_pct=round(disp, 3), expected_return_pct=round(exp_ret, 3),
                horizon_bars=horizon, bar_ts=str(df.index[-1]),
                rationale=(f"Kronos {self.cfg.model_name.split('/')[-1]}: {len(ends)} sampled "
                           f"{horizon}-bar paths, P(up)={p_up:.0%}, E[ret]={exp_ret:+.2f}%, "
                           f"dispersion {disp:.2f}%"),
                meta={"n_paths": len(ends), "model": self.cfg.model_name},
            )
            return sig
        except Exception:
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
    def log_and_maybe_resolve(self, df: pd.DataFrame, sig: KronosSignal):
        """Record the forecast for later IC scoring (anchored to ITS decision
        bar), and resolve pending forecasts whose horizon bars have since
        closed. FLAT forecasts carry no information and are not scored."""
        if sig is None:
            return
        self.tracker.resolve(df["close"])
        if sig.direction == "FLAT":
            return
        # signed conviction in [-1, 1]: strong up-read -> +, strong down-read -> -
        score = 2.0 * sig.p_up - 1.0
        self.tracker.log_forecast(score, sig.bar_ts or str(df.index[-1]), sig.horizon_bars)
