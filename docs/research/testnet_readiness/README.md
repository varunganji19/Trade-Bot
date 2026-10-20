# Testnet readiness check — 2026-10-20

**Readiness checked; authenticated operation remains blocked.** Neither
`BINANCE_TESTNET_API_KEY` nor `BINANCE_TESTNET_API_SECRET` is configured in
the process or repository `.env`. Only presence was checked; no credentials
were printed or committed. Account creation and a four-week run cannot be
replaced by local tests.

Public HTTPS `/api/v3/ping` and `/api/v3/time` returned valid HTTP 200
responses from `testnet.binance.vision`; timestamps and response hashes are
in [public_checks.json](public_checks.json). No authenticated account was
contacted and no order was submitted.

The check reproduced a host-guard defect: a lookalike hostname containing
`testnet.binance.vision` passed the substring check. The guard now parses
the URL and requires the exact hostname, HTTPS, port 443 (or its default),
and no URL credentials. Seven bypass/insecure cases are rejected, and an
explicit HTTPS port 443 is accepted. Both required public/private endpoints
are checked even if their URL lacks an `/api/` path; eight additional cases
reject missing or misconfigured endpoints. The genuine ccxt sandbox construction
test still passes. All 88 testnet and durable-execution regressions pass,
covering partial fills, recovery, operator resolution and reconciliation.

To resume, configure the two testnet-only variables locally and follow
[TESTNET.md](../../TESTNET.md). `testnet run --once` records a starting
baseline, then the normal loop can accumulate four weeks for comparison
with paper. These checks do not establish execution parity at the venue,
and a gate with no promoted strategy may legitimately submit no orders.
