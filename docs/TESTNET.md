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
python3 main.py testnet resolve --intent-id 12  # resolve one intent from verified exchange evidence
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

Execution intentions are committed to SQLite before submission, with a stable
client order ID. A timeout, lost response, failed journal write, or interrupted
process leaves an execution to recover; it never rolls back an exchange fill
or sends a replacement order merely because an initial lookup says “not found”.
At startup and before each cycle, the engine resolves pending orders and applies
confirmed terminal fills exactly once. Missing commission details are fetched
from the order's executions before settlement.

Recovery restores confirmed holdings automatically and keeps **new entries
halted** until you explicitly run `testnet unkill`. Existing `kill --all` is
preserved. Protective exits resume once their symbol's durable state is
consistent. An unresolved order prevents another submission on that symbol,
and inconsistent execution state prevents a cash checkpoint.

Partial exits settle only what the exchange executed; the remaining quantity
keeps its stops and remains visible. Trade `qty` retains its original entry
quantity, while `remaining_qty` reports the current holding. A remainder that
cannot meet the venue's quantity or notional filters becomes an explicit dust
holding with its quantity and cost basis. Dust remains in balances, equity and
exposure; it does not permanently prevent another entry after you clear the
kill switch. Commissions are charged once, including commissions paid in base.

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
| Size cap | an entry order above `TESTNET_MAX_ORDER_USDT` (default 1,000) | that entry refused; exits are exempt |
| Count cap | more than `TESTNET_MAX_ORDERS_PER_DAY` (default 50) orders in a UTC day | further orders refused |

The paper book's own gates (daily loss kill switch, drawdown throttle,
leverage and cluster caps, manual pause) apply unchanged.

A rejected order on one market is recorded as that market's error, and the
cycle carries on with the others, so one stuck order cannot stop the stops
on every other position.

## Resolving an uncertain execution

`testnet status` lists unresolved intent IDs and their stable client order IDs.
Stop the testnet engine before resolving one: `resolve` takes the same book
lease and refuses to run while another process owns it. It first queries the
stable client ID. If you have an exchange order ID, add `--order-id ID` for a
fallback query; the returned client ID, symbol, side and order ID must agree.
Confirmed terminal fills settle once; a confirmed terminal zero-fill result is
rejected. Active orders, missing identity, lookup errors and ambiguous evidence
remain blocked. The command sends no replacement order and preserves every
existing kill switch. Resolving an intent never runs `unkill`.
Verified acceptance and cumulative fill observations are persisted before active
status or unavailable commissions can interrupt resolution. Once an order has
been observed, later missing API data cannot license a never-accepted attestation,
including after restart; settlement still waits for terminal commission evidence.

A request that truly never reached Binance can remain `UNKNOWN` indefinitely.
Repeated not-found responses or elapsed time do **not** prove it was rejected:
[Binance documents execution uncertainty and asynchronous data sources](https://developers.binance.com/en/docs/products/spot/rest-api).
After investigating the request and independently establishing that no order
was accepted, an operator can explicitly attest that conclusion:

```bash
python3 main.py testnet resolve --intent-id 12 --confirm-never-accepted \
  --reason "request never reached the venue" \
  --evidence "incident record and independent account/order-history inspection"
```

Both reason and evidence are required. This is an operator assertion, not a
conclusion the application can prove from absence. The application additionally
requires a fresh typed not-found response, no known exchange ID or fills,
successful empty open-order and order/trade-history checks for that symbol since
the intent, and consistent reconciliation apart from that selected intent.
Any recent order or fill makes the check conservative and refuses abandonment;
settle actual exchange evidence instead. Only `UNKNOWN` submissions qualify;
legacy orders with known IDs cannot be discarded this way. The timestamped
attestation and checks are stored in `testnet_execution_audit`, separate from
immutable intent inputs and cash events. It changes no cash or owned quantity.
An abandoned exit leaves its position protected and eligible for a later exit;
new entries remain halted until explicit inspection and `testnet unkill`.

## Reconciliation

SQLite execution records are authoritative. `testnet_orders.jsonl` beside the
journal is a compatibility audit export; an export failure cannot undo a fill.
Unchanged recovery cycles refresh the position view without rewriting that log;
a failed export remains eligible for retry.
The first recovery imports an old JSONL log once, retaining a `.legacy` copy,
verifies recorded orders with the exchange, and restores confirmed residual
positions without replaying previously settled cash. Run `testnet reconcile`
to compare exchange holdings with journal inventory. Legacy positions whose
original stop cannot be reconstructed use the engine's protective close path. An
existing starting-balance baseline is never replaced with current holdings;
a missing or invalid baseline blocks new entries.

`reconcile` checks:

- each order, fetched back from the exchange: same filled quantity and
  average price as recorded;
- each journal trade: original and remaining quantities conserve actual net
  buys, sells, base commissions and recorded dust; entry and aggregate exit
  prices agree with executions;
- each base asset: exchange balance = starting balance + remaining inventory
  + explicit dust holdings. Dust is computed from venue filters independently
  of the journal quantities, so an incorrect quantity cannot cancel itself out.

Cash ledger accounting is checked separately by `Journal.ledger_check` and
the execution regressions. Partial realized exits and all commissions settle
once, even while a trade remains open. Final closure records the aggregate
P&L without adding that P&L to cash a second time.

## Status

Built and tested against a fake exchange (`tests/test_testnet.py`). **Not
yet run against the real testnet**: that needs the owner's testnet keys.
