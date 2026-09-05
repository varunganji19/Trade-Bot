# AI Trading Bot — Crypto & Forex (Paper Trading)

An autonomous trading bot for **crypto and forex** that reads chart data and news,
decides **buy / sell / hold** with written reasoning, sizes and manages positions
by itself — plus a live **dashboard** (equity curve, trade history, strategy
attribution, decision feed) and a **chatbot** that answers questions about its
own trading record.

Built for a competition with an explicit engineering thesis: **the edge is the
process** — evidence-based strategies (researched from the most profitable
traders in history), honest validation with fees and slippage, strict risk
control, and full attribution of every decision. No strategy here is presented
as "guaranteed profitable" (see [RESEARCH.md](RESEARCH.md) §4).

```
                    ┌──────────────────────────────────────────┐
 news RSS ─────────▶│ Sentiment overlay (veto/shrink, never     │
                    │ initiates)                                │
 candles (ccxt      │                                          │
 fallback chain /   │ Indicators ─▶ Strategies ─▶ Orchestrator │
 yfinance, closed   │   (RSI/ATR/ADX/   (Turtle,   (regime +    │
 bars only,         │    VWAP/EMA...)  Connors,    weighted    │
 validated+stamped)  │                   Scalper)    vote)      │
                    │                        ▲                  │
                    │    Kronos (probabilistic forecast,        │
                    │     IC ledger — votes only after it      │
                    │     EARNS voting rights)                 │
                    │                     │                     │
                    │  Allocator (skfolio) ─┐│                 │
                    │  risk budget per sym  │▼                 │
                    │              RiskManager (final veto)     │
                    │                     │                     │
                    │              PaperBroker (fees+slippage,  │
                    │               OCO brackets, gap-aware)   │
                    │                     │                     │
                    │              SQLite Journal ────────────────┼──▶ Dashboard
                    └──────────────────────────────────────────┘      (FastAPI + Chart.js)
                                                                    + Chatbot
                     Shadow Account (journal vs its own rules)        + Purged-CV
                     Kronos IC ledger (earned voting rights)           validation
```

## Quick start

```bash
# 1. backtest on real exchange data (no account needed)
python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 --strategy turtle_trend
python3 main.py backtest --symbol ETH/USDT --timeframe 5m --days 30 --strategy ensemble --walk-forward

# 2. paper trade (one cycle or forever)
python3 main.py run --once
python3 main.py run                 # loops every 60s, journals everything

# 2b. honesty tooling
python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 \
  --strategy turtle_trend --purged-cv      # OOS path distribution + signal IC
python3 main.py validate --symbol BTC/USDT --timeframe 1h --days 365 \
  --strategy turtle_trend                  # purged-CV, PBO, Deflated Sharpe, Monte Carlo
python3 main.py kronos --symbol BTC/USDT --days 60  # Kronos IC verdict (earned vote?)
python3 main.py shadow                     # journal vs its own rules

# 3. dashboard + chatbot
python3 main.py dashboard           # → http://127.0.0.1:8000
#   (start/stop the engine from the UI; ask "how much did you earn?",
#    "why did you buy BTC?", "which strategy is best?")

# 4. other commands
python3 main.py status              # journal summary
python3 main.py chat "explain the connors strategy"
python3 main.py seed-demo           # fill journal with real backtest history for the demo
python3 tests/test_bot.py           # 88 tests
```

## The strategies (each mapped to evidence — see RESEARCH.md)

| Strategy | Lineage | Style | Timeframe |
|---|---|---|---|
| **Turtle Trend** | Donchian / Richard Dennis's Turtles + ADX regime filter (SSRN 6272239) | trend-following breakout, 2×ATR stop, opposite-channel exit | 1h |
| **Connors Mean Reversion** | Larry Connors RSI(2) + EMA(200) trend filter (documented ~75% win rate on indices) | buy deep pullbacks in uptrends, snapback exits, 3×ATR stop + time stop | 4h / 1d |
| **VWAP Scalper** | Opening Range Breakout evidence (Zarattini & Aziz 2023, SSRN 4416622) + VWAP institutional benchmark + team's earlier VWAP prototype | VWAP reclaim/loss with momentum + volume confirmation, rolling-range breakout, breakeven trail, time stop | 5m / 15m |

