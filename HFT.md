# HFT.md — the high-frequency paper book

*Strategy research scraped via the agent-reach channels (r.jina.ai web reads +
web search over SSRN/arXiv/Quantitative Finance/GitHub) and implemented on
this repo's own fill model. Read this as a lab notebook, not a brochure —
every expectation below is stated before the measurement that tests it.*

## What this is (and is not)

The HFT book (`mode='hft'` in the journal, bot/hft/) is a **second paper
account** that trades **1-minute bars** alongside the standard book
(1h/4h/15m, untouched). Same engine class, same PaperBroker, same RiskManager,
same journal machinery — different universe, capital, fee tier, cadence, and
a dedicated dashboard page (the **HFT** tab) where **all HFT trades live in
one place**.

What it is NOT, stated plainly:

- **Not real high-frequency trading.** True HFT is a latency game measured in
  microseconds, fought with colocated servers, order-book feeds and queue
  models (Budish, Cramton & Shim QJE 2015; Aquilina et al. QJE 2022 measured
  the arms race on LSE message data). On free 1-minute OHLCV bars, every
  signal you can see has been arbitraged ~10⁸ times before the bar prints.
- **Not an edge claim.** The book exists to *measure* — with full costs —
  whether any 1m-cadence strategy survives realistic fees. The first
  measured answers are below, and one of them is negative.

## The cost wall (the one number that rules this book)

| Fee tier | Maker | Taker | Slippage | Taker round trip | Maker round trip (fees only) |
|---|---|---|---|---|---|
| **perp** (HFT default, `HFT_FEE_TIER=perp`) | 2bp | 5bp | 3bp | **16bp** | **4bp** |
| **spot** (base tier, `HFT_FEE_TIER=spot`) | 10bp | 10bp | 5bp | **30bp** | **20bp** |

Rule of thumb from the research: a taker 1m strategy needs **>20bp gross edge
per trade** to survive; a maker strategy needs **>5bp plus an honest fill
model** (a touch is not a fill). Published per-signal edges at this cadence:
order-flow imbalance ≈ a few bp with per-second Sharpe 0.12 (Cont/Kukanov/
Stoikov lineage; dm13450's measured writeup: "trading costs will eat you
alive"); the Quarter-Hour effect ≈ 0.5bp/signal vs 5bp taker fee (arXiv
2607.09426); mean reversion peaks at 4–8 minute horizons but "struggles to
overcome trading costs" (Carver 2025). Hence: **the HFT book's strategies are
maker-leaning by construction, and the harness runs every cell under BOTH
fee tiers so the fee sensitivity is a measured number, never a claim.**

## The strategies (each grounded, each falsifiable)

| Strategy | Execution | Lineage | The honest risk |
|---|---|---|---|
| `hft_market_maker` | **maker** entry (resting limit), maker TP / taker stop | Avellaneda & Stoikov 2008: quote width scales with realized vol (the γσ² term), quotes skew against the drift (the OHLCV stand-in for inventory skew); simplified to a single-position loop | Adverse selection is INVISIBLE on OHLCV: a limit that fills is disproportionately the one the market moved through. The bracket inverts the swing ratio (target ~⅓ of stop), so the win rate must exceed ~75% to break even |
| `hft_exhaustion_fade` | **maker** entry at the exhaustion close | Carver 2025 (4–8min reversion horizon) + capitulation filter: 3× volume spike, close in the bar's extreme tail (CLV ≤ −0.8), z of log(close/ema50) beyond ±2.5 | The fade fires INTO momentum; a regime that keeps trending costs 2×ATR + time |
| `hft_micro_breakout` | **taker** at next open | Zarattini & Aziz 2023 (SSRN 4416622) ORB: rolling micro-range break with 2R target, volume confirmation, and a volatility floor (0.08% 1m ATR) because the 2R target must clear the round trip | The only family with net-of-cost academic validation — on 5m US equities, not 1m crypto; the floor gate refuses most minutes |
| `hft_triangular_arb` | atomic 3-leg round trip, cash-settled | Muck & Schmidl 2025 (FRL 73, 106508): single-venue triangular mispricings are 1–5bp and last seconds | **Implemented as a monitor that is expected to fire ~never** — see the first measured result below |

### Candidates (registered, backtestable, NOT voting)

| Strategy | Execution | Lineage | Status |
|---|---|---|---|
| `hft_ofi_momentum` | **taker** at next open | Cont, Kukanov & Stoikov 2014: order-flow imbalance is near-linearly related to the next interval's price change. OHLCV proxy: signed volume (CLV × volume) summed over 5 bars, z-scored over 100, traded as continuation when aligned with EMA20 | **Not shipped into the vote.** Measured 2026-09-19 (3d, perp): PF 0.32 (BTC, 7 trades) / 0.68 (ETH, 12) / no signal on ETH/BTC and EUR/USD — it does not beat the incumbents it was written to replace |

