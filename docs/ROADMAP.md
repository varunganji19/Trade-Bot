# Roadmap: from "trading bot" to an honest strategy-validation platform

*Plan only — nothing here is implemented yet. Written 2026-10-01 from the
mentor and VC reviews. Each item has a deliverable, an acceptance check, an
effort estimate and an owner: **Claude** (code and docs I can build in this
repo) or **You** (decisions, outreach, money, people — things code cannot do).*

## The one decision that shapes everything

Both reviews point the same way: the project's strongest asset is its
**validation discipline** (walk-forward promotion gate, cost realism,
live-vs-backtest parity, overfitting statistics, rule-adherence audits), not
the bot. This plan assumes the positioning:

> **"A trading research platform that refuses to fool itself — and the
> evidence of what it proved and disproved."**

Everything below either sharpens that story (mentor track) or turns it into
something a VC can evaluate (VC track). If you prefer to stay a "trading bot"
project, the mentor track still applies; the VC track's product items (V3)
would be dropped.

**Decisions needed from you before Phase 1** (marked ◆ below):
1. ◆ Positioning as above (yes / no).
2. ◆ Raising the promotion bar (M3) will very likely demote today's voters,
   possibly leaving both books with nothing allowed to trade. Accept that?
3. ◆ Licence: the repo is MIT. Fine for an open project; for a business,
   consider open core (keep MIT, sell hosted service) — decide before V3.
4. ◆ Real-money live trading (V6): only if you choose to, only after testnet,
   and you place and fund it yourself.

---

## Phase 0 — Demo-ready (2–3 days, before the mentor meeting)

| # | Change | Deliverable | Done when | Owner | Effort |
|---|---|---|---|---|---|
| 0.1 | Clean journal | Reset the standard paper account (backup kept); leave the 898 demo rows badged | Overview headline numbers agree with each other | You (one click), Claude verifies | 15 min |
| 0.2 | Fast book labelled honestly | Rename "HFT" to "Fast book (experimental)" everywhere user-facing; note that its only voter is unproven | No UI or README text calls it HFT or implies an edge | Claude | 0.5 day |
| 0.3 | Demo storyline | 6-slide outline + talk track: the problem (overfitting), the platform, live demo path, what was disproved, what's next, limitations | You can give the demo in 7 minutes without reading | Claude drafts, You rehearse | 0.5 day |
| 0.4 | README top rewritten | First screen states the positioning, the honest headline result, and the 5-minute demo | A newcomer understands the project in 60 seconds | Claude | 0.5 day |

---

## Track M — Mentor changes

### M1. Narrative: lead with what's true
- **Deliverable:** README restructured as *Problem → Platform → Evidence →
  Results (including failures) → Limitations*. Marketing-style claims removed.
- **Done when:** every number in the README links to the artifact or test that
  produces it.
- **Owner/Effort:** Claude, 1 day.

### M2. Cut scope, keep depth
- **Deliverable:**
  - Chatbot and Kronos moved under an "Extras" section in the UI and README
    (code kept, still tested), out of the main demo path.
  - Fast book kept but marked experimental (0.2).
  - **Docs consolidated** from seven large files (≈235 KB) into four:
    `README.md` (overview), `docs/METHODOLOGY.md` (how evidence is produced),
    `docs/RESULTS.md` (every strategy and experiment with verdicts),
    `CHANGELOG.md` (dated changes). HISTORY.md (82 KB) and old round-by-round
    narratives move to `docs/archive/` unchanged.
- **Done when:** a reader finds any result in ≤ 2 clicks; no fact is stated in
  two places.
- **Owner/Effort:** Claude, 2 days. ◆ You approve the archive move.

