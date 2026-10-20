"""Bounded live recorder and opt-in local verdict-store acceptance check.

Raw market messages and sample verdicts stay in an isolated, ignored data
directory. Only hashes/counts and explicit sample provenance enter the report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import recorder, verdict_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300")
    if args.root.exists() or args.report.exists():
        parser.error("use new data and report paths to preserve earlier evidence")
    args.root.mkdir(parents=True)
    started = time.monotonic()
    rec = recorder.Recorder(root=str(args.root/"orderbook"))
    rec.run(["BTC/USDT", "ETH/USDT"], stop=lambda: time.monotonic()-started >= args.seconds)
    elapsed = time.monotonic()-started
    assert rec.counts["depth"] > 0 and rec.counts["trade"] > 0, "capture needs both data streams"
    files, counts, per_symbol = [], Counter(), {}
    for path in sorted((args.root/"orderbook").rglob("*.jsonl.gz")):
        raw = path.read_bytes()
        # A strict gzip reader must agree with the crash-tolerant reader.
        strict = [json.loads(line) for line in gzip.decompress(raw).splitlines()]
        rows = list(recorder.iter_records(str(path)))
        assert rows == strict
        symbol = path.parents[1].name
        symbol_counts = per_symbol.setdefault(symbol, Counter())
        for row in rows:
            counts[row["k"]] += 1
            symbol_counts[row["k"]] += 1
            assert isinstance(row["r"], int) and isinstance(row["e"], int)
            data = row["d"]
            if row["k"] == "depth":
                bids, asks = data["bids"], data["asks"]
                assert 0 < len(bids) <= 20 and 0 < len(asks) <= 20
                assert float(bids[0][0]) < float(asks[0][0])
                assert all(float(price)>0 and float(qty)>0 for price, qty in bids+asks)
                assert [float(p) for p, _ in bids] == sorted((float(p) for p, _ in bids), reverse=True)
                assert [float(p) for p, _ in asks] == sorted(float(p) for p, _ in asks)
            else:
                assert float(data["p"]) > 0 and float(data["q"]) > 0 and isinstance(data["m"], bool)
        files.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                      "bytes": len(raw), "records": len(rows)})
    assert dict(counts) == {k: rec.counts[k] for k in ("depth", "trade")}
    assert set(per_symbol) == {"BTCUSDT", "ETHUSDT"}
    assert all(c["depth"] and c["trade"] for c in per_symbol.values())

    source_root = Path(__file__).resolve().parents[1]/"docs/research/external_validation"
    store = args.root/"upstream_sample_verdicts.jsonl"
    report_files = [source_root/"backtest-result.report.json", source_root/"backtest-result_multistrat.report.json"]
    added = 0
    for path in report_files:
        added += verdict_store.record(json.loads(path.read_text()), path=str(store))
    records = verdict_store.load(str(store))
    assert added == len(records) == 3
    assert all(set(record) == set(verdict_store.FIELDS) for record in records)
    assert all(record["format"] == "freqtrade" for record in records)
    result = {"checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "passed": True,
              "capture_seconds_requested": args.seconds, "capture_seconds_actual": round(elapsed, 3),
              "counts": dict(counts), "per_symbol": {k: dict(v) for k, v in per_symbol.items()},
              "files": files, "strict_and_recovery_readers_agree": True,
              "verdict_store": {"source": "public_upstream_samples_not_customers", "records": added,
                                "fixed_field_privacy_check": True, "path": str(store),
                                "sha256": hashlib.sha256(store.read_bytes()).hexdigest(),
                                "summary": verdict_store.summarise(records)},
              "raw_data_published": False, "external_user_submissions": 0,
              "sustained_collection_started": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({"passed": True, "counts": dict(counts), "sample_verdicts": added,
                      "evidence": str(args.report)}, indent=2))


if __name__ == "__main__":
    main()
