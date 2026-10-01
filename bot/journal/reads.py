"""Journal reads: open positions, recent rows, the equity curve and the
headline statistics the dashboard polls."""
from __future__ import annotations

import bisect

import bot.journal as base   # shared helpers, looked up at call time
from config import CONFIG

class ReadsMixin:
    # ---------------------------------------------------------------- reads
    @base._retry_busy
    def open_trades(self, mode: str | None = None) -> list:
        """OPEN rows. mode filter matters: two engine books (standard 'paper'
        and 'hft') share this DB, and each engine's restart-restore must
        rebuild ONLY its own positions — an unfiltered restore would pull the
        other book's open trades into the wrong broker."""
        q, params = "SELECT * FROM trades WHERE status='OPEN'", []
        if mode:
            q += " AND mode=?"
            params.append(mode)
        q += " ORDER BY id"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @base._retry_busy
    def recent_trades(self, limit: int = 100, mode: str | tuple | None = None,
                      since_id: int | None = None) -> list:
        return self._recent("trades", limit, mode, since_id)

    def _recent(self, table: str, limit: int, mode: str | tuple | None,
                since_id: int | None) -> list:
        """Newest-first rows of `table`, optionally limited to one mode or a
        tuple of modes and to ids above `since_id`."""
        where, params = [], []
        if mode:
            modes = (mode,) if isinstance(mode, str) else tuple(mode)
            where.append(f"mode IN ({','.join('?' * len(modes))})")
            params.extend(modes)
        if since_id is not None:
            where.append("id>?")
            params.append(since_id)
        q = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?",
                                                  params + [limit])]

    @base._retry_busy
    def trade_mode_counts(self) -> dict[str, int]:
        """Closed+open trade counts per mode ('paper' vs 'demo'): the dashboard
        badges seeded demo rows instead of silently presenting them as the
        bot's own paper record."""
        with self._conn() as conn:
            return {r["mode"]: r["n"] for r in conn.execute(
                "SELECT mode, COUNT(*) AS n FROM trades GROUP BY mode")}

    @base._retry_busy
    def recent_transactions(self, limit: int = 100, mode: str | None = None) -> list:
        q, params = "SELECT * FROM transactions", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @base._retry_busy
    @base._retry_busy
    def recent_decisions(self, limit: int = 60, mode: str | tuple | None = None,
                         since_id: int | None = None) -> list:
        """mode filters like the other read paths: the dashboard feed and the
        chatbot read the bot's OWN paper decisions first and only fall back to
        the demo rows on a journal with no paper decisions."""
        return self._recent("decisions", limit, mode, since_id)

    @base._retry_busy
    def equity_curve(self, limit: int = 2000, mode: str | None = None,
                     since_id: int | None = None) -> list:
        q, params = "SELECT id, ts, equity, cash FROM equity", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        if since_id is not None:
            q += (" AND id>?" if mode else " WHERE id>?")
            params.append(since_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(q, params)]
        rows = list(reversed(rows))
        # sort chronologically by timestamp: seeded history is written
        # market-by-market, so insertion order is not time order
        rows.sort(key=lambda r: (r["ts"],))
        return rows

    def last_equity_point(self, mode: str | None = None) -> dict | None:
        """The most recent equity row (the restart anchor for broker cash).

        Ordered by timestamp, not insertion id — the journal's own
        equity_curve() sorts by ts because seeded rows are written
        market-by-market, so the last-inserted row is not the latest point.
        """
        q, params = "SELECT id, ts, equity, cash, mode FROM equity", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY ts DESC, id DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(q, params).fetchone()
        return dict(row) if row else None

    @base._retry_busy
    def stats(self, mode: str | None = None) -> dict:
        # SQL aggregates — the old Python-side full-table scan read every
        # rationale TEXT blob on each 4s dashboard poll. mode is a bound
        # parameter everywhere (the old f-string built invalid SQL:
        # "WHERE status='CLOSED' WHERE mode=..." — a crash on any mode filter).
        mode_sql, mode_args = (" AND mode=?", (mode,)) if mode else ("", ())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN 1 ELSE 0 END), 0) AS wins,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN COALESCE(pnl,0) ELSE 0 END), 0) AS gross_win,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) <= 0 THEN ABS(COALESCE(pnl,0)) ELSE 0 END), 0) AS gross_loss,"
                " COALESCE(SUM(COALESCE(pnl,0)), 0) AS total"
                f" FROM trades WHERE status='CLOSED'{mode_sql}", mode_args).fetchone()
            n_open = conn.execute(
                f"SELECT COUNT(*) FROM trades WHERE status='OPEN'{mode_sql}",
                mode_args).fetchone()[0]
            by_strategy = {r["strategy"]: {"trades": r["n"], "wins": r["wins"],
                                            "pnl": round(r["pnl"] or 0.0, 2)}
                           for r in conn.execute(
                               "SELECT strategy, COUNT(*) AS n,"
                               " SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN 1 ELSE 0 END) AS wins,"
                               " SUM(COALESCE(pnl,0)) AS pnl"
                               f" FROM trades WHERE status='CLOSED'{mode_sql} GROUP BY strategy",
                               mode_args)}
            eq_q, eq_args = "SELECT mode, equity, cash_event_id FROM equity", []
            if mode:
                eq_q += " WHERE mode=?"
                eq_args.append(mode)
            # ORDER BY ts: seeded rows are inserted market-by-market, so id
            # order scrambles the peak-to-trough walk (drawdown, start/end).
            # The scan is bounded (stats() runs on every 4s dashboard poll; a
            # year of sub-minute equity points would otherwise read ~500k rows
            # each time) — 200k rows covers years of realistic runs identically.
            eq = conn.execute(eq_q + " ORDER BY ts, id LIMIT 200000", eq_args).fetchall()
            # deposits/withdrawals move equity without being performance: a
            # $1,000 withdrawal must not read as a drawdown, nor a deposit as
            # a return. Each equity row records the last cash event it saw
            # (cash_event_id), which places every flow exactly on the curve.
            flows = conn.execute(
                "SELECT mode, id, amount FROM cash_events"
                f" WHERE kind IN ('deposit','withdrawal'){mode_sql} ORDER BY id",
                mode_args).fetchall()

        n, wins = row["n"], row["wins"]
        losses = n - wins
        gross_win = row["gross_win"] or 0.0
        gross_loss = row["gross_loss"] or 0.0
        total = row["total"] or 0.0

        # per-mode running totals of external flows, keyed by cash-event id
        flow_ids: dict[str, list[int]] = {}
        flow_sums: dict[str, list[float]] = {}
        for m, fid, amount in flows:
            sums = flow_sums.setdefault(m, [0.0])
            flow_ids.setdefault(m, []).append(fid)
            sums.append(sums[-1] + amount)
        net_flow = sum(sums[-1] for sums in flow_sums.values())

        def flows_before(m: str, cash_event_id: int | None) -> float:
            if m not in flow_ids or cash_event_id is None:
                return 0.0
            return flow_sums[m][bisect.bisect_right(flow_ids[m], cash_event_id)]

        # peak-to-trough walk on the curve with external flows netted out
        max_dd, peak = 0.0, float("-inf")
        for m, e, cash_event_id in eq:
            perf = e - flows_before(m, cash_event_id)
            peak = max(peak, perf)
            max_dd = min(max_dd, (perf - peak) / peak if peak > 0 else 0.0)

        start_eq = eq[0][1] if eq else CONFIG.paper_capital
        end_eq = eq[-1][1] if eq else start_eq
        # flows recorded before the first equity point are already in start_eq
        if eq:
            net_flow -= flows_before(eq[0][0], eq[0][2])
        out = {
            "total_pnl": round(total, 2),
            "closed_trades": n,
            "open_trades": n_open,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_win": round(gross_win / wins, 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / losses, 2) if losses else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "start_equity": start_eq,
            "current_equity": round(end_eq, 2),
            "net_deposits": round(net_flow, 2),
            "return_pct": (round((end_eq - net_flow - start_eq) / start_eq * 100, 2)
                           if start_eq else 0.0),
            "by_strategy": by_strategy,
        }
        if getattr(self, "last_error", None):
            out["journal_error"] = self.last_error
        return out
