# Strategy Research — Evidence-Based Foundations for the AI Trading Bot

> **Purpose:** This document reviews the strategies of the most consistently profitable traders in
> history and the quantitative evidence behind them, then maps each proven idea onto a concrete,
> testable strategy that our bot implements. It doubles as the competition write-up: every design
> decision below traces back to published evidence, not guesswork.

---

## 1. Why not "invent" a strategy?

Retail algo projects usually fail in one of two ways: they overfit a curve-fitted backtest, or they
trade a strategy with no statistical basis. Our approach is the opposite — we start from strategies
with **documented, multi-decade, multi-asset evidence**, adapt their parameters to crypto/forex
timeframes, and validate honestly (out-of-sample, with fees and slippage). If a strategy doesn't
survive that process, it doesn't ship.

---

## 2. The traders and the evidence

### 2.1 Trend following — Richard Donchian & the Turtle Traders (Richard Dennis)

- Richard Donchian pioneered channel-breakout trend following in the mid-20th century.
- Richard Dennis trained the famous "Turtles" (1983–84) whose rules — buy a 20-period breakout,
  exit on a 10-period opposite breakout, position size = 2% account risk per 1 ATR ("N") of price
  movement — were published in full by freethewales.com and Curtis Faith's *"Way of the Turtle"*.
  Ex-students (Jerry Parker, Paul Rabar) ran billions with these rules.
- **Academic backing:** time-series momentum (Moskowitz, Ooi & Pedersen, *Journal of Financial
  Economics* 2012) documents that trailing returns predict future returns across 58 instruments
  over 45+ years — the statistical engine behind trend following. AQR's managed-futures research
  shows the same edge persisting for a century.
