# JUDGE REPORT — Algo (AI Trading Bot)

*Review method: two independent read-only review agents were dispatched against the current working tree (including uncommitted changes to `bot/dashboard.py` and `design-system/algo-trading-bot/MASTER.md`). The Judge read the full codebase and ran the test suite; the Presenter evaluated the repo as a competition panel / hiring manager / GitHub visitor would. No code was changed during review.*

---

## PART 1 — JUDGE: FLAW AUDIT

### Verdict

**Correctness: B.** The paper engine is genuinely solid for its class — journal-first opens with abort self-healing, atomic close+equity transactions, restart replay of missed stop breaches, NaN-hardened sizing, gap-aware OCO fills, all pinned by tests. But one real backtest/live parity bug survives (the fill bar is scanned live and skipped in backtest — the exact divergence the project claims to have eliminated), plus several smaller asymmetries (live strategy-exit fills at an unobservable price).

**Methodology: C+.** The statistics machinery looks impressive and is mostly real (purged path partitioning, PBO rank logic, Monte Carlo are correctly built), but the Deflated Sharpe — the tool that would quantify the project's ~30 tuned constants — is unit-broken and **cannot fail**, the "ensemble orchestrator" is degenerate (regime weights and the conflict guard are unreachable dead code because no two strategies share a timeframe), and the one gate shipped ON this round rests on 4–26 trades.

**Security: B+.** For a localhost paper tool the posture is above-average: TrustedHost anti-rebinding (tested), body-required POSTs defeating form CSRF (tested), optional constant-time bearer token, vendored Chart.js with license, no secrets in code, nothing under data/ tracked. Docked for the Google Fonts @import contradicting "works offline" and no CSRF token if ever exposed.

**Engineering quality: B−.** Strong module boundaries and honest comments, undercut by a 2,071-line dashboard.py that is ~1,400 lines of inline HTML/CSS/JS with a stale "via CDN" docstring, per-query SQLite connection churn that is never closed, and a non-atomic Kronos ledger in a codebase that otherwise treats atomic writes as a discipline.

**Docs honesty: B.** BACKTESTS.md is a genuinely exemplary lab notebook (negative results, ships-OFF flags), but README/RESEARCH oversell the ensemble blend and conflict guard as functioning features, and the "Shadow Account on a real 428-trade journal" numbers are computed on **seed_demo backtest replays**, not trades the bot took.

**Overall: B−** — a competition-strong project whose two signature claims ("ensemble orchestration" and "deflated Sharpe honesty") do not survive contact with the code.

### Flaws

