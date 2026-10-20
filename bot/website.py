"""Public-facing introduction and local waitlist, with no trading API imports.

Run separately from the operator dashboard: python3 -m uvicorn bot.website:app.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from pathlib import Path
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bot import waitlist

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "static" / "website"


class Signup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    consent: StrictBool


class Removal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class RateLimit:
    """Short-lived per-process buckets; addresses are never written to disk."""
    def __init__(self):
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self.lock:
            bucket = self.buckets.pop(address, deque())
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            self.buckets[address] = bucket
            if len(self.buckets) > 2048:
                self.buckets.popitem(last=False)
            if len(bucket) >= 10:
                return False
            bucket.append(now)
            return True


async def read_json(request: Request, model):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "send application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise HTTPException(413, "request is too large")
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "check the email, consent, or removal link")


def create_app() -> FastAPI:
    app = FastAPI(title="Algo — validate before you trust", docs_url=None, redoc_url=None,
                  openapi_url=None)
    hosts = [h.strip() for h in os.environ.get("ALGO_WEBSITE_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    limit = RateLimit()

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def landing():
        return FileResponse(ASSETS/"index.html")

    @app.get("/privacy")
    def privacy():
        return FileResponse(ASSETS/"privacy.html")

    @app.get("/remove")
    def removal():
        return FileResponse(ASSETS/"remove.html")

    @app.get("/site.css")
    def css():
        return FileResponse(ASSETS/"site.css", media_type="text/css")

    @app.get("/site.js")
    def js():
        return FileResponse(ASSETS/"site.js", media_type="text/javascript")

    @app.get("/sample")
    def sample():
        return FileResponse(ROOT/"docs/research/external_validation/backtest-result.report.md",
                            media_type="text/plain", filename="algo-sample-report.md")

    @app.post("/api/waitlist", status_code=201)
    async def signup(request: Request):
        if not limit.allow(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "please wait a minute before trying again", headers={"Retry-After": "60"})
        body = await read_json(request, Signup)
        try:
            token = waitlist.subscribe(body.email, body.consent)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"removal_token": token, "expires_in_days": 30}

    @app.post("/api/waitlist/remove")
    async def delete_signup(request: Request):
        body = await read_json(request, Removal)
        waitlist.remove(body.token)
        return {"ok": True}

    return app


app = create_app()
