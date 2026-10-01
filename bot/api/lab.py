"""Strategy Lab API: pick any crypto/forex pair, apply the strategies
registered for it and backtest — in either book. Pure backtest: its own
broker and risk per run, no journal writes, no engine interference."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class LabIn(BaseModel):
    book: str = Field(default="standard", max_length=16)
    kind: str = Field(default="crypto", max_length=16)
    symbol: str = Field(min_length=1, max_length=24)
    timeframe: str = Field(default="1h", max_length=8)
    strategy: str = Field(default="ensemble", max_length=32)
    days: int = Field(default=0, ge=0, le=3650)
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    fee_tier: str | None = Field(default=None, max_length=8)


@router.post("/api/lab/run")
def api_lab_run(body: LabIn):
    from bot import lab
    try:
        return lab.start_lab_run(body.model_dump())
    except lab.LabError as exc:
        raise HTTPException(422, str(exc))


@router.get("/api/lab/status")
def api_lab_status():
    from bot import lab
    return lab.lab_status()


@router.get("/api/lab/meta")
def api_lab_meta():
    """One source of truth for the Lab form: suggestions, the timeframes
    offered per book+kind, and the strategies REGISTERED per book+timeframe
    (derived from the registry, never hand-maintained)."""
    from bot import lab
    books = ("standard", "hft")
    kinds = ("crypto", "forex")
    tfs_all = sorted(set(lab._STANDARD_TFS) | set(lab._HFT_TFS))
    return {
        "suggestions": lab.SUGGESTIONS,
        "timeframes": {b: {k: lab.timeframes_for(b, k) for k in kinds} for b in books},
        "strategies": {b: {tf: lab.strategies_for(b, tf) for tf in tfs_all} for b in books},
        "days_default": {b: {k: {tf: lab.default_days(k, tf) for tf in tfs_all}
                             for k in kinds} for b in books},
        "days_cap": {b: {k: {tf: lab.days_cap(k, tf) for tf in tfs_all}
                         for k in kinds} for b in books},
    }
