# Demo: a trading research platform that refuses to fool itself

*Six slides and a talk track for a 7-minute mentor demo (roadmap item 0.3).
Every number below comes from an artifact in this repo; the source is given
next to it so you can answer "where does that come from?" without notes.*

**Before you start (2 minutes, off the clock)**

```bash
ALGO_NO_AUTO_RESUME=1 python3 main.py dashboard   # → http://127.0.0.1:8000
```

- Open the tabs in this order so they are already loaded: Overview, Evidence,
  Strategy Lab, Fast book.
- In the Lab, pre-select BTC/USDT, 1h, **Compare all**. The run fetches data,
  so start it once before the meeting to warm the cache.
- Leave the engine stopped. Starting it live is optional and not part of the
  story.

---

## Slide 1 — The problem (0:00–1:00)

**On the slide:** "Most backtests lie. Not on purpose: the tester picks the
best of many tries, ignores fees, and runs different code live than in the
backtest."

**Say:**
> Retail strategies usually look great in a backtest and lose money live.
> Three reasons: you tried many variants and kept the best one (selection
> bias), the backtest ignored fees and slippage, and the live bot does not
> run the same code as the backtest. I built a platform whose job is to catch
> all three, and I pointed it at my own strategies first.

## Slide 2 — The platform (1:00–2:00)

**On the slide:** the pipeline from the README (candles → strategies →
orchestrator → risk manager → paper broker → journal), with four labels:
*promotion gate*, *cost realism*, *live = backtest*, *overfitting statistics*.

**Say:**
> Every strategy has to earn its vote. A walk-forward battery tests it on
> data it was not tuned on, after fees on both legs, and only strategies
> whose out-of-sample profit factor clears 1.0 may trade. The live engine and
> the backtester run the same decision code, and `make verify` fails if they
> ever diverge. On top of that: purged cross-validation, the probability of
> backtest overfitting, and the Deflated Sharpe ratio.

Sources: `bot/promotion.py`, `scripts/parity_smoke.py`, `bot/validation.py`.

## Slide 3 — Live demo (2:00–4:30)

Switch to the browser. Four stops, about 35 seconds each.

1. **Overview → Engine card → Strategies box.** Point at the gate:
   "Five standard strategies measured; three are measured losers and cannot
   vote. Two may trade, Connors mean reversion and the VWAP scalper, each on
   about 60 out-of-sample trades." (`data/results/promotions_standard.json`)
2. **Evidence tab.** "Kronos is a published foundation model for price
   forecasting. It had to earn a vote like any strategy: on BTC 1h it scored
   an IC of −0.075 against a +0.02 hurdle, so it was rejected and runs
   offline only." Then scroll to the **Shadow account**: "The bot audits
   itself against its own rules: on the seeded replay history, 236 of 428
   trades blew through their initial stop, and it says so."
3. **Strategy Lab → Compare all → Run.** "Every strategy on the same real
   data after fees. Anyone can check a claim here in under a minute."
4. **Fast book (experimental).** "This is a separate 5-minute book. It is
   labelled experimental because it has no proven edge: the list shows each
   strategy's measured verdict, and the one still allowed to trade is on
   probation."

## Slide 4 — What was disproved (4:30–5:45)

**On the slide:** a table of failures, the headline of the demo.

| Idea | Result | Source |
|---|---|---|
| Kronos foundation model as a voter | IC −0.075 on BTC 1h → no vote | README "Kronos" |
| Turtle trend, TS momentum, FX mean reversion | median OOS PF 0.71 / 0.78 / 0.55 → demoted | `promotions_standard.json` |
| Fast-book micro-breakout, market maker, order-flow proxy | median OOS PF 0.27 / 0.66 / 0.58 → demoted | `promotions.json` |
| Cross-pair spread and funding-rate reversion | PF 0.58 / 0.39, negative even before fees | `docs/HFT_TRADE_FREQUENCY.md` |
| The surviving fade, tuned 27 ways (thresholds, exits, hold length) | positive before fees, every variant negative after | `docs/HFT_TRADE_FREQUENCY.md` |
| RVOL volume filter (published Sharpe 0.48 → 2.81 on equities) | neutral here → shipped off | `BACKTESTS.md` Round 5 |

**Say:**
> The most useful thing this platform produced is a list of things that do
> not work. My favourite example: when I tested longer holding periods for
> the fade, the 6-hour hold looked best on the data I selected on, and lost
> three times as much as the 45-minute hold on the 30 days it had never
> seen. If I had reported the selection result, I would have shipped the
> worst setting.

## Slide 5 — What's next (5:45–6:30)

**On the slide:** three bullets.

- **A stricter bar.** Promote only when the *lower* end of a 90% bootstrap
  interval on profit factor is at least 1.0, over 100+ out-of-sample trades
  in three market regimes. Today's two voters have about 60 trades each, so
  they will probably lose their vote. That is the point.
- **An experiment registry.** Every variant ever tried is recorded, and the
  Deflated Sharpe is computed from that count automatically, so selection
  bias is always priced in.
- **A forward track record** that cannot be backfilled: the paper journal is
  hashed daily and the hash committed.

## Slide 6 — Limitations (6:30–7:00)

**On the slide, and say it plainly:**

- Paper trading only; no real-money result, and none is claimed.
- Samples are small: the two standard voters rest on about 60 trades each.
- Fills are simulated from candles; there is no order book, so the fast
  book cannot model queue position or adverse selection.
- Built with AI coding assistants; every change was verified with tests, the
  parity check and measured experiments, and I can walk through any of it.

> So the honest headline is: no proven edge yet, and a platform that would
> have told me if I had been fooling myself.

---

## Likely questions

| Question | Short answer | Where to point |
|---|---|---|
| "So does it make money?" | No proven edge. Two strategies pass today's gate on about 60 trades each; the stricter gate will likely demote them. | Overview → Strategies box |
| "Why does the Kronos chart go above the hurdle?" | The chart pools every market and horizon from a ledger with no market keys; pooling unlike series inflates rank IC. The vote is decided per market, and the BTC 1h verdict was −0.075. | Evidence → Kronos caption |
| "Why does the fast book never trade?" | It decides once per 5-minute bar, and its one voter fires about 0.6–0.9 times a day per market. Trading more often was measured: 9–15 trades a day on 15 markets, negative after fees. | `docs/HFT_TRADE_FREQUENCY.md` |
| "Why do the overview numbers not add up?" | They should after the paper-account reset (roadmap 0.1). If a mismatch ever reappears, roadmap item M4 adds an automatic "ledger inconsistent" banner. | Paper account tab |
| "How do you know live and backtest match?" | `make verify` runs a parity smoke: the same bars through the engine and the backtester must produce the same decisions. | `scripts/parity_smoke.py` |
| "What did you design yourself?" | Answer from your own experience; the roadmap's M6 item prepares the architecture notes for this. | `docs/ROADMAP.md` |
