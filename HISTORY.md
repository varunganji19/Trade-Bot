# HISTORY.md — the record

Four separate documents used to carry the project's history: an external
audit report, a flaw-by-flaw validation, the self-audit passes, and the
milestone log. They were each written at a moment in time and none of them
described the code as it stands — which made them a liability, because a
reader could not tell which parts were still true.

They are folded in here, verbatim and dated, as HISTORY. Nothing below is a
description of the CURRENT system: for that, read README.md (what it is),
RESEARCH.md (why these strategies), BACKTESTS.md (what the standard book
measured), HFT.md (the fast book, its cost wall and its promotion gate) and
CHANGELOG.md (what changed, newest first).

Kept because the reasoning is worth more than the conclusions: several of
these entries record a fix that a later measurement overturned, and that is
exactly the kind of thing a trading repo should not quietly delete.


---

# External audit report

*An outside review of the codebase and the fixes it produced.  
Folded in from `JUDGE_REPORT.md` on 2026-09-19.*

## JUDGE REPORT — Algo (AI Trading Bot)

*Review method: two independent read-only review agents were dispatched against the current working tree (including uncommitted changes to `bot/dashboard.py` and `design-system/algo-trading-bot/MASTER.md`). The Judge read the full codebase and ran the test suite; the Presenter evaluated the repo as a competition panel / hiring manager / GitHub visitor would. No code was changed during review.*

---

### PART 1 — JUDGE: FLAW AUDIT

#### Verdict

**Correctness: B.** The paper engine is genuinely solid for its class — journal-first opens with abort self-healing, atomic close+equity transactions, restart replay of missed stop breaches, NaN-hardened sizing, gap-aware OCO fills, all pinned by tests. But one real backtest/live parity bug survives (the fill bar is scanned live and skipped in backtest — the exact divergence the project claims to have eliminated), plus several smaller asymmetries (live strategy-exit fills at an unobservable price).

**Methodology: C+.** The statistics machinery looks impressive and is mostly real (purged path partitioning, PBO rank logic, Monte Carlo are correctly built), but the Deflated Sharpe — the tool that would quantify the project's ~30 tuned constants — is unit-broken and **cannot fail**, the "ensemble orchestrator" is degenerate (regime weights and the conflict guard are unreachable dead code because no two strategies share a timeframe), and the one gate shipped ON this round rests on 4–26 trades.

**Security: B+.** For a localhost paper tool the posture is above-average: TrustedHost anti-rebinding (tested), body-required POSTs defeating form CSRF (tested), optional constant-time bearer token, vendored Chart.js with license, no secrets in code, nothing under data/ tracked. Docked for the Google Fonts @import contradicting "works offline" and no CSRF token if ever exposed.

**Engineering quality: B−.** Strong module boundaries and honest comments, undercut by a 2,071-line dashboard.py that is ~1,400 lines of inline HTML/CSS/JS with a stale "via CDN" docstring, per-query SQLite connection churn that is never closed, and a non-atomic Kronos ledger in a codebase that otherwise treats atomic writes as a discipline.

**Docs honesty: B.** BACKTESTS.md is a genuinely exemplary lab notebook (negative results, ships-OFF flags), but README/RESEARCH oversell the ensemble blend and conflict guard as functioning features, and the "Shadow Account on a real 428-trade journal" numbers are computed on **seed_demo backtest replays**, not trades the bot took.

**Overall: B−** — a competition-strong project whose two signature claims ("ensemble orchestration" and "deflated Sharpe honesty") do not survive contact with the code.

#### Flaws

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

#### Strengths (must NOT break these while fixing)

- **Broker fill honesty** — `bot/broker.py:102-165,200-249`: maker/taker leg pricing, stop-checked-first OCO, gap-through-stop fills at the open never better than the level, deferred entry fee — pinned by tests.
- **Risk manager depth** — `bot/risk.py:163-174` explicit `isfinite` gate + vol-explosion cap; simulated-clock kill switch keeps backtest/live calendar-identical.
- **Crash-safe engine recovery** — journal-first open with abort rollback, close+equity in one SQLite transaction, downtime stop-replay, stop-less-row forced close, cash reconciliation — each with a regression test.
- **Data quality gate** — `bot/data.py:148-184` fail-loud OHLCV validation, forming-bar drop shared by both paths, tested fallback chain.
- **Dashboard security basics** — TrustedHost anti-rebinding, JSON-body-required mutating POSTs, constant-time bearer predicate, atomic engine-state/watchlist writes with corrupt-file quarantine.
- **Test suite is real** — 92 tests, all network calls monkeypatched, end-to-end engine stop-loss, restart replay, purge/IC/PBO/MC determinism, dashboard smoke via TestClient. **Ran clean: 92/92 in 3.99s.**
- **BACKTESTS.md lab-notebook honesty** — negative results, ships-OFF flags, Round-4 self-audit, "no result here is a promise".

---

### PART 2 — PRESENTER: STANDOUT REVIEW

#### Current standing

| Audience | Grade | One-line reason |
|---|---|---|
| Competition panel | **B−** (substance A−, presentation C) | The lab notebook and validation machinery are far above the field, but there is not a single screenshot/GIF/chart image in the repo and the differentiators are CLI-only. |
| Hiring manager (quant) | **B** (code A−, repo mechanics C+) | Code and tests are the credential; but no CI, no LICENSE, no pyproject.toml, no lock file, 6 squashed commits contradicting "the edge is the process". |
| GitHub visitor | **C+** | Fails the 60-second test: dense ASCII diagram, differentiators in paragraph 9, best artifact (the dashboard) invisible until you run it. |

#### The gap (ranked)

1. **The differentiators are invisible** — validate/shadow/kronos/purged-CV produce terminal text only; the dashboard has no evidence view.
2. **Zero visual proof** — no screenshot, GIF, or rendered chart anywhere; architecture diagram is hard-to-parse ASCII.
3. **Reproducibility is claimed but not delivered** — no pinned date ranges in the CLI, `data/results/*_v1.json` referenced by BACKTESTS.md doesn't exist, unpinned requirements, `run_battery.py` not wired into the story.
4. **Trust scaffolding missing** — no LICENSE, no CI, no pyproject.toml, no Makefile, no `.env.example`, no coverage artifact.
5. **Results framing buries the wins** — Round 2 table leads with −8.7% ETH with no benchmark columns; small staleness errors (main.py seed-demo docstring vs "no synthetic numbers"; chatbot says Connors buys RSI<10, shipped config uses 5) erode trust.

#### Standout plan (top items)

Quick wins: **(1)** dashboard screenshots + 30s GIF in the README (S, impact 5); **(2)** five-bullet "Why this is different" block above the fold — Kronos denied a vote at IC −0.056, RVOL measured-neutral shipped OFF, 236/428 blew through stop and the bot says so, fees on both legs, causality-tested (S, 4); **(3)** LICENSE + `.env.example` + `pyproject.toml` + committed ruff config + Makefile (S, 4); **(4)** GitHub Actions CI: pytest + ruff, coverage badge (S, 4); **(5)** benchmark columns vs buy-and-hold in BACKTESTS.md headline table (S, 4); **(6)** fix every stale reference (S, 2).

