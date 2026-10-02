# Market: who might pay for a second opinion on a backtest

*Roadmap V10. **Sourced** figures were checked on 2026-10-02 and are cited
at the end. Every multiplier marked **A1, A2, …** is an assumption, not a
fact; it is there to be replaced by what the V4 interviews and waitlist
actually show. No figure here is a forecast.*

## Who the first user is

People who already backtest strategies in code, then trade them, and want
to know whether the backtest is evidence: retail and semi-professional
algorithmic traders, starting with freqtrade users (the validator reads
freqtrade's export format today).

## What can be measured today

| Signal | Figure | What it says | Source |
|---|---|---|---|
| freqtrade official Discord | **18,293 members** (1,446 online when checked) | the size of the most direct first audience | Discord invite API |
| freqtrade GitHub stars | ~55,000 | interest, not users | GitHub API |
| freqtrade downloads from PyPI | **73,211 in the last month** | installs, including CI and repeat installs, so an upper bound on active users | pypistats.org |
| vectorbt downloads from PyPI | 339,088 in the last month | the wider Python backtesting audience (same caveat) | pypistats.org |
| backtesting.py downloads from PyPI | 166,975 in the last month | same | pypistats.org |
| jesse downloads from PyPI | 4,210 in the last month | a smaller framework with its own Monte Carlo tools | pypistats.org |
| Paid hosted bot services | 3Commas: $20 / $50 / $140 a month | retail traders already pay monthly for bot tooling | 3commas.io/pricing |
| Prop-firm challenge traders | FTMO alone: "4.5M+ customers worldwide", "$650M+ paid in rewards" | a large population that pays to prove a strategy; FTMO does not publish pass rates | ftmo.com |
| r/algotrading | not measured | Reddit refused automated requests; check by hand before quoting | — |

## A bottom-up estimate (assumptions labelled)

Starting from the one audience the validator already serves:

| Step | Value | Basis |
|---|---|---|
| freqtrade Discord members | 18,293 | sourced |
| … who backtest seriously and trade live | × 20% = ~3,700 | **A1** (guess: most members are lurkers) |
| … who would run a validation if it is free | × 25% = ~900 | **A2** |
| … who would pay for hosted validation, re-validation or drift alerts | × 5% of A2 = ~45 | **A3** |
| at **A4** = $15 a month | ≈ $8,000 a year | **A4** (below 3Commas' cheapest plan) |

Across all Python backtesting users the funnel is wider, but PyPI downloads
are not people (CI, mirrors and repeat installs inflate them), so no number
is derived from them here.

**Prop-firm applicants** are the larger, less certain segment: they pay to
take challenges and fail most of them, so a pre-challenge report ("is this
strategy evidence or luck?") has an obvious use. How many of FTMO's
customers trade systematically, and would pay per report, is unknown (**A5**);
it is the first question for that segment's interviews.

**Honest reading:** on these assumptions the freqtrade segment is a hobby-
sized business. The case for more rests on A3, A4 and A5, which only
real users can settle, and on V7's network effect (a growing database of
what overfits) making the product better than a script anyone could write.

## Pricing hypothesis (to test in V4 interviews)

| Tier | Price | What it buys | Why |
|---|---|---|---|
| Open source | free | the CLI validator, this repo | distribution and trust; the code is the marketing |
| Hosted validation | ~$10–20 a month (A4) | upload an export, get the report; history of past verdicts | users who will not run Python |
| Re-validation and drift alerts | in the hosted tier | weekly re-check of a live strategy against its expected range (the V5 monitor, as a service) | the only part that is recurring by nature |
| Per-report, prop-firm applicants | ~$20–50 a report (A6) | one strategy, one report, before paying for a challenge | priced against the challenge fee it might save |

## What would change this page

- Waitlist size and conversion (V4), replacing A1–A3.
- Interviews: what users pay for today, and whether "your backtest is
  probably luck" is something they want to hear (A3–A6).
- Whether the verdict store grows (V7): a network effect is the only
  defensibility argument on this page.
- Legal answers (COMPLIANCE.md Q1, Q7): if verdicts count as research or
  advice, or Yahoo's data cannot be used commercially, pricing changes.

## Sources (accessed 2026-10-02)

- freqtrade Discord member count: `https://discord.com/api/v9/invites/p7nuUNVfP7?with_counts=true`
  (the invite linked from freqtrade's README)
- GitHub stars: GitHub API, `repos/freqtrade/freqtrade`
- PyPI downloads (last month): <https://pypistats.org/api/packages/freqtrade/recent>,
  `…/vectorbt/recent`, `…/backtesting/recent`, `…/jesse/recent`
- 3Commas pricing: <https://3commas.io/pricing>
- FTMO figures: <https://ftmo.com/en/>
