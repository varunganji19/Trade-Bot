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

## Measured: more markets and calibrated thresholds for the live fade (2026-10-01)

`hft_exhaustion_fade` (the one strategy allowed to trade) on **15 liquid
USDT pairs** (BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC, DOT, TRX,
BCH, NEAR, SUI), 90 days of 5m Binance data, perp fee tier, full costs.
Thresholds were **chosen on the first 60 days** (four walk-forward folds per
market) and **judged on the last 30 days**, which the choice never saw.

**Selection (60 days, out-of-sample folds):**

| z entry \ volume spike | 2.0× | 2.5× | 3.0× |
|---|---|---|---|
| **2.0σ** | 16.2/day · PF 0.72 | 11.5/day · PF 0.76 | 8.5/day · PF 0.73 |
| **2.25σ** | 13.7/day · PF 0.73 | 9.7/day · **PF 0.80** | 7.2/day · PF 0.76 |
| **2.5σ** (live today) | 11.1/day · PF 0.66 | 8.0/day · PF 0.75 | 6.0/day · PF 0.75 |

Trades per day are for the whole 15-market book; PF is the median
out-of-sample profit factor. **Every cell is "demoted"** under the live rule.

**Held-out 30 days (never used for selection):**

| Setting | Trades | Per day | Gross P&L (before fees) | Fees | Net P&L |
|---|---|---|---|---|---|
| Live (2.5σ, 3×) | 278 | 9.3 | **+$78** | $485 | −$407 |
| Best of grid (2.25σ, 2.5×) | 445 | 14.8 | **+$459** | $778 | −$319 |

**What this says:**

1. **Breadth delivers the frequency the mentors asked for.** The same
   strategy on 15 markets takes 9–15 trades a day instead of 2–4.
2. **The fade has a small real edge before costs, and fees take all of it.**
   Positive gross on unseen data at both settings, unlike the two new
   candidates (negative even gross). This is the one place a cost
   improvement could change the verdict.
3. **The live fade is a measured loser once there is enough evidence.** Its
   "probation" status came from too few trades on four markets over 14 days,
   not from good results. On 15 markets it would be demoted, leaving the fast
   book with no voter at all.
4. **Per-market results scatter widely** (NEAR +$238, AVAX −$149 at the live
   setting). Keeping only the winners would be cherry-picking a 30-day sample.
5. **The loosest settings are not better per trade**, only busier. The grid's
   best cell (PF 0.80) is still below the 1.0 promotion line.

**Next experiment that could actually flip the result: cheaper exits.** The
fade enters as a maker (2 bp) but exits as a taker (5 bp plus 3 bp
slippage). A resting limit exit at the mean would cut the round trip from
about 10 bp to about 4 bp, roughly the size of the measured gross edge. It
has to be modelled honestly: a resting exit can miss, and the stop must
stay a taker order.

## Measured: limit exits for the fade (2026-10-01)

Hypothesis from the previous section: the fade is positive before fees, so a
cheaper exit might flip it. Implemented as the opt-in parameter
`hft_fade_limit_exit_bps` (a resting take-profit at the entry-time mean,
placed N bp beyond it so price must trade through; maker fee, no slippage;
stops and time stops stay market orders). Same 15 markets and the same
60-day selection / 30-day holdout split; all variants fixed in advance.

| Thresholds | Exit | Selection median OOS PF | Holdout trades/day | Gross | Fees | **Net (30 days)** |
|---|---|---|---|---|---|---|
| live 2.5σ / 3× | market (today) | 0.75 | 9.3 | +$78 | $485 | −$407 |
| live 2.5σ / 3× | limit, touch | 0.78 | 9.3 | +$111 | $472 | −$361 |
| live 2.5σ / 3× | limit +2bp | 0.76 | 9.3 | +$101 | $473 | −$372 |
| live 2.5σ / 3× | limit +5bp | 0.78 | 9.3 | +$91 | $474 | −$383 |
| 2.25σ / 2.5× | market | 0.80 | 14.8 | +$459 | $778 | −$319 |
| 2.25σ / 2.5× | limit, touch | 0.79 | 14.8 | +$574 | $745 | −$171 |
| 2.25σ / 2.5× | limit +2bp | 0.81 | 14.8 | +$576 | $746 | **−$170** |
| 2.25σ / 2.5× | limit +5bp | 0.83 | 14.8 | +$574 | $748 | −$174 |
| 2.25σ / 2.5× | limit +2bp, **entry must also trade through 2bp** | 0.74 | 14.7 | +$482 | $737 | −$255 |

