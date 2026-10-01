"""Request bodies for the dashboard API. Every mutating POST takes a JSON
body (even an empty one): the JSON content type forces the CORS preflight
that defeats form-encoded CSRF."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class EngineIn(BaseModel):
    # ge=5: interval=0 was a hot loop hammering the exchanges; negative killed
    # the loop thread silently (sleep() raised outside the try)
    interval: int = Field(default=60, ge=5, le=3600)


class HftEngineIn(BaseModel):
    """The HFT book's interval floor is 1s (paper trading: the whole point is
    minimal bar-close -> decision -> fill latency). The standard engine keeps
    its ge=5 floor."""
    interval: int = Field(default=2, ge=1, le=3600)


class WatchlistIn(BaseModel):
    kind: str
    symbol: str = Field(min_length=1, max_length=24)
    timeframe: str
    display: str | None = Field(default=None, max_length=64)


class AmountIn(BaseModel):
    # the le bound also rejects inf (gt=0 alone passed it) and values that
    # would blow up derived stats (return_pct -> inf)
    amount: float = Field(gt=0, le=1_000_000_000)


class ResetIn(BaseModel):
    capital: float = Field(gt=0, le=1_000_000_000)


class PositionCloseIn(BaseModel):
    symbol: str
    timeframe: str


class PauseIn(BaseModel):
    """Manual pause/resume body: optional human note. The body-less variant
    is a plain {} like every other mutating POST (the JSON content type forces
    the CORS preflight that defeats form-encoded CSRF)."""
    note: str = Field(default="", max_length=200)


class EmptyIn(BaseModel):
    """Body-required marker for POSTs that take no fields: a JSON body forces
    the CORS preflight that defeats form-encoded CSRF (same rule every other
    mutating endpoint already follows)."""
