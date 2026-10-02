# Competition: who else does this, and where they are better

*Roadmap V2. Facts were checked on 2026-10-02 against each project's own
repository, documentation or pricing page (sources at the end). GitHub stars
are a rough proxy for users, not a user count. Where a tool's documentation
does not describe a capability, this page says "not described", not "does
not have": absence from the docs is all that was checked.*

## The one-sentence difference

**Every tool below helps you build or run a strategy; this project's
product is a second opinion on a backtest you already ran — in freqtrade or
anything that exports trades — that prices in how many variants you tried
and says plainly when the result is not evidence.**

## The matrix

| | What it is | Validation it offers | Selection-bias statistics (Deflated Sharpe, PBO) | Same code backtest and live | Price | Adoption (GitHub stars) | Licence |
|---|---|---|---|---|---|---|---|
| **This project** | Paper-trading research platform + validator | Promotion gate on out-of-sample PF intervals; pre-registered experiments; regime coverage; cost sensitivity; drift demotion; tamper-evident forward record | **Yes**: Deflated Sharpe priced from a trial registry; PBO across a family | Yes, enforced by a parity check in CI | Free | small (personal project) | MIT |
| **freqtrade** | Crypto bot: backtest, hyperopt, live | `lookahead-analysis` and `recursive-analysis` commands detect look-ahead and indicator warm-up bias | Not described; the hyperopt docs warn that very precise parameters "usually" overfit | Yes (one strategy class for both) | Free | ~55,000 | GPL-3.0 |
| **jesse** | Crypto framework: backtest, optimise, live | Monte Carlo (trade shuffling, candle simulation) and "rule significance testing" against random entries | Not described | Yes ("identical code" for live/paper) | Free core | ~8,600 | MIT |
| **nautilus_trader** | Rust/Python event-driven engine, multi-asset | Realistic execution on tick and order-book data | Not described | Yes ("the same strategy and execution-algorithm code") | Free | ~29,600 | LGPL-3.0 |
| **QuantConnect / LEAN** | Multi-asset engine (open) + cloud platform | Broad historical data, cloud backtests, live brokerages | Not described on the pages checked | Yes (LEAN runs both) | Free plan; paid tiers priced per configuration (no list price shown) | ~21,800 (LEAN) | Apache-2.0 |
| **hummingbot** | Market-making and arbitrage bots | Backtestable "controllers" | Not described | Yes | Free | ~20,300 | Apache-2.0 |
| **StrategyQuant X** | Desktop strategy generator for retail | Mature robustness suite: walk-forward optimisation, walk-forward matrix, Monte Carlo, parameter permutation, multi-market checks | Not described as such | n/a (generates code for other platforms) | Paid licence | n/a (closed source) | Proprietary |
| **3Commas** | Hosted bot service | Backtesting listed as a feature | Not described | n/a (hosted) | $20 / $50 / $140 a month (monthly billing) | not stated | Proprietary |
| **Cryptohopper** | Hosted bot service | — | — | — | pricing page could not be fetched | not stated | Proprietary |
| **backtesting.py, vectorbt, backtrader** | Python backtesting libraries | Backtest metrics and optimisation; validation is left to the user | Not described | Library only | Free | ~9,000 / ~9,300 / ~23,400 | AGPL-3.0 / custom / GPL-3.0 |

## Where they are better — plainly

- **Scale and community.** freqtrade alone has two orders of magnitude more
  users, real exchange connectors and years of production use. This project
  has no users yet (roadmap V4).
- **Execution realism.** nautilus_trader and hummingbot work with tick and
  order-book data; this project simulates fills from candles and says it
  cannot measure market making honestly (ARCHITECTURE §4).
- **Data and asset coverage.** QuantConnect offers far more history, asset
  classes and brokerages.
- **Robustness tooling maturity.** StrategyQuant X's walk-forward matrix and
  Monte Carlo suite are older and broader than this validator; jesse ships
  Monte Carlo and significance tests inside the framework users already use.
- **Live trading.** Every open-source tool above trades real money today;
  this project is paper only, with testnet readiness the next step.

## Where this project is different

- **It judges backtests it did not run.** `validate-trades` reads a
  freqtrade export or any trade CSV; a user does not have to switch
  frameworks to get a verdict.
- **Selection is priced in, from a record.** The Deflated Sharpe uses the
  number of variants actually tried (from the experiment registry here, or
  `--trials` for a user), and PBO ranks a family of strategies. None of the
  tools checked describes either.
- **The bar is pessimistic by construction.** A strategy passes only if the
  *lower* end of a 90% interval clears 1.0 over three market regimes; a
  check that could not run caps the verdict instead of passing silently.
- **It publishes its own failures.** The sample report runs the validator on
  this project's strategies; all four come out "likely overfit"
  ([VALIDATION_SAMPLE.md](VALIDATION_SAMPLE.md)).
- **Forward results cannot be backfilled** (hash-chained track record) and
  **decayed strategies are demoted automatically** (drift monitor).

## Does the difference survive the matrix?

Partly. jesse and StrategyQuant X both attack overfitting, so "the only tool
that checks for overfitting" would be false. What survives is narrower:
*a framework-independent second opinion that counts your trials*. Whether
anyone will use it for that is the open question V4's interviews must answer.

## Sources (accessed 2026-10-02)

- Stars, licences, last push: GitHub API for freqtrade/freqtrade,
  jesse-ai/jesse, nautechsystems/nautilus_trader, QuantConnect/Lean,
  hummingbot/hummingbot, kernc/backtesting.py, polakowo/vectorbt,
  mementum/backtrader.
- freqtrade: <https://www.freqtrade.io/en/stable/lookahead-analysis/>,
  <https://www.freqtrade.io/en/stable/hyperopt/>
- jesse: <https://github.com/jesse-ai/jesse>
- nautilus_trader: <https://github.com/nautechsystems/nautilus_trader>
- QuantConnect: <https://www.quantconnect.com/pricing/>
- hummingbot: <https://github.com/hummingbot/hummingbot>
- StrategyQuant X: <https://strategyquant.com/doc/strategyquant/cross-checks-automated-strategy-robustness-tests/>,
  <https://strategyquant.com/blog/robustness-tests-and-analysis/>
- 3Commas: <https://3commas.io/pricing>
- Cryptohopper: <https://www.cryptohopper.com/pricing> (returned HTTP 403)
