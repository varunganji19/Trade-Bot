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
from dataclasses import dataclass
from types import SimpleNamespace

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
            lines = fh.readlines()
    except OSError:
        return []
    rows = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            if i == len(lines)-1 and not line.endswith("\n"):
                break  # preserve the complete legacy prefix after a crash
            raise TestnetError("invalid legacy testnet order log; recovery requires inspection")
    return rows


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
@dataclass
class ExecutionResult:
    state: str
    trade_id: int
    closed: bool = False
    position: Position | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    executed_price: float = 0.0
    filled_qty: float = 0.0
    error: str | None = None


class TestnetBroker(PaperBroker):
    """A position view over durable testnet executions, never simulated undo."""
    isolate_market_errors = True
    external_execution = True

    def __init__(self, exchange, starting_capital=None, costs=None):
        assert_testnet(exchange)
        super().__init__(starting_capital=starting_capital, costs=costs)
        self.exchange, self.journal = exchange, None
        self.starting_capital = self.cash
        self.failures = 0
        self.dust_positions = []
        self.last_execution_error = None
        self._needs_reload = False
        self._exported_revision = None

    def bind(self, journal):
        self.journal = journal
        self._reload()
        return self

    @property
    def pending_symbols(self):
        return {o["symbol"] for o in self.journal.testnet_orders(pending=True)} if self.journal else set()

    @property
    def execution_blocked(self):
        return self._needs_reload or bool(self.pending_symbols)

    def equity(self, price_map=None):
        eq = super().equity(price_map)
        for p in self.dust_positions:
            mark = (price_map or {}).get(p.symbol, p.entry_price)
            eq += (mark-p.entry_price)*p.qty
        return eq

    def _reload(self):
        if self.journal is None:
            raise TestnetError("testnet execution requires a bound journal")
        book = self.journal.testnet_book(self.starting_capital)
        self.positions.clear()
        for row in book["positions"]:
            self.restore_position(row, "crypto", timeframe=row["timeframe"])
        self.cash, self.fees_paid, self.realized_pnl = book["cash"], book["fees_paid"], book["realized_pnl"]
        self.dust_positions = [SimpleNamespace(symbol=r["symbol"], qty=r["qty"],
                                               entry_price=r["entry_price"], entry_fee=r["entry_fee"],
                                               trade_id=r["trade_id"], timeframe=r["timeframe"],
                                               side="long") for r in book["dust"]]
        self._needs_reload = False

    def _trip(self, reason):
        # Recovery must never downgrade a manual full kill or resume entries.
        if kill_state()["level"] != "all":
            set_kill("entries", reason)
        self.last_execution_error = reason

    def _guard(self, closing, notional):
        level = kill_state()["level"]
        if level == "all" or (level == "entries" and not closing):
            raise TestnetError(f"testnet kill switch ({level}): {kill_state().get('reason', '')}")
        if not closing and self.execution_blocked:
            raise TestnetError("unresolved testnet execution: new entries halted")
        if not closing and notional > MAX_ORDER_NOTIONAL:
            raise TestnetError(f"order notional {notional:.2f} exceeds the cap {MAX_ORDER_NOTIONAL:.2f} (TESTNET_MAX_ORDER_USDT)")
        if not closing:
            try:
                with open(baseline_path()) as fh:
                    balances = json.load(fh)["balances"]
                if not isinstance(balances, dict) or any(not math.isfinite(float(v)) or float(v)<0 for v in balances.values()):
                    raise ValueError("invalid balances")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._trip(f"testnet baseline unavailable: {exc}")
                raise TestnetError("a valid starting-balance baseline is required for entries") from exc
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        orders = self.journal.testnet_orders() if self.journal else recorded_orders()
        if sum(o.get("created_ts", o.get("ts", "")).startswith(today) for o in orders) >= MAX_ORDERS_PER_DAY:
            raise TestnetError(f"daily order cap {MAX_ORDERS_PER_DAY} reached (TESTNET_MAX_ORDERS_PER_DAY)")

    def _fail(self, exc):
        self.failures += 1
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            self._trip(f"{self.failures} consecutive order failures; last: {type(exc).__name__}: {exc}")

    def _amount(self, symbol, qty):
        if hasattr(self.exchange, "load_markets") and not getattr(self.exchange, "markets", None):
            self.exchange.load_markets()
        amount = float(self.exchange.amount_to_precision(symbol, qty))
        if not math.isfinite(amount) or amount <= 0:
            raise TestnetError(f"{qty} {symbol} rounds to zero at the exchange's step")
        return amount

    def _tradable(self, symbol, qty, price):
        import ccxt
        if symbol not in getattr(self.exchange, "markets", {}) and hasattr(self.exchange, "load_markets"):
            self.exchange.load_markets()
        try:
            amount = float(self.exchange.amount_to_precision(symbol, max(0.0, qty)))
        except ccxt.InvalidOrder:
            # CCXT rejects a quantity that truncates to zero instead of
            # returning "0". That is explicit precision dust, not an
            # unresolved external execution.
            return False
        market = (getattr(self.exchange, "markets", None) or {}).get(symbol, {})
        limits = market.get("limits", {})
        return (amount > 0 and amount >= ((limits.get("amount") or {}).get("min") or 0)
                and amount*price >= ((limits.get("cost") or {}).get("min") or 0))

    def _fees(self, intent, order, filled):
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") is not None else [])
        if not fees:
            # Order-status responses do not necessarily contain commissions.
            trades, seen, cursor = [], set(), None
            fetch = getattr(self.exchange, "fetch_my_trades", None)
            if fetch is None:
                raise TestnetError("confirmed order is missing its commission details")
            while True:
                params = {"orderId": str(order["id"])}
                if cursor is not None:
                    params["fromId"] = cursor
                page = fetch(intent["symbol"], limit=1000, params=params)
                for t in page:
                    oid = t.get("order") or (t.get("info") or {}).get("orderId")
                    if str(oid) != str(order["id"]) or str(t["id"]) in seen:
                        continue
                    seen.add(str(t["id"]))
                    trades.append(t)
                if len(page) < 1000:
                    break
                nxt = max(int(t["id"]) for t in page)+1
                if cursor is not None and nxt <= cursor:
                    raise TestnetError("order fill pagination did not advance")
                cursor = nxt
            if not math.isclose(sum(float(t["amount"]) for t in trades), filled, rel_tol=1e-9, abs_tol=1e-12):
                raise TestnetError("terminal order and execution quantities do not yet agree")
            fees = [f for t in trades for f in (t.get("fees") or ([t["fee"]] if t.get("fee") is not None else []))]
            if any(t.get("fee") is None and not t.get("fees") for t in trades):
                raise TestnetError("execution commission details are incomplete")
        base, quote = intent["symbol"].split("/")
        quote_fee, base_fee, estimated = 0.0, 0.0, False
        for f in fees:
            if f.get("cost") is None or (f.get("currency") is None and f.get("cost")):
                raise TestnetError("execution commission details are incomplete")
            cost = float(f.get("cost") or 0)
            if f.get("currency") == quote:
                quote_fee += cost
            elif f.get("currency") == base:
                base_fee += cost
                quote_fee += cost*float(order["average"])
            elif cost:
                estimated = True
        if estimated:
            quote_fee += self._fee(filled*float(order["average"]), "crypto")
        return quote_fee, base_fee, estimated

    def _snapshot(self, intent, order, owner_token=None, *, observed=False):
        from bot.broker import quantize_price
        if not observed:
            self._observe(intent, order, owner_token)
        status = str(order.get("status") or "").lower()
        if status not in {"closed", "canceled", "expired", "rejected"}:
            # Only a confirmed live order may be cancelled. Never replace it.
            if status == "open" and hasattr(self.exchange, "cancel_order"):
                self.exchange.cancel_order(order["id"], intent["symbol"])
            order = self.exchange.fetch_order(order["id"], intent["symbol"])
            self._observe(intent, order, owner_token)
            status = str(order.get("status") or "").lower()
        if status not in {"closed", "canceled", "expired", "rejected"}:
            raise TestnetError("submitted order has no terminal outcome yet")
        self._verify_resolution_fills(intent, order)
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            return None
        if order.get("average") is None:
            raise TestnetError("terminal order has no average price yet")
        average = float(order["average"])
        if not math.isfinite(average) or average <= 0 or not math.isfinite(filled):
            raise TestnetError("invalid exchange fill")
        fee, base_fee, estimated = self._fees(intent, order, filled)
        snap = {"id": str(order["id"]), "terminal": True, "filled": filled,
                "average": average, "fee_quote": fee, "base_fee": base_fee,
                "fee_estimated": estimated, "dust_qty": 0.0}
        p = intent["payload"]
        if intent["leg"] == "entry":
            if intent["legacy"]:
                snap["stop"], snap["target"] = p.get("stop"), p.get("target")
            else:
                distance = float(p["stop_distance"])
                snap["stop"] = quantize_price(average-distance, "crypto")
                snap["target"] = quantize_price(average+float(p["target_rr"])*distance, "crypto") if p.get("target_rr") else None
        else:
            row = self.journal.testnet_trade(intent["trade_id"])
            remaining = row["remaining_qty"] if row["remaining_qty"] is not None else row["qty"]
            rest = max(0.0, remaining-filled-base_fee)
            if rest and not self._tradable(intent["symbol"], rest, average):
                snap["dust_qty"] = rest
        return snap

    def _result(self, intent, row):
        key = self.position_key(row["symbol"], row["timeframe"])
        snap = intent.get("snapshot") or {}
        return ExecutionResult("SETTLED", row["id"], row["status"] == "CLOSED",
                               self.positions.get(key), row["pnl"] or 0.0, row["pnl_pct"] or 0.0,
                               row["fees"] if row["fees"] is not None else (row["entry_fee"] or 0),
                               snap.get("average", 0.0), snap.get("filled", 0.0))

    def _observe(self, intent, order, owner_token):
        status = str(order.get("status") or "")
        # Even an accepted response with unavailable fill details is evidence
        # against "never accepted"; keep the identity before validating fills.
        self.journal.observe_testnet_order(intent["id"], order.get("id"), None, status,
                                           owner_token=owner_token)
        self._verify_resolution_fills(intent, order)
        self.journal.observe_testnet_order(intent["id"], order["id"], float(order["filled"]), status,
                                           owner_token=owner_token)
        intent["exchange_order_id"] = str(order["id"])
        intent["observed_filled"] = max(intent["observed_filled"], float(order["filled"]))

    def _consume(self, intent, order, owner_token, *, observed=False):
        self._needs_reload = True
        snap = self._snapshot(intent, order, owner_token, observed=observed)
        if snap is None:
            self.journal.testnet_order_state(intent["id"], "REJECTED", "terminal zero-fill order", owner_token=owner_token)
            self._reload()
            return ExecutionResult("REJECTED", intent["trade_id"], error="terminal zero-fill order")
        self.journal.confirm_testnet_order(intent["id"], snap, owner_token=owner_token)
        intent["snapshot"] = snap
        row = self.journal.settle_testnet_order(intent["id"], owner_token=owner_token)
        self._reload()
        self.failures = 0
        self._export()
        return self._result(intent, row)

    def _submit(self, intent, owner_token):
        import ccxt
        self.journal.testnet_order_state(intent["id"], "SUBMITTING", owner_token=owner_token)
        try:
            order = self.exchange.create_order(intent["symbol"], "market",
                                                "buy" if intent["leg"] == "entry" else "sell",
                                                intent["requested"], params={"newClientOrderId": intent["client_order_id"]})
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.AuthenticationError,
                ccxt.PermissionDenied, ccxt.BadSymbol) as exc:
            self.journal.testnet_order_state(intent["id"], "REJECTED", str(exc), owner_token=owner_token)
            self._fail(exc)
            raise
        except Exception as exc:
            self.journal.testnet_order_state(intent["id"], "UNKNOWN", str(exc), owner_token=owner_token)
            self._trip(f"unknown testnet execution {intent['client_order_id']}: {exc}")
            self._fail(exc)
            raise
        try:
            return self._consume(intent, order, owner_token)
        except Exception as exc:
            # The durable SUBMITTING/CONFIRMED intent survives a failed write.
            self._trip(f"testnet execution requires recovery {intent['client_order_id']}: {exc}")
            try:
                self._reload()
            except Exception:
                self._needs_reload = True
            raise

    def _import(self, owner_token):
        if self.journal.testnet_legacy_imported():
            return
        if os.path.exists(orders_path()):
            import shutil
            backup = orders_path()+".legacy"
            if not os.path.exists(backup):
                shutil.copy2(orders_path(), backup)
        if self.journal.import_testnet_orders(recorded_orders(), owner_token=owner_token):
            self._trip("legacy testnet execution recovery; clear the entry kill explicitly after inspection")

    def recover(self, owner_token=None):
        self._import(owner_token)
        problems = []
        pending = self.journal.testnet_orders(pending=True)
        if pending:
            self._trip("testnet execution recovery; new entries remain halted until explicitly cleared")
        for intent in pending:
            try:
                if intent["state"] == "PREPARED":
                    # No call was started; abandon this stale intention safely.
                    self.journal.testnet_order_state(intent["id"], "REJECTED", "unsubmitted intent after restart", owner_token=owner_token)
                    continue
                if intent["state"] == "CONFIRMED":
                    self.journal.settle_testnet_order(intent["id"], owner_token=owner_token)
                else:
                    if intent["state"] == "SUBMITTING":
                        self.journal.testnet_order_state(intent["id"], "UNKNOWN", "submission outcome requires lookup", owner_token=owner_token)
                    if intent["exchange_order_id"]:
                        order = self.exchange.fetch_order(intent["exchange_order_id"], intent["symbol"])
                    else:
                        order = self.exchange.fetch_order(None, intent["symbol"],
                                                         params={"origClientOrderId": intent["client_order_id"]})
                    self._consume(intent, order, owner_token)
            except Exception as exc:
                problems.append(f"{intent['symbol']} {intent['client_order_id']}: {type(exc).__name__}: {exc}")
                # Not-found can lag acceptance; it never licenses resubmission.
        self._reload()
        export_error = self._export()
        if export_error:
            problems.append(export_error)
        self.last_execution_error = "; ".join(problems) if problems else None
        return problems

    @staticmethod
    def _verify_resolution_identity(intent, order, order_id=None):
        expected_id = intent["exchange_order_id"] or order_id
        if (not order.get("id") or (expected_id and str(order["id"]) != str(expected_id))
                or order.get("symbol") != intent["symbol"]
                or order.get("side") != ("buy" if intent["leg"] == "entry" else "sell")
                or (not intent["legacy"] and order.get("clientOrderId") != intent["client_order_id"])):
            raise TestnetError("exchange order identity is missing or differs from the execution intent")

    @staticmethod
    def _verify_resolution_fills(intent, order):
        try:
            filled = float(order["filled"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TestnetError("exchange fill quantity is unknown") from exc
        if not math.isfinite(filled) or filled < 0:
            raise TestnetError("exchange fill quantity is invalid")
        known_filled = max(intent["observed_filled"], (intent["snapshot"] or {}).get("filled", 0))
        if filled < known_filled:
            raise TestnetError("exchange cumulative fills are below the execution's known fills")

    def resolve(self, intent_id, *, order_id=None, confirm_never_accepted=False,
                reason="", evidence="", owner_token=None):
        """Resolve one intent under the lease; never replace an uncertain order.

        API absence is not proof of rejection. The optional abandonment path
        records the operator's explicit attestation and all supporting checks.
        """
        import ccxt
        intent = self.journal.testnet_resolution_order(intent_id, owner_token=owner_token)
        if confirm_never_accepted and (not reason.strip() or not evidence.strip()):
            raise TestnetError("--confirm-never-accepted requires a nonempty reason and evidence")
        if kill_state()["level"] is None:
            set_kill("entries", "operator execution resolution; clear the entry kill explicitly after inspection")
        if intent["state"] == "SETTLED":
            self._reload()
            return self._result(intent, self.journal.testnet_trade(intent["trade_id"]))
        if intent["state"] == "REJECTED":
            return ExecutionResult("REJECTED", intent["trade_id"], error=intent["error"])
        if intent["state"] == "PREPARED":
            self.journal.testnet_order_state(intent_id, "REJECTED", "unsubmitted intent", owner_token=owner_token)
            self._reload()
            return ExecutionResult("REJECTED", intent["trade_id"], error="unsubmitted intent")
        if intent["state"] == "CONFIRMED":
            row = self.journal.settle_testnet_order(intent_id, owner_token=owner_token)
            self._reload()
            self._export()
            return self._result(intent, row)
        if intent["state"] == "SUBMITTING":
            self.journal.testnet_order_state(intent_id, "UNKNOWN", "operator status lookup", owner_token=owner_token)
            intent["state"] = "UNKNOWN"
        if intent["state"] != "UNKNOWN":
            raise TestnetError("execution is not eligible for operator resolution")
        try:
            # The stable client ID is canonical for uncertain new submissions.
            # Legacy logs have only the already recorded exchange order ID.
            lookup_id = intent["exchange_order_id"] if intent["legacy"] else None
            params = {} if lookup_id else {"origClientOrderId": intent["client_order_id"]}
            try:
                order = self.exchange.fetch_order(lookup_id, intent["symbol"], params=params)
            except ccxt.OrderNotFound:
                if not order_id:
                    raise
                order = self.exchange.fetch_order(str(order_id), intent["symbol"])
        except ccxt.OrderNotFound as exc:
            if not confirm_never_accepted:
                raise TestnetError("order remains UNKNOWN; not-found does not prove rejection") from exc
            if (intent["legacy"] or intent["exchange_order_id"] or order_id
                    or intent["observed_filled"] or (intent["snapshot"] or {}).get("filled", 0)):
                raise TestnetError("known order identity or fills prevent a never-accepted attestation") from exc
            since = int(dt.datetime.fromisoformat(intent["created_ts"]).timestamp()*1000)
            checks = {"client_order_id": intent["client_order_id"], "lookup": "OrderNotFound",
                      "checked_at": _now(), "since_ms": since}
            for name in ("fetch_open_orders", "fetch_orders", "fetch_my_trades"):
                fetch = getattr(self.exchange, name, None)
                if not callable(fetch):
                    raise TestnetError(f"cannot verify absence: exchange lacks {name}") from exc
                rows = fetch(intent["symbol"]) if name == "fetch_open_orders" else fetch(intent["symbol"], since=since)
                if not isinstance(rows, list) or rows:
                    raise TestnetError(f"cannot attest never accepted: {name} contains orders/fills or ambiguous evidence") from exc
                checks[name] = "empty"
            expected_problem = f"{intent['symbol']} order {intent['client_order_id']}: unresolved UNKNOWN"
            problems = [p for p in reconcile(self.exchange, self.journal, trip=False) if p != expected_problem]
            if problems:
                raise TestnetError("cannot attest never accepted: reconciliation mismatch: " + "; ".join(problems)) from exc
            checks["reconciliation"] = "matches excluding selected unknown execution"
            self.journal.abandon_testnet_order(intent_id, reason, evidence, checks, owner_token=owner_token)
            self._reload()
            return ExecutionResult("REJECTED", intent["trade_id"], error="operator attests never accepted")
        self._verify_resolution_identity(intent, order, order_id)
        self._observe(intent, order, owner_token)
        if str(order.get("status") or "").lower() not in {"closed", "canceled", "expired", "rejected"}:
            raise TestnetError("exchange order is still active or its terminal status is unknown")
        return self._consume(intent, order, owner_token, observed=True)

    def execute_entry(self, spec, decision, qty, price, *, ts="", decision_bar_ts=None,
                      fill_bar_ts=None, owner_token=None):
        if decision.action != "LONG":
            raise TestnetError("the spot testnet cannot short; entry refused")
        if self.journal is None:
            raise TestnetError("testnet execution requires a bound journal")
        self._import(owner_token)
        self._guard(False, qty*price)
        amount = self._amount(spec.symbol, qty)
        intent = self.journal.prepare_testnet_entry(spec, decision, amount, price, ts=ts,
                                                   decision_bar_ts=decision_bar_ts, fill_bar_ts=fill_bar_ts,
                                                   owner_token=owner_token)
        return self._submit(intent, owner_token)

    def execute_exit(self, spec, price, reason, *, owner_token=None):
        self._import(owner_token)
        pos = self.positions.get(self.position_key(spec.symbol, spec.timeframe))
        if pos is None:
            raise KeyError(self.position_key(spec.symbol, spec.timeframe))
        self._guard(True, pos.qty*price)
        if spec.symbol in self.pending_symbols:
            raise TestnetError("unresolved testnet execution owns this symbol")
        amount = self._amount(spec.symbol, pos.qty)
        intent = self.journal.prepare_testnet_exit(pos.trade_id, amount, price, reason, owner_token=owner_token)
        return self._submit(intent, owner_token)

    def open_position(self, spec, decision, qty, price, trade_id=None, ts="",
                      decision_bar_ts=None, maker_entry=False, fill_bar_ts=None):
        # Compatibility for direct callers; the engine uses execute_entry.
        result = self.execute_entry(spec, decision, qty, price, ts=ts,
                                    decision_bar_ts=decision_bar_ts, fill_bar_ts=fill_bar_ts)
        if result.position is None:
            raise TestnetError(result.error or "entry did not fill")
        return result.position

    def close_position(self, spec, price, reason):
        pos = self.positions.get(self.position_key(spec.symbol, spec.timeframe))
        result = self.execute_exit(spec, price, reason)
        if not result.closed:
            raise TestnetError("partial exit settled; remaining inventory is still managed")
        return pos, result.pnl, result.pnl_pct, result.fees, result.executed_price

    def _export(self):
        # Compatibility/audit export. SQLite remains authoritative if this fails.
        revision = (orders_path(), self.journal.testnet_export_revision())
        if revision == self._exported_revision and os.path.isfile(orders_path()):
            return None
        rows = []
        for o in self.journal.testnet_orders():
            if not o["snapshot"] or o["state"] != "SETTLED":
                continue
            rows.append({"ts": o["created_ts"], "trade_id": o["trade_id"], "leg": o["leg"],
                         "symbol": o["symbol"], "side": "buy" if o["leg"] == "entry" else "sell",
                         "requested": o["requested"], **o["snapshot"]})
        try:
            os.makedirs(_dir(), exist_ok=True)
            tmp = orders_path()+".tmp"
            with open(tmp, "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True)+"\n")
            os.replace(tmp, orders_path())
            self._exported_revision = revision
        except OSError as exc:
            self.last_execution_error = f"testnet audit export: {exc}"
            return self.last_execution_error
        return None


# ---------------------------------------------------------- reconciliation
def snapshot_baseline(exchange, symbols: list[str]) -> dict:
    """Base-asset balances before the testnet book trades (the testnet
    faucet starts every account with balances that are not ours)."""
    from bot.journal import Journal
    assert_testnet(exchange)
    if os.path.exists(baseline_path()):
        raise TestnetError("the existing testnet baseline must not be replaced")
    journal = Journal()
    if recorded_orders() or journal.testnet_orders() or journal.open_trades(mode=MODE):
        raise TestnetError("starting balances cannot be captured after testnet trading began")
    bal = exchange.fetch_balance()
    base = {s.split("/")[0]: float((bal.get(s.split("/")[0]) or {}).get("total") or 0)
            for s in symbols}
    os.makedirs(_dir(), exist_ok=True)
    with open(baseline_path(), "w") as fh:
        json.dump({"at": _now(), "balances": base}, fh, indent=1)
    return base


def reconcile(exchange, journal, *, trip: bool = True) -> list[str]:
    """Independent order, quantity, dust and asset-balance verification."""
    assert_testnet(exchange)
    problems = []
    broker = TestnetBroker(exchange).bind(journal)
    orders = journal.testnet_orders()
    rows = {t["id"]: t for t in journal.recent_trades(limit=100_000, mode=MODE)}
    quantities, dust_totals = {}, {}
    for o in orders:
        if o["state"] == "REJECTED":
            continue
        if o["state"] != "SETTLED":
            problems.append(f"{o['symbol']} order {o['client_order_id']}: unresolved {o['state']}")
            continue
        recorded = o["snapshot"]
        try:
            ex = exchange.fetch_order(o["exchange_order_id"], o["symbol"])
            filled, avg = float(ex.get("filled") or 0), ex.get("average")
            if abs(filled-recorded["filled"]) > max(1e-12, QTY_TOLERANCE*recorded["filled"]):
                problems.append(f"order {o['exchange_order_id']}: exchange filled {filled}, recorded {recorded['filled']}")
            if avg is None or abs(float(avg)-recorded["average"]) > PRICE_TOLERANCE*recorded["average"]:
                problems.append(f"order {o['exchange_order_id']}: exchange average {avg}, recorded {recorded['average']}")
            fee, base_fee, _ = broker._fees(o, ex, filled)
            if not math.isclose(fee, recorded["fee_quote"], rel_tol=1e-6, abs_tol=1e-10) or not math.isclose(base_fee, recorded["base_fee"], rel_tol=1e-6, abs_tol=1e-12):
                problems.append(f"order {o['exchange_order_id']}: exchange commission differs from recorded")
        except Exception as exc:
            problems.append(f"order {o['exchange_order_id']} ({o['symbol']}): cannot verify: {exc}")
        row = rows.get(o["trade_id"])
        if row is None:
            problems.append(f"order {o['exchange_order_id']}: no journal trade #{o['trade_id']}")
            continue
        q = quantities.setdefault(row["id"], {"entry": 0.0, "sold": 0.0, "dust": 0.0,
                                              "entry_fee": 0.0, "exit_qty": 0.0,
                                              "exit_value": 0.0, "exit_fee": 0.0})
        if o["leg"] == "entry":
            q["entry"] += recorded["filled"]-recorded["base_fee"]
            q["entry_fee"] += recorded["fee_quote"]
            if not math.isclose(row["entry_price"], recorded["average"], rel_tol=PRICE_TOLERANCE):
                problems.append(f"trade #{row['id']} entry: journal price {row['entry_price']}, exchange fill {recorded['average']}")
        else:
            q["sold"] += recorded["filled"]+recorded["base_fee"]
            q["exit_qty"] += recorded["filled"]
            q["exit_value"] += recorded["filled"]*recorded["average"]
            q["exit_fee"] += recorded["fee_quote"]
            dust = recorded.get("dust_qty", 0.0)
            q["dust"] += dust
            if dust and broker._tradable(o["symbol"], dust, recorded["average"]):
                problems.append(f"trade #{row['id']}: recorded dust was tradable at its transfer price")
    book = journal.testnet_book(CONFIG.paper_capital)
    for d in book["dust"]:
        dust_totals[d["trade_id"]] = dust_totals.get(d["trade_id"], 0.0)+d["qty"]
    for trade_id, q in quantities.items():
        row = rows[trade_id]
        want_remaining = q["entry"]-q["sold"]-q["dust"]
        remaining = row["remaining_qty"] if row["remaining_qty"] is not None else row["qty"]
        for label, actual, wanted in (("entry quantity", row["qty"], q["entry"]),
                                      ("remaining quantity", remaining, want_remaining),
                                      ("dust quantity", dust_totals.get(trade_id, 0.0), q["dust"])):
            if not math.isclose(actual, wanted, rel_tol=QTY_TOLERANCE, abs_tol=1e-12):
                problems.append(f"trade #{trade_id}: journal {label} {actual}, executions require {wanted}")
        if row["status"] == "CLOSED" and remaining > 1e-12:
            problems.append(f"trade #{trade_id}: CLOSED trade still owns {remaining}")
        if not math.isclose(row["entry_fee"] or 0.0, q["entry_fee"], rel_tol=1e-6, abs_tol=1e-10):
            problems.append(f"trade #{trade_id}: journal entry commission differs from executions")
        if q["exit_qty"]:
            avg = q["exit_value"]/q["exit_qty"]
            if row["exit_price"] is None or not math.isclose(row["exit_price"], avg, rel_tol=PRICE_TOLERANCE):
                problems.append(f"trade #{trade_id} exit: journal price {row['exit_price']}, executions require {avg}")
            if not math.isclose(row["exit_fee"], q["exit_fee"], rel_tol=1e-6, abs_tol=1e-10):
                problems.append(f"trade #{trade_id}: journal exit commission differs from executions")
    try:
        with open(baseline_path()) as fh:
            baseline = json.load(fh)["balances"]
        if not isinstance(baseline, dict) or any(not math.isfinite(float(x)) or float(x)<0 for x in baseline.values()):
            raise ValueError("invalid starting balances")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"baseline unavailable: {exc}; existing balances cannot become a new baseline")
        baseline = None
    if baseline is not None:
        held = {}
        for row in book["positions"]:
            asset = row["symbol"].split("/")[0]
            qty = row["remaining_qty"] if row["remaining_qty"] is not None else row["qty"]
            held[asset] = held.get(asset, 0.0)+qty
        for d in book["dust"]:
            asset = d["symbol"].split("/")[0]
            held[asset] = held.get(asset, 0.0)+d["qty"]
        bal = exchange.fetch_balance()
        for asset in sorted(set(baseline)|set(held)):
            want = float(baseline.get(asset, 0.0))+held.get(asset, 0.0)
            have = float((bal.get(asset) or {}).get("total") or 0)
            if abs(have-want) > max(1e-8, QTY_TOLERANCE*max(have, want)):
                problems.append(f"{asset}: exchange holds {have}, journal expects {want} (baseline + remaining and dust)")
    if not journal.testnet_legacy_imported() and recorded_orders():
        problems.append("legacy testnet executions require recovery before trading")
    if problems and trip and kill_state()["level"] != "all":
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
    if journal is None:
        from bot.journal import Journal
        journal = Journal()
    broker.bind(journal)
    return TradingEngine(cfg=cfg, mode=MODE, journal=journal, broker=broker)