Strategic bets: **(7)** an **"Evidence" tab** on the dashboard rendering the purged-CV path distribution, PBO/DSR/MinTRL verdict cards, the Kronos IC ledger over time with the 0.02 promotion hurdle drawn, and the shadow adherence donut — the backend JSON already exists (M/L, 5); **(8)** make reproducibility true — `--start/--end` pinned dates, committed `data/manifest.json` + small results JSONs, `make report` (M, 5); **(9)** auto-generated validation report artifact (REPORT.md) from `validate` (M, 4); **(10)** DEMO.md runbook + offline demo mode + rehearsed panel Q&A (S/M, 4); **(11)** make the process visible in git history — CHANGELOG.md narrating rounds 1–6, one commit per measured change going forward (S, 3); **(12)** split the 2,183-line test file into modules with a coverage config (M, 3).

**Priority if time is short: 1 → 7 → 3+4 → 8 → 2 → 9.**

#### One-line pitch candidates

1. *"A trading bot where even an AAAI foundation model has to earn its vote — its forecasts are scored against reality, it failed the hurdle (IC −0.06), and the bot says so on its own dashboard."*
2. *"The bot that shows its work: purged-CV validation, PBO and Deflated-Sharpe statistics, a shadow account that audits every trade against its own rules, and a chatbot that answers for every decision it ever made."*

---

### PART 3 — LOOP ENGINEERING LOG

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

## PART 4 — BREAKDOWN ANALYSIS (3rd audit: runtime failure modes, demo-day)

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

### BREAKDOWNS (consolidated, deduplicated, severity-ordered)

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

### Part 4 — Loop Engineering Log (breakdown fixes)

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

### Part 4 — Verification (independent judge, 4th agent)

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

## PART 5 — VERIFICATION-GAP PASS (2026-09-08)

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


---

# Flaw validation

*Each reported flaw re-derived from the code before it was fixed — including the ones that turned out to be wrong.  
Folded in from `FLAW_VALIDATION.md` on 2026-09-19.*

## Gemini 3.8 Flash Flaw Audit — Validation Results & Fix Plan (2026-09-09)

Every claimed flaw was validated against the actual code AND, where refutable,
against empirical runs on the repo's own cached data and journal. Baseline:
114/114 tests pass. No code was changed for this document.

Verdict scale: **CONFIRMED** (mechanism + consequence verified) · **PARTIAL**
(mechanism real, consequence overstated or fix wrong) · **REFUTED**.

### Verdict table

| ID | Claim | Verdict | Corrected severity | Empirical evidence |
|----|-------|---------|--------------------|--------------------|
| 1.1 | Turtle opposite-channel exit impossible | **CONFIRMED** | P0 | 0 of 8,759 bars can satisfy `close < don_exit_low`; BTC 1h 365d: 10 trades, 0 channel exits (9 stops + 1 held ~10 months to end-of-backtest); unit test at `tests/test_bot.py:1681` is vacuous (`below=False` on its seed, so it asserts `None==None`) |
| 1.2 | Bar i+1 stop/target scanned before strategy exit | **PARTIAL** — real ordering flaw, *not* lookahead; ~2–3% of trades affected | P2 (was P1) | With 1.1 fixed, conflicting exits: 4/115 BTC, 2/70 ETH, 1/57 SOL |
| 1.3 | Breakeven stop ignores fees/slippage | **CONFIRMED** | P1 | Broker sim: exit at entry = −0.30% notional; journal shows real scalper rows `stop==entry` exiting "stop loss" with pnl −3.74 |
| 2.1 | Trailed stops overwrite initial stop in DB | **CONFIRMED** | P1 | Real journal: 80/431 closed trades have stop within 0.1% of entry; 79 excluded from R-stats (risk=0), 5 explosive R (max +22.2), 239/352 flagged "blew through stop" (mostly BE artifacts, not breaches) |
| 2.2 | No margin/principal accounting | **PARTIAL** — mechanism real, "infinite leverage" false | P2 | `max_position_pct` 25% × `max_open_positions` 4 caps gross at ~1× equity; it is a margin-style PnL-settlement model with no margin call/liquidation modeling — undocumented, not unbounded |
| 2.3 | Cash reconciliation double-counts fees | **CONFIRMED** (diagnosis); Gemini's prose mis-describes it; formula fix right for the standard case, wrong for one edge case | P2 | Broker sim: true close-event delta = `pnl + entry_fee`; query returns `pnl + fees` = gross → overstates by exactly the **exit** fee per reconciled trade (not "refunds entry fee") |
| 3.1 | DSR omits skew/kurtosis | **CONFIRMED** formula gap; measured impact small here | P3 (was P2) | On this bot's own equity curves the moment-adjusted SE is 1.00×/1.11×/0.91× the normal SE (kurtosis 10–14 but tiny per-bar SR makes the SR² term negligible) |
| 3.2 | Allocator `dropna(any)` drops crypto weekends | **CONFIRMED** | P2 | Real frames: BTC 199 bars → 141 aligned rows (29% dropped); weekend share 24% → 0.7% |
| 3.3 | Lexicon sentiment ignores asset_hint | **CONFIRMED** (live paper path only — backtests pass `include_sentiment=False`) | P2 | `assess()` lexicon branch never reads `asset_hint`; orchestrator passes it (`spec.display`) |
| 4.1 | Cache date-stamp forces refetch | **PARTIAL** — mechanism true; freshness semantics make blind reuse wrong | P3 | `data/cache` holds multiple day-stamps per (symbol, tf, days); but a 365d file from 3 days ago is a *different window* than requested — that's why pinned `--start/--end` exists |
| 4.2 | Kronos loop slow + `infer_freq` fallback | **PARTIAL** — both sub-issues real; **Gemini's batching fix is wrong** | P2 (freq) / P3 (perf) | Vendored `kronos.py:auto_regressive_inference` ends with `np.mean(preds, axis=1)` — `predict(sample_count=30)` returns ONE mean path, so batching would collapse P(up) to exactly 0/1 and dispersion to 0 |

### Corrections to the Gemini report worth recording

1. **1.2 is an event-ordering flaw, not lookahead** — nothing reads future
   data. The strategy exit decided at bar i's close fills at bar i+1's open;
   that open fill must execute *before* any intra-bar-i+1 stop/target scan.
   The current code scans first, so a same-bar stop/target can "win" over an
   exit order that was already filled at the open. Mixed-direction bias,
   ~2–3% of trades.
2. **2.3 arithmetic, precisely**: `pnl = gross − entry_fee − exit_fee`;
   `fees = entry_fee + exit_fee`; query `pnl+fees = gross`. The correct
   crash-window delta (entry fee already reflected in the anchor equity
   point) is `pnl + entry_fee = gross − exit_fee`. The query therefore
   overstates by the **exit fee only**. Gemini's fix formula is right for the
   standard crash window but **wrong for the edge case where the trade both
   opened and closed after the anchor** (equity writes are skipped when all
   held symbols fail to fetch): there the true delta is plain `pnl`, and
   `pnl + entry_fee` overstates by the entry fee. A per-trade realized-cash
   column is the robust fix.