`CANDIDATE_STRATEGIES` (bot/strategies/__init__.py) is the same evidence
standard Kronos lives under: a candidate is registered, runs in the Lab and
in the battery, and is **skipped entirely** by the live orchestrator — not
merely given zero weight, because the orchestrator picks the stop and the
maker limit from the highest-confidence signal irrespective of weight, and
the conflict guard counts any strong directional signal.

## The cost floor (why the book traded ZERO times for a week)

Measured 2026-09-19 on the live 1m book: **15 entry decisions, 0 trades, no
error anywhere.** The cause was not one bug but a missing link between two
correct pieces:

- `RiskManager.approve` refuses any entry whose stop is tighter than the
  modeled taker round trip — "tiny stop (dust)". A win that cannot pay its
  own fees is dust; the gate is right.
- The 1m strategies sized stops off raw ATR with **no reference to that
  number**. On a quiet 1m tape (BTC ATR ≈ 5–8bp against a 16bp perp round
  trip) the market maker quoted 2bp half-widths → a 6bp stop → vetoed. Every
  time. The decision was journaled, the veto was a log line, and the
  dashboard showed a healthy engine with an empty trade table.

The floors are now DERIVED from the book's fee tier in `build_hft_config`
and carried in `StrategyParams` (`hft_cost_floor_bps` = taker in + taker
out; `hft_maker_cost_floor_bps` = maker in + taker out), so the live engine
and the backtester read the same numbers, and `HFT_FEE_TIER=spot` moves
every floor together (16 → 30bp). Each strategy refuses a setup it cannot
pay for **itself**, with a readable rationale, instead of emitting a signal
the risk manager kills silently.

What it did to the market maker (BTC, 3d, perp): quotes went from 2bp to
≥12.5bp wide, trades 316 → 63, win rate 22% → 70%, PF 0.11 → 0.57. Still
below 1.0 — the fix makes the book trade honestly, it does not manufacture
an edge that the fee schedule does not permit.

## First measured results (2026-09-13, real exchange data, 3 days / ~4,320 bars per cell)

**Full battery (24 cells, `hft-battery`): every cell is net-negative after
full costs** — which is the fee math of the previous section made empirical,
not a surprise. The spread between tiers is the story:

| Cell (return %, trades, win rate) | perp tier | spot tier |
|---|---|---|
| BTC 1m micro_breakout | −0.38% (16, 18.8%) | −0.74% (14, 28.6%) |
| BTC 1m exhaustion_fade | −0.38% (16, 18.8%) | −0.97% (16, 12.5%) |
| BTC 1m market_maker | −4.12% (316, 22.2%) | −7.83% (151, 0.0%) |
| ETH 1m micro_breakout | **−0.07% (31, 41.9%, PF 0.95)** | −1.03% (31, 32.3%) |
| ETH 1m market_maker | −4.65% (472, 32.6%) | −7.83% (165, 0.0%) |
| ETH/BTC 1m micro_breakout | −0.02% (6) | −0.21% (6) |
| EURUSD 1m market_maker | −0.61% (140, **73.6% win**, PF 0.27) | −0.61% (140, 73.6%) |

- Reading 1 — **the maker strategies die on the bracket, not the signal**:
  EURUSD MM wins 73.6% of its trades and still loses (PF 0.27) because the v1
  target (⅓ of the stop) needs a win rate the width doesn't deliver. ETH MM
  on the spot tier: 0% win rate — the 20bp maker fee exceeds the entire
  half-width.
- Reading 2 — **fee sensitivity is measurable and roughly doubles the
  losses** perp → spot. Any 1m claim gross of this is fiction.
- Reading 3 — **the micro-breakout is the closest to viable** (ETH −0.07%,
  PF 0.95 at perp): few trades, wide targets, the only family with
  net-of-cost academic precedent. The tuning roadmap starts there.
- **Triangular monitor:** max |mispricing| **8.3bp** vs the **24bp** 3-leg
  cost — **zero opportunities fired** (p95 4.0bp) over 4,319 aligned bars.
  The published literature reproduces on current data.
- The HFT kill switch engaged repeatedly and correctly (−2% days halted
  entries) — risk machinery works at this cadence.

These are *starting points for tuning*, not shipped edges — same lab-notebook
status as the standard book's Round-1 scalper autopsy.

## The harness (what makes this more than three scripts)

- **One fill model, two runners:** `broker.limit_fill_price()` is the single
  maker-fill simulation used by BOTH the backtester and the live engine —
  paper trades and backtests cannot drift apart. Penetration knob
  (`HFT_PENETRATION_BPS`) approximates queue priority; 0.0 is optimistic
  touch-fills.
- **Resting orders are first-class:** the backtester and engine both carry
  pending-limit state with expiry (`hft.limit_wait_bars`), fill at the level
  (or the better open on a gap), maker fee, no slippage — the same OCO
  bracket and gap rules as every other order.
