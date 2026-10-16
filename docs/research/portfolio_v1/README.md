# Portfolio view of the standard gate — replay v1

The promotion gate pools trades from independently funded single-market
accounts. This replay holds each strategy's market accounts together as one
book, marks it daily, and measures that book. The method is in
[METHODOLOGY §4](../../METHODOLOGY.md); the results and what they mean are in
[RESULTS §1](../../RESULTS.md). These figures are reported only: the gate's
verdicts still read the trade-pooled evidence.

[`standard_gate.portfolio.json`](standard_gate.portfolio.json) holds, per
strategy:

- the committed verdict and the committed trade-pooled interval;
- the replay's trade-pooled interval, and whether it matches the committed
  one;
- the book: days, capital, net P&L, return, volatility, Sharpe with its 90%
  interval, maximum drawdown, mean pairwise correlation and effective
  independent bets;
- the Deflated Sharpe of the best book against the five books.

## How it was produced

- **Inputs:** the 17 cached Parquets whose first and last bars equal the
  windows recorded in `experiments/standard_gate.results.json` (file names
  and SHA-256 in `inputs`), so the replay sees exactly the gate's bars.
- **Code:** commit `90abde0` (`git_head`; `sources_modified_since_head` is
  false), with the SHA-256 of every source that decides the result.
- **Isolation:** networking disabled, a temporary journal per market run,
  nothing written to `experiments/`, the registry or the verdicts.
- **Not replayed:** regime labels. The daily caches they came from are not
  all kept, so the trade-pooled rows here carry no regime counts.
- **Check:** trade counts and net P&L equal the verified v3 replay
  ([verified_metrics_v3](../verified_metrics_v3/README.md)) to the cent.
  They differ slightly from the committed gate run, because later fixes
  changed some trades.

Reproduce into a scratch directory from the repository root:

```sh
BOT_DB_PATH=/tmp/algo-scratch/t.db ALGO_SKIP_DOTENV=1 \
  python3 scripts/portfolio_replay.py experiments/standard_gate.toml \
  --cache-dir data/cache --output /tmp/algo-portfolio-replay
```