### M3. Raise the statistical bar
- **Deliverables:**
  1. **Longer, multi-regime evidence.** Standard-book battery on 2 years of
     1h/4h data (BTC, ETH, SOL, BNB, XRP + EUR/USD, GBP/USD), split into
     labelled regimes (trend up / trend down / range, by 200-day slope and
     ADX). Fast book on 90+ days and 15 markets (the harness already exists).
  2. **Confidence intervals.** Block-bootstrap 90% intervals for profit factor
     and Sharpe on every verdict; the dashboard shows the interval, not just
     the point.
  3. **Stricter gate.** Promote only when the *lower* bound of the PF
     interval is ≥ 1.0 and there are ≥ 100 out-of-sample trades over ≥ 3
     regimes (today: median PF ≥ 1.0 over ≥ 30 trades). ◆
  4. **Experiment registry.** Every variant ever run (including today's
     9 + 9 + 9 fade settings) is recorded in `data/experiments.jsonl` with its
     parameters, date and result. The Deflated Sharpe calculation reads the
     trial count from it automatically, so selection bias is always priced in.
  5. **Pre-registration.** Experiments are declared in a small YAML file
     *before* they run (variants, markets, split); the runner refuses
     anything undeclared and writes results back next to the declaration.
- **Done when:** `make evidence` regenerates every verdict, interval and the
  registry from scratch on a clean checkout.
- **Owner/Effort:** Claude, 5–6 days.

### M4. Data hygiene
- **Deliverable:** a consistency check that runs every poll: realized P&L +
  net deposits + unrealized must reconcile with equity within a tolerance;
  if not, the dashboard shows a "ledger inconsistent" banner naming the gap
  (today's legacy mismatch would have been flagged automatically).
- **Done when:** a test injects a mismatched journal and the banner appears.
- **Owner/Effort:** Claude, 1 day.

### M5. Code a newcomer can read
- **Deliverables:**
  - Split the three largest files along their existing seams, **no behaviour
    change**:
    - `dashboard.py` (1,696 lines) into routers: `api/standard`, `api/fast`,
      `api/lab`, `api/evidence`, `api/account`, plus `engines.py` for
      start/stop plumbing.
    - `journal.py` (1,096) into `schema`, `ledger` (cash/equity/leases),
      `trades`, `reads`.
    - `engine.py` (1,067) into the cycle loop and the position manager.
  - Target ≤ ~500 lines per file.
  - **Comment policy:** comments explain *why the code is this way now*;
    "this used to…" incident narratives move to CHANGELOG/git history.
  - Source-text regression tests that pin exact strings are rewritten as
    behaviour tests where the refactor moves code.
- **Done when:** all tests and the parity smoke pass unchanged in count; no
  file over 600 lines; a reviewer can trace one trade from candle to journal
  by reading ≤ 5 files.
- **Owner/Effort:** Claude, 4–5 days (highest-risk item; done in small
  commits, each verified).

### M6. Show you own it
- **Deliverables:**
  - `docs/ARCHITECTURE.md` with one diagram per core mechanism: the decision
    path, the risk manager's gates, the promotion gate, and the fill model
    (OCO brackets, maker vs taker, gaps).
  - A short "How this was built" section in the README stating that AI
    coding assistants were used, how changes were verified (tests, parity,
    measured experiments), and what you designed yourself.
  - A self-check list of 10 questions mentors are likely to ask, with
    pointers to the code that answers each.
- **Done when:** you can whiteboard the risk manager, the gate and the fill
  model without notes.
- **Owner/Effort:** Claude writes (1.5 days); You study and rehearse.

### M7. Make the learnings a feature
- **Deliverable:** `docs/RESULTS.md` table of every strategy and experiment —
  hypothesis, method, result, verdict, why — and an "Experiment log" panel on
  the Evidence tab reading the registry from M3.
- **Done when:** "four of five fast strategies failed, here is why" is visible
  in the product, not only in prose.
- **Owner/Effort:** Claude, 1.5 days.

### M8. Finish the open fast-book questions
- Hold-length study (running now) → record the verdict.
- Real order flow: keep Binance's `taker_buy_base_volume` from klines, rebuild
  the order-flow strategy on it, measure with the M3 bar.
- **Owner/Effort:** Claude, 2 days.

---

## Track V — Answering the VC's questions

Each question gets an *answer artifact*. Where the honest answer is "not yet",
the artifact is the system that will produce the answer.

### V1. "Do you have an edge?"
- **Answer today:** No proven edge. Two standard-book strategies pass the
  current gate on small samples; M3 will say whether they survive a stricter
  one.
- **Build:**
  - The M3 evidence package (multi-year, regimes, intervals, trial-counted
    Deflated Sharpe).
  - A **tamper-evident forward track record.** Every day the paper journal's
    trades and equity are hashed and the hash is committed to the repo (or
    published), so performance from that date on cannot be backfilled or
    edited. A VC cannot trust a backtest; they can trust a timestamped forward
    record.
- **Done when:** `docs/TRACK_RECORD.md` shows forward performance since a
  start date with daily verifiable hashes.
- **Owner/Effort:** Claude, 2 days (plus M3). The clock then needs months to run.

### V2. "Who are you competing with, and why do you win?"
- **Build:** `docs/COMPETITION.md` — a matrix of freqtrade, hummingbot, jesse,
  QuantConnect/LEAN, nautilus_trader, 3Commas/Cryptohopper-type services, and
  backtesting libraries. Columns: what they validate, cost realism,
  walk-forward/overfitting statistics, live-vs-backtest parity, price, users.
  States plainly where they are better.
- **Done when:** the differentiation fits in one sentence and survives the matrix.
- **Owner/Effort:** Claude researches and drafts (1.5 days); You sanity-check with users.

### V3. "What is the product, and for whom?"
- **Answer:** an *overfitting check for strategies people already have.* First
  user: retail and freqtrade quants who backtest and then lose money live.
- **Build (MVP):**
  - A "Validate a strategy" flow (CLI + dashboard tab) that accepts a trade
    list (CSV) or a **freqtrade backtest-result JSON export** (an output
    format — no GPL code is used) plus the price data. It reports:
    - walk-forward stability;
    - cost sensitivity (fees ×0.5 / ×1 / ×2);
    - PBO and trial-adjusted Deflated Sharpe;
    - bootstrap intervals;
    - regime breakdown;
    - a plain-language verdict: *robust / fragile / likely overfit*.
  - It reuses `bot/validation.py` and the M3 statistics.
  - A sample report generated on this repo's own strategies (including the
    failed ones) serves as the demo.