3. **4.2 batching fix would corrupt the signal**: the vendored predictor
   averages the sample dimension before returning, so the loop of
   single-sample calls is currently the *only correct* way to get 30 distinct
   paths. Batching requires patching the vendored model (MIT-licensed, allowed
   — but it is a deliberate divergence) or accepting mean-path output.
4. **3.1's proposed snippet is dimensionally wrong**: the Bailey–LdP moment
   formula applies to the **per-period** SR. Feeding the *annualized* `best`
   into `(1 − skew·SR + (kurt−1)/4·SR²)` mis-scales the correction. Must
   de-annualize first (`SR_p = best / sqrt(bars_per_year)`), compute the
   moment variance, then re-annualize the SE.
5. **2.2 "infinite unconstrained leverage" is false** — gross notional is
   bounded near 1× equity by existing caps. The real gap is that the account
   model (margin-style PnL settlement, no principal escrow, no margin call,
   shorts impossible on spot) is nowhere documented.

---

## Fix plan (no fixes applied yet — implementation phases, file-level specs)

Execution order is by risk: strategy correctness first (it invalidates all
documented turtle numbers), then audit/accounting integrity, then engine
fidelity, then statistics and infra. Each phase lands green on the full suite.

### Phase 0 — pin the baseline (before touching code)

- Re-run the three standard windows with **pinned** `--start/--end` (BTC/ETH/SOL
  1h turtle, connors 4h, scalper 15m) and save results to `data/results/` so
  before/after drift is attributable, not re-rollable.
- Accept that **every turtle number in BACKTESTS.md is stale after Fix 1.1**
  (BTC 1h 365d goes from 10 trades to ~115, with 49 opposite-channel exits).

### Phase 1 — strategy & accounting correctness (P0/P1)