**Orchestrator**: classifies each market's regime (ADX + EMA structure) and
weight-blends the strategies (trending → breakout-weighted; ranging →
mean-reversion-weighted). A conflict guard stands down when strategies strongly
disagree. News sentiment (RSS headlines, LLM-scored if a key is configured,
deterministic lexicon otherwise) can **veto or shrink** a trade but never
initiate one — per the Lopez-Lira & Tang (2023) finding that headline sentiment
is predictive but small relative to costs.

**LLM integration is optional**: with `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`)
set, the LLM scores news, acts as a tie-breaker/veto, and powers the chatbot's
answers; without a key the bot runs fully deterministic ("quant mode") and the
chatbot answers from the journal with template logic.

## Risk management (the part that survives)

- 1% of equity risked per trade, sized off the ATR stop distance
- **portfolio allocation** (skfolio inverse-volatility by default, HRP optional):
  the risk budget is divided across the symbols that can hold positions, so
  correlated majors (BTC/ETH/SOL) can't each take a full 1% — the whole book
  stays bounded. Per-symbol share is capped (`max_sym_weight`) and floored.
- max 25% notional per position, max 4 concurrent positions
- 3% daily-loss kill switch; per-symbol cooldown after a stop-out
- 0.55 minimum confidence for any entry
- R-distance gate: stops wider than 10% of entry price are refused (vol-explosion guard); declared targets below 1.2R are refused
- positions, their stops and the account's cash survive restarts
  (journaled state; a restart continues the equity curve instead of
  silently resetting it to paper capital)
- one position per symbol across timeframes; each (symbol, timeframe)
  book manages only its own position — a 4h bar can never stop out a
  1h trade opened seconds ago

## Honesty rules (backtester & data)

- decisions on closed bars only — the **still-forming candle is dropped**
  before the engine ever sees it (backtest/live parity; unit-tested)
- fills at next bar's open with slippage; taker fees on every fill
  (crypto 0.10%, forex 0.02%) — per-trade `fees` report the **full round trip**
- **OCO bracket semantics**: stop and target are linked; both inside one bar
  resolves to the stop (conservative); a gap through the stop fills at the
  bar's open (you get the market, not the level)
- stop-loss fills can never be better than the stop level (unit-tested)
- strategy exit signals computed on bar *i*'s close fill at bar *i+1*'s open
  (that close was not tradable at decision time)
- `--walk-forward` reports per-fold out-of-sample stats; **`--purged-cv`**
  (skfolio CombinatorialPurgedCV) produces a *distribution* of OOS paths —
  trades straddling path boundaries are purged, and only paths that actually
  traded count toward the stats
- the daily kill switch follows **simulated bar time** in backtests, not the
  wall clock — the same rule runs in backtest and live
- causality is unit-tested (`test_strategies_never_read_future`): truncating
  history at bar *i* cannot change the bar-*i* signal; determinism is tested
  too (identical inputs → identical trades, curve, stats)
- **data quality**: every OHLCV frame is validated (positive prices, high/low
  bracket the body, sorted unique index) before indicators see it; crypto
  fetches have a **fallback chain** (Binance → Bybit → OKX) so one exchange
  being down never stops the engine; price caliber (raw vs adjusted) is
  stamped on every frame; backtest data is disk-cached per day for
  byte-identical repeat runs

## Kronos — a foundation-model voter that must EARN its vote

