# Recorder and verdict-store operation check — 2026-10-20

**Bounded live capture and local storage verified. Sustained collection and
external-user collection remain incomplete.**

The live check requested 30 seconds of BTC/USDT and ETH/USDT market data;
connection setup/teardown made the operation 39.329 seconds in total. It
captured **60 depth snapshots and 306 aggregated trades**. Both symbols
received both streams. No key, account endpoint or trading operation was used.

[checks.json](checks.json) contains file hashes and per-symbol counts.
Every compressed record was read back through both a strict gzip reader
and the recovery reader, with identical results. Depth levels, spreads,
positive prices/quantities and trade-side flags were checked. Raw messages
remain locally under `data/operation_checks/2026-10-20/`; only counts and
hashes are committed. The ignore rules now cover this location and the
normal `data/orderbook/` root, correcting the earlier unfulfilled claim
that recorder output was ignored.

Operational checks also addressed a silent-stream failure: blocking
receives could prevent a recording deadline from taking effect. Receives
now poll once per second, idle buffers still flush, reconnect backoff checks
the stop request every quarter-second, and malformed message metadata is
skipped without discarding the session. Seven new regressions exercise
these cases. All 32 recorder/verdict tests and the complete 698-test suite,
Python/JavaScript lint and lifecycle parity pass.

The opt-in verdict-store path was exercised with the three **public upstream
sample strategy entries** from the external Freqtrade acceptance check.
They reside in an isolated `upstream_sample_verdicts.jsonl`, have only the
fixed coarse fields, and contain no source filenames, strategy identities,
markets, individual trade dates or P&L. They are not customer submissions,
user traction, or entries in the normal user-verdict database.

To repeat with new output paths:

```sh
ALGO_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_data_operations.py \
  --seconds 30 --root data/operation_checks/new-check \
  --report /tmp/algo-data-operations.json
```

The check fails if either stream is missing, readers disagree, or the
sample verdict fields differ from the privacy allowlist. Existing data and
report paths are refused so evidence is preserved.

The [Binance stream reference](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)
documents these public streams; it does not establish this project's storage
or redistribution rights. [COMPLIANCE Q8 and Q10](../../COMPLIANCE.md)
still require review for sustained/commercial recording and shared user
verdicts. No indefinite collector, public dataset, user recruitment, or
external-verdict sharing was enabled by this check.
