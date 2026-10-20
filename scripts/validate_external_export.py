"""Recheck pinned upstream Freqtrade exports through import, CLI and upload.

Download the two inputs from the URLs in docs/research/external_validation/README.md.
No strategy code, credentials, network calls or real journal are needed here.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import validator as v
from bot.api.validate import router
from bot.cli.research import cmd_validate_trades

REVISION = "7ff17f5f5ac2f7bbc55a2937fd493a68c9951647"
INPUTS = {
    "backtest-result.json": "2d067ed77338c9c44fe46a70c7976e20aa09cd033be0504dab7ee67e936ef34d",
    "backtest-result_multistrat.json": "ae17bf91805d93763860291c27da72596779fdf1b5348440ee04d62481fad937",
}


def check(input_dir: Path) -> tuple[dict, dict]:
    app = FastAPI()
    app.include_router(router)
    results, reports = [], {}
    with TestClient(app) as client, tempfile.TemporaryDirectory(prefix="algo-ft-check-") as tmp:
        for filename, expected_hash in INPUTS.items():
            path = input_dir / filename
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected_hash:
                raise ValueError(f"{filename}: input hash differs from the pinned upstream export")
            original = json.loads(raw)
            started = time.perf_counter()
            report = v.validate(path)
            seconds = time.perf_counter() - started
            assert seconds < 60, f"{filename}: exceeded the roadmap's one-minute acceptance limit"
            source, parsed = v.load_trades(path)
            assert source == "freqtrade"
            checks = []
            for name, strategy in original["strategy"].items():
                trades = [t for t in strategy["trades"] if not t.get("is_open", False)]
                net = math.fsum(t["profit_abs"] for t in trades)
                fees = math.fsum(t["amount"] * (t["open_rate"] * t["fee_open"]
                                                + t["close_rate"] * t["fee_close"])
                                 for t in trades)
                result = report["strategies"][name]
                assert result["trades"] == len(trades) == len(parsed[name])
                assert math.isclose(result["net_pnl"], net, rel_tol=1e-12, abs_tol=1e-16)
                assert math.isclose(net, strategy["profit_total_abs"], rel_tol=1e-12)
                assert result["currency"] == strategy["stake_currency"]
                assert math.isclose(result["costs"]["fees_total"], fees, rel_tol=1e-12)
                for row in result["costs"]["rows"]:
                    assert math.isclose(row["net"], net + (1-row["fee_mult"])*fees,
                                        rel_tol=1e-12, abs_tol=1e-16)
                assert result["verdict"] != v.ROBUST  # no regime prices or trial count supplied
                checks.append({"strategy": name, "trades": len(trades), "net_pnl": net,
                               "fees": fees, "currency": result["currency"],
                               "verdict": result["verdict"]})

            archive = Path(tmp) / (path.stem + ".zip")
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("result.meta.json", '{"strategy":"metadata, not results"}')
                z.writestr("result_config.json", '{"strategy":"config, not results"}')
                z.writestr(filename, raw)
            assert v.validate(archive)["strategies"] == report["strategies"]
            for upload in (path, archive):
                response = client.post("/api/validate", json={
                    "filename": upload.name,
                    "content_b64": base64.b64encode(upload.read_bytes()).decode(),
                })
                assert response.status_code == 200, response.text
                assert response.json()["report"]["strategies"] == report["strategies"]
                assert " BTC" in response.json()["markdown"]
                assert response.json()["recorded"] == 0
            json_out, md_out = Path(tmp)/"report.json", Path(tmp)/"report.md"
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_validate_trades(SimpleNamespace(file=str(path), trials=None, strategy=None,
                                                   prices=None, regime_market=None,
                                                   report=str(md_out), json=str(json_out), record=False))
            assert json.loads(json_out.read_text())["strategies"] == report["strategies"]
            assert md_out.read_text() == v.render_markdown(report)
            results.append({"input": filename, "sha256": digest, "seconds": round(seconds, 4),
                            "checks": checks, "json_zip_cli_http_parity": True})
            reports[filename] = report
    return {"upstream_revision": REVISION, "inputs": results, "passed": True}, reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new directory so earlier evidence stays intact")
    checks, reports = check(args.input_dir)
    args.output.mkdir(parents=True)
    (args.output/"checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    for filename, report in reports.items():
        (args.output/(Path(filename).stem+".report.json")).write_text(
            json.dumps(report, indent=2) + "\n")
        (args.output/(Path(filename).stem+".report.md")).write_text(v.render_markdown(report))
    print(f"External Freqtrade validation passed; evidence: {args.output}")


if __name__ == "__main__":
    main()
