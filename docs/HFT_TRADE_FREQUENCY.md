# Why the fast book rarely trades, and what it would take to trade often

*Research report, 2026-10-01. Question from the mentor review: "HFT is
supposed to take trades frequently, yet after 85+ cycles it has taken none."
Nothing below is implemented yet; each option lists what it would cost and
how we would measure it before shipping.*

## Short answer

The fast book is working as built: it is **selective by design, not
broken**. Three things combine:

1. **A cycle is not a decision.** The engine polls every 5–10 s but decides
   only when a new 5-minute bar closes. 85 cycles at 5 s is about 7 minutes,
   which is one or two decisions per market. In this session: 15 evaluations
   in 8 minutes, all "no strategy sees a setup".
2. **Only one strategy may trade, and it is the rarest one.** The promotion
   gate (walk-forward, out-of-sample) demoted three of the four fast
   strategies as measured losers. The survivor, `hft_exhaustion_fade`, needs
   a 3× volume spike, a close in the bar's extreme tail and a 2.5σ stretch
   from its EMA, all at once. In the battery it fired about **0.6–0.9 times
   per day per market**, so roughly **2–4 trades a day** across the 5-market
   book.
3. **The strategy that did trade often lost money.** `hft_market_maker`
   traded 161–264 times per market per fortnight (11–19 a day), but out of
   sample its median profit factor was 0.66 over 433 trades, so the gate
   removed its vote.

The underlying reason is the **cost wall**: at our modelled perp fees a round
trip costs 16 bp as a taker (4 bp maker fees plus spread and slippage on the
exit). Published short-horizon crypto edges are smaller than that. Trading
more often without a larger edge per trade just loses money faster.

## What the evidence says

