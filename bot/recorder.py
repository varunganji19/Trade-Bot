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
import fcntl
import gzip
import json
import os
import shutil
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterator

STREAM_URL = "wss://stream.binance.com:9443/stream?streams="
FLUSH_SECONDS = 5.0
SPEEDS = ("1000ms", "100ms")
_CHUNK_BYTES = 64 * 1024
_GZIP_HEADER = b"\x1f\x8b\x08"


def _next_member(fh, after: int) -> bytes:
    """Rescan raw bytes after a failed member, using bounded reads."""
    fh.seek(after)
    data = fh.read(_CHUNK_BYTES)
    while data:
        start = data.find(_GZIP_HEADER)
        if start >= 0:
            return data[start:]
        more = fh.read(_CHUNK_BYTES)
        if not more:
            break
        data = data[-2:] + more
    return b""


def _decode_member(fh, start: int, target, end: int | None = None) -> tuple[bool, int]:
    """Decode one member to a bounded spool, preserving output before errors."""
    fh.seek(start)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = b""
    while True:
        if not data:
            remaining = _CHUNK_BYTES if end is None else min(_CHUNK_BYTES, end - fh.tell())
            if remaining <= 0:
                return False, fh.tell()
            data = fh.read(remaining)
            if not data:
                return False, fh.tell()
        before = decoder.copy()
        try:
            decoded = decoder.decompress(data, _CHUNK_BYTES)
        except zlib.error:
            # A corrupt trailer can reject a whole input chunk after decoding
            # its payload. Retry that chunk incrementally to keep complete lines.
            for i in range(len(data)):
                try:
                    target.write(before.decompress(data[i:i + 1]))
                except zlib.error:
                    break
            return False, fh.tell()
        target.write(decoded)
        if decoder.eof:
            return True, fh.tell() - len(decoder.unused_data)
        data = decoder.unconsumed_tail


def _member_records(fh, state: dict) -> Iterator[dict]:
    """Stream newline-terminated JSON through a bounded per-member spool.

    Delay yielding a member until its boundary is known. A torn deflate
    stream can consume the following gzip header and manufacture parseable
    garbage; re-decode only the bytes before that header during recovery.
    """
    cursor = 0
    while True:
        data = _next_member(fh, cursor)
        if not data:
            if fh.tell() > cursor:
                state["damaged"] = True
            return
        member_start = fh.tell() - len(data)
        if member_start != cursor:
            state["damaged"] = True
        with tempfile.SpooledTemporaryFile(max_size=_CHUNK_BYTES, mode="w+b") as decoded:
            valid, following = _decode_member(fh, member_start, decoded)
            if not valid:
                state["damaged"] = True
                data = _next_member(fh, member_start + len(_GZIP_HEADER))
                following = fh.tell() - len(data) if data else fh.tell()
                decoded.seek(0)
                decoded.truncate()
                _decode_member(fh, member_start, decoded, end=following)
            decoded.seek(0)
            for line in decoded:
                if not line.endswith(b"\n"):
                    state["damaged"] = True
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("record must be an object")
                except (ValueError, UnicodeError):
                    state["damaged"] = True
                    continue
                yield row
        cursor = following


def _repair_file(path: str) -> None:
    """Validate before append; retain damaged originals and atomically repair."""
    if not os.path.exists(path):
        return
    state = {"damaged": False}
    with open(path, "rb") as source:
        for _ in _member_records(source, state):
            pass
    if not state["damaged"]:
        return
    directory, name = os.path.split(path)
    fd, quarantine = tempfile.mkstemp(prefix=name + ".damaged.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as target, open(path, "rb") as source:
            shutil.copyfileobj(source, target, _CHUNK_BYTES)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        os.unlink(quarantine)
        raise
    fd, repaired = tempfile.mkstemp(prefix=name + ".repair.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as target:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as compressed:
                with open(path, "rb") as source:
                    for row in _member_records(source, {"damaged": False}):
                        compressed.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(repaired, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(repaired):
            os.unlink(repaired)


def _file_stamp(path: str):
    try:
        stat = os.stat(path)
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


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
        self._validated: dict[str, tuple] = {}
        self._lock = threading.RLock()

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
        with self._lock:
            self.buffers.setdefault(self.path_for(symbol, recv), []).append(line)
            self.counts[kind] += 1
            if self.clock() - self.last_flush >= FLUSH_SECONDS:
                self.flush()

    def flush(self) -> None:
        with self._lock:
            for path, lines in list(self.buffers.items()):
                if not lines:
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                # Stable lock inode survives an atomic repair/replacement.
                with open(path + ".lock", "a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    if self._validated.get(path) != _file_stamp(path):
                        _repair_file(path)
                    with open(path, "ab") as target:
                        original_size = target.tell()
                        try:
                            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as compressed:
                                compressed.write(("\n".join(lines) + "\n").encode())
                            target.flush()
                            os.fsync(target.fileno())
                        except Exception:
                            self._validated.pop(path, None)
                            # A retry must not duplicate a partially written batch.
                            target.truncate(original_size)
                            target.flush()
                            raise
                    self._validated[path] = _file_stamp(path)
                del self.buffers[path]
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
    """Complete records, including recoverable lines and later intact members."""
    with open(path, "rb") as fh:
        yield from _member_records(fh, {"damaged": False})


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
