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
    btc = sorted(f for f in os.listdir(tmp_path / "BTCUSDT" / "2026-01-01")
                 if f.endswith(".jsonl.gz"))
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


def _member(*rows):
    return gzip.compress(b"".join((json.dumps(row) + "\n").encode() for row in rows))


@pytest.mark.parametrize("chunk_size", [23, 64 * 1024])
@pytest.mark.parametrize("damage", ["torn", "crc", "junk"])
def test_reader_recovers_records_before_damage_and_from_later_members(tmp_path, monkeypatch,
                                                                     chunk_size, damage):
    monkeypatch.setattr(rec, "_CHUNK_BYTES", chunk_size)
    first, middle, last = ({"n": 1}, {"n": 2}, {"n": 3})
    member = _member(middle)
    if damage == "torn":
        member = member[:-8]  # payload complete, trailer lost
    elif damage == "crc":
        member = member[:-8] + bytes([member[-8] ^ 1]) + member[-7:]
    else:
        member += b"not a gzip member"
    path = tmp_path / "damaged.jsonl.gz"
    path.write_bytes(_member(first) + member + _member(last))
    assert list(rec.iter_records(str(path))) == [first, middle, last]


def test_reader_discards_incomplete_line_without_hiding_next_member(tmp_path):
    path = tmp_path / "partial.jsonl.gz"
    path.write_bytes(gzip.compress(b'{"n":1}\n{"n":')[:-8] + _member({"n": 2}))
    assert list(rec.iter_records(str(path))) == [{"n": 1}, {"n": 2}]


def test_reader_does_not_read_the_entire_hour_at_once(tmp_path, monkeypatch):
    import builtins
    path = tmp_path / "many.jsonl.gz"
    path.write_bytes(_member(*({"n": i} for i in range(2000))))
    original = builtins.open
    class BoundedReader:
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.fh.close()
        def read(self, size):
            assert 0 < size <= rec._CHUNK_BYTES
            return self.fh.read(size)
        def tell(self):
            return self.fh.tell()
        def seek(self, offset):
            return self.fh.seek(offset)
    monkeypatch.setattr(rec, "open", lambda *args, **kw: BoundedReader(original(*args, **kw)),
                        raising=False)
    assert [r["n"] for r in rec.iter_records(str(path))] == list(range(2000))


def test_resumed_flush_quarantines_damage_and_preserves_later_members(tmp_path):
    recorder = rec.Recorder(root=str(tmp_path), clock=Clock())
    path = tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz"
    path.parent.mkdir(parents=True)
    original = _member({"n": 1}) + _member({"n": 2})[:-8] + _member({"n": 3})
    path.write_bytes(original)
    recorder.handle(_depth())
    recorder.flush()
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    assert rows[:3] == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert rows[3]["k"] == "depth"
    quarantined = list(path.parent.glob("00.jsonl.gz.damaged.*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == original
    assert not recorder.buffers


def test_failed_atomic_repair_preserves_file_and_buffer_until_retry(tmp_path, monkeypatch):
    recorder = rec.Recorder(root=str(tmp_path), clock=Clock())
    path = tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz"
    path.parent.mkdir(parents=True)
    original = _member({"n": 1})[:-8]
    path.write_bytes(original)
    recorder.handle(_depth())
    replace = rec.os.replace
    def fail_replace(*args):
        raise OSError("repair failed")
    monkeypatch.setattr(rec.os, "replace", fail_replace)
    with pytest.raises(OSError, match="repair failed"):
        recorder.flush()
    assert path.read_bytes() == original and len(recorder.buffers[str(path)]) == 1
    monkeypatch.setattr(rec.os, "replace", replace)
    recorder.flush()
    assert [r.get("n", r.get("k")) for r in rec.iter_records(str(path))] == [1, "depth"]


def test_failed_batch_keeps_only_unwritten_buffers_and_retry_does_not_duplicate(tmp_path,
                                                                             monkeypatch):
    recorder = rec.Recorder(root=str(tmp_path), clock=Clock())
    recorder.handle(_depth())
    recorder.handle(_depth("ethusdt"))
    write = rec.gzip.GzipFile.write
    def fail_second(self, data):
        result = write(self, data)
        if "ETHUSDT" in str(self.fileobj.name):
            raise OSError("write failed")
        return result
    monkeypatch.setattr(rec.gzip.GzipFile, "write", fail_second)
    with pytest.raises(OSError, match="write failed"):
        recorder.flush()
    assert len(recorder.buffers) == 1
    monkeypatch.setattr(rec.gzip.GzipFile, "write", write)
    recorder.flush()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        path = tmp_path / symbol / "2026-01-01" / "00.jsonl.gz"
        assert len(list(rec.iter_records(str(path)))) == 1


def test_two_recorders_serialize_repair_and_append(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    recorders = [rec.Recorder(root=str(tmp_path), clock=Clock()) for _ in range(2)]
    path = tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(_member({"n": 0})[:-8])
    for recorder in recorders:
        recorder.handle(_depth())
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda r: r.flush(), recorders))
    assert len(list(rec.iter_records(str(path)))) == 3
    assert len(list(path.parent.glob("00.jsonl.gz.damaged.*"))) == 1


def test_append_fsync_failure_rolls_back_and_retains_the_batch(tmp_path, monkeypatch):
    recorder = rec.Recorder(root=str(tmp_path), clock=Clock())
    recorder.handle(_depth())
    recorder.flush()
    path = tmp_path / "BTCUSDT" / "2026-01-01" / "00.jsonl.gz"
    original = path.read_bytes()
    recorder.handle(_trade())
    sync = rec.os.fsync
    def fail_sync(*args):
        raise OSError("fsync failed")
    monkeypatch.setattr(rec.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="fsync failed"):
        recorder.flush()
    assert path.read_bytes() == original and len(recorder.buffers[str(path)]) == 1
    monkeypatch.setattr(rec.os, "fsync", sync)
    recorder.flush()
    assert [r["k"] for r in rec.iter_records(str(path))] == ["depth", "trade"]


@pytest.mark.parametrize("chunk_size", [7, 23, 64 * 1024])
def test_later_members_survive_every_truncation_point(tmp_path, monkeypatch, chunk_size):
    monkeypatch.setattr(rec, "_CHUNK_BYTES", chunk_size)
    member = _member(*({"n": i} for i in range(30)))
    later = {"later": True}
    path = tmp_path / "cut.jsonl.gz"
    for cut in range(1, len(member)):
        path.write_bytes(member[:cut] + _member(later))
        rows = list(rec.iter_records(str(path)))
        assert rows[-1:] == [later], f"later member lost after byte {cut}"
        recovered = [row["n"] for row in rows if "n" in row]
        assert recovered == list(range(len(recovered))), f"fabricated records after byte {cut}"
