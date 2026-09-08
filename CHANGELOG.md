# Changelog

BACKTESTS.md is the real lab notebook (six measured rounds, negative results
included). This file maps that history onto the repo, newest first.

## [1.1.1] — 2026-09-08 — verification-gap pass

Re-audit of the working tree against the (externally re-derived) JUDGE_REPORT
flaw list confirmed every Part-3/Part-4 fix present, then closed the gaps
where a fix had shipped with no regression test standing guard: reset-409
while the engine is 'stopping', torn kronos ledger quarantine + restart
roundtrip, per-timeframe forecast horizon, forex ×5/7 bars/year, deterministic
journal-connection close, and the warmup-RSI/short-gate interaction (with the
100.0 counterfactual proving the NaN is what disarms the gate). Stale
"Chart.js via CDN" comment corrected. 108 → 114 tests; ruff zero; no trading
logic touched.

## [1.1.0] — 2026-09-06 — independent audit + hardening (loop engineering)

A two-agent audit (`JUDGE_REPORT.md`) found 20 flaws; 18 fixed and re-verified
by a fresh judge pass, plus the presentation layer (this release's scaffolding).
Highlights, each pinned by a test where testable:

- **Deflated Sharpe unit fix** — the SE was per-period while trial Sharpes were
  annualized, so DSR read ~1.0 for any input and could never reject. Pinned
  reference case: Sharpe 1.0 / 5 trials / 26k bars → 0.89 ("suggestive").
- **Backtest/live fill-bar parity** — the backtester now scans the bar the
  entry filled in (its whole range is post-fill), matching the live engine.
- **Purged-CV** now drops trades whose whole holding spans a path boundary,
  not just entry proximity.
- **Kronos IC ledger** keyed per market (a BTC forecast can no longer resolve
  against ETH's closes); atomic saves; torn-file quarantine; horizon
  normalized to ~1 day ahead on every timeframe.
- **seed-demo rows labeled `mode='demo'`** — badged in the dashboard, excluded
  from paper-record answers (chatbot) and from `shadow` by default.
- Journal timestamps canonicalized (write-time + boot migration); SQLite
  connections closed deterministically; data-outage force-close retries every
  cycle; account reset refuses while the engine thread is still stopping;
  forex Sharpe annualization ×5/7; RSI warmup returns NaN; dashboard startup
  moved to lifespan; torch/transformers split into `requirements-kronos.txt`.
- 92 → 98 tests (six new regression tests); ruff pyflakes baseline unchanged.
- Presentation scaffolding: LICENSE (MIT), `.env.example`, `pyproject.toml`
  (committed ruff + pytest config), `Makefile`, GitHub Actions CI, `DEMO.md`,
  the dashboard **Evidence** tab, `--start/--end` pinned reproducibility with a
  data manifest, and `validate --report REPORT.md`.

## [1.0.0] — 2026-09 — six measured rounds (see BACKTESTS.md)

- **Round 1 (v1):** full-cost battery across crypto/forex. Connors RSI-2 on 1h
  −24% (per-trade edge < round-trip cost); VWAP scalper on 5m −28…−31%; cost
  decomposition instead of parameter tweaking.
- **Round 2 (v2):** timeframe specialization (turtle 1h / connors 4h / scalper
  15m), deep-bull guard for connors longs, scalper re-priced at maker-style
  fees — final shipped numbers with the honest verdicts attached.
- **Round 3 (v3):** execution truth + portfolio allocation + the Kronos
  verdict: rolling IC −0.056 → **NOT promoted**; the earned-vote gate held.
- **Round 4 (v4):** live-engine self-audit — three paper/backtest divergences
  found from journal fingerprints and fixed.
- **Round 5 (v5):** time-of-day RVOL filter (Zarattini–Barbon–Aziz 2024)
  measured neutral on 24/7 crypto → **ships OFF** (an honest negative result).
- **Round 6 (v6):** Chan AR(1)/OU half-life gate for connors — 3 of 4 cells
  positive, walk-forward positive on both symbols → ships ON (`mr_halflife_max`).
- **Autonomy + security:** downtime stop-replay, data-outage guard, engine
  auto-resume, optional token auth, CSRF-hardened mutating endpoints,
  TrustedHost anti-rebinding.