1. **[P1] Deflated Sharpe is unit-inconsistent and can essentially never reject** — `bot/validation.py:265-270`: `se = 1.0 / math.sqrt(max(1, n_obs - 1))` is the standard error of a **per-period** Sharpe, but `cmd_validate` (`main.py:190`) feeds `stats["sharpe"]` which `backtest.py:74` **annualizes** by `sqrt(bars_per_year)`. Verified numerically: `deflated_sharpe([1.0, 0.5, 0.6, 0.4, 0.5], n_obs=26_000)` → DSR 1.0; the consistent computation gives ≈0.89. The unit test (`tests/test_bot.py:1453-1470`) enshrines the bug. Fix: express SR and SR₀ in the same basis; pin a case that must fail.
2. **[P1] Backtest/live fill-bar divergence — the parity claim is false at the bar that matters most** — `bot/backtest.py:141-145`: once a position exists, the loop only scans `i+2`; **the fill bar i+1's high/low are never tested** against stop/target. Live, `bot/engine.py:446-451` scans the fill bar over its full range including movement before the fill; a bar that dips through the stop before rallying phantom-stops live but not backtest. Related: live strategy-exits fill at the last closed bar's close (`engine.py:463`), backtest at next bar's open. Fix: backtest scans the fill bar from the fill price onward; live doesn't scan bars that closed before the fill timestamp.
3. **[P1] The "ensemble orchestrator" is degenerate: regime weights and the conflict guard are unreachable** — `bot/orchestrator.py:26-29, 81-84, 125-129`: strategies have disjoint preferred timeframes (`turtle`: 1h, `meanrev`: 4h/1d, `scalper`: 15m/5m), so every spec evaluates exactly one strategy; the blend always reduces to that strategy's own confidence and the conflict guard can never see two opposing signals. README.md:82-88 and RESEARCH.md:115-118 present both as working features. Fix: state plainly that each timeframe is single-strategy, or co-evaluate.
4. **[P2] Mixed journal timestamp formats corrupt ts-string ordering** — `bot/journal.py:282,296` sort ISO **strings**; the engine writes `datetime.isoformat()` (`T` separator) while `bot/seed_demo.py:48-51` writes `str(pandas Timestamp)` (space separator). Within any day containing both, `last_equity_point` (the broker-cash restart anchor, `engine.py:111-127`) and the drawdown walk can select an out-of-order point. Fix: normalize on write; sort robustly.
5. **[P2] Kronos IC ledger is cross-symbol — forecasts resolve against the wrong market's closes** — `bot/kronos_signal.py:99-130`: pending forecasts keyed only by `ts`; `log_and_maybe_resolve(df["close"])` is called per-spec from `engine.py:307-313`, so a BTC forecast can resolve against ETH's closes. Corrupts the reported IC and the promotion gate. Fix: key by (symbol, timeframe).
6. **[P2] seed_demo backfills the live paper journal with backtest trades indistinguishable from real paper trades** — `bot/seed_demo.py:52-62` writes `mode="paper"`, no marker; 431 trades in the DB. Chatbot earnings answers, `/api/stats`, and the dashboard present them as the bot's record; README/BACKTESTS quote "57.6% adherence, 236/428 blew through stop" as the bot's own record. Fix: `mode="demo"` and filter/badge.
7. **[P2] The one gate shipped ON this round rests on 4–26 trades, and the multiple-testing counterweight is broken (see #1)** — `config.py:109` `mr_halflife_max=12.0`; BACKTESTS.md:299-305 shows the win comes from the gate skipping one losing trade per symbol; ~30 hand-tuned constants in `config.py:80-141` selected on the same windows.
8. **[P2] Data-outage forced close resets the failure counter even when the close fails** — `bot/engine.py:262-269`: if `_last_good_price` is `None`, `float(price)` raises, the close never happens, but `self._fetch_fails[key] = 0` executes regardless — the position stays unguarded for another 10 failed fetches. Fix: reset only after a successful close.
9. **[P2] Account reset wipes the DB while a "stopping" engine thread may still be writing** — `bot/dashboard.py:532-555`: reset ignores `api_engine_stop`'s `{"status": "stopping"}` return (thread alive after the 300s join, `dashboard.py:608-611`) and proceeds to `DELETE FROM` all tables. Fix: refuse reset unless stop status is `stopped`/`not_running`.
10. **[P2] dashboard.py is a 2,071-line module with ~1,400 lines of inline HTML/CSS/JS and internal doc rot** — docstring says "Chart.js via CDN" while lines 322-330 vendor it; `@import` of Google Fonts (line 684) makes "works fully offline" (line 328) false in spirit; deprecated `@app.on_event("startup")` (line 616). Fix: docs truth + deprecation cleanup; full static-file split deferred.
11. **[P2] Kronos ledger writes are non-atomic and evaluate() swallows everything** — `bot/kronos_signal.py:89-97` writes `data/kronos_ic.json` in place (torn file → `_load` silently resets the ledger); `evaluate()` at 257-258 catches bare `Exception` and returns None; `engine.py:307` hardcodes `horizon=24` for every timeframe. Fix: atomic write, surfaced errors, per-timeframe horizon.
12. **[P2] "Purged" CV purges entry proximity only, not label overlap of long-held trades** — `bot/validation.py:108-110` drops a trade only if its **entry** sits within `purge_bars` of a path boundary; a turtle trade holds hundreds of bars, so outcomes still span boundaries and per-path returns are not independent OOS segments. Fix: also drop trades whose exit crosses a boundary.
13. **[P2] Live strategy-exit fills use an unobservable price; equity double-written on close cycles** — `bot/engine.py:463` closes at the last closed bar's close though the cycle may run a full interval later; backtest fills at next-open. Also `_close` → `journal.close_trade(equity=...)` writes one equity point at entry marks and the cycle tail writes a second at real marks. Fix: fetch a current price for live exits; one equity point per cycle.
14. **[P3] `_rma` docstring is wrong; warmup RSI reads 100.0, not NaN** — `bot/indicators.py:17-19,35`: `ewm(adjust=False)` seeds with the first value; `fillna(100.0)` would auto-fire any `>95` short gate during warmup (currently masked by a guard in meanrev.py — fragile). Fix: NaN warmup + correct docstring.
15. **[P3] Forex Sharpe annualization uses 24/7 bar counts** — `config.py:330-331`; Yahoo forex 1h trades ~24×5, so forex Sharpes in BACKTESTS.md Round 2 are overstated ~18% in magnitude. Fix: per-kind bars-per-year.
16. **[P3] Vendored Kronos ships without its license text** — `models/kronos/README.md` references an upstream MIT LICENSE that isn't vendored; no headers on `model/kronos.py`/`module.py`. Fix: vendor upstream LICENSE + attribution.
17. **[P3] Journal connections are opened per query and never closed** — `bot/journal.py:141-148`: `with self._conn() as conn` commits but does not close; every 4s dashboard poll opens fresh connections relying on GC. Fix: close deterministically.
18. **[P3] Chatbot intent precedence accident** — `bot/chatbot.py:145`: `if "strategy" in ql and "best" in ql or "which strategy" in ql` parses as `(A and B) or C`. Also `bot/validation.py:116-117` compounds in PnL order while the comment claims bar order. Fix: parenthesize; fix comment.
19. **[P3] requirements.txt is floor-pinned only and heavy deps are unconditional** — `torch>=2.2, transformers>=4.40` mandatory although Kronos is optional; no lock file. Fix: extras/guarded import.
20. **[P3] Repo clutter and dead knobs** — `.backup/` tarball, `.DS_Store`, `__pycache__` in `models/kronos/model/` (gitignored but present); `data/cache/` parquets never pruned; `journal._migrate` backfill (`journal.py:131-134`) unreachable.

### Strengths (must NOT break these while fixing)

- **Broker fill honesty** — `bot/broker.py:102-165,200-249`: maker/taker leg pricing, stop-checked-first OCO, gap-through-stop fills at the open never better than the level, deferred entry fee — pinned by tests.
- **Risk manager depth** — `bot/risk.py:163-174` explicit `isfinite` gate + vol-explosion cap; simulated-clock kill switch keeps backtest/live calendar-identical.
- **Crash-safe engine recovery** — journal-first open with abort rollback, close+equity in one SQLite transaction, downtime stop-replay, stop-less-row forced close, cash reconciliation — each with a regression test.
- **Data quality gate** — `bot/data.py:148-184` fail-loud OHLCV validation, forming-bar drop shared by both paths, tested fallback chain.
- **Dashboard security basics** — TrustedHost anti-rebinding, JSON-body-required mutating POSTs, constant-time bearer predicate, atomic engine-state/watchlist writes with corrupt-file quarantine.
- **Test suite is real** — 92 tests, all network calls monkeypatched, end-to-end engine stop-loss, restart replay, purge/IC/PBO/MC determinism, dashboard smoke via TestClient. **Ran clean: 92/92 in 3.99s.**
- **BACKTESTS.md lab-notebook honesty** — negative results, ships-OFF flags, Round-4 self-audit, "no result here is a promise".

---

## PART 2 — PRESENTER: STANDOUT REVIEW

### Current standing

| Audience | Grade | One-line reason |
|---|---|---|
| Competition panel | **B−** (substance A−, presentation C) | The lab notebook and validation machinery are far above the field, but there is not a single screenshot/GIF/chart image in the repo and the differentiators are CLI-only. |
| Hiring manager (quant) | **B** (code A−, repo mechanics C+) | Code and tests are the credential; but no CI, no LICENSE, no pyproject.toml, no lock file, 6 squashed commits contradicting "the edge is the process". |
| GitHub visitor | **C+** | Fails the 60-second test: dense ASCII diagram, differentiators in paragraph 9, best artifact (the dashboard) invisible until you run it. |

### The gap (ranked)

1. **The differentiators are invisible** — validate/shadow/kronos/purged-CV produce terminal text only; the dashboard has no evidence view.
2. **Zero visual proof** — no screenshot, GIF, or rendered chart anywhere; architecture diagram is hard-to-parse ASCII.
3. **Reproducibility is claimed but not delivered** — no pinned date ranges in the CLI, `data/results/*_v1.json` referenced by BACKTESTS.md doesn't exist, unpinned requirements, `run_battery.py` not wired into the story.
4. **Trust scaffolding missing** — no LICENSE, no CI, no pyproject.toml, no Makefile, no `.env.example`, no coverage artifact.
5. **Results framing buries the wins** — Round 2 table leads with −8.7% ETH with no benchmark columns; small staleness errors (main.py seed-demo docstring vs "no synthetic numbers"; chatbot says Connors buys RSI<10, shipped config uses 5) erode trust.

### Standout plan (top items)

Quick wins: **(1)** dashboard screenshots + 30s GIF in the README (S, impact 5); **(2)** five-bullet "Why this is different" block above the fold — Kronos denied a vote at IC −0.056, RVOL measured-neutral shipped OFF, 236/428 blew through stop and the bot says so, fees on both legs, causality-tested (S, 4); **(3)** LICENSE + `.env.example` + `pyproject.toml` + committed ruff config + Makefile (S, 4); **(4)** GitHub Actions CI: pytest + ruff, coverage badge (S, 4); **(5)** benchmark columns vs buy-and-hold in BACKTESTS.md headline table (S, 4); **(6)** fix every stale reference (S, 2).

Strategic bets: **(7)** an **"Evidence" tab** on the dashboard rendering the purged-CV path distribution, PBO/DSR/MinTRL verdict cards, the Kronos IC ledger over time with the 0.02 promotion hurdle drawn, and the shadow adherence donut — the backend JSON already exists (M/L, 5); **(8)** make reproducibility true — `--start/--end` pinned dates, committed `data/manifest.json` + small results JSONs, `make report` (M, 5); **(9)** auto-generated validation report artifact (REPORT.md) from `validate` (M, 4); **(10)** DEMO.md runbook + offline demo mode + rehearsed panel Q&A (S/M, 4); **(11)** make the process visible in git history — CHANGELOG.md narrating rounds 1–6, one commit per measured change going forward (S, 3); **(12)** split the 2,183-line test file into modules with a coverage config (M, 3).

**Priority if time is short: 1 → 7 → 3+4 → 8 → 2 → 9.**

### One-line pitch candidates

1. *"A trading bot where even an AAAI foundation model has to earn its vote — its forecasts are scored against reality, it failed the hurdle (IC −0.06), and the bot says so on its own dashboard."*
2. *"The bot that shows its work: purged-CV validation, PBO and Deflated-Sharpe statistics, a shadow account that audits every trade against its own rules, and a chatbot that answers for every decision it ever made."*

---

## PART 3 — LOOP ENGINEERING LOG

Fix pass applied 2026-09-06 across 19 files (+999/−418 lines), then re-audited
by a fresh verifying judge against every claim. Final status per flaw:

| # | Flaw | Severity | Status |
|---|---|---|---|
| 1 | Deflated Sharpe unit bug | P1 | **FIXED** — SE now annualized (√(apy/n)); pinned reference case: Sharpe 1.0 / 5 trials / 26k bars → **0.89** (was 1.0) |
| 2 | Backtest/live fill-bar divergence | P1 | **FIXED** — backtest scans the fill bar (entry→open bar) for stop/target, in time order before the next bar; end-of-backtest path scans it too; parity comment now truthful |
| 3 | Orchestrator oversell | P1 | **FIXED (docs)** — README / RESEARCH / BACKTESTS / orchestrator.py / backtest.py docstrings all state the blend + conflict guard are dormant scaffolding (disjoint timeframes) |
| 4 | Journal ts format mixing | P2 | **FIXED** — ts normalized to ISO on every write + one-time boot migration of legacy rows; both pinned by test |
| 5 | Kronos cross-symbol IC | P2 | **FIXED** — ledger keyed by (symbol\|timeframe); a BTC forecast can no longer resolve against ETH closes; pinned by test |
| 6 | seed_demo indistinguishable from paper | P2 | **FIXED** — seeds write mode='demo'; dashboard badges them + headline stats/equity/chart exclude them (demo-only journals still render); chatbot answers paper record with explicit exclusion note; shadow defaults to paper. Caveat kept: pre-existing rows in data/trading.db remain mode='paper' |
| 7 | Gate on 4–26 trades | P2 | **PARTIAL (by design)** — the counterweight (DSR) is now functional; BACKTESTS.md changelog discloses the fix; sample-size caveat stands as documented honesty |
| 8 | Outage counter reset on failed close | P2 | **FIXED** — no-mark case defers and retries EVERY cycle (counter keeps growing); counter resets only after a successful close; pinned by test |
| 9 | Reset during stopping engine | P2 | **FIXED** — reset reads the stop status and 409s while 'stopping' |
| 10 | dashboard.py doc rot / deprecation | P2 | **FIXED** — docstring truthful (vendored Chart.js, fonts honesty); lifespan replaces @on_event (no orphans — verified) |
| 11 | Kronos non-atomic save + swallowed errors | P2 | **FIXED** — atomic tmp+replace save; torn files quarantined to .corrupt (not silently reset); evaluate() errors surfaced via last_error; horizon normalized to ~1 day per timeframe |
| 12 | Purge ignores exit-boundary overlap | P2 | **FIXED** — a trade is kept only when entry AND exit sit inside one block, purge_bars clear of both edges; pinned by test (220-bar hold dropped, short control kept) |
| 13 | Live exit price + double equity write | P2 | **FIXED (equity)** — one equity point per cycle with real marks (crash window covered by restart reconciliation); exit-price staleness documented honestly |
| 14 | RSI warmup 100.0 + _rma docstring | P3 | **FIXED** — warmup NaN (gates auto-pass on NaN), all-gains → 100, flat → 50; docstring corrected; test extended |
| 15 | Forex bars-per-year | P3 | **FIXED** — ×5/7 for forex at all three call sites |
| 16 | Kronos license not vendored | P3 | **FIXED** — upstream MIT (© 2025 ShiYu) vendored at models/kronos/LICENSE |
| 17 | Journal connections never closed | P3 | **FIXED** — _conn is a contextmanager: commit/rollback AND guaranteed close |
| 18 | Chatbot precedence + comment lie | P3 | **FIXED** — parens added; comment corrected |
| 19 | requirements floors / heavy deps | P3 | **FIXED** — torch/transformers etc moved to requirements-kronos.txt |
| 20 | Repo clutter (local, gitignored) | P3 | **NOT FIXED (accepted)** — user's local files, gitignored |
| + | bot/llm.py F821 undefined `LLMConfig` (bonus) | — | **FIXED** — import added |

**Verification (fresh judge, independent): PASS.** 18 fixed + 1 partial-by-design
+ 1 accepted + 1 bonus; **no regressions**; the two low-severity follow-ups it
flagged (vacuous-test guards, dashboard headline stats still blending demo
rows) were fixed immediately after the verdict. Suite: **98/98 tests** (6 new
regression tests), ruff pyflakes findings byte-identical to pre-fix baseline.

**What still stands from the PRESENTER's plan** (presentation work, not flaws):
screenshots/GIF in the README, an "Evidence" dashboard tab, CI + badge,
LICENSE/Makefile/pyproject, pinned `--start/--end` reproducibility, DEMO.md
runbook, test-suite split. The substance is now as honest as the docs claim;
the next leverage is making it *visible*.

---

# PART 4 — BREAKDOWN ANALYSIS (3rd audit: runtime failure modes, demo-day)

Two new agents, both brutally honest, both read-only on the repo (all execution
in /tmp copies):

- **FMEA JUDGE** — systematic runtime-failure sweep: fresh-machine boot,
  resource growth over weeks-months, concurrency/lifecycle, network degradation,
  failure modes in the newest features, crash/power-loss windows, operator
  surprises. *Executed*: fresh-clone `status`/`chat`/dashboard boot, proof-crash
  of missing-`data/results`, 500k-row SQLite benchmark (57ms scan vs ~0 indexed),
  WAL backup-gap experiment, 0.70s rolling-IC measurement, token-paradox 401
  proof, real uvicorn auto-resume proof. **Grade: B−.**
- **RED-TEAM OPERATOR** — demo-day rehearsal: cold start, DNS-blackhole outage
  with an open position, token mode, two-tab concurrency + chat mid-cycle
  (190/190 requests 200), SIGINT mid-cycle (DB integrity ok), `make demo` ×3
  (419→1,256 stacked trades, headline return → 0.0%), port squatters. **Grade: C+**
  (plumbing unbreakable, *content* misfires in the exact demo configuration).

## BREAKDOWNS (consolidated, deduplicated, severity-ordered)

| # | Breakdown | Sev | Status |
|---|-----------|-----|--------|
| 1 | `main.py shadow` + `run_battery.py` crash on fresh machine — write to `data/results/` with no `os.makedirs` (whole battery's work lost at the write) | P0 | FIXED |
| 2 | Reset backup can miss WAL-resident commits — `copy2` of the main db file only (proven: copy missing even the schema) | P0 | FIXED |
| 3 | Red-team: seed-demo stacks history — `make demo` ×2 doubles trades (838), ×3 → 1,256, headline return degrades to 0.0% | P0 | FIXED |
| 4 | Red-team: fresh-clone headline self-contradicts — "−$1,020.30 P&L" beside "+3.40% return" (demo equity walk restarts per spec, uncorrelated with trade P&L) | P0 | FIXED |
| 5 | Red-team: "why did you buy BTC?" answers about GBPUSD — `why` branch takes newest non-HOLD decision, no symbol match, no mode filter | P0 | FIXED |
| 6 | `/api/decisions` + Overview terminal mix demo rows into the live paper feed, unbadged (both agents; proven) | P0 | FIXED |
| 7 | Engine stop strands the UI — loop sleeps the FULL interval before its identity check; Start refused + reset 409-blocked up to interval−300s (~55 min at 3600s) | P1 | FIXED |
| 8 | Evidence tab costs ~0.7s CPU + destroys/recreates both charts every 4s poll while active (12% of a core on a demo laptop) | P1 | FIXED |
| 9 | Token paradox — `DASHBOARD_TOKEN` set ⇒ `GET /` itself 401s: no browser can open the UI (proven) | P1 | FIXED |
| 10 | Kronos weights download runs synchronously inside the start-HTTP request — minutes-long dead Start button on cold HF cache | P1 | FIXED |
| 11 | No ccxt timeout configured (default 10s × 3 sources × 7 specs) — a blackout cycle can exceed the 300s stop-join | P1 | FIXED |
| 12 | `health_note` (unguarded position behind dead feed) exists in the payload but NOTHING renders it — engine pill stays green on stage | P1 | FIXED |
| 13 | `main.py dashboard` exits 0 when the port is taken — `make demo` "succeeds" with nothing served | P1 | FIXED |
| 14 | Engine cycle = 1.5–2.5 min (Kronos 24-path forecasts); first decisions land +150–200s after boot; interval measured after the cycle, not from cycle start | P1 | FIXED |
| 15 | Auto-resume surprise — SIGINT'd rehearsal + fresh boot silently starts a live paper engine (proven); no confirmation, no opt-out, DEMO.md silent on it | P1 | FIXED |
| 16 | Pinned `--start` WITHOUT `--end` → constant `_now` filename freezes the cache on day-1's window, forever, never pruned | P1 | FIXED |
| 17 | No `equity(ts)` index — `last_equity_point` full-scans 500k rows every 4s poll (57ms measured, ~0 indexed) | P2 | FIXED |
| 18 | Reset backups accumulate forever (each a full db copy, ~20MB × weekly) | P2 | FIXED |
| 19 | Manifest write racy across CLI processes — shared tmp path can clobber/lose provenance | P2 | FIXED |
| 20 | Evidence validation cards default to the OLDEST `validation_*.json` and show no fetch window | P2 | FIXED |
| 21 | `make ui` / `/api/positions` — the names a presenter guesses — both don't exist (404 / no rule) | P2 | FIXED |
| 22 | Red-team: chatbot "how much did you earn?" — P&L excludes $1,688 deposits in the same sentence ("−$194.71 (return 6.89%…)") | P2 | FIXED |
| 23 | Backup naming collision — two resets in the same second overwrite the first backup | P3 | FIXED |
| 24 | `_iso()` passes unparseable timestamps through unchanged (latent: would sort after every ISO string) | P3 | FIXED |
| 25 | Corrupt `trading.db` → import-time crash → uvicorn dies, no quarantine (every OTHER artifact has one) | P3 | FIXED |
| 26 | DB-corrupt boot loop / stale `data/` hand-copied via zip reviving old engine state + watchlist | P2/P3 | DOCUMENTED (DEMO.md runbook) |

**Cleared suspicions (verified false by the agents)**: `status`/`chat`/dashboard
boot on a fresh machine (all fine); LLM and news requests missing timeouts (both
have 30s/8s); `_prune_rolling_cache` ever deleting pinned files (impossible by
name construction); ccxt geo-block 451 unhandled (chain works; the re-pay cost
was real and is fixed via the source-cooldown); auto-resume leaking into pytest
(guarded); MarketData/_NEWS_CACHE/_ccxt_clients unbounded growth (all bounded);
WAL file growth (per-request connections close; auto-checkpoint keeps it small);
`stats()` 200k-row scan at 4s poll (30ms measured — bounded).

**Verified strengths to not break**: journal-first opens with self-healing
abort; atomic close+equity with restart cash reconciliation; the outage ladder
(force-close fired exactly at fail #10 in a live DNS-blackhole test); two-tab
concurrency under load 190/190 with zero non-200s; SIGINT mid-cycle leaves
`integrity_check=ok`; corrupt kronos ledger quarantines; zombie-engine
containment; offline SPA (vendored Chart.js + boot banner).

*Fix log and verification verdict follow in the Loop Engineering section below.*

## Part 4 — Loop Engineering Log (breakdown fixes)

All 24 actionable breakdowns **FIXED**; 2 accepted as documented design
(FMEA #17 kill-window force-close of a stop-less restored row — conservative
by design; FMEA #18 chat/decisions unbounded growth — reads stay LIMIT-bounded,
disk polish only). Suite grew 101 → **108 tests** (7 new regression tests),
ruff **zero findings**, and the token-guard contract was live-verified on a
real uvicorn boot (page 200 unguarded / API 401 → 200 with bearer).

| # | Fix | Where |
|---|-----|-------|
| 1 | `os.makedirs` before every results write | main.py cmd_shadow, run_battery.py main() |
| 2 | `PRAGMA wal_checkpoint(TRUNCATE)` before the backup copy | dashboard.api_account_reset |
| 3 | seed() clears prior mode='demo' rows first (idempotent re-seed) | bot/seed_demo.py |
| 4 | ONE portfolio-threaded demo equity walk (exit-ordered, thinned) — equity/return/DD exactly coherent with summed trade P&L | bot/seed_demo.py |
| 5 | "why did you buy X?" matches the asked symbol (BTC→BTC/USDT, GBPUSD→GBPUSD=X), prefers paper rows, labels demo rows, honest "no decision on X" | bot/chatbot.py `_symbol_from_question`/`_same_symbol` |
| 6 | `recent_decisions(mode=)` + paper-first feed with demo fallback + demo badge in the terminal | bot/journal.py, dashboard api_decisions, JS refreshDecisions |
| 7 | interruptible 1s-tick sleep in the engine loop — stop honored within ~1s instead of stranding up to ~55 min | dashboard `_loop` |
| 8 | Evidence tab: loads once per tab entry (not per 4s poll) + server-side (mtime,size)-keyed payload cache (60s TTL) | dashboard refreshVisible/api_evidence |
| 9 | token guard exempts GET / + vendored chart.js; SPA attaches bearer from localStorage, prompts once on 401 | dashboard `_TokenGuard`, JS jget/jreq/tokenGate |
| 10 | Kronos probe (`available`) no longer downloads weights — first evaluate() loads them inside the engine thread | kronos_signal.KronosPredictorLazy._probe |
| 11 | explicit ccxt `timeout: 15000` + per-source 3-strike/5-minute cooldown (auto chain only; all-benched self-releases) | bot/data.py |
| 12 | health_note rendered: amber 'degraded' pill + dismissible banner (re-arms on a NEW note) | dashboard api_stats + JS |
| 13 | port-in-use exits non-zero — `make demo` aborts loudly | main.py cmd_dashboard |
| 14 | interval counts from cycle START (bursts land on cadence, not cycle+interval) | dashboard `_loop` |
| 15 | ALGO_NO_AUTO_RESUME opt-out + boot toast on auto-resume (trading never silently begins) | dashboard `_auto_resume_engine`/api_stats/JS |
| 16 | `--start` without `--end` = date-stamped rolling cache entry (day 2+ refetches; pruneable) | data._disk_cache_path |
| 17 | `idx_equity_ts` index — `last_equity_point` stops full-scanning 500k rows | journal._SCHEMA (picked up on next boot) |
| 18 | reset backups pruned to newest 5 | dashboard `_prune_reset_backups` |
| 19 | manifest: per-pid tmp + fresh re-read-merge before replace (no clobber/lost provenance) | data._record_manifest |
| 20 | evidence validations sorted newest-first by mtime + window on selector & card | dashboard `_evidence_validations`/JS |
| 21 | `GET /api/positions` alias + `make ui` target | dashboard, Makefile |
| 22 | earnings answer separates net deposits from trade P&L | journal.deposits_net + chatbot |
| 23 | backup names microsecond-unique (double-click-safe) | api_account_reset |
| 24 | `_iso()` stamps unparseable ts with now() (never sorts after every ISO string); corrupt db quarantined (`.corrupt.<ts>`), busy/locked/disk-full NOT quarantined | journal.py |
| 26 | operator runbook: DEMO.md "Operator notes" (re-seed safe, auto-resume, port, Ctrl-C, backups, wifi arithmetic, first-cycle, token flow) | DEMO.md |

**Verification:** independent verification judge follows below.

## Part 4 — Verification (independent judge, 4th agent)

**VERDICT: PASS-WITH-FOLLOWUPS → all follow-ups fixed immediately.**

All 24 fixes verified correct and regression-free. The judge's own experiments:
pinned-reader proof that the WAL checkpoint is the difference (0 vs 1 rows in
the backup); mutant tests of the seed walk (old code gives 9700/10600 vs the
coherent 10300); live uvicorn token-guard boot (page 200 / API 401→200, POSTs
and DELETEs all guarded, query-string and path tricks rejected); stop-latency
measurement (stop detected ≤1s; slow cycles don't busy-loop — remaining≤0
skips the inner sleep); manifest 100×2-threaded writer race (both entries
survive, no tmp leftovers); quarantine false-positive check (live-locked and
disk-full DBs re-raise, never quarantined); cooldown off-by-one audit (exact
3-strike bench, explicit-source bypass intentional). All 7 new tests judged
SOLID/adequate — none pass vacuously.

Five follow-ups flagged (2×P2, 3×P3) — **all fixed**:

| Follow-up | Fix |
|---|---|
| [P2] stoplist approach to symbol extraction missed words ('LONG' extracted from 'open a long on SOL') | replaced with known-symbols matching (watchlist + journaled markets are the only valid bare tokens) + a verb-gated tier for explicitly-asked-but-untraded markets (DOGE/AAPL get an honest 'never traded', not a wrong answer) |
| [P2] api_stats fallback counted paper trades only — a paper-trade-without-equity journal (hand-producible) still self-contradicted | fallback now requires BOTH a paper trade and a paper equity point |
| [P3] undashed --start 20240601 made a "pinned" cache name that the 14d prune would delete | dates normalized to dashed ISO in _disk_cache_path — pinned names can never carry the rolling signature |
| [P3] test_reset_backup left dash.journal pointed at a deleted tempdir | module-state restore block added (mirrors the decisions test) |
| [P3] quarantine wal/shm sidecars had inconsistent names | sidecars now `<db>.corrupt.<ts>-wal/-shm` |

Final state: **108/108 tests, ruff zero findings.** Regression tests extended
with the judge's exact false-extraction case ('open a long on SOL?' → SOL).

---

# PART 5 — VERIFICATION-GAP PASS (2026-09-08)

KIMI K2.6 was asked to re-derive the flaw list from this report; its summary
restated Part 1 accurately but was checked against the *uncommitted working
tree* this time. Re-verification (three independent read-only agents, plus
direct code reads of the five most load-bearing paths) confirmed every Part-3
and Part-4 fix is genuinely present — the 16-item summary describes commit
`69dd4d1`, not the tree. What the pass found and closed:

**Five fixes had shipped with no regression test standing guard** (the Part-4
verification judge's own standard: "none pass vacuously" — these could have
regressed silently):

| Guard added | What it pins |
|---|---|
| `test_account_reset_refused_while_engine_stopping` | the 409 path: reset while status='stopping' touches nothing (trade survives, no backup, no wipe); 'not_running' proceeds — previously only the happy path was tested |
| `test_kronos_ledger_quarantines_torn_file_and_survives_restart` | torn `kronos_ic.json` → `.corrupt` preserved (not silently reset, not deleted) + healthy save/load roundtrip through a restart, no `.tmp` litter |
| `test_kronos_horizon_normalized_to_one_day_per_timeframe` | horizon = exactly one day of bars on every timeframe (5m→288 … 1d→1), the old hardcoded 24 made 4h forecast four days out |
| `test_bars_per_year_forex_weekday_scaling` | kind='forex' → ×5/7 (≈6257 vs 8760 bars/yr at 1h, the ~18% Sharpe overstatement); default kind unchanged |
| `test_journal_conn_closes_deterministically` | `_conn()` closes on BOTH exit paths (clean + exception) — the old code committed but never closed |
| `test_meanrev_never_fires_short_gate_on_warmup_rsi` | warmup RSI(2)=NaN cannot fire the >95 short gate, with the 100.0 counterfactual SHORTING at the same bar — pinning the exact fragile-masking scenario the original audit flagged |

Also: the stale "Chart.js loads from a CDN" JS comment corrected (it is
vendored — that is *why* offline works); suite 108 → **114**; ruff zero.
No trading logic, strategy parameter, or measured result was touched by this
pass — the protected strengths named in Part 1 are untouched by construction.