| Finding | Source |
|---|---|
| Short-horizon crypto mean reversion is real but the gross edge peaks near **1.3 bp per trade** against a ~5 bp maker round trip, "too small to capture at benchmark spot costs". | Kitron & Wengrowicz, [arXiv 2608.21888](https://arxiv.org/html/2608.21888v1) |
| On SOL/USDT 5m perps with realistic Binance costs, a 0.15% stop is "mathematically unviable" (costs eat 93% of the risk budget); only 1–2% stops worked. Tight stops mean frequent trades, and frequent trades mean costs dominate. | Perera, [SSRN 6932998](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6932998) |
| An open-source test of 31 pre-registered market-making strategies on real BTCUSDT order books lost money at every fee and latency setting: spread ≈ 0.01 bp, one-second adverse selection 0.30–0.41 bp, maker fee 2.0 bp. (A single repository, not peer reviewed, but consistent with our own OHLCV result.) | [pdwi2020/p2_market_maker](https://github.com/pdwi2020/p2_market_maker) |
| Binance USDⓈ-M futures: 0.02% maker / 0.05% taker for regular users; 0% maker only at VIP 9. That matches our `perp` tier. | [Finder](https://www.finder.com/cryptocurrency/trading/binance-futures-fees), [BitDegree](https://www.bitdegree.org/crypto/tutorials/binance-fees) |
| Maker **rebates** (the exchange pays you to post liquidity) exist, but need $5M–$20M+ monthly volume or an approved market-maker program. | [Deribit Insights](https://insights.deribit.com/market-research/maker-taker-fees-on-crypto-exchanges-a-market-structure-analysis/), [Gate GMMC](https://www.gate.com/announcements/article/28165), [CoinEx](https://www.coinex.com/en/activity/market-maker) |
| Market making is a bet on spread capture beating adverse selection, and quote width must scale with volatility and inventory risk. | Avellaneda & Stoikov 2008; [Hummingbot implementation](https://hummingbot.org/strategies/v1-strategies/avellaneda-market-making/) |

Our own measurements agree (HFT.md): at 1m the book traded **zero** times in
a week because no stop could pay for its fees. At 5m it trades, and every
cell turns negative again on the spot fee tier.

## Options for trading more often

| Option | Effect on trade count | Cost / risk | Effort | How we would prove it |
|---|---|---|---|---|
| **A. Show the book's activity** — "next decision in 3:12", bars evaluated today, and the nearest-to-trigger market for each strategy | None (makes waiting visible, not a fake trade) | None | Small (UI + one stats field) | Screenshot; no strategy change |
| **B. Widen the universe** from 5 to ~15 liquid perps (BNB, XRP, DOGE, ADA, AVAX, LINK…) | ~3× more fade opportunities, same edge per trade | More data fetches per cycle; must re-check correlation caps | Small (watchlist + battery run) | `hft-battery` on the new markets; per-market promotion verdicts |
| **C. Calibrate the fade thresholds** (e.g. z 2.5→2.0, volume spike 3×→2×) via a grid in the walk-forward battery | 2–4× more signals (estimate) | Weaker signals; profit factor may fall below 1 | Medium | Only ship settings whose out-of-sample median PF ≥ 1.0 over ≥ 30 trades (the existing gate) |
| **D. Labelled "frequency experiment" track** — let the market maker trade in its own paper sub-book, visibly marked as a measured loser | 10–20 trades/day per market | Loses paper money by our own measurement; must never read as an edge claim | Medium | Separate equity curve; compare against the gate's PF 0.66 prediction |
| **E. Model a rebate fee tier** (maker −0.5 to −1.5 bp) as a what-if | Would let the market maker's frequency become viable on paper | Unrealistic for a student account (volume requirements above) | Small | Battery under a `rebate` tier, reported as hypothetical |
| **F. Order-book (L2) data for maker strategies** — use `ccxt.fetch_order_book` to see queue and imbalance, which OHLCV bars hide | Real market making needs this | Large: new data pipeline, fill model and storage | Large | Replay on recorded books; adverse-selection measured per fill |
| **G. Go back to 1-minute bars** | More decisions | Already measured: **0 trades in a week** (cost floor) | — | Not recommended |

## Recommendation

1. **Now, for the mentor demo:** do **A**. The honest answer to "why no
   trades?" is on screen: the book evaluated N bars today, and here is how
   close each market is to a setup.
2. **Next sprint:** do **B** and **C** together, measured through the
   existing walk-forward promotion gate. This is the only route that raises
   frequency *and* keeps the profitability claim honest. Target: 10+ trades
   a day across the book with out-of-sample PF ≥ 1.0.
3. **Only if the mentors want to see frequent fills specifically:** add
   **D**, clearly labelled as an experiment expected to lose, so the
   dashboard never presents it as an edge.
4. **Long term:** **F** is what real high-frequency market making requires.
   It is a project of its own.
5. **Naming:** call it the "fast book" in the demo, not "HFT". True HFT is a
   microsecond latency game on order-book feeds (HFT.md); this is a 5-minute
   bar strategy book.

## How to read the live logs

`[cycle N] … holds 0` means no new bar has closed since the last cycle, so
there was nothing to decide. A line with `holds 5` is one decision per market
on a freshly closed bar. `opened 0` with `holds 5` means every market was
evaluated and none met the entry conditions. That is the fade strategy
declining a setup, not a failure.

## Measured: the two replacement candidates (2026-10-01)

Both were built as candidates (`hft_cross_reversion`, `hft_funding_reversion`
in bot/strategies/hft.py) and measured with the gate's own rule: 60 days of
5m Binance data, four walk-forward folds per market, perp fee tier, full
costs. Neither ever voted live.

| Strategy | Markets | Trades | Trades/day/market | Win rate | Median OOS PF | Gross P&L (before fees) | Net P&L | Verdict |
|---|---|---|---|---|---|---|---|---|
| Cross-pair spread reversion | ETH/BTC, SOL/BTC, BNB/BTC, XRP/BTC, SOL/ETH, BNB/ETH | 661 | 1.8 | 41–48% | **0.58** (24 folds) | −$394 | −$1,536 | demoted |
| Funding-rate reversion | BTC, ETH, SOL, BNB, XRP, DOGE (USDT perps) | 83 | 0.2 | 22–44% | **0.39** (10 folds) | −$356 | −$559 | demoted |

**What this says:**

- **Spread reversion delivers the frequency, not the edge.** Close to two
  trades a day per pair, but it loses before fees on four of six pairs (the
  other two are barely positive gross and negative net). Crosses mean-revert
  on paper, yet the 2σ stretches that pass the half-life gate keep running
  often enough (stops ≈ half of all exits) to cancel the reversions.
- **Funding reversion is rare and wrong-footed.** Funding sat near its
  1bp/8h cap or slightly negative for most of the window; the few 2σ
  extremes mostly coincided with continuation (BTC on 2026-09-23: six longs
  into a selloff, five stopped). Gross negative on five of six markets.
- **Fees are not the reason.** Both lose before costs, so a cheaper fee tier
  or maker exits cannot rescue them; only a better signal can.

**Found on the way:** the broker rounded every crypto stop/target to 2
decimals, so on sub-$1 coins (ETH/BTC ≈ 0.0325) a short's stop landed below
its entry and every trade stopped out on the bar it opened. Fixed in the
broker; every earlier ETH/BTC battery cell for every strategy was affected.
Re-run `make hft-battery` to refresh the live verdicts with correct ETH/BTC
numbers.

**Next:** options B (more markets) and C (calibrating the one surviving
strategy's thresholds) from the table above remain the measured route to
more trades. Both new candidates stay registered so the Lab and the battery
can re-test them on future data; they cannot vote.
