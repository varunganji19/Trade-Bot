"""Validate a strategy (roadmap V3): the dashboard face of `validate-trades`.

The page reads the user's file in the browser and posts it here base64
encoded inside a JSON body (every mutating route takes JSON: the content type
forces the CORS preflight that defeats form-encoded CSRF, and no multipart
dependency is needed). The file is written to a temporary directory, judged
by bot/validator.py exactly as the CLI would judge it, and deleted. Optional
verdict summaries and usage counts require separate opt-ins; the trade file
is never retained and no journal is touched.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import tempfile

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictBool

router = APIRouter()

MAX_BYTES = 20 * 1024 * 1024
# markets offered for the regime labels; the page shows the same list
REGIME_MARKETS = ("BTC/USDT", "ETH/USDT")
_SUFFIX = re.compile(r"\.(json|zip|csv)$", re.IGNORECASE)


class ValidateIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    # base64 of at most MAX_BYTES (4/3 overhead, rounded up)
    content_b64: str = Field(min_length=1, max_length=(MAX_BYTES * 4) // 3 + 8)
    trials: int | None = Field(default=None, ge=1, le=1_000_000)
    regime_market: str | None = Field(default=None, max_length=24)
    # opt-in: add an anonymised summary to the local verdict store
    record: bool = False
    usage_opt_in: StrictBool = False
    usage_client_id: str | None = Field(default=None, max_length=36)


class UsageClient(BaseModel):
    client_id: str = Field(min_length=36, max_length=36)


@router.post("/api/validate")
def api_validate(body: ValidateIn):
    from bot import validator as v
    from bot import usage
    client_id = None
    if body.usage_opt_in:
        try:
            client_id = usage.validate_client(body.usage_client_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    m = _SUFFIX.search(body.filename)
    if not m:
        raise HTTPException(422, "the file must be .json, .zip or .csv")
    if body.regime_market and body.regime_market not in REGIME_MARKETS:
        raise HTTPException(422, f"regime market must be one of {', '.join(REGIME_MARKETS)}")
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "the file could not be decoded")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    with tempfile.TemporaryDirectory(prefix="algo-validate-") as tmp:
        # the upload's own name is never used as a path: only its extension
        path = os.path.join(tmp, "upload." + m.group(1).lower())
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            daily = None
            if body.regime_market:
                _, by_strategy = v.load_trades(path)
                daily = v.regime_daily(by_strategy, body.regime_market)
            report = v.validate(path, trials=body.trials, daily=daily)
        except (v.ValidatorError, ValueError, KeyError, json.JSONDecodeError,
                UnicodeDecodeError) as exc:
            raise HTTPException(422, f"cannot validate this file: {exc}")
        except Exception as exc:          # data fetch for the regimes, mostly
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")
    report["input"] = os.path.basename(body.filename)
    # timestamps and numpy scalars become plain JSON once, here
    report = json.loads(json.dumps(report, default=str))
    recorded = 0
    if body.record:
        from bot import verdict_store
        recorded = verdict_store.record(report)
    usage_recorded, usage_warning = False, None
    if client_id:
        try:
            usage_recorded = usage.record(client_id)
            if not usage_recorded:
                usage_warning = "The report is ready; usage was not counted because this ID was deleted."
        except (OSError, sqlite3.Error, ValueError):
            # Optional measurement must never withhold a completed verdict.
            usage_warning = "The report is ready, but local usage counters could not be saved."
    return {"report": report, "markdown": v.render_markdown(report), "recorded": recorded,
            "usage_recorded": usage_recorded, "usage_warning": usage_warning}


@router.get("/api/usage")
def api_usage():
    from bot import usage
    return usage.stats()


@router.post("/api/usage/forget")
def forget_usage(body: UsageClient):
    from bot import usage
    try:
        usage.forget(body.client_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True}
