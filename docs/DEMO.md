# Demo: a trading research platform that refuses to fool itself

*Six slides and a talk track for a 7-minute mentor demo (roadmap item 0.3).
Every number below comes from an artifact in this repo; the source is given
next to it so you can answer "where does that come from?" without notes.*

**Before you start (off the clock)**

```bash
make evidence                                      # ~10 min: rule-v2 verdicts + registry
ALGO_NO_AUTO_RESUME=1 python3 main.py dashboard    # → http://127.0.0.1:8000
```

- `make evidence` writes the rule-v2 verdicts beside your journal. Without
  it the Strategies box shows the older verdicts marked "STALE — OLD RULE",
  which contradicts slides 3–4. Run it the day before; it makes both books
  stop trading, which is the point of slide 4.
- Open the tabs in this order so they are already loaded: Overview, Evidence,
  Strategy Lab, Fast book.
- In the Lab, pre-select BTC/USDT, 1h, **Compare all**. The run fetches data,
  so start it once before the meeting to warm the cache.
- Leave the engines stopped.

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
*promotion gate*, *cost realism*, *live = backtest*, *pre-registration*.

**Say:**
> A strategy may trade only if the pessimistic end of a 90% confidence
> interval on its out-of-sample profit factor is above 1.0 after fees, over
> at least 100 trades in rising, falling and ranging markets. Every
> experiment is declared in git before it runs, and every variant is counted,
> so the overfitting statistics know how many tries there were. And the live
> engine and the backtester run the same decision code — `make verify` fails
> if they ever diverge.

Sources: `bot/promotion.py`, `bot/experiments.py`, `scripts/parity_smoke.py`.

## Slide 3 — Live demo (2:00–4:30)

Switch to the browser. Four stops, about 35 seconds each.

1. **Overview → Engine card → Strategies box.** "Under the platform's own
   rule nothing may trade. Each strategy shows its interval: time-series
   momentum is the best at 1.07, but the interval runs from 0.81 to 1.37, so
   it is unproven." (`experiments/standard_gate.results.json`)
2. **Evidence → Experiment log.** "Every experiment ever run, with its
   verdict — the red ones are the failures, and there are a lot of them."
   Then the **Shadow account**: "The bot audits itself against its own rules:
   on the seeded replay history, 236 of 428 trades blew through their initial
   stop, and it says so."
3. **Strategy Lab → Compare all → Run.** "Every strategy on the same real
   data after fees. Anyone can check a claim here in under a minute."
4. **Fast book (experimental).** "A separate 5-minute book, labelled
   experimental because nothing in it has an edge after fees."

## Slide 4 — What was disproved (4:30–5:45)

**On the slide:** a table of failures — the headline of the demo.

| Idea | Result | Source |
|---|---|---|
| The two strategies that passed the old gate (Connors, VWAP scalper) | PF ≈ 1.55 on ≈ 60 trades → on 2 years and 5 markets: 0.75 (unproven) and 0.57 (loser) | `experiments/standard_gate.results.json` |
| A market maker "promoted" at PF 1.41 on 36,094 trades | PF 0.96 once quotes must trade through by 5 bp: an artefact of the fill model | `experiments/market_maker_fill_model.results.json` |
| The fast book's fade, tuned 27 ways | the 6-hour hold looked best on selection (PF 0.96) and lost $526 on the unseen 30 days | `experiments/fade_hold.results.json` |
| Kronos foundation model as a voter | IC −0.075 on BTC 1h against a +0.02 hurdle → no vote | `docs/RESULTS.md` §2 |
| RVOL volume filter (published Sharpe 0.48 → 2.81 on equities) | neutral here → shipped off | `docs/archive/BACKTESTS.md` Round 5 |

**Say:**
> The most useful thing this platform produced is a list of things that do
> not work. Two strategies were trading under my first gate. When I made the
> gate stricter and gave it two years of data, neither survived. And a market
> maker that looked like the best strategy I had turned out to be profitable
> only because my simulator let its orders fill too easily. I declared that
> test before running it, so I could not move the goalposts afterwards.

## Slide 5 — What's next (5:45–6:30)

**On the slide:** three bullets.

- **Real order flow.** Binance's candles include the taker-buy volume; the
  order-flow strategy has been using a guess. Rebuild it on the real data
  and put it through the same gate.
- **A forward track record** that cannot be backfilled: the paper journal
  hashed daily, the hash committed, so results from that date on are
  verifiable.
- **A validator for other people's strategies.** Take a freqtrade backtest
  export and report: robust, fragile or likely overfit.

## Slide 6 — Limitations (6:30–7:00)

**On the slide, and say it plainly:**

- Paper trading only; no real-money result, and none is claimed.
- Fills are simulated from candles; without order-book data, market making
  cannot be measured honestly at all.
- The regime labels and intervals are only as good as two years (standard)
  and 90 days (fast) of history.
- Built with AI coding assistants; every change was verified with tests, the
  parity check and measured experiments, and I can walk through any of it.

> So the honest headline is: no proven edge, nothing allowed to trade, and a
> platform that caught me every time I was about to fool myself.

---

## Likely questions

| Question | Short answer | Where to point |
|---|---|---|
| "So does it make money?" | No. Nothing passes the gate; the best standard strategy's interval is 0.81–1.37. | Overview → Strategies box |
| "Isn't a gate that rejects everything useless?" | It is a measuring instrument. It rejected two strategies that a looser gate passed on 60 trades, and both failed on more data. | `docs/RESULTS.md` §1 |
| "Why does the Kronos chart go above the hurdle?" | It pools every market and horizon from a ledger with no market keys; pooling unlike series inflates rank IC. The vote is decided per market: BTC 1h scored −0.075. | Evidence → Extras → Kronos caption |
| "Why does the fast book never trade?" | Every fast strategy is a measured loser after fees; trading more often was measured too (9–15 trades a day, still negative). | `docs/RESULTS.md` §3 |
| "Why do the overview numbers not add up?" | They do after the paper-account reset. If a gap ever appears, a "Ledger inconsistent" banner names it automatically. | Paper account tab; `docs/METHODOLOGY.md` §10 |
| "How do you know live and backtest match?" | `make verify` runs a parity smoke: the same bars through the engine and the backtester must produce the same decisions. | `scripts/parity_smoke.py` |
| "What did you design yourself?" | Answer from your own experience; the roadmap's M6 item prepares the architecture notes for this. | `docs/ROADMAP.md` |
