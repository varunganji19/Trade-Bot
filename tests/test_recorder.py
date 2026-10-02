"""Order-book recorder (bot/recorder.py), fed by a fake websocket."""
from __future__ import annotations

import gzip
import json
import os

import pytest

from bot import recorder as rec

T0 = 1_767_225_600.0          # 2026-01-01 00:00:00 UTC


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _depth(sym="btcusdt"):
    return json.dumps({"stream": f"{sym}@depth20@1000ms",
                       "data": {"lastUpdateId": 7, "bids": [["100.0", "1.5"]],
                                "asks": [["100.1", "2.0"]]}})


def _trade(sym="btcusdt", e=1_767_225_600_500):
    return json.dumps({"stream": f"{sym}@aggTrade",
                       "data": {"e": "aggTrade", "E": e, "s": sym.upper(), "p": "100.05",
                                "q": "0.3", "m": True}})


def test_stream_names_cover_depth_and_trades():
    assert rec.stream_names(["BTC/USDT"]) == ["btcusdt@depth20@1000ms", "btcusdt@aggTrade"]
    with pytest.raises(ValueError):
        rec.stream_names(["BTC/USDT"], speed="10ms")


def test_messages_land_in_hourly_gzip_files_per_symbol(tmp_path):
    clock = Clock()
    r = rec.Recorder(root=str(tmp_path), clock=clock)
    r.handle(_depth())
    r.handle(_trade())
    r.handle(_depth("ethusdt"))
    clock.t += 3600                                   # the next UTC hour
    r.handle(_depth())
    r.handle("not json")
    r.flush()
    btc = sorted(os.listdir(tmp_path / "BTCUSDT" / "2026-01-01"))
    assert btc == ["00.jsonl.gz", "01.jsonl.gz"]
    rows = list(rec.iter_records(str(tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz")))
    assert [x["k"] for x in rows] == ["depth", "trade"]
    assert rows[0]["d"]["bids"] == [["100.0", "1.5"]] and rows[1]["e"] == 1_767_225_600_500
    assert r.counts == {"depth": 3, "trade": 1, "skipped": 1}
    s = rec.summary(str(tmp_path))
    assert s["BTCUSDT"]["hours"] == 2 and s["ETHUSDT"]["hours"] == 1


def test_flushes_append_members_and_a_torn_tail_is_dropped(tmp_path):
    clock = Clock()
    r = rec.Recorder(root=str(tmp_path), clock=clock)
    r.handle(_depth())
    r.flush()
    r.handle(_trade())
    r.flush()
    path = str(tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz")
    assert len(list(rec.iter_records(path))) == 2      # two gzip members, both read
    good = open(path, "rb").read()
    torn = good + gzip.compress(b'{"r":1,"k":"depth","d":{}}\n')[:-12]   # crash mid-write
    with open(path, "wb") as fh:
        fh.write(torn)
    assert [x["k"] for x in rec.iter_records(path)] == ["depth", "trade"]


def test_it_flushes_on_its_own_every_few_seconds(tmp_path):
    clock = Clock()
    r = rec.Recorder(root=str(tmp_path), clock=clock)
    r.handle(_depth())
    assert not os.path.exists(tmp_path / "BTCUSDT")
    clock.t += rec.FLUSH_SECONDS
    r.handle(_depth())
    assert os.path.exists(tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz")


def test_run_reconnects_after_a_dropped_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    sessions = [[_depth(), ConnectionError("dropped")], [_trade(), _depth()]]
    urls, received = [], []

    class FakeWS:
        def __init__(self, msgs):
            self.msgs = list(msgs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def recv(self):
            item = self.msgs.pop(0)
            if isinstance(item, Exception):
                raise item
            received.append(item)
            return item

    def connect(url):
        urls.append(url)
        return FakeWS(sessions.pop(0))

    r = rec.Recorder(root=str(tmp_path), clock=Clock())
    r.run(["BTC/USDT"], connect=connect, stop=lambda: len(received) >= 3, quiet=True)
    assert len(urls) == 2 and urls[0].endswith("btcusdt@depth20@1000ms/btcusdt@aggTrade")
    assert r.counts["depth"] == 2 and r.counts["trade"] == 1
    assert len(list(rec.iter_records(
        str(tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz")))) == 3
