"""Durable spot-testnet order intents and atomic, once-only settlement.

An intent is committed before network IO. A terminal cumulative order snapshot
is settled once, in the same transaction as its position and cash effects.
"""
from __future__ import annotations

import json
import math
import uuid

import bot.journal as base


def _order(row):
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"])
    out["snapshot"] = json.loads(out["snapshot"]) if out["snapshot"] else None
    return out


class ExecutionsMixin:
    @base._retry_busy
    def prepare_testnet_entry(self, spec, decision, qty, price, *, ts=None,
                              decision_bar_ts=None, fill_bar_ts=None, owner_token=None):
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("entry quantity must be positive and finite")
        payload = {"stop_distance": decision.stop_distance, "target_rr": decision.target_rr,
                   "reason": "entry", "ref_price": price}
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            if conn.execute("SELECT 1 FROM testnet_execution_orders WHERE symbol=?"
                            " AND state NOT IN ('SETTLED','REJECTED')", (spec.symbol,)).fetchone():
                raise ValueError("an unresolved testnet order already owns this symbol")
            if conn.execute("SELECT 1 FROM trades WHERE mode='testnet' AND symbol=?"
                            " AND status IN ('OPEN','PENDING')", (spec.symbol,)).fetchone():
                raise ValueError("a testnet position already owns this symbol")
            cur = conn.execute(
                "INSERT INTO trades(symbol,side,qty,entry_price,strategy,status,opened_ts,"
                " rationale_open,mode,timeframe,decision_bar_ts,fill_bar_ts,remaining_qty)"
                " VALUES (?,'long',?,?,?,'PENDING',?,?,'testnet',?,?,?,0)",
                (spec.symbol, qty, price, decision.strategy_name or "orchestrator",
                 base._iso(ts), decision.rationale, spec.timeframe, decision_bar_ts, fill_bar_ts))
            return self._prepare_testnet_order(conn, cur.lastrowid, spec.symbol, "entry", qty, payload)

    @base._retry_busy
    def prepare_testnet_exit(self, trade_id, qty, price, reason, *, owner_token=None):
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            row = conn.execute("SELECT * FROM trades WHERE id=? AND mode='testnet'",
                               (trade_id,)).fetchone()
            if row is None or row["status"] != "OPEN":
                raise ValueError("exit requires an open testnet trade")
            if conn.execute("SELECT 1 FROM testnet_execution_orders WHERE symbol=?"
                            " AND state NOT IN ('SETTLED','REJECTED')", (row["symbol"],)).fetchone():
                raise ValueError("an unresolved testnet order already owns this symbol")
            remaining = row["remaining_qty"] if row["remaining_qty"] is not None else row["qty"]
            if not math.isfinite(qty) or qty <= 0 or qty > remaining + 1e-12:
                raise ValueError("exit quantity exceeds the owned testnet inventory")
            return self._prepare_testnet_order(conn, trade_id, row["symbol"], "exit", qty,
                                                {"reason": reason, "ref_price": price})

    @staticmethod
    def _prepare_testnet_order(conn, trade_id, symbol, leg, qty, payload):
        client_id = "algo-" + uuid.uuid4().hex[:30]
        cur = conn.execute(
            "INSERT INTO testnet_execution_orders(trade_id,symbol,leg,client_order_id,"
            " requested,created_ts,payload) VALUES (?,?,?,?,?,?,?)",
            (trade_id, symbol, leg, client_id, qty, base._now(), json.dumps(payload, allow_nan=False)))
        return _order(conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?",
                                   (cur.lastrowid,)).fetchone())

    @base._retry_busy
    def testnet_order_state(self, intent_id, state, error=None, *, owner_token=None):
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            conn.execute("UPDATE testnet_execution_orders SET state=?,error=? WHERE id=?"
                         " AND state NOT IN ('SETTLED','REJECTED')", (state, error, intent_id))
            if state == "REJECTED":
                conn.execute("UPDATE trades SET status='ABORTED' WHERE status='PENDING'"
                             " AND id=(SELECT trade_id FROM testnet_execution_orders"
                             " WHERE id=? AND leg='entry')", (intent_id,))

    @base._retry_busy
    def observe_testnet_order(self, intent_id, exchange_order_id, filled, status, *, owner_token=None):
        """Persist accepted-order evidence before terminal/commission checks.

        This carries no inventory or cash effects. Observations can increase
        cumulative fills but cannot erase earlier acceptance or greater fills.
        """
        if not exchange_order_id or (filled is not None and (not math.isfinite(filled) or filled < 0)):
            raise ValueError("order observation requires an ID and finite nonnegative fills")
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            row = conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?", (intent_id,)).fetchone()
            if row is None or row["state"] == "REJECTED":
                raise ValueError("accepted-order observation requires an unrejected intent")
            if row["exchange_order_id"] and row["exchange_order_id"] != str(exchange_order_id):
                raise ValueError("an execution's observed exchange order ID cannot change")
            snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
            known = max(row["observed_filled"], snapshot.get("filled", 0))
            if filled is not None and filled < known:
                raise ValueError("cumulative fill observations cannot decrease")
            conn.execute("UPDATE testnet_execution_orders SET exchange_order_id=?,"
                         " observed_filled=MAX(observed_filled,?),observed_status=?,observed_ts=? WHERE id=?",
                         (str(exchange_order_id), known if filled is None else filled, status, base._now(), intent_id))

    @base._retry_busy
    def confirm_testnet_order(self, intent_id, snapshot, *, owner_token=None):
        if not snapshot.get("terminal"):
            raise ValueError("only terminal cumulative orders can be confirmed")
        encoded = json.dumps(snapshot, sort_keys=True, allow_nan=False)
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            row = conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?",
                               (intent_id,)).fetchone()
            if row is None:
                raise ValueError("unknown testnet intent")
            if row["state"] == "SETTLED":
                if row["snapshot"] != encoded:
                    raise ValueError("a settled testnet snapshot cannot change")
                return
            if (row["exchange_order_id"] and row["exchange_order_id"] != str(snapshot["id"])):
                raise ValueError("a confirmed exchange order ID cannot change")
            if snapshot["filled"] < row["observed_filled"]:
                raise ValueError("confirmed cumulative fills cannot decrease")
            conn.execute("UPDATE testnet_execution_orders SET state='CONFIRMED',"
                         " exchange_order_id=?,snapshot=?,error=NULL WHERE id=?",
                         (str(snapshot["id"]), encoded, intent_id))

    @base._retry_busy
    def settle_testnet_order(self, intent_id, *, owner_token=None):
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            order = _order(conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?",
                                        (intent_id,)).fetchone())
            if order is None or order["state"] not in ("CONFIRMED", "SETTLED"):
                raise ValueError("settlement requires a confirmed terminal order")
            row = conn.execute("SELECT * FROM trades WHERE id=?", (order["trade_id"],)).fetchone()
            if row is None:
                raise ValueError("testnet execution has no trade")
            if order["state"] != "SETTLED":
                s, p = order["snapshot"], order["payload"]
                if order["leg"] == "entry":
                    held = s["filled"] - s["base_fee"]
                    if held <= 0:
                        raise ValueError("a confirmed entry delivered no owned inventory")
                    fee = s["fee_quote"]
                    prior = conn.execute("SELECT amount FROM cash_events WHERE trade_id=?"
                                         " AND kind='entry'", (row["id"],)).fetchone()
                    if order["legacy"] and prior is not None:
                        correction = -fee - prior["amount"]
                        self._cash_event(conn, "testnet", f"testnet_import:{intent_id}", correction, row["id"])
                    elif order["legacy"] and p.get("cash_accounted"):
                        self._cash_event(conn, "testnet", "entry", 0.0, row["id"])
                        self._cash_event(conn, "testnet", f"testnet_import:{intent_id}",
                                         float(p.get("cash_accounted_fee") or 0.0)-fee, row["id"])
                    else:
                        self._cash_event(conn, "testnet", "entry", -fee, row["id"])
                    conn.execute("UPDATE trades SET status='OPEN',qty=?,remaining_qty=?,"
                                 " entry_price=?,entry_fee=?,stop_price=?,initial_stop_price=?,"
                                 " target_price=?,dust_qty=0,exit_qty=0,exit_value=0,exit_fee=0,"
                                 " exit_gross=0 WHERE id=?",
                                 (held, held, s["average"], fee, s.get("stop"), s.get("stop"),
                                  s.get("target"), row["id"]))
                else:
                    remaining = row["remaining_qty"] if row["remaining_qty"] is not None else row["qty"]
                    depletion = s["filled"] + s["base_fee"]
                    if depletion > remaining + 1e-10:
                        raise ValueError("confirmed sell consumed inventory outside this trade")
                    remaining = max(0.0, remaining - depletion)
                    dust = float(s.get("dust_qty") or 0.0)
                    if dust < 0 or dust > remaining + 1e-10:
                        raise ValueError("invalid dust transfer")
                    remaining = max(0.0, remaining - dust)
                    # A commission paid in base also leaves our inventory.
                    # Realize its price movement before deducting its value.
                    gross = (s["average"] - row["entry_price"]) * depletion
                    delta = gross - s["fee_quote"]
                    if order["legacy"]:
                        delta -= float(p.get("cash_accounted_delta") or 0.0)
                    self._cash_event(conn, "testnet", f"testnet_exit:{intent_id}", delta, row["id"])
                    exit_qty = row["exit_qty"] + s["filled"]
                    exit_value = row["exit_value"] + s["filled"] * s["average"]
                    exit_fee = row["exit_fee"] + s["fee_quote"]
                    exit_gross = row["exit_gross"] + gross
                    fee = (row["entry_fee"] or 0.0) + exit_fee
                    pnl = exit_gross - fee
                    average = exit_value / exit_qty if exit_qty else s["average"]
                    pct = (average / row["entry_price"] - 1) * 100.0
                    closed = remaining <= 1e-12
                    conn.execute("UPDATE trades SET status=?,remaining_qty=?,dust_qty=dust_qty+?,"
                                 " exit_qty=?,exit_value=?,exit_fee=?,exit_gross=?,exit_price=?,"
                                 " pnl=?,pnl_pct=?,fees=?,realized_cash_delta=?,exit_reason=?,closed_ts=?"
                                 " WHERE id=?",
                                 ("CLOSED" if closed else "OPEN", remaining, dust, exit_qty, exit_value,
                                  exit_fee, exit_gross, average, pnl, pct, fee, exit_gross-exit_fee,
                                  p.get("reason", "recovered exit"), base._now() if closed else None, row["id"]))
                    if dust:
                        conn.execute("INSERT INTO testnet_dust(trade_id,symbol,qty,entry_price,entry_fee)"
                                     " VALUES (?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET"
                                     " qty=qty+excluded.qty,entry_fee=entry_fee+excluded.entry_fee",
                                     (row["id"], row["symbol"], dust, row["entry_price"],
                                      (row["entry_fee"] or 0.0) * dust / row["qty"]))
                    if closed and not conn.execute("SELECT 1 FROM cash_events WHERE trade_id=?"
                                                   " AND kind='close'", (row["id"],)).fetchone():
                        self._cash_event(conn, "testnet", "close", 0.0, row["id"])
                conn.execute("UPDATE testnet_execution_orders SET state='SETTLED',error=NULL WHERE id=?",
                             (intent_id,))
            return dict(conn.execute("SELECT * FROM trades WHERE id=?", (row["id"],)).fetchone())

    def testnet_orders(self, *, pending=False):
        with self._conn() as conn:
            query = "SELECT * FROM testnet_execution_orders"
            if pending:
                query += " WHERE state NOT IN ('SETTLED','REJECTED')"
            return [_order(r) for r in conn.execute(query + " ORDER BY id")]

    def testnet_resolution_order(self, intent_id, *, owner_token):
        """Read an operator's selected execution while checking its book lease."""
        if not owner_token or owner_token == base.NO_OWNER:
            raise ValueError("operator resolution requires the testnet book lease")
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN")
            self._require_owner(conn, "testnet", owner_token)
            row = conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?",
                               (intent_id,)).fetchone()
            if row is None:
                raise ValueError("unknown testnet intent")
            return _order(row)

    @base._retry_busy
    def abandon_testnet_order(self, intent_id, reason, evidence, checks, *, owner_token):
        """Record an explicit operator attestation, without inventing cash effects."""
        if not reason.strip() or not evidence.strip():
            raise ValueError("abandonment requires a reason and evidence")
        if not owner_token or owner_token == base.NO_OWNER:
            raise ValueError("operator resolution requires the testnet book lease")
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token)
            row = _order(conn.execute("SELECT * FROM testnet_execution_orders WHERE id=?",
                                      (intent_id,)).fetchone())
            if row is None:
                raise ValueError("unknown testnet intent")
            prior = conn.execute("SELECT 1 FROM testnet_execution_audit WHERE execution_id=?",
                                 (intent_id,)).fetchone()
            if row["state"] == "REJECTED" and prior:
                return
            if (row["state"] != "UNKNOWN" or row["legacy"] or row["exchange_order_id"]
                    or row["observed_filled"] or (row["snapshot"] or {}).get("filled", 0)):
                raise ValueError("only an unknown execution with no known order or fills may be abandoned")
            conn.execute("INSERT INTO testnet_execution_audit VALUES (?,?,?,?,?,?)",
                         (intent_id, base._now(), "operator_never_accepted", reason.strip(),
                          evidence.strip(), json.dumps(checks, sort_keys=True, allow_nan=False)))
            conn.execute("UPDATE testnet_execution_orders SET state='REJECTED',error=? WHERE id=?",
                         ("operator attests never accepted; see execution audit", intent_id))
            if row["leg"] == "entry":
                conn.execute("UPDATE trades SET status='ABORTED' WHERE id=? AND status='PENDING'",
                             (row["trade_id"],))

    def testnet_resolution_audits(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM testnet_execution_audit ORDER BY execution_id")]

    def testnet_export_revision(self):
        with self._conn() as conn:
            return tuple(conn.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM testnet_execution_orders"
                                      " WHERE state='SETTLED' AND snapshot IS NOT NULL").fetchone())

    def testnet_legacy_imported(self):
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM testnet_metadata WHERE key='legacy_imported'").fetchone() is not None

    def testnet_trade(self, trade_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id=? AND mode='testnet'", (trade_id,)).fetchone()
            return dict(row) if row else None

    def testnet_book(self, initial_cash):
        with self._conn() as conn:
            conn.execute("BEGIN")
            cash = self._recover_cash(conn, initial_cash, "testnet")
            rows = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE mode='testnet' AND status='OPEN'")]
            dust = [dict(r) for r in conn.execute("SELECT d.*,t.timeframe FROM testnet_dust d"
                                                 " JOIN trades t ON t.id=d.trade_id WHERE d.qty>0")]
            totals = conn.execute("SELECT COALESCE(SUM(entry_fee),0)+COALESCE(SUM(exit_fee),0),"
                                  " COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl ELSE exit_gross-exit_fee END),0)"
                                  " FROM trades WHERE mode='testnet' AND status IN ('OPEN','CLOSED')").fetchone()
            return {"cash": cash, "positions": rows, "dust": dust,
                    "fees_paid": totals[0], "realized_pnl": totals[1]}

    @base._retry_busy
    def import_testnet_orders(self, records, *, owner_token=None):
        """Import legacy JSONL history once, without replaying settled cash."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, "testnet", owner_token if owner_token is not None else base.NO_OWNER)
            if conn.execute("SELECT 1 FROM testnet_metadata WHERE key='legacy_imported'").fetchone():
                return False
            anchor = conn.execute("SELECT * FROM equity WHERE mode='testnet' ORDER BY id DESC LIMIT 1").fetchone()
            seen_closes = set()
            imported = 0
            for o in records:
                row = conn.execute("SELECT * FROM trades WHERE id=? AND mode='testnet'",
                                   (o["trade_id"],)).fetchone()
                if row is None:
                    raise ValueError(f"legacy order {o['id']} has no testnet journal trade")
                payload = {"reason": row["exit_reason"] or "recovered exit",
                           "stop": row["stop_price"], "target": row["target_price"]}
                if o["leg"] == "entry":
                    payload["cash_accounted"] = (row["status"] != "ABORTED" and anchor is not None
                                                  and anchor["ts"] >= row["opened_ts"])
                    payload["cash_accounted_fee"] = row["entry_fee"] or 0.0
                elif row["id"] not in seen_closes:
                    prior = conn.execute("SELECT amount FROM cash_events WHERE trade_id=? AND kind='close'",
                                         (row["id"],)).fetchone()
                    if prior is not None:
                        payload["cash_accounted_delta"] = prior["amount"]
                    elif anchor is not None and row["closed_ts"] and anchor["ts"] >= row["closed_ts"]:
                        payload["cash_accounted_delta"] = (row["realized_cash_delta"] if row["realized_cash_delta"]
                                                           is not None else (row["pnl"] or 0)+(row["entry_fee"] or 0))
                    seen_closes.add(row["id"])
                cur = conn.execute("INSERT OR IGNORE INTO testnet_execution_orders(trade_id,symbol,leg,"
                             " client_order_id,exchange_order_id,state,requested,created_ts,payload,legacy)"
                             " VALUES (?,?,?,?,?,'UNKNOWN',?,?,?,1)",
                             (row["id"], o["symbol"], o["leg"], f"legacy-{o['symbol']}-{o['id']}",
                              str(o["id"]), o.get("requested", o["filled"]), o.get("ts", base._now()),
                              json.dumps(payload, allow_nan=False)))
                imported += cur.rowcount
            conn.execute("INSERT INTO testnet_metadata(key,value) VALUES ('legacy_imported','1')")
            return bool(imported)