- **Recent/asset-specific evidence:**
  - SSRN: [Evaluating the Performance of a Donchian Channel Breakout Strategy](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6272239) —
    breakout strategy improved by an **ATR-based volatility regime filter** and ATR risk management.
  - [QuantifiedStrategies: Donchian Channel strategy guide](https://www.quantifiedstrategies.com/donchian-channel/) —
    backtest discussions across markets.
  - Practitioner reports: [r/algotrading real-money Donchian thread](https://www.reddit.com/r/algotrading/comments/1s7eqm7/finding_my_first_glimpse_of_success_with_my_algo/).

**Profile:** low win rate (~35–45%), large payoff ratio (wins ≫ losses), long flat/drawdown periods,
captures big trends. Works best when a market is *trending*; bleeds in chop.

### 2.2 Short-term mean reversion — Larry Connors (RSI-2)

- Larry Connors (Connors Research, *Street Smarts*, *Short Term Trading Strategies That Work*)
  popularized the RSI-2 strategy: buy pullbacks to RSI(2) extremes **only in the direction of the
  long-term trend (200-day MA filter)**, exit on a snapback (close above 5-day MA).
- **Evidence:** independent backtests put the win rate at **75–79% on S&P 500 instruments when the
  200-period filter is applied** — the filter is the strategy. Without it, results collapse.
- **Documented caveats (we adopt them as constraints):**
  - It is a *high-win-rate / negative-skew* profile: many small winners, occasional larger losers.
  - It works best on mean-reverting instruments (indices, large-cap crypto) and poorly when applied
    blindly to trending markets. Sources:
    [TopTradingStrategy RSI-2 backtest](https://www.reddit.com/r/algotrading/comments/1fm5lfj/backtest_results_for_connors_rsi2_strategy/),
    QuantifiedStrategies RSI-2 analysis (75% win rate confirmation), Trade2Win/EliteTrader variant threads.
- **Our adaptation:** 200-period EMA trend filter, RSI(2) < 5 entries (long side; tightened from Connors' 10 — fewer, deeper pullbacks only), exit on RSI(2)
  > 65 or close above EMA(5), mandatory ATR stop (Connors used none — we refuse unbounded risk),
  and a time stop.

### 2.3 Intraday breakout & VWAP scalping — the quantified day-trading evidence

- **Zarattini & Aziz (2023), [Can Day Trading Really Be Profitable?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622)**
  (SSRN): the 5-minute **Opening Range Breakout (ORB)** on QQQ outperformed buy-and-hold by a wide
  margin over 2016–2023 with disciplined ATR-based sizing. Follow-up replication and tooling by
  [Concretum Group](https://concretumgroup.com/backtesting-the-opening-range-breakout-orb-strategy-using-polygon-io/).
- Earlier academic support: Holmberg (2012, Stockholm School of Economics),
  [Assessing the profitability of intraday ORB strategies](https://ideas.repec.org/p/hhs/umnees/0845.html).
- Independent tracking of ORB performance:
  [TradeThatSwing ORB report](https://tradethatswing.com/opening-range-breakout-strategy-up-400-this-year/),
  [Edgeful 5-minute ORB on ES](https://www.edgeful.com/blog/posts/5-minute-opening-range-breakout-es-strategy).
- **VWAP** is the institutional execution benchmark: reclaiming/holding VWAP with volume
  confirmation is one of the few intraday setups with a structural reason to work (institutional
  order flow is benchmarked to VWAP). Crypto trades 24/7 — there are no sessions to anchor to —
  so we use a **rolling VWAP** and a **rolling N-bar range breakout** (the 24/7 analogue of the
  opening range).
- **Honest caveat:** scalping is the most cost-sensitive style — fees and slippage eat the edge.
  That's why our scalper runs on 5m/15m candles (not seconds), uses ATR-proportional stops, and why
  the backtester charges full taker fees + slippage.
- Our team's earlier VWAP-intraday prototype (since retired from the repo; its ideas live on in the
  scalper) contributed the volume-confirmation and VWAP-breakdown exit ideas.

### 2.4 News & sentiment as an *overlay*, not a signal

- Recent research (Lopez-Lira & Tang, 2023, *"Can ChatGPT Forecast Stock Price Movements?"*) found
  LLM-scored headline sentiment has measurable next-day predictive value — but the effect is small
  relative to transaction costs and decays fast.
- **Our design consequence:** sentiment can *veto* or *shrink* a trade, but never *initiate* one by
  itself. Primary entries always come from the quantified strategies above.

### 2.5 Risk management — the part that actually survives

Convergent wisdom from the traders above:
- **Richard Dennis / Turtles:** risk ≤ 2% of equity per trade, size positions off volatility (ATR),
  never average down.
- **Paul Tudor Jones:** "5:1 [reward-to-risk]. I can miss 80% of my shots and still make money."
  Cut size after drawdowns.
- **Ed Seykota:** risk control *is* the system ("Everybody gets what they want out of the market").
- Our rules: 1% risk per trade (crypto/forex volatility > 1980s commodities), ATR-proportional
  stops, max 25% notional per position, max 4 concurrent positions, **3% daily loss kill switch**,
  per-symbol cooldown after a stop-out, minimum confidence threshold for any entry, R-distance cap
  (stops wider than 10% of entry price refused — vol-explosion guard), reward floor (declared
  fixed targets must be ≥ 1.2R; signal-exit strategies pass None).

---

## 3. What we implemented (research → code)

| Strategy in bot | Source trader/idea | Timeframe | Entry | Exit | Stop |
|---|---|---|---|---|---|
| **Turtle Trend** | Donchian/Dennis breakout + ATR regime filter | 1h | Close breaks prior 20-bar high/low, ADX > 20 | Opposite 10-bar channel | 2 × ATR(14) |
| **Connors Mean Reversion** | Connors RSI-2 | 4h / 1d (evidence is daily bars) | RSI(2) < 5 long ( > 95 short) with EMA(200) trend filter | RSI(2) > 65 or cross of EMA(5); time stop | 3 × ATR(14) |
| **VWAP Scalper** | Zarattini/Aziz ORB + VWAP institutional flow + our VWAP prototype | 15m (5m enabled but measured cost-negative) | VWAP reclaim with EMA(9)>EMA(21) momentum + volume confirmation, or N-bar range breakout | VWAP cross-down, breakeven trail after 1R, time stop | 2 × ATR(14) |
| **Sentiment Overlay** | LLM headline scoring (Lopez-Lira & Tang 2023) | live only | — (never initiates) | can veto/shrink entries | — |

**Orchestrator:** ADX + EMA structure classifies the regime (trending vs. ranging). Trending →
Turtle weight 0.55 / Scalper 0.30 / MeanRev 0.15. Ranging → inverted. Weighted confidence vote with
conflict guard; optional LLM as tie-breaker/veto with strict guardrails; RiskManager has final veto
over everything.

---

## 4. Honest limitations

1. **Backtests are not promises.** Past performance ≠ future results; regime shifts can kill any edge.
2. **Costs matter more than signals at high frequency.** We charge taker fees + slippage in every
   backtest and still expect live results to be worse than backtests.
3. **Sentiment overlay is live-only** — we cannot reconstruct point-in-time news archives reliably
   for backtesting, so it is excluded from backtests rather than faked.
4. **No strategy here is "the best."** The edge is the *process*: evidence-based design, honest
   validation, strict risk control, and full attribution of every trade to the strategy and reasoning
   that produced it.