**Result: limit exits help a little and flip nothing.** The best variant
loses $170 over 30 days instead of $319, and once the entry limit is also
required to trade through (the realistic case), it loses $255 and is
demoted. No variant clears the 1.0 promotion line.

**Why the cheaper exit barely matters:** only about 10% of trades ever reach
the mean. Of 445 holdout trades at the looser setting, roughly 50% are closed
by the 45-minute time stop and 40% by the stop loss — both market orders by
design. The exit being priced is the rare one. The fade's problem is not
what its winning exit costs, but that most stretches do not revert within 45
minutes.

`hft_fade_limit_exit_bps` stays off (live behaviour unchanged). It is kept
because it is strictly cheaper whenever the fade does revert, and it is the
right exit if a future variant reverts more often.

**What would actually test the remaining idea:** a longer hold (the time
stop closes half the trades; does the reversion simply need more time?),
and real taker-buy order flow as an entry filter (resource idea 1 below).

## Open-source resources worth using (GitHub survey, 2026-10-01)

Licences matter: **MIT / Apache-2.0** code can be reused with attribution;
**GPL-3.0** code would force this project under the GPL, and vectorbt's
licence carries a Commons Clause (no commercial use) — borrow *ideas* from
those, not code.

| Project | ★ | Licence | What it offers us |
|---|---|---|---|
| [nkaz001/hftbacktest](https://github.com/nkaz001/hftbacktest) | 4.8k | MIT | Tick-level backtester with **queue-position fill models and feed/order latency**, L2/L3 order-book replay, Binance Futures and Bybit; examples include GLFT market making and order-book-imbalance alpha. The tool option F needs: our market maker lost partly because a candle cannot show queue position or adverse selection. |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | 20.3k | Apache-2.0 | Production market-making framework: Avellaneda–Stoikov with an order-book liquidity estimator, pure and cross-exchange market making, V2 "controllers" for multi-pair strategies. Reference implementation if the market maker is ever rebuilt on real order books. |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) + [freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | 55k / 5.5k | GPL-3.0 | The largest 5m crypto-bot community. Ideas to borrow: **pairlist filters** (VolumePairList, VolatilityFilter, SpreadFilter, and PrecisionFilter, which exists for the exact tick-size trap we just fixed), **protections** (StoplossGuard, LowProfitPairs, CooldownPeriod), higher-timeframe "informative" confirmation for 5m entries, and FreqAI for ML. |
| [iterativv/NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | 3.4k | GPL-3.0 | The most-used community 5m strategy. Its setup guidance is itself evidence for option B: **40–80 volume-ranked USDT pairs**, 6–12 open trades. Frequency comes from breadth, not from loosening one market's trigger. |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | 29.6k | LGPL-3.0 | Deterministic event-driven engine with a Rust core and L2/L3 support; a reference for "backtest equals live" design, which this repo enforces with its parity smoke. |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | 8.6k | MIT | Python crypto framework with multi-timeframe candles and route-based multi-symbol backtests; readable reference code. |
| [binance/binance-public-data](https://github.com/binance/binance-public-data) | — | — | Free bulk history: klines for every interval plus trades and aggTrades for spot and USD-M futures (no order-book depth). Bulk downloads beat paging the REST API for 90-day-plus studies. |
| [wilsonfreitas/awesome-quant](https://github.com/wilsonfreitas/awesome-quant), [paperswithbacktest/awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading) | 30k / 14.5k | — | Curated indexes of libraries, data sources and papers; starting points rather than code. |

**Concrete ideas from the survey, ranked by value for effort:**

1. **Real order flow for free.** Binance's kline API returns
   `taker_buy_base_volume` with every candle (verified: one BTC 5m bar had
   29.96 of 42.75 BTC bought by takers, a +17.2 BTC imbalance). ccxt drops
   that column. Keeping it would replace `hft_ofi_momentum`'s CLV×volume
   *guess* at order flow with the real aggressor imbalance — the signal
   Cont, Kukanov & Stoikov actually describe. Small data-layer change; then
   re-measure the order-flow strategy.
2. **Breadth with a volume- and volatility-filtered universe** (freqtrade's
   pairlist idea): rank USDT pairs by 24h volume and drop those whose ATR
   cannot clear the cost floor, instead of a hand-picked list.
3. **Higher-timeframe confirmation**: only fade a 5m exhaustion when the 1h
   trend is not strongly against the fade (freqtrade's "informative pairs").
4. **Protections per pair**: pause a market after N stop-outs in a window
   (StoplossGuard), so one trending market cannot rack up repeated losses.
5. **Order-book backtesting with hftbacktest** if market making is revisited:
   record L2 from the Binance websocket (Binance publishes no historical
   depth), then measure with queue-position fills. A project of its own.
