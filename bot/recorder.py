"""Order-book recorder (roadmap V7): Binance publishes no historical depth.

Fills in this project are simulated from candles, which is why market making
cannot be measured honestly here (docs/ARCHITECTURE.md §4). The only way to
get order-book history is to record it. This module subscribes to Binance's
PUBLIC market streams (no API key) and stores, per symbol:

  * `<sym>@depth20@<speed>`: the top 20 bid and ask levels, a self-contained
    snapshot every second (or every 100 ms), so no diff-stream bookkeeping
    can go wrong;
  * `<sym>@aggTrade`: every aggregated trade, with the taker side.

STORAGE. gzip-compressed JSON lines, one file per symbol per UTC hour:
`<root>/<SYMBOL>/<YYYY-MM-DD>/<HH>.jsonl.gz`. Each line is
`{"r": receive ms, "e": event ms, "k": "depth"|"trade", "d": payload}`.
Lines are buffered and flushed every FLUSH_SECONDS as a new gzip member, so
a crash loses at most that buffer, and `iter_records` reads a file whose
last member was cut off. The data directory is gitignored (data/).

Before running it for long, read docs/COMPLIANCE.md Q8: whether Binance's
terms allow storing and using its market data this way is a question for
the owner and an adviser.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import time
import zlib
from collections.abc import Callable, Iterator

STREAM_URL = "wss://stream.binance.com:9443/stream?streams="
FLUSH_SECONDS = 5.0
SPEEDS = ("1000ms", "100ms")


def default_root() -> str:
    from config import db_dir
    return os.path.join(db_dir(), "orderbook")


def stream_names(symbols: list[str], speed: str = "1000ms") -> list[str]:
    """Binance stream names for `symbols` like 'BTC/USDT'."""
    if speed not in SPEEDS:
        raise ValueError(f"speed must be one of {SPEEDS}")
    out = []
    for s in symbols:
        sym = s.replace("/", "").lower()
        out += [f"{sym}@depth20@{speed}", f"{sym}@aggTrade"]
    return out


class Recorder:
    """Turns stream messages into hourly gzip files. Network-free: `run`
    feeds it from a websocket, tests feed `handle` directly."""

    def __init__(self, root: str | None = None, clock: Callable[[], float] = time.time):
        self.root = root or default_root()
        self.clock = clock
        self.buffers: dict[str, list[str]] = {}
        self.last_flush = clock()
        self.counts = {"depth": 0, "trade": 0, "skipped": 0}

    def path_for(self, symbol: str, ms: int) -> str:
        t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
        return os.path.join(self.root, symbol, t.strftime("%Y-%m-%d"), t.strftime("%H") + ".jsonl.gz")

    def handle(self, message: str) -> None:
        """One combined-stream message: {"stream": "...", "data": {...}}."""
        recv = int(self.clock() * 1000)
        try:
            msg = json.loads(message)
            stream, data = msg["stream"], msg["data"]
        except (ValueError, KeyError, TypeError):
            self.counts["skipped"] += 1
            return
        sym_raw, kind = stream.split("@", 1)
        if kind.startswith("depth"):
            kind, event = "depth", recv          # partial-book snapshots carry no event time
        elif kind == "aggTrade":
            kind, event = "trade", int(data.get("E") or data.get("T") or recv)
        else:
            self.counts["skipped"] += 1
            return
        symbol = sym_raw.upper()
        line = json.dumps({"r": recv, "e": event, "k": kind, "d": data},
                          separators=(",", ":"))
        self.buffers.setdefault(self.path_for(symbol, recv), []).append(line)
        self.counts[kind] += 1
        if self.clock() - self.last_flush >= FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        for path, lines in self.buffers.items():
            if not lines:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # appending a complete gzip member: earlier members stay intact
            with gzip.open(path, "ab") as fh:
                fh.write(("\n".join(lines) + "\n").encode())
        self.buffers = {}
        self.last_flush = self.clock()

    def run(self, symbols: list[str], speed: str = "1000ms", *,
            connect: Callable | None = None, stop: Callable[[], bool] = lambda: False,
            max_backoff: float = 60.0, quiet: bool = False) -> None:
        """Record until `stop()` is true, reconnecting with backoff."""
        if connect is None:
            from websockets.sync.client import connect
        url = STREAM_URL + "/".join(stream_names(symbols, speed))
        backoff = 1.0
        while not stop():
            try:
                with connect(url) as ws:
                    backoff = 1.0
                    if not quiet:
                        print(f"[recorder] connected: {len(symbols)} symbols, depth every {speed}")
                    while not stop():
                        self.handle(ws.recv())
            except KeyboardInterrupt:
                break
            except Exception as exc:
                if not quiet:
                    print(f"[recorder] {type(exc).__name__}: {exc}; reconnecting in {backoff:.0f}s")
                self.flush()
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
        self.flush()


def iter_records(path: str) -> Iterator[dict]:
    """Every complete line of one file, tolerating a member cut off by a
    crash (the lines before it are returned, the torn tail is dropped)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    out = b""
    while raw:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += d.decompress(raw)
        except zlib.error:
            break
        if not d.eof:            # truncated member: keep what decoded fully
            break
        raw = d.unused_data
    lines = out.split(b"\n")
    if out and not out.endswith(b"\n"):
        lines = lines[:-1]       # a partly decoded last line is not a record
    for line in lines:
        if line.strip():
            try:
                yield json.loads(line)
            except ValueError:
                continue


def summary(root: str | None = None) -> dict:
    """Files, bytes and hours recorded per symbol."""
    root = root or default_root()
    out: dict[str, dict] = {}
    if not os.path.isdir(root):
        return out
    for symbol in sorted(os.listdir(root)):
        files = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(root, symbol))
                 for f in fs if f.endswith(".jsonl.gz")]
        if files:
            days = sorted({os.path.basename(os.path.dirname(f)) for f in files})
            out[symbol] = {"hours": len(files), "bytes": sum(os.path.getsize(f) for f in files),
                           "first_day": days[0], "last_day": days[-1]}
    return out