- **Done when:** a real freqtrade export produces a verdict report in under
  a minute.
- **Owner/Effort:** Claude, 6–8 days.

### V4. "Is there demand?" (traction)
- **Build (Claude):** a one-page landing page with a waitlist (email capture),
  and anonymous usage counters in the validator (opt-in).
- **Do (You):**
  - Publish the sample "my own strategies, honestly validated" report on
    r/algotrading and the freqtrade Discord.
  - Offer free validations to the first 20 users, and run 10 user interviews
    (script provided).
  - Track waitlist size, validations run and repeat users.
- **Done when:** you can state a traction number (e.g. "40 sign-ups, 25
  strategies validated, 8 repeat users") instead of "zero".
- **Owner/Effort:** Claude 1.5 days; You 2–4 weeks of outreach.

### V5. "Why won't the edge disappear?"
- **Build:** a **live drift monitor.** For every voting strategy, the
  live/forward profit factor is compared against its out-of-sample
  expectation. When it falls outside the bootstrap interval for N weeks, it is
  automatically demoted, with an alert on the dashboard.
- **Answer:** "Edges do decay, so the system measures decay continuously and
  stops trading what stopped working." That is a defensible answer; claiming
  permanence is not.
- **Owner/Effort:** Claude, 2 days.

### V6. "Show me live results with real money."
- **Build (readiness only):**
  - An exchange adapter for the **Binance testnet** behind the existing broker
    interface.
  - Reconciliation of exchange fills vs the journal, plus hard kill switches.
  - 4 weeks of testnet running compared against the paper engine (the
    slippage and fill-model reality check).
- **Then, only if you decide to (◆):** small real capital that you can afford
  to lose, placed and funded by you; the same drift monitor and kill switches.
  I can build and test the plumbing on testnet; I will not place real-money
  trades or move funds.
- **Owner/Effort:** Claude 4–5 days (testnet); You decide and operate any live step.

### V7. "What makes this defensible?"
- **Build:**
  - **Data asset:** a recorder for Binance order-book snapshots and taker
    flow (Binance publishes no historical depth), stored and compressed.
    After months it is a dataset that is costly to recreate, and it enables
    real market-making research (hftbacktest).
  - **Verdict database:** anonymised validation results (strategy type,
    parameters, verdict) across users — a growing benchmark of "what
    overfits" that improves the validator. This is the network effect.
- **Owner/Effort:** Claude 3 days (recorder) + 2 days (verdict store).

### V8. "Is this legal to run or sell?"
- **Build:** `docs/COMPLIANCE.md` listing what the project does *not* do
  (manage others' money, sell trade signals, give investment advice), UI
  disclaimers, and a checklist of questions for a qualified adviser
  (investment-adviser and research-analyst rules in India, crypto taxation,
  data-licence terms of exchanges).
- **Owner:** Claude drafts the checklist (0.5 day); **You** get it reviewed by
  a professional — this is not legal advice.

### V9. "Who is on the team?"
- **Do (You):** name the gaps (a markets/trading practitioner, someone for
  distribution/community); ask your mentors for one advisor intro each.
- **Build (Claude):** a one-page team and advisors slide template; a
  contributor guide (`CONTRIBUTING.md`) so others can join the repo.

### V10. "How big is the market, and how do you make money?"
- **Build:** `docs/MARKET.md` with sourced estimates (active open-source
  trading-bot communities, paid bot-service users, prop-firm challenge
  traders) and clearly labelled assumptions. Includes a pricing hypothesis:
  free open-source validator; paid hosted validation, scheduled re-validation
  and drift alerts; per-report pricing for prop-firm applicants.
- **Owner/Effort:** Claude researches (1.5 days); You validate pricing in the
  V4 interviews.

---

## Sequencing

| Phase | When | Contents | Outcome |
|---|---|---|---|
| **0** | Next 2–3 days | 0.1–0.4 | A demo you can give honestly |
| **1** | Weeks 1–2 | M2, M3, M4, M5, M7, M8 | Rigorous, readable, consolidated project; stricter verdicts |
| **2** | Weeks 3–4 | V1 (track-record clock starts), V3 MVP, V5, M6, V2 | A product a user can try; forward record running |
| **3** | Weeks 5–8 | V4 outreach, V6 testnet, V7 recorder, V8, V10 | First traction numbers; live-readiness evidence |
| **4** | Months 3–6 | Track record matures; drift monitor live; interviews → pricing | What a VC actually asks to see |

Every change is committed separately, verified with `make verify` (tests,
lint, parity smoke) and, for UI changes, checked in a browser at desktop and
phone widths in both themes.

## Risks and honest caveats

- **M3 may remove every voter.** That is the point of a stricter bar, but it
  means "the bot trades nothing" until a strategy earns its place. The story
  then becomes the validator and the track-record system, which is fine for
  the VC track and survivable for mentors if framed as a result.
- **The M5 refactor is the main regression risk.** It is done in small,
  separately verified commits. Some source-pinned tests will be rewritten as
  behaviour tests, and no test is deleted without an equivalent behaviour test.
- **Traction cannot be engineered.** V4's tooling is a day of work; the
  numbers depend entirely on your outreach.
- **A real-money track record takes months** and carries real financial risk;
  nothing in this plan requires it for the mentor demo.
- **Effort estimates assume** today's pace with AI assistance, and that you
  review and understand each change (M6). Unreviewed speed would undercut the
  mentors' main question.