- **Fee-tier scenarios:** `build_hft_config(fee_tier=...)` swaps the whole
  cost model per run; the battery runs both tiers per cell.
- **Book isolation, tested:** journal rows are tagged `mode='hft'`; the
  restore path is mode-filtered, so the two books' positions, equity curves
  and histories can never cross-contaminate (regression-tested).
- **Determinism + causality, tested:** identical 1m inputs → identical
  trades; truncating history at bar *i* cannot change the bar-*i* signal.
- **The kill switch runs on the HFT book too** (−2% day vs the standard
  book's −3%), sized off the book's own equity.

## Using it

```bash
# backtest one strategy on real 1m data (perp tier by default)
python3 main.py hft-backtest --symbol BTC/USDT --timeframe 1m --days 3 \
  --strategy hft_market_maker
python3 main.py hft-backtest --symbol BTC/USDT --days 3 --strategy hft_exhaustion_fade --fee-tier spot
python3 main.py hft-backtest --symbol RELIANCE.NS --days 3 --strategy hft_micro_breakout  # india 1m: backtestable, kind-aware costs

# the triangular-arb monitor over real history
python3 main.py hft-backtest --triangular --days 3

# the harness: every strategy x symbol x fee tier + the monitor
python3 main.py hft-battery --days 3            # both tiers
python3 main.py hft-battery --tier spot

# live paper engine for the HFT book (writes mode='hft' rows)
python3 main.py hft-run                          # 20s cadence, 1m bars
python3 main.py hft-status                       # the HFT book's summary

# or from the dashboard: the HFT tab (separate engine start/stop, equity
# curve, ALL high-frequency trades, decision feed)
python3 main.py dashboard                        # → http://127.0.0.1:8000/#hft
```

Env: `HFT_PAPER_CAPITAL` (default 10000), `HFT_INTERVAL` (2s), `HFT_FEE_TIER`
(perp|spot), `HFT_PENETRATION_BPS` (0), `HFT_ENABLED` (1), `ALGO_NO_AUTO_RESUME`
(also keeps the HFT engine from auto-resuming on dashboard boot).

## The latency budget (paper = poll as fast as the exchange allows)

The book is PAPER, so every artificial wait is pure loss — the loop is tuned
for minimal bar-close -> fill latency:

| Stage | Standard book | HFT book |
|---|---|---|
| Poll interval | 60 s default | **2 s** (`HFT_INTERVAL`, floor 1 s via the API) |
| Data cache TTL | max(tf/2, 15 s) = 30 s on 1m | **2 s override** (`MarketData(ttl_seconds=2.0)`) |
| Wake granularity | 1 s sleep slices | **0.25 s** |
| Duplicate candles | re-evaluated every cycle | **skipped** (new-bar gate: one decision per closed bar, per market AND the TRI-ETH monitor) |
| Fill | decision price + slippage in the same cycle | same |

End-to-end: a 1m bar closes -> the engine wakes within <=2 s -> fetches a
fresh frame (~200-500 ms) -> decides -> fills in that cycle. **~1-3 s from
bar close to a paper fill.** Backtests keep the conservative next-open fill
for market orders (maker fills are sub-bar by construction) — the gap
between the two is exactly the latency cost live trading pays.

## The Strategy Lab (pick a stock -> apply strategies -> backtest)

The dashboard's **Lab** tab serves BOTH books: toggle Standard / HFT, pick a
market (crypto / forex / NSE — aliases like "btcusdt", "reliance", "nifty"
normalize automatically), and the strategy menu shows every strategy
REGISTERED for the chosen timeframe (derived from the registry, never
hand-maintained). "ALL strategies" runs a comparison on one fetched frame;
the run is async (poll status), lands in `data/results/lab_*.json`, and is a
pure backtest — open paper positions are never touched. The lab's
regime-aware note: with three strategies voting on 1m, the ensemble blend is
now live (an abstaining strategy's weight no longer dilutes the vote into
permanent HOLD — that orchestrator fix is regression-tested).

## Scope notes, stated honestly

- **Universe is crypto + forex.** The HFT book is USD-only (single-currency
  accounting, like the standard book's rule). India 1m is *backtestable* via
  the CLI with the full NSE cost stack, but yfinance's 1m history cap
  (~7d/request) and the currency rule keep it out of the live HFT book.
- **No order book, no ticks.** All signals are OHLCV proxies; the fill model
  is bar-range based with a penetration knob. Real MM viability needs L2
  queues (see nkaz001/hftbacktest for the gold standard, which is tick-based).
- **Pending limit orders are process-local.** A dashboard restart expires
  unfilled HFT resting orders (the DECISION rows record the intent; a live
  fill never strands a stop-less position because the journal-first entry
  happens only AT fill).