#### Fix 1.1 — Turtle exit channel: use the prior 10-bar channel
- `bot/strategies/turtle.py:87,91`: `_at(df, "don_exit_low"/"don_exit_up", i, shift=1)`.
- Matches entry semantics (`don_up20/low20` already read with `shift=1`) and
  the classic Turtle S1 exit (prior channel, never the decision bar's own).
- **Tests**: make `tests/test_bot.py:1681` non-vacuous — assert the exit
  *fires* on a trend-then-reversal synthetic (the current seed frame has 14
  bars satisfying the shifted condition; construct a position/bar that hits
  one). Add a property test: for random frames, `check_exit` long fires iff
  `close < rolling-10 low shifted 1`.
- **Docs**: BACKTESTS.md turtle rows + §"Backtest parity" re-measured; README
  strategy table wording unchanged (it already says "opposite-channel exit").

#### Fix 2.1 — persist the initial stop
- `bot/journal.py` schema + `_migrate`: add `initial_stop_price REAL` column;
  backfill `initial_stop_price = stop_price` for existing rows (best available
  for legacy data — document the approximation).
- `open_trade(...)`: accept and write `initial_stop` (engine passes the
  fill-derived stop once at entry).
- `update_trade_stops`: keeps overwriting `stop_price` only; never touches
  `initial_stop_price`.
- `bot/broker.py restore_position`: `risk_per_unit` from
  `initial_stop_price` (fallback `stop_price`), so a post-restart scalper
  BE-trail computes R against the *initial* risk.
- `bot/shadow.py`: `_pos_view` and `behavior_profile` use
  `t.get("initial_stop_price") or t.get("stop_price")`.
- `bot/backtest.py _trade_dict`: include `initial_stop` (shadow's profile also
  consumes backtest trade dicts).
- **Test**: trail a stop via `update_trade_stops`, assert `initial_stop_price`
  unchanged; behavior_profile on a BE-trailed loser no longer reports R < −5.

#### Fix 1.3 — cost-aware breakeven
- `bot/strategies/scalper.py:218-223`: for longs `be = entry * (1 + fee + slip)`,
  shorts `be = entry * (1 − fee − slip)` with `fee = CONFIG.costs.fee(kind, maker=False)`,
  `slip = CONFIG.costs.slippage(kind, maker=False)` (stop exits are taker legs;
  the scalper needs kind — take from CONFIG or pass spec into check_exit via
  the position's timeframe/spec; simplest: read CONFIG.costs and infer kind
  from the symbol convention `infer_kind`).
- Precision note: exact BE also owes `entry_fee/qty` and the fee on the exit
  notional; the multiplicative buffer is within a rounding of it and simpler —
  keep the buffer, comment the derivation.
- **Test**: BE-trailed position exiting at `be` realizes ≥ −0.01% notional.

#### Fix 2.3 — exact realized-cash reconciliation
- `bot/trades` schema: add `realized_cash_delta REAL` (migration + backfill
  NULL for legacy rows).
- `bot/broker.py close_position`: return the cash delta it applied
  (`gross − exit_fee`) alongside existing returns (or the split fees).
- `engine._close` writes it at `close_trade`; `close_trade` gains the param.
- `journal.closed_cash_delta_since`: sum
  `COALESCE(realized_cash_delta, pnl + fees/2)` — the `fees/2` term
  approximates the entry fee for legacy rows (standard crash window),
  documented in the docstring.
- **Test**: crash-window simulation — open, write equity point, close, no
  equity write, restore; broker cash must equal the uninterrupted path to the
  cent, for both a trade opened before and after the anchor.

### Phase 2 — engine & infra fidelity (P2)

#### Fix 1.2 — exit ordering in the backtester
- `bot/backtest.py:157-192` reorder to: (a) fill-bar (`cur_bar`) stop/target
  scan when `fill_scan_pending` (its whole range is post-fill, pre-decision),
  (b) `check_exit(ind, i, pos)` — if it fires, exit at `next_open` with
  `exit_idx = i+1` and **do not scan `next_bar`**, (c) otherwise scan
  `next_bar` for stop/target.
- Preserves within-bar conservatism (stop before target) and the live path's
  semantics (engine closes at the closed bar's close).
- **Test**: crafted frame where bar i+1 both gaps through the target and the
  strategy exit fired at bar i — assert the open fill, not the target.
- Re-measure the three windows; expect low-single-digit trade deltas.

#### Fix 3.2 — allocator weekend massacre
- Default `inverse_vol`: compute per-symbol vol on each symbol's **own bars**
  (no alignment needed) — crypto keeps 24/7 bars, forex its trading bars.
- `hrp`: keep the aligned matrix but restrict to common *trading* rows
  (weekday overlap) for the correlation structure only, documented.
- **Test**: crypto+forex frames → inverse-vol weights must use ≥95% of crypto
  bars; a weekend-heavy crypto regime must move weights.

#### Fix 3.3 — asset-relevant lexicon sentiment
- `bot/sentiment.py` lexicon branch: filter headlines by an asset keyword map
  (BTC→bitcoin/btc, ETH→ethereum/ether, SOL→solana, EURUSD→euro/dollar/ECB,
  shared macro keys fed/rates/inflation apply to all); fall back to all
  headlines when nothing matches (Gemini's fallback is right).
- Map lives next to the lexicon; `asset_hint` already flows from
  `spec.display`.
- **Test**: crypto-crash headline vetoes a BTC long but not an EUR/USD long.

#### Fix 4.2 (correctness half) — Kronos forecast stamps
- Derive the future-stamp frequency from the bar spacing actually passed in:
  add `timeframe` (or `freq`) param to `KronosSignalEngine.evaluate`; the
  engine already knows `spec.timeframe` (`TIMEFRAME_SECONDS[tf]` seconds).
  Call sites: `engine._kronos_eval`, tests.
- Keeps the sequential sampling loop (see correction #3 — batching without a
  vendored patch returns one mean path).

#### Fix 2.2-lite — document the account model + gross cap
- `RiskManager.approve`: new gate `gross_notional(open) + new ≤
  risk.max_gross_leverage × equity` (default 1.0× — matches today's implicit
  bound, now explicit and enforced across mixed timeframes).
- README/RESEARCH §accounting: state plainly — margin-style PnL settlement,
  principal not escrowed, no liquidation modeling, shorts imply perp-style
  execution; the cap is the enforced bound.
- Full principal/margin accounting (spot cash deduction, margin reservation,
  margin calls) is a **separate scope decision** — it changes equity/cash
  semantics, restart reconciliation, dashboard, and many tests. Do not
  bundle it here.

#### Fix 4.1 — freshness-bounded cache reuse
- `_disk_cache_path`/`fetch_history`: glob `{safe}_{tf}_{days}d_*.parquet`,
  reuse the newest if its mtime is ≤ `CACHE_FRESHNESS_HOURS` (default 24,
  env-overridable) — bounded staleness for rolling windows instead of a 14-day
  free-for-all; log the reuse and age. Pinned `start/end` windows keep exact
  byte-identical semantics.

### Phase 3 — statistics & perf polish (P3)

#### Fix 3.1 — moment-aware DSR (done correctly)
- `deflated_sharpe(sharpes, n_obs, bars_per_year, returns=None)`: when a
  returns series is supplied, `SR_p = best / sqrt(bars_per_year)`;
  `var_p = (1 − skew·SR_p + (kurt−1)/4·SR_p²)/(n_obs−1)`;
  `se = sqrt(bars_per_year · var_p)`. No returns → current normal SE, and say
  so in the returned dict (`se_model: "normal"|"moments"`).
- Caller (`main.py cmd_validate`): pass the primary run's equity-curve returns;
  docstring notes the approximation (trial sharpes come from sibling configs).
- Optionally the same correction for `min_trl` later.

#### Fix 4.2 (perf half, optional) — Kronos sampling cost
- Options, in preference order: (a) lower `sample_count` (30 → 10–15) and
  measure the P(up)/dispersion stability loss; (b) keep 30 but make the IC
  ledger tolerate fewer; (c) patch the vendored `kronos.py` behind a flag to
  return per-sample paths (`return_samples=True`) so one batched call yields
  30 paths — MIT-licensed, allowed, but record the divergence in
  `models/kronos/README` notes. No change unless the engine cadence actually
  hurts (kronos is throttled to every 4 closed bars and is a non-voter).

### Regeneration & docs (same commits as the fixes)

- BACKTESTS.md: re-measure every turtle table row and the parity note
  (10 → ~115 trades on BTC 1h 365d; exit mix now 49 channel / 65 stop / 1 EOB).
- README strategy table + RESEARCH.md §2.5: no behavior claims change except
  turtle's exit (now truly the prior-10-bar channel) and the new accounting
  paragraph (2.2-lite).
- JUDGE_REPORT.md items stay as-is (no overlap with these flaws except item 12,
  already fixed).

### Explicit non-goals / risks

- **Do not** batch Kronos via `sample_count` as Gemini proposed — it returns
  a mean path; P(up) and dispersion would be destroyed.
- **Do not** "fix" 2.3 with `pnl + entry_fee` alone — it overstates the
  open-after-anchor edge case; the realized-cash column is the honest version.
- **Do not** implement spot-principal accounting inside this plan (2.2) —
  it is an account-model redesign; ship the doc + gross cap and decide
  separately.
- Expect BACKTESTS.md turtle numbers to move a lot after 1.1 — that is the
  point (the old numbers were produced by a strategy with a dead exit).

---

## IMPLEMENTATION LOG (2026-09-09) — all CONFIRMED flaws fixed

The seven CONFIRMED flaws are fixed; the PARTIAL ones (1.2 ordering, 2.2
account model, 4.1 cache freshness, 4.2 Kronos perf) remain unfixed by scope
decision above. 121/121 tests green (114 prior + 7 new regression tests).

| ID | Status | What shipped | Where |
|----|--------|--------------|-------|
| 1.1 | **FIXED** | Exit channel reads the PRIOR 10-bar channel (`shift=1`), same convention as the entry breakout; vacuous test replaced with a firing-assert + end-to-end exit-mix check | `bot/strategies/turtle.py`, `tests/test_bot.py` |
| 1.3 | **FIXED** | Breakeven stop = entry ± (taker fee + slippage), inferred per-kind from the symbol; short side mirrored | `bot/strategies/scalper.py`, `tests/test_bot.py` |
| 2.1 | **FIXED** | `initial_stop_price` column latched at fill (`record_fill`), never trailed over; broker `Position.initial_stop`; `restore_position`/`_pos_view`/`behavior_profile` R math reads the initial stop; migration backfills legacy rows from their final stop (documented approximation) | `bot/journal.py`, `bot/broker.py`, `bot/shadow.py`, `bot/engine.py`, `bot/backtest.py` |
| 2.3 | **FIXED** | `realized_cash_delta` + `entry_fee` columns recorded at close; `closed_cash_delta_since` is anchor-aware (entry-before-anchor → close-event delta; entry-after-anchor → plain pnl); engine passes the split values; both crash windows verified to the cent against the uninterrupted broker path | `bot/journal.py`, `bot/engine.py`, `tests/test_bot.py` |
| 3.1 | **FIXED** | Moment-aware DSR: de-annualize best → per-period SR → Merton/OPM variance with sample skew/kurtosis → re-annualize the SE; `se_model` field says which was used; `cmd_validate` passes the primary run's equity returns; no-returns path keeps the normal SE | `bot/validation.py`, `main.py`, `tests/test_bot.py` |
| 3.2 | **FIXED** | `inverse_vol` computes each symbol's vol on its OWN bars (`per_symbol_vols`) — no timestamp alignment, crypto keeps weekends; HRP keeps the aligned matrix (correlations need common observations) with the tradeoff documented; weights identical to skfolio's InverseVolatility on aligned-only books | `bot/allocator.py`, `tests/test_bot.py` |
| 3.3 | **FIXED** | Lexicon branch filters headlines by asset keyword map (BTC/ETH/SOL/EUR/GBP + shared crypto/macro keys), with whole-batch fallback when nothing matches; LLM branch unchanged (it already received `asset_hint`) | `bot/sentiment.py`, `tests/test_bot.py` |

Post-fix re-measurement (BACKTESTS.md Round 8, same cached windows as the
audit): BTC 1h turtle −5.0%/115 trades (was +1.5%/11 — the dead-exit artifact),
SOL +10.6%/PF 1.68 (edge survives with real exits), ETH +0.5%/PF 1.02 (coin
flip), scalper 15m moves little (the fix removes a guaranteed leak, it does
not create an edge). Exit-mix proof the fix is live: 108 of 242 turtle exits
are now opposite-channel (structurally 0 before).

Two side effects of the fixes worth recording:
- `oos_trade_distribution` now reports `kept_trades_unique` — with short
  holding periods, the same trade legitimately appears in several overlapping
  OOS paths, so the per-path counts no longer sum to total_trades; the unique
  count restores the partition identity.
- The rule-adherence replay now classifies some fabricated "discretionary"
  closes as LATE instead of RULE BREAK — with a live exit signal, "closed
  later than the strategy fired" is the accurate description.


---

# Self-audit passes

*The repo's own audit sweeps: what each pass looked for and what it found.  
Folded in from `AUDIT.md` on 2026-09-19.*

## Project audit and implemented plan — 2026-09-18, second pass 2026-09-19

The first pass fixed account-reset isolation, reset atomicity, position
sizing, and broker/journal divergence after persistence errors, and listed
four priorities it had not implemented. The second pass re-verified every
claim in this document against the source and closed those four: durable
risk state and event-ordered cash recovery across restart, database-level
ownership of each book across processes, and a reproducible locked
environment that CI actually exercises. One finding the first pass reported
as fixed was not fully fixed — see the second-pass table.

### Scope and evidence

Reviewed the current working tree, including the substantial changes already
present when this audit began. Those edits were preserved; this pass did not
commit, reset, or stage them. The implementation changes are in
`bot/dashboard.py`, `bot/journal.py`, `bot/engine.py`, `bot/risk.py`,
`tests/test_bot.py`, and the new `tests/test_audit_regressions.py`.

Used the codebase-memory graph for architecture, symbol discovery, and
bidirectional traces, then checked the relevant source. The initial full
index generation was `2026-09-18T15:39:31Z`, project
`Users-varun-Downloads-Varun-Algo`. Coverage reported no recorded issue for
the selected source paths. Reported gaps within `bot/` and `tests/` were
generated cache directories. This is best-effort coverage, not a proof that
every runtime path is represented. For example, dynamic risk calls required
source verification beyond the graph's direct caller results.

The main review covered broker accounting, risk sizing, engine persistence and
restart paths, journal transactions, dashboard account/lifecycle operations,
data-cache behavior, and CI/test configuration. Backtest ordering, validation,
and Strategy Lab limits received targeted source checks; the existing strategy,
causality, and accounting tests were executed. Vendored model/JavaScript code,
live exchange behavior, pretrained weights, dependency vulnerability feeds,
and historical profitability were not independently audited.

Baseline: **187 passed, 4 failed**; lint passed. The four failures were in
exchange cooldown and disk-cache tests. Local Python is **3.14.1**; the CI
matrix's Python 3.11–3.13 environments were not run locally.

### Findings fixed in the first pass

| Priority | Finding and evidence | Implemented behavior |
| --- | --- | --- |
| P1 | `api_account_reset` deleted every row in the shared database after stopping only the standard engine. HFT positions, equity, decisions, and transactions could disappear while that engine continued running. | Reset deletes only `mode='paper'` rows. HFT/demo data and shared chat remain. Dashboard reset text now states this scope. |
| P1 | Stop cleared `_engine` before its thread finished. A second reset saw `not_running` and could delete state during an in-flight cycle. Engine construction could also overlap reset. | Stop reports the remaining thread/construction state. Reset rechecks both under the lifecycle lock and holds that lock through its transaction. Startup retains its construction marker until thread publication. |
| P1 | Reset used a best-effort WAL checkpoint plus file copy, then committed deletion, fresh equity, and the reset ledger separately. Backup or insert failure could leave an incomplete account. | `Journal.reset_account` reserves the SQLite writer, makes and checks a SQLite backup through a separate reader, then deletes and inserts the new balance/ledger in one transaction. Failure leaves the original account intact. |
| P1 | `_fill_entry` aborted the journal row when `record_fill` failed, but retained the simulated position and fee. `_close` removed the position and settled cash before a failing journal close. | Entry persistence failure restores cash/fee/P&L totals and removes the simulated fill. Exit persistence failure restores the position and totals so its exit can be retried. Success bookkeeping follows the journal commit. *Incomplete:* the triangular arb settled outside this pattern — see the second-pass table.* |
| P1 | `size_position` used nearest rounding despite promising to round down. At 10,000 equity, 1,600 price and a 60 stop, it returned 1.5625 rounded to 2 shares: 3,200 notional against a 2,500 cap and 120 stop risk against a 100 budget. | Whole-unit and six-decimal quantities round down. Invalid numeric sizing inputs return zero. Approval rejects non-finite equity, confidence, target, and gross exposure rather than letting NaN comparisons bypass gates. |
| P2 | Four baseline tests assumed unprefixed cache filenames or bypassing an explicitly selected exchange's cooldown. A separate sizing test explicitly expected upward rounding. | Updated fixtures/assertions to preserve market-kind cache isolation, require cooldown expiry before a probe, and expect downward sizing. Production cache/cooldown behavior was retained. |

### Plan chosen after the first pass

1. Protect account reset against cross-book deletion, unfinished engine work,
   incomplete backups, and partial reset transactions. **Implemented.**
2. Enforce finite risk inputs and downward quantity sizing. **Implemented.**
3. Keep the paper broker and journal aligned when fill/close writes fail.
   **Implemented for recoverable exceptions in the running process.**
4. Repair obsolete test assumptions, add targeted regressions, and run the
   full suite, lint, dependency checks, and CLI smoke checks. **Implemented.**

The regressions use temporary databases and mocked/local inputs. They include
real SQLite trigger failures to verify transaction rollback, uncheckpointed
WAL backup content, mode isolation, repeat reset attempts, numeric boundaries,
entry rollback, exit retry, and the actual ASGI bearer-token middleware stack.
No operator account reset or trading-engine start was performed.

### Second pass — 2026-09-19

Every finding in the table above was re-verified against the source rather
than against the claim. Rows 1, 2, 3, 5 and 6 hold as written. Row 4 did not:
its two named paths (`_fill_entry`, `_close`) are correct, but the audit's
wider framing — no remaining path mutating broker state without rollback —
was untrue for the triangular arb. The four priorities listed as remaining
have since been implemented; the two P1s were already done when this pass
began and are re-verified below, the two P2s were completed in this pass.

| Priority | Finding and evidence | Implemented behavior |
| --- | --- | --- |
| P1 | `_settle_tri_pending` journaled the arb with neither `entry_fee` nor `realized_cash_delta`, so `close_trade` fell back to `pnl + fees/2` while the broker moved by `pnl` alone. On a 1,000 notional at 30bp the ledger recorded +2.25 more than the broker held; a restart before the next checkpoint replayed the inflated number. Writing the row open-then-close also left a window where process death stranded an `OPEN` TRI-ETH row — a symbol in no watchlist, so nothing could ever mark or close it, and every later restart restored the ghost. | New `Journal.record_round_trip` writes the CLOSED row, its single cash event, the Account-tab row and the equity anchor in ONE transaction, with the exact cash delta separated from the rounded display P&L. The broker moves only after that commit, so a failure anywhere leaves ledger and broker on the pre-arb number and nothing half-written. |
| P2 | Ownership of a book was an in-process variable. The dashboard's guards read `_engine`/`_engine_thread`, all `None` when the engine runs as a separate `main.py run` process, so a deposit committed while that engine held cash in memory was erased by its next checkpoint — reproduced as a silently vanished $500 whose `transactions` row survived. | A `book_owner` row per book is the lease. `claim_book`/`heartbeat_book`/`release_book` take it under `BEGIN IMMEDIATE`; the engine claims it in `own_book()` when it starts trading (CLI loop, single cycle, and both dashboard threads) and its checkpoints and closes carry the token, which is re-checked inside the same transaction. Deposits, withdrawals, resets, the market switch's offline force-close and a second engine start are refused with 409 while any process owns the book. A dead owner's pid frees it at once; a silent one expires after 15 minutes. |
| P2 | `adjust_account` and `reset_account` were the only writers without `@_retry_busy`, and `_adjust_account` mapped only `ValueError`, so cross-process contention surfaced as a 500. | Both retry, and the dashboard maps a surviving busy error to 503 and an ownership refusal to 409. |
| P2 | The `locked` CI job installed only `requirements.lock` and then ran `ruff`, which the lock does not contain — the job failed on every run. It also ran Python 3.12 against a lock compiled for 3.14, so the pinned set was not the resolved set. | The job pins `ruff==0.16.5` alongside the lock, runs the lock's own interpreter, and adds `pip check`. `make lock` records the command that reproduces the file. |

The ownership work was then reviewed against its own failure modes, which
found five more defects in it; all are fixed and covered by regressions:

- The lease ANDed the owner's pid with heartbeat freshness, so an engine on
  an interval longer than the 15-minute TTL — or a laptop that slept — lost
  its own book: its checkpoint was rejected, the dashboard's guard let a
  deposit through, and another process could claim the book. On the owning
  host the pid alone now decides; the heartbeat only speaks for a host whose
  pids cannot be probed.
- `record_round_trip` writes an equity anchor but took no lease, leaving the
  one unguarded path into the state the feature protects.
- `@_retry_busy` on `adjust_account` replayed `on_adjust`, which shifts the
  caller's in-memory risk baselines before the commit: a lost writer race
  applied the delta twice in memory against one row in the database. Only
  the callback-free form retries now.
- A failed thread start in `_spawn_hft_engine` published the engine and
  leaked the lease to a live pid, making the HFT book permanently
  unstartable and unresettable. It now mirrors the paper path's rollback.
- The arb anchored equity on `broker.equity({})`, which marks every open
  position at its entry price — a zero-unrealized point that stayed in the
  curve and in `stats()`'s drawdown scan. It uses the cycle's marks.

Smaller ones from the same review: `owner_token` now distinguishes "mine",
`NO_OWNER` ("nobody's") and None ("not a trading write"), so the market
switch's offline force-close asserts an unowned book inside each close's own
transaction instead of checking beforehand; `close_trade` guards the book its
row belongs to rather than the caller's argument; a lease lost mid-cycle is
re-raised instead of being filed as a degraded cycle; `BookOwnedError` no
longer subclasses `RuntimeError` (a handler for close failures was reporting
it as "retry from the Portfolio tab"); the CLI prints the refusal and exits 1
instead of a traceback; and a failing auto-resume no longer aborts uvicorn
startup, which would leave no UI to explain itself.

#### P1 — Persist risk state across restart. **Implemented.**

`RiskManager` takes `state_db_path`/`mode` and restores in its constructor,
before `_restore_positions` and before any cycle can approve an entry.
Verified against the acceptance criteria: a same-day restart keeps a latched
halt after equity recovers; a UTC rollover clears only the daily controls
while `peak_equity`, `dd_risk_scale` and cooldowns survive; `adjust_cash_flow`
shifts both baselines without erasing the trading loss; `risk_state` is keyed
by mode, so the books are independent; backtests construct a memory-only
manager and write nothing. An unreadable checkpoint is preserved for
diagnosis and vetoes new entries rather than being overwritten with defaults.

#### P1 — Make cash recovery independent of timestamp windows. **Implemented.**

`_recover_cash` anchors on the last equity row's `cash_event_id` and sums
`cash_events` by id, so clock rollback and same-second events are immaterial.
The audit's own reproduction now returns 9,999.89995 before and after
restart, stable across repeats, separately for paper and HFT, with entry
fees on still-open trades included. Databases written before `cash_events`
existed keep the old timestamp fallback for their untracked legs and switch
to exact cursors at the next checkpoint; the one case that cannot be
reconstructed — a legacy event in the same second as its anchor — is
reported through `Journal.last_error` rather than guessed at.

#### P2 — Complete account-operation and replay persistence. **Implemented.**

`adjust_account` recovers the balance inside its own `BEGIN IMMEDIATE`, so
the engine-off read/compute/write is one unit against a SQLite RESERVED
lock — effective across `Journal` instances and across processes — and the
cash event, equity checkpoint, typed ledger row and risk baselines commit
together. The engine-on path snapshots and restores the risk fields its
callback mutates and moves broker cash only after the commit. Ownership of a
standalone CLI engine is now enforced at the database level (above).
`decision_bar_ts` is written on every live fill path, including resting
maker/limit fills and the HFT book, and recovery keys the replay window and
the bars-held clock off it. A parity test drives the same bars continuously
and across a restart and asserts an identical exit bar, exit reason and
bars-held trace.

#### P2 — Make dependency reproducibility claims precise. **Implemented.**

`requirements.lock` is a complete `pip-compile` resolution of the direct and
transitive core/dev environment, exercised by a dedicated `locked` CI job
while the Python matrix keeps installing the floor file for upgrade drift.
The job's breakage described above is fixed. `pip check` reports no broken
requirements; that is a consistency check, not a vulnerability verdict.

### Remaining priorities

None from this audit's list. Two limits are disclosed rather than fixed:

- A legacy database's same-second event and anchor cannot be ordered after
  the fact. Recovery reports the ambiguity instead of inventing a number.
- Cross-host ownership rests on the heartbeat alone (a 15-minute lease),
  because a remote pid cannot be probed. Sharing one SQLite file across
  hosts was never a supported deployment.

### Validation

Second pass, on the current tree:

- `python3 -m pytest tests/ -q`: **278 passed** (258 before this pass, plus
  16 ownership cases, 3 arb-settlement cases and the restart parity case).
  One Starlette/httpx TestClient deprecation warning.
- Each new regression was confirmed to FAIL against the pre-fix behavior
  before being kept, by reverting the fix and re-running it: the parity test
  against a restore that ignores `decision_bar_ts`, the arb tests against the
  `pnl + fees/2` fallback and the `equity({})` anchor, and the lease tests
  against the stale-heartbeat rule, the unguarded round trip and the blind
  retry.
- `python3 -m ruff check bot main.py config.py run_battery.py tests scripts`:
  **passed**.
- `git diff --check`: **passed**.
- All **16 CLI subcommand `--help` checks passed**.
- `python3 -m pip check`: **no broken requirements found**.
- `requirements.lock` was re-resolved into a scratch file to confirm it is a
  genuine, installable `pip-compile` output; the committed pins were kept
  rather than bumped, since upgrading pytest/skfolio is not an audit fix.

Historical backtest reports were not regenerated. Flooring quantities can
change future backtest results, especially for whole-share positions; the
intended behavioral change is adherence to the configured sizing budget.
Live exchange behavior, vendored model code, pretrained weights, dependency
vulnerability feeds and historical profitability remain outside this audit.


---

# Milestones

*The build order, and what each milestone actually shipped.  
Folded in from `MILESTONES.md` on 2026-09-19.*

## Milestones A–C — Execution Plan (2026-09-10)

Source of requirements: `trade-bot-next-goals.md` (next-goal set). This file is the
orchestrator's cross-check + wave plan. Status markers update as waves land.

### Cross-check: FLAW_VALIDATION.md claims vs the code (audited 2026-09-10)

Baseline before any work: **121/121 tests green**, ruff clean, working tree clean.

| Claim in FLAW_VALIDATION | Verdict on the code | Evidence |
|---|---|---|
| 1.1 turtle exit fixed | **TRUE** | `bot/strategies/turtle.py:87-98` reads `don_exit_low/up` with `shift=1`; exit-mix test fires |
| 1.3 cost-aware BE | **TRUE** | `bot/strategies/scalper.py:218-233` buffers by taker fee + slippage per kind |
| 2.1 initial_stop latched | **TRUE** | `journal._migrate`/`record_fill` COALESCE-latch; `broker.restore_position` prefers it |
| 2.3 exact cash reconciliation | **TRUE** | `realized_cash_delta` + `entry_fee` columns; anchor-aware `closed_cash_delta_since` (`journal.py:469-503`); crash-window test to the cent |
| 3.1 moment-aware DSR | **TRUE** | `validation.py:269-342` de-annualizes → moments → re-annualizes; `se_model` field |
| 3.2 per-symbol-vol allocator | **TRUE** | `allocator.per_symbol_vols` on each symbol's own bars |
| 3.3 asset-filtered lexicon | **TRUE** | `sentiment.py:50-92` `_ASSET_KEYWORDS` + whole-batch fallback |
| Fix 1.2 exit ordering (PARTIAL, Phase 2) | **NOT LANDED** | `backtest.py` still scans `next_bar` stop/target BEFORE `check_exit` — a same-bar stop/target can win over an exit order already filled at that bar's open |
| Fix 2.2-lite gross cap (pulled forward by the goals file) | **NOT LANDED** | `RiskConfig` has no `max_gross_leverage`; `approve()` has no gross-notional gate — the ~1× bound is still only implicit (25% × 4) |
| Fix 4.2 correctness half (Kronos stamps) | **NOT LANDED** | `kronos_signal.py:278` still `pd.infer_freq(...) or "1h"`; engine passes no timeframe |
| Fix 4.1 cache freshness | **NOT LANDED** | rolling windows reuse only the same-day stamp |
| Manual "pause all trading" | **ABSENT** | no CLI command, no dashboard control |
| Kill-switch documentation | **ABSENT** | README has one bullet; dashboard has no explanation copy |
| Phase 0 pinned baselines | **NOT DONE** | Round 8 used rolling cached windows, not pinned `--start/--end` |

So Milestone A = the four unlanded fixes + pause control + docs + pinned
regeneration. The already-landed Phase-1 work is NOT redone.

### Milestone A — waves, ownership, contracts

**Phase 0 (orchestrator, before any code edit):** run 7 pinned backtests on the
pristine tree and store JSONs in `data/results/pinned_before/` (gitignored;
the numbers themselves go into BACKTESTS.md):

| Run | Window (pinned) | Strategy |
|---|---|---|
| BTC/USDT 1h | 2025-09-03 → 2026-09-04 | turtle_trend |
| ETH/USDT 1h | 2026-01-04 → 2026-09-04 | turtle_trend |
| SOL/USDT 1h | 2026-01-10 → 2026-09-04 | turtle_trend |
| BTC/USDT 4h | 2026-03-09 → 2026-09-06 | connors_meanrev |
| ETH/USDT 4h | 2026-03-09 → 2026-09-06 | connors_meanrev |
| BTC/USDT 15m | 2026-03-09 → 2026-09-06 | vwap_scalper |
| ETH/USDT 15m | 2026-01-05 → 2026-09-05 | vwap_scalper |

(Windows match the Round-8 cached frames so continuity is preserved.)

**Wave W1 (agent: risk-cap + pause + docs).** Owns `config.py`, `bot/risk.py`,
`bot/engine.py`, `main.py`, `bot/dashboard.py`, `README.md`, appends tests.
- `RiskConfig.max_gross_leverage: float = 1.0`.
- `RiskManager.approve(..., open_gross_notional: float = 0.0)` — new gate after
  sizing: `open_gross_notional + qty*price > max_gross_leverage × equity` →
  refuse with a clear reason. Engine computes gross from
  `broker.positions_snapshot()` marked at `_last_good_price` (fallback entry
  price); backtest call site passes its broker's gross the same way.
- Pause: flag file `data/trading_paused.json` `{"paused": bool, "ts", "note"}`
  written atomically (tmp+replace, `save_watchlist` pattern). `RiskManager.paused`
  (in-memory; checked first in `approve`, reason states "blocks new entries
  only — open positions are still managed"). Engine reads the flag ONCE per
  cycle (no file IO inside backtests). Missing file → not paused; corrupt →
  quarantine + treat as PAUSED (fail-safe direction) with a loud message.
- CLI: `python3 main.py pause [note]` / `python3 main.py resume`; `status`
  shows pause state. Dashboard: `POST /api/trading/pause|resume`,
  `/api/engine/status` gains `paused`, button + banner next to engine
  start/stop. UI copy states explicitly: blocks NEW entries only, does not
  force-close open positions.
- README: "What the daily kill switch does and does not do" section + pause
  control; same summary copy on the dashboard.

**Wave W3 (agent: Kronos stamps + cache freshness; parallel to W1).** Owns
`bot/kronos_signal.py`, `bot/data.py`; in `bot/engine.py` may touch ONLY the
`self.kronos.evaluate(...)` call inside `_kronos_eval` (adds
`timeframe=spec.timeframe`).
- `KronosSignalEngine.evaluate(df, horizon=24, timeframe=None)`: derive future
  stamps from `TIMEFRAME_SECONDS[timeframe]` via a pure helper
  `_future_index(last_ts, horizon, timeframe)` (unit-testable without the
  model); `timeframe=None` keeps the `infer_freq` fallback. Keep the sequential
  sampling loop (FLAW_VALIDATION correction #3 — batching returns a mean path).
- `fetch_history` rolling reuse: glob `{safe}_{tf}_{days}d_*.parquet` (and the
  `{start}_now_*` form), newest by mtime, reuse when age ≤
  `CACHE_FRESHNESS_HOURS` (default 24, env-overridable, read in `data.py`);
  log reuse + age. **Pinned `--start/--end` keeps exact-name, byte-identical
  semantics — the glob must never touch it.**

**Interface contract W1↔W2/W3:** `approve()`'s new param is optional
positional-last keyword; nothing else changes signature. `RiskManager.paused`
is a plain attribute. No other module reads the pause flag directly.

**Intermediate check (orchestrator, after W1+W3):** re-run the 7 pinned
backtests into `data/results/pinned_mid/`. MUST be identical to `pinned_before`
(same cached parquet files, same stats) — proves W1/W3 are numerically inert
for backtests; any drift = a bug to fix before W2.

**Wave W2 (agent: backtest exit ordering + regeneration; runs alone).** Owns
`bot/backtest.py` (scan-order block only — NOT the `approve` call), BACKTESTS.md,
appends tests.
- Reorder per FLAW_VALIDATION Fix 1.2: (a) fill-bar (`cur_bar`) scan when
  `fill_scan_pending`; (b) `check_exit(ind, i, pos)` — apply any stop-trail
  update, and if it fires, exit at `next_open` with `exit_idx = i+1` and DO
  NOT scan `next_bar`; (c) otherwise scan `next_bar` stop/target (with the
  newly-trailed stop active — live parity: the engine trails at bar i's close,
  the next bar's scan uses the new level).
- Test: crafted frame where bar i+1 gaps through the target AND the strategy
  exit fired at bar i → assert the open fill wins, not "take profit".
- Re-run the 7 pinned windows into `data/results/pinned_after/`, diff all three
  sets, write BACKTESTS.md "Round 9" (before/mid/after table, exit mix, and
  the note that only Fix 1.2 moved numbers). Expected: low-single-digit trade
  deltas (audit measured conflicting exits 4/115 BTC, 2/70 ETH, 1/57 SOL).

**Non-vacuity protocol (every agent):** each new test must FAIL against the
pre-fix code — prove by temporarily reverting your own edit in-place (edit
back → run test → must fail → re-apply). Do NOT `git stash` shared files.
Agents never run `git commit`/`git push` (orchestrator commits at milestone
boundaries); never touch `data/trading.db`; tests use temp paths only.

### Milestone B — India market + Forex/India toggle (after A is green)

- **B1 (core):** `config.py` `SPECS_INDIA` (`^NSEI` + RELIANCE/TCS/HDFCBANK/
  INFY/ICICIBANK `.NS`), `MARKET_MODE` (`"forex"` default | `"india"`) with
  atomic persisted mode file `data/market_mode.json` + `get/set_market_mode()`;
  `infer_kind` learns `.NS`/`^` → `"india"`; `VALID_KINDS` extended. One active
  book at a time; the two lists never merge.
- `CostConfig` India branch, **rates verified against a published broker charge
  sheet via WebFetch and cited in a comment** (they change): brokerage (flat
  discount-broker), STT (delivery vs intraday differ), exchange txn, SEBI,
  stamp duty (buy-side), GST on brokerage+txn+sebi. `fee()`/`slippage()` branch
  on `kind == "india"` explicitly; leg-aware (stamp buy-only) — `fee(kind,
  maker, side)`, `side=None` → conservative max. Slippage for liquid large caps.
- `risk.size_position`: whole shares — the `round(qty, 0)` branch covers
  `"india"`.
- NSE calendar: 09:15–15:30 IST Mon–Fri + 2026 holiday list (verified, cited,
  "update annually" comment) in a new `bot/calendar.py`; session guard gates
  NEW entries only (exits/replays always run); `bars_per_year` kind-aware
  (~245 sessions). Note: the whole NSE session maps inside one UTC day, so
  the kill-switch UTC-day semantics already align — document it.
- `data.py`: india fetch via the yahoo path (`.NS`/`^` tickers, 4h resample,
  1d direct); Fix 3.2 (per-symbol vols) is already landed — the prerequisite.
- Acceptance: full 90-day pinned India backtest on real yfinance data with the
  modeled costs, BACKTESTS.md "India" section. (yfinance intraday limits: 15m
  max 60d — the India universe uses 1h/4h/1d specs so 90d is fetchable.)
- **B2 (UI/docs):** dashboard toggle at the top — **On = Forex active,
  Off = India active**; switching blocked while open paper positions exist
  (explicit-confirm path force-closes at last marks first — never orphans a
  position); mode persisted so restarts don't flip markets; journal/dashboard
  filtering + README/BACKTESTS describe the `india` kind.

### Milestone C — SSRN-grounded strategies (after B is green)

New separate modules; turtle/connors/scalper untouched. Each: docstring citing
the paper(s), params in `StrategyParams`, `BACKTESTS.md` entry, unit tests,
registered in `get_strategy`, **disabled by default** (not in any SPECS list)
until its own measured entry survives realistic costs.

- **C1 India time-series momentum** (`bot/strategies/ts_momentum.py`, 1d bars):
  MVP path (a) from the goals file — absolute/time-series momentum per symbol
  (fits `BaseStrategy`; no cross-sectional ranking infrastructure). Grounded in
  SSRN 3345280 (momentum convexity/alpha in India), 3510433 (long-only
  systematic momentum in India), 4587697 (52-week-high momentum, India).
  Long-only (delivery equities — no shorts). The (b) cross-sectional decile
  portfolio is a deliberately deferred bigger lift, per the goals file.
- **C2 Forex regime-conditioned mean reversion**
  (`bot/strategies/fx_regime_meanrev.py`, 1h): single-pair MVP from SSRN
  6087107 — z-score deviation + regime gate (reuse the repo's AR(1) half-life
  machinery) with ATR stop/time stop. True cointegration pairs trading
  (SSRN 4771108) is the stretch goal, NOT in scope, flagged in the docstring.

### Rules for all waves

Paper mode only — anything that only makes sense for live trading gets flagged,
not built. Full suite green (≥121) + ruff clean at every boundary. Old turtle
numbers (pre-Round-8) are never carried forward anywhere.