`bot/kronos_signal.py` wraps [Kronos](https://github.com/shiyu-coder/Kronos)
(AAAI 2026, MIT): a foundation model pre-trained on K-lines from 45+
exchanges. Every cycle it samples ~30 forecast paths and reports P(up),
expected return, and dispersion — but it **starts as a tracked non-voter**. A rolling rank-IC ledger scores its forecasts against what
actually happened; it joins the orchestrator vote (weight 0.20) only after
60+ resolved forecasts with IC ≥ 0.02, and loses the vote if IC decays.
First measured verdict on BTC 1h: IC −0.06 over 115 forecasts → **not
promoted**. The gate is the point: no signal votes on faith, not even a
foundation model. (`python3 main.py kronos` runs the evaluation.)

## Shadow Account — did the bot follow its own rules?

`python3 main.py shadow` replays every journaled trade against its owning
strategy's exit rules on the same bars and reports:

- **rule adherence** — on-rule vs *late* (lingered after the strategy's exit
  signal) vs *rule break* (discretionary exit with the strategy silent);
- **behavior profile** — R-multiple distribution, disposition effect,
  stop discipline (trades that blew through their initial stop);
- **shadow comparison** — actual journal PnL vs the pure-strategy backtest
  over the same window (the measured cost/value of orchestration).

On the current journal it surfaced: 57.6% adherence on BTC 1h, 16 lingering
exits, 236 trades that blew through their initial stop distance, and a +342h
disposition gap (losers held much longer than winners) — exactly the
diagnostics the attribution story needs.

## Layout

```
config.py            all tunables (watchlist, risk, costs, strategy params, allocation)
RESEARCH.md          the evidence behind every strategy + honest limitations
BACKTESTS.md         real-data results across symbols/strategies
main.py              CLI (backtest / run / dashboard / status / chat / kronos / shadow / seed-demo)
bot/
  data.py            ccxt fallback chain (Binance→Bybit→OKX) + yfinance + RSS;
                     OHLCV validation, caliber stamps, forming-bar drop, parquet cache
  indicators.py      Wilder RSI/ATR/ADX, EMA, Donchian, VWAP (session + rolling)
  strategies/        base + turtle + meanrev + scalper (stateless, testable)
  sentiment.py       lexicon + LLM headline scoring; veto/shrink only
  llm.py             optional OpenAI/Anthropic client (auto-detected)
  orchestrator.py     regime detection, weighted vote, conflict guard, Kronos
                      earned vote, LLM guardrails
  risk.py            sizing, caps, kill switch (simulated-clock aware), cooldowns
  allocator.py       skfolio inverse-vol / HRP cross-symbol risk budget
  broker.py          paper fills with fees/slippage; OCO brackets, gap-aware fills
  engine.py          autonomous live loop (journal-recovered positions, Kronos eval)
  backtest.py        event-driven backtester + walk-forward + allocation hooks
  validation.py      purged-CV OOS path distribution + signal IC reports
  kronos_signal.py   Kronos foundation-model signal: probabilistic forecast,
                     IC ledger, earned voting rights
  shadow.py          Shadow Account: rule-adherence replay + behavior profile
  journal.py         SQLite: decisions / trades / equity / chat_log
  chatbot.py         journal-aware Q&A (LLM or deterministic)
  dashboard.py       FastAPI + Chart.js single-page dashboard
  seed_demo.py       fill the journal from real backtests for the demo
models/kronos/       vendored Kronos model source (MIT; weights via HF Hub)
tests/test_bot.py    88 tests: indicators, strategies, causality, determinism,
                     risk, broker fills/OCO, allocator, purged CV, Kronos gate,
                     shadow, journal, backtest, live-engine regressions
                     (cross-timeframe isolation, restart cash, bars_held)
run_battery.py       full backtest battery across symbols/strategies
```

## Environment

- Python 3.11+ — `pip install -r requirements.txt`
  (core: `pandas numpy ccxt yfinance fastapi uvicorn requests pyarrow skfolio`;
  Kronos extras: `torch transformers huggingface_hub einops tqdm`)
- Vendor the Kronos model source once: `git clone https://github.com/shiyu-coder/Kronos models/kronos`
- Network access for market data + RSS (no API keys required for data)
- Optional: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (LLM mode), `OPENAI_BASE_URL`
- `PAPER_CAPITAL`, `LIVE_INTERVAL`, `BOT_DB_PATH`, `PORTFOLIO_METHOD`,
  `PORTFOLIO_ALLOC`, `MAKER_PRICING` env overrides
- `DASHBOARD_TOKEN` — optional: set it to require `Authorization: Bearer <token>`
  on every dashboard request (default off; the dashboard binds to 127.0.0.1 only).
  The engine auto-resumes on dashboard restart if it was running when the last
  session ended (a manual stop stays stopped).
- Without torch or the vendored model, the bot runs normally — Kronos reports
  "unavailable" and never touches the vote

## Status & scope

Paper trading only, by design. The same `MarketSpec` + broker interfaces that
make crypto/forex pluggable are where a real-broker adapter (e.g., Binance
testnet, OANDA practice) would slot in — but going live is a decision that
belongs to a regulated environment, not a competition repo.
