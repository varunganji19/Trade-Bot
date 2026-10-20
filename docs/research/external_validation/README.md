# External Freqtrade acceptance check

Completed on 2026-10-20 using two native exports from the upstream Freqtrade
repository, pinned to revision `7ff17f5f5ac2f7bbc55a2937fd493a68c9951647`:

- [Single strategy export](https://github.com/freqtrade/freqtrade/blob/7ff17f5f5ac2f7bbc55a2937fd493a68c9951647/tests/testdata/backtest_results/backtest-result.json)
- [Multi-strategy export](https://github.com/freqtrade/freqtrade/blob/7ff17f5f5ac2f7bbc55a2937fd493a68c9951647/tests/testdata/backtest_results/backtest-result_multistrat.json)

These are genuine upstream backtest artifacts used as compatibility samples,
not customer traction or evidence of strategy profitability. No Freqtrade code
was copied or executed. Input SHA-256 hashes, independent reconciliation and
measured runtime are in [checks.json](checks.json). The generated reports sit
beside it. Each of three strategy entries contains 179 closed trades.

The validator now passes JSON and ZIP ingestion, the CLI, and actual HTTP
uploads through an isolated FastAPI application. Trade counts, net P&L and
fee totals are independently reconstructed from raw export fields and agree
with the source totals. Each verdict takes less than one minute. Missing
regime prices and unknown trial counts remain explicit; none is called robust.

This check found and fixed two compatibility gaps: monetary outputs rounded
BTC amounts to cents (and the dashboard labelled them as dollars), and a ZIP
metadata member with a string `strategy` field could hide the actual result.
JSON retains full monetary precision, reports use the source stake currency,
and unknown units are labelled `input units`. Offline regressions in
`tests/test_validator.py` retain those cases for future changes.

To repeat, download both linked raw files into an input directory, then run:

```sh
ALGO_SKIP_DOTENV=1 python3 scripts/validate_external_export.py \
  --input-dir /tmp/algo-external-validation --output /tmp/algo-ft-recheck
```

The output directory must be new. The runner refuses changed input hashes,
compares all monetary totals without cent rounding, tests JSON/ZIP/CLI/HTTP
parity, and never accesses the paper journal or records verdicts.
