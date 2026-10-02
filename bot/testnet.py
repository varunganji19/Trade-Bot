"""Binance SPOT TESTNET execution (roadmap V6): real orders, fake money.

The paper broker simulates every fill from candles. This broker sends the
same decisions to the Binance spot **testnet** as market orders and books
what the exchange actually did: the fill price, the filled quantity and the
commission. Comparing it with the paper engine is the reality check on the
fill model (slippage, fees, rejected orders).

TESTNET ONLY, BY CONSTRUCTION
  * Keys are read from BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
    and nothing else; BINANCE_API_KEY is never read.
  * The ccxt client is put in sandbox mode and the broker refuses to start
    unless every API URL it would call is on testnet.binance.vision.
  * There is no switch to turn this off. Real money is a separate decision
    for the owner (docs/ROADMAP.md, V6) and would be separate code.

WHAT IS STILL SIMULATED
  * Brackets are client-side, as in paper: the engine scans each closed bar
    for the stop or target and then sends a MARKET exit, so a stop fills at
    the next cycle's market, not at the level. Spot cannot short, so short
    entries are refused.

KILL SWITCHES (state in testnet_kill.json beside the journal)
  * manual: `testnet kill` blocks new entries (exits still go out, so open
    positions keep their stops); `testnet kill --all` blocks every order;
  * automatic: MAX_CONSECUTIVE_FAILURES order errors in a row, or any
    reconciliation mismatch, trips the entry kill;
  * caps: a per-order notional cap and a daily order-count cap.

RECONCILIATION
  Every order is appended to testnet_orders.jsonl. `reconcile` fetches each
  one back from the exchange and compares it with the journal row it
  filled (quantity and average price), and compares each base asset's
  balance with its starting balance plus the journal's open quantity.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

from bot.broker import PaperBroker, Position
from config import CONFIG, MarketSpec

TESTNET_HOST = "testnet.binance.vision"
KEY_ENV, SECRET_ENV = "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"
MODE = "testnet"

MAX_ORDER_NOTIONAL = float(os.environ.get("TESTNET_MAX_ORDER_USDT", 1_000))
MAX_ORDERS_PER_DAY = int(os.environ.get("TESTNET_MAX_ORDERS_PER_DAY", 50))
MAX_CONSECUTIVE_FAILURES = 3
QTY_TOLERANCE = 1e-6          # relative, for reconciliation
PRICE_TOLERANCE = 1e-6


class TestnetError(RuntimeError):
    """An order was refused (kill switch, cap, exchange error)."""


class NotTestnetError(RuntimeError):
    """The client would talk to something other than the Binance testnet."""


# ------------------------------------------------------------------ state
def _dir() -> str:
    from config import db_dir
    return db_dir()


def kill_path() -> str:
    return os.path.join(_dir(), "testnet_kill.json")


def orders_path() -> str:
    return os.path.join(_dir(), "testnet_orders.jsonl")


def baseline_path() -> str:
    return os.path.join(_dir(), "testnet_baseline.json")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def kill_state() -> dict:
    """{"level": None | "entries" | "all", "reason", "at"}; never raises."""
    try:
        with open(kill_path()) as fh:
            st = json.load(fh)
        if st.get("level") in ("entries", "all"):
            return st
    except (OSError, ValueError):
        pass
    return {"level": None}


def set_kill(level: str | None, reason: str = "") -> None:
    if level not in (None, "entries", "all"):
        raise ValueError("level must be None, 'entries' or 'all'")
    os.makedirs(_dir(), exist_ok=True)
    tmp = kill_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"level": level, "reason": reason, "at": _now()}, fh)
    os.replace(tmp, kill_path())


def _record_order(row: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    with open(orders_path(), "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def recorded_orders() -> list[dict]:
    try:
        with open(orders_path()) as fh:
            return [json.loads(s) for s in fh if s.strip()]
    except OSError:
        return []


# ----------------------------------------------------------------- client
def make_exchange(api_key: str | None = None, secret: str | None = None):
    """A ccxt Binance spot client pointed at the testnet, or an error."""
    import ccxt
    key = api_key or os.environ.get(KEY_ENV)
    sec = secret or os.environ.get(SECRET_ENV)
    if not key or not sec:
        raise NotTestnetError(f"set {KEY_ENV} and {SECRET_ENV} (testnet keys from "
                              f"https://{TESTNET_HOST}) — no other keys are read")
    ex = ccxt.binance({"apiKey": key, "secret": sec, "enableRateLimit": True,
                       "options": {"defaultType": "spot"}})
    ex.set_sandbox_mode(True)
    assert_testnet(ex)
    return ex


def assert_testnet(exchange) -> None:
    """Refuse any client whose spot API URLs leave the testnet host."""
    api = exchange.urls.get("api") if isinstance(getattr(exchange, "urls", None), dict) else None
    urls = [v for v in (api.values() if isinstance(api, dict) else [api]) if isinstance(v, str)]
    spot = [u for u in urls if "/api/" in u]
    if not spot or any(TESTNET_HOST not in u for u in spot):
        raise NotTestnetError(f"refusing: the client's API URLs are not all on {TESTNET_HOST}: "
                              f"{spot or urls}")


# ----------------------------------------------------------------- broker
class TestnetBroker(PaperBroker):
    """PaperBroker whose fills are real testnet market orders.

    Cash, positions and P&L are kept exactly as the paper broker keeps them,
    but from the exchange's fills, so the engine, the risk manager and the
    journal need no changes."""

    # an order can be rejected; the engine then records the error for that
    # market and carries on with the rest (bot/engine.py)
    isolate_market_errors = True

    def __init__(self, exchange, starting_capital: float | None = None, costs=None):
        assert_testnet(exchange)
        super().__init__(starting_capital=starting_capital, costs=costs)
        self.exchange = exchange
        self.failures = 0

    # ---- guards
    def _guard(self, closing: bool, notional: float) -> None:
        level = kill_state()["level"]
        if level == "all" or (level == "entries" and not closing):
            raise TestnetError(f"testnet kill switch ({level}): {kill_state().get('reason', '')}")
        if not closing and notional > MAX_ORDER_NOTIONAL:
            raise TestnetError(f"order notional {notional:.2f} exceeds the cap "
                               f"{MAX_ORDER_NOTIONAL:.2f} (TESTNET_MAX_ORDER_USDT)")
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if sum(1 for o in recorded_orders() if o.get("ts", "").startswith(today)) \
                >= MAX_ORDERS_PER_DAY:
            raise TestnetError(f"daily order cap {MAX_ORDERS_PER_DAY} reached "
                               "(TESTNET_MAX_ORDERS_PER_DAY)")

    def _fail(self, exc: Exception) -> None:
        self.failures += 1
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            set_kill("entries", f"{self.failures} consecutive order failures; last: "
                                f"{type(exc).__name__}: {exc}")

    # ---- the one place an order is sent
    def _market(self, symbol: str, side: str, qty: float, trade_id: int, leg: str,
                closing: bool, ref_price: float) -> dict:
        """Send a market order; return {filled, average, fee_quote, base_fee, id}."""
        self._guard(closing, qty * ref_price)
        try:
            amount = float(self.exchange.amount_to_precision(symbol, qty))
            if amount <= 0:
                raise TestnetError(f"{qty} {symbol} rounds to zero at the exchange's step")
            order = self.exchange.create_order(symbol, "market", side, amount)
            if order.get("average") is None or not order.get("filled"):
                order = self.exchange.fetch_order(order["id"], symbol)
            filled, average = float(order.get("filled") or 0), order.get("average")
            if filled <= 0 or average is None:
                raise TestnetError(f"order {order.get('id')} did not fill ({order.get('status')})")
        except Exception as exc:
            self._fail(exc)
            raise
        self.failures = 0
        base, quote = symbol.split("/")
        fee_quote, base_fee, estimated = 0.0, 0.0, False
        for f in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cost = float(f.get("cost") or 0)
            if f.get("currency") == quote:
                fee_quote += cost
            elif f.get("currency") == base:
                base_fee += cost
                fee_quote += cost * float(average)
            elif cost:
                # paid in a third asset (BNB): its price is not in the order,
                # so the modelled taker fee stands in, and the record says so
                fee_quote += self._fee(filled * float(average), "crypto")
                estimated = True
        fill = {"id": str(order["id"]), "filled": filled, "average": float(average),
                "fee_quote": fee_quote, "base_fee": base_fee, "fee_estimated": estimated}
        _record_order({"ts": _now(), "trade_id": trade_id, "leg": leg, "symbol": symbol,
                       "side": side, "requested": amount, **fill})
        return fill

    # ---- fills
    def open_position(self, spec: MarketSpec, decision, qty: float, price: float,
                      trade_id: int, ts: str = "", decision_bar_ts: float | None = None,
                      maker_entry: bool = False) -> Position:
        if decision.action != "LONG":
            raise TestnetError("the spot testnet cannot short; entry refused")
        fill = self._market(spec.symbol, "buy", qty, trade_id, "entry", False, price)
        # a base-asset fee is taken from the bought quantity; round DOWN so the
        # exit never tries to sell more than the account holds (the remainder
        # is dust, which reconcile() accounts for)
        held = math.floor((fill["filled"] - fill["base_fee"]) * 1e6) / 1e6
        pos = super().open_position(spec, decision, held, fill["average"], trade_id, ts=ts,
                                    decision_bar_ts=decision_bar_ts, maker_entry=True)
        # replace the simulated maker fee with the exchange's actual commission
        self.cash += (pos.entry_fee or 0.0) - fill["fee_quote"]
        self.fees_paid += fill["fee_quote"] - (pos.entry_fee or 0.0)
        pos.entry_fee = fill["fee_quote"]
        return pos

    def close_position(self, spec: MarketSpec, price: float, reason: str):
        key = self.position_key(spec.symbol, spec.timeframe)
        pos = self.positions.get(key)
        if pos is None:
            raise KeyError(key)
        fill = self._market(spec.symbol, "sell", pos.qty, pos.trade_id, "exit", True, price)
        self.positions.pop(key)
        gross = (fill["average"] - pos.entry_price) * fill["filled"]
        fee = fill["fee_quote"]
        self.cash += gross - fee
        self.fees_paid += fee
        entry_fee = pos.entry_fee or 0.0
        pnl = gross - fee - entry_fee
        self.realized_pnl += pnl
        pnl_pct = (fill["average"] / pos.entry_price - 1.0) * 100.0
        return pos, pnl, pnl_pct, fee + entry_fee, fill["average"]


# ---------------------------------------------------------- reconciliation
def snapshot_baseline(exchange, symbols: list[str]) -> dict:
    """Base-asset balances before the testnet book trades (the testnet
    faucet starts every account with balances that are not ours)."""
    bal = exchange.fetch_balance()
    base = {s.split("/")[0]: float((bal.get(s.split("/")[0]) or {}).get("total") or 0)
            for s in symbols}
    with open(baseline_path(), "w") as fh:
        json.dump({"at": _now(), "balances": base}, fh, indent=1)
    return base


def reconcile(exchange, journal, *, trip: bool = True) -> list[str]:
    """Exchange fills and holdings against the journal. Returns problems;
    with trip=True any problem engages the entry kill switch."""
    problems = []
    rows = {t["id"]: t for t in journal.recent_trades(limit=100_000, mode=MODE)}
    for o in recorded_orders():
        try:
            ex = exchange.fetch_order(o["id"], o["symbol"])
        except Exception as exc:
            problems.append(f"order {o['id']} ({o['symbol']}): cannot fetch: {exc}")
            continue
        filled, avg = float(ex.get("filled") or 0), ex.get("average")
        if abs(filled - o["filled"]) > QTY_TOLERANCE * max(1.0, o["filled"]):
            problems.append(f"order {o['id']}: exchange filled {filled}, recorded {o['filled']}")
        if avg is None or abs(float(avg) - o["average"]) > PRICE_TOLERANCE * o["average"]:
            problems.append(f"order {o['id']}: exchange average {avg}, recorded {o['average']}")
        row = rows.get(o["trade_id"])
        if row is None:
            problems.append(f"order {o['id']}: no journal trade #{o['trade_id']}")
            continue
        price = row["entry_price"] if o["leg"] == "entry" else row.get("exit_price")
        if price is None or abs(float(price) - o["average"]) > PRICE_TOLERANCE * o["average"]:
            problems.append(f"trade #{o['trade_id']} {o['leg']}: journal price {price}, "
                            f"exchange fill {o['average']}")
    try:
        with open(baseline_path()) as fh:
            base = json.load(fh)["balances"]
    except (OSError, ValueError, KeyError):
        base = None
    if base is not None:
        held: dict[str, float] = {}
        for t in journal.open_trades(mode=MODE):
            asset = t["symbol"].split("/")[0]
            held[asset] = held.get(asset, 0.0) + float(t["qty"])
        # dust: what each buy delivered beyond the quantity the book holds
        for o in recorded_orders():
            row = rows.get(o["trade_id"])
            if o["leg"] == "entry" and row is not None:
                asset = o["symbol"].split("/")[0]
                held[asset] = held.get(asset, 0.0) + (o["filled"] - o["base_fee"]
                                                      - float(row["qty"]))
        bal = exchange.fetch_balance()
        for asset in sorted(set(base) | set(held)):
            want = base.get(asset, 0.0) + held.get(asset, 0.0)
            have = float((bal.get(asset) or {}).get("total") or 0)
            if abs(have - want) > max(1e-8, QTY_TOLERANCE * max(have, want)):
                problems.append(f"{asset}: exchange holds {have}, journal expects {want} "
                                f"(baseline {base.get(asset, 0.0)} + open and dust "
                                f"{held.get(asset, 0.0)})")
    if problems and trip:
        set_kill("entries", f"reconciliation: {len(problems)} mismatch(es); first: {problems[0]}")
    return problems


def testnet_watchlist(cfg=None) -> list[MarketSpec]:
    """The standard watchlist's crypto USDT specs: the testnet has no forex."""
    cfg = cfg or CONFIG
    return [s for s in cfg.watchlist if s.kind == "crypto" and s.symbol.endswith("/USDT")]


def build_testnet_engine(exchange=None, journal=None):
    """The standard book's decision code, trading the testnet: crypto USDT
    specs only, journal book mode='testnet' with its own lease."""
    from dataclasses import replace

    from bot.engine import TradingEngine
    cfg = replace(CONFIG, watchlist=testnet_watchlist())
    if not cfg.watchlist:
        raise TestnetError("the watchlist has no crypto USDT specs to trade on the testnet")
    exchange = exchange or make_exchange()
    broker = TestnetBroker(exchange, starting_capital=cfg.paper_capital, costs=cfg.costs)
    return TradingEngine(cfg=cfg, mode=MODE, journal=journal, broker=broker)
