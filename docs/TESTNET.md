# Testnet: real orders, fake money

*Roadmap V6, readiness only. The standard book's decision code sends real
market orders to the **Binance spot testnet** and books what the exchange
actually did. Nothing here can reach a real-money account (`bot/testnet.py`).*

## Why

The paper broker simulates every fill from candles. Running the same
decisions against a real matching engine measures what the simulation
cannot: real slippage, real commissions, rejected or partial orders. The
roadmap asks for four weeks of testnet results compared with the paper
book before any real-money decision, which stays the owner's.

## Setup (owner)

1. Create testnet API keys at <https://testnet.binance.vision> (log in with
   GitHub; the testnet account is funded with test balances).
2. Put them in `.env`:
   ```
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
   No other key is read. A real `BINANCE_API_KEY` in the environment is
   ignored.

## Commands

```bash
python3 main.py testnet run --once     # one cycle (records the starting balances first)
python3 main.py testnet run            # loop
python3 main.py testnet status         # kill switch, P&L, balances, reconciliation
python3 main.py testnet reconcile      # exchange vs journal; any mismatch engages the kill switch
python3 main.py testnet kill --reason "why"   # block new entries (exits still go out)
python3 main.py testnet kill --all     # block every order
python3 main.py testnet unkill
```

## How it is kept on the testnet

- The ccxt client is created in sandbox mode, and the broker refuses to
  start unless every spot API URL is on `testnet.binance.vision`
  (`assert_testnet`, tested against a production client).
- There is no setting that points it elsewhere.

## What is real and what is not

| Real (from the exchange) | Still simulated |
|---|---|
| fill price, filled quantity, commission | stops and targets: the engine scans each closed bar, then sends a market exit, so a stop fills at the next cycle's market price, not at its level |
| rejected orders (kept as errors; the position is untouched and retried) | no shorts (spot): short entries are refused |
| base-asset fees reduce the quantity held | a fee paid in BNB is valued at the modelled taker fee and flagged in the order log |

The book is `mode='testnet'` in the journal, with its own lease, its own
cash and its own risk state. It trades the standard watchlist's crypto USDT
markets (the testnet has no forex) and is not counted against the paper
books' leverage caps.

## Kill switches

| Switch | Trigger | Effect |
|---|---|---|
| Manual, entries | `testnet kill` | no new entries; exits still go out, so open positions keep their stops |
| Manual, all | `testnet kill --all` | no orders at all |
| Order failures | 3 consecutive failed orders | entry kill engaged, with the last error as the reason |
| Reconciliation | any mismatch in `testnet reconcile` | entry kill engaged |
| Size cap | an order above `TESTNET_MAX_ORDER_USDT` (default 1,000) | that order refused |
| Count cap | more than `TESTNET_MAX_ORDERS_PER_DAY` (default 50) orders in a UTC day | further orders refused |

The paper book's own gates (daily loss kill switch, drawdown throttle,
leverage and cluster caps, manual pause) apply unchanged.

A rejected order on one market is recorded as that market's error, and the
cycle carries on with the others, so one stuck order cannot stop the stops
on every other position.

## Reconciliation

Every order is appended to `testnet_orders.jsonl` beside the journal.
`reconcile` checks:

- each order, fetched back from the exchange: same filled quantity and
  average price as recorded;
- each journal trade: entry and exit price equal to the exchange fill;
- each base asset: exchange balance = starting balance + open quantity in
  the journal + fee dust from past buys.

## Status

Built and tested against a fake exchange (`tests/test_testnet.py`). **Not
yet run against the real testnet**: that needs the owner's testnet keys.
