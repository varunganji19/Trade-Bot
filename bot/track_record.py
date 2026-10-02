"""Tamper-evident forward track record of the paper book (roadmap V1).

A backtest can be rerun until it looks good; a forward record cannot, if
every day is sealed shortly after it ends. Each finished UTC day of the
paper book (closed trades, equity points, deposits and withdrawals) is
serialised canonically and hashed, and each day's line carries the previous
line's hash, so changing, adding or removing any row of a sealed day breaks
that day's hash and every link after it. Days with no activity are sealed
too: otherwise a trade could later be backfilled into a quiet day.

The chain lives in track_record/paper.jsonl and is committed to git. The
hashes prove nothing on their own: someone could rewrite the journal and the
chain together. What fixes a day in time is the chain line reaching a place
the author does not control (a push to GitHub records when it was received).
docs/TRACK_RECORD.md states this and the other limits.

The journal is opened read-only: sealing and verifying never write to it
and never run its migrations.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = 1
GENESIS = "0" * 64
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHAIN = ROOT / "track_record" / "paper.jsonl"
DEFAULT_DOC = ROOT / "docs" / "TRACK_RECORD.md"
# a trade closed at 23:59:59 commits a moment later; sealing waits this long
# after midnight UTC so a day is never sealed while its last write is in flight
SETTLE = dt.timedelta(hours=1)

# Fixed column lists, not SELECT *: a later migration adding a column must not
# change the hash of days that were sealed before it existed.
TRADE_FIELDS = ("id", "symbol", "side", "qty", "entry_price", "exit_price", "pnl",
                "fees", "strategy", "timeframe", "opened_ts", "closed_ts", "exit_reason")
EQUITY_FIELDS = ("id", "ts", "equity", "cash")
FLOW_FIELDS = ("id", "ts", "kind", "amount")
FLOW_KINDS = ("deposit", "withdrawal", "reset")

BEGIN, END = "<!-- track-record:begin -->", "<!-- track-record:end -->"


class ChainError(Exception):
    """The chain cannot be extended as asked (broken, or no start date)."""


class NotStartedYet(ChainError):
    """The start day has not finished yet, so nothing can be sealed (and the
    start is not recorded anywhere): run the first append again later."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, table: str, fields: tuple, ts_col: str, day: dt.date, mode: str,
          extra: str = "", extra_args: tuple = ()) -> list[dict]:
    """Rows of one book whose `ts_col` falls on `day` (UTC). Journal
    timestamps are ISO-UTC strings, so a half-open string range is exact."""
    lo, hi = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
    q = (f"SELECT {', '.join(fields)} FROM {table}"
         f" WHERE mode=? AND {ts_col}>=? AND {ts_col}<?{extra} ORDER BY id")
    return [dict(r) for r in conn.execute(q, (mode, lo, hi, *extra_args))]


def day_payload(conn, day: dt.date, mode: str = "paper") -> dict:
    """Everything the record seals for one day of one book."""
    return {
        "date": day.isoformat(),
        "trades": _rows(conn, "trades", TRADE_FIELDS, "closed_ts", day, mode,
                        " AND status='CLOSED'"),
        "equity": _rows(conn, "equity", EQUITY_FIELDS, "ts", day, mode),
        "flows": _rows(conn, "transactions", FLOW_FIELDS, "ts", day, mode,
                       f" AND kind IN ({','.join('?' * len(FLOW_KINDS))})", FLOW_KINDS),
    }


def seal(payload: dict, prev: str) -> dict:
    """One chain line: a readable summary of the day plus the hashes. The
    line's own hash covers every other field, the summary included."""
    trades, equity, flows = payload["trades"], payload["equity"], payload["flows"]
    last = max(equity, key=lambda r: (r["ts"], r["id"])) if equity else None
    line = {
        "schema": SCHEMA,
        "date": payload["date"],
        "trades": len(trades),
        "pnl": round(sum(t["pnl"] or 0.0 for t in trades), 2),
        # the transactions ledger stores both kinds as positive amounts
        "flows": round(sum(f["amount"] if f["kind"] == "deposit" else -f["amount"]
                           for f in flows if f["kind"] != "reset"), 2),
        "end_equity": round(last["equity"], 2) if last else None,
        "payload_sha256": _sha256(_canonical(payload)),
        "prev": prev,
    }
    line["hash"] = _sha256(_canonical(line))
    return line


def read_chain(chain_path: str | Path) -> list[dict]:
    path = Path(chain_path)
    if not path.exists():
        return []
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


def verify(db_path: str | Path, chain_path: str | Path = DEFAULT_CHAIN,
           mode: str = "paper") -> list[str]:
    """Recompute every sealed day from the journal. Returns the problems
    found, each naming its date; an empty list means the record holds."""
    chain = read_chain(chain_path)
    problems = []
    prev, expected_day = GENESIS, None
    with contextlib.closing(_connect_ro(db_path)) as conn:
        for n, line in enumerate(chain, 1):
            date = line.get("date", f"line {n}")
            day = dt.date.fromisoformat(line["date"])
            if expected_day is not None and day != expected_day:
                problems.append(f"{date}: expected {expected_day} next — a day is "
                                "missing or out of order")
            if line.get("prev") != prev:
                problems.append(f"{date}: does not link to the line before it")
            body = {k: v for k, v in line.items() if k != "hash"}
            if line.get("hash") != _sha256(_canonical(body)):
                problems.append(f"{date}: the line was edited after it was sealed")
            fresh = seal(day_payload(conn, day, mode), line.get("prev", ""))
            if fresh["payload_sha256"] != line.get("payload_sha256"):
                problems.append(f"{date}: the journal no longer matches the sealed day "
                                f"(now {fresh['trades']} trades, pnl {fresh['pnl']:+.2f}; "
                                f"sealed {line.get('trades')} trades, "
                                f"pnl {line.get('pnl', 0):+.2f})")
            prev, expected_day = line.get("hash"), day + dt.timedelta(days=1)
    return problems


def last_sealable_day(now: dt.datetime) -> dt.date:
    """The latest UTC day that has ended at least SETTLE ago."""
    return ((now - SETTLE).astimezone(dt.timezone.utc).date()
            - dt.timedelta(days=1))


def append(db_path: str | Path, chain_path: str | Path = DEFAULT_CHAIN,
           start: dt.date | None = None, now: dt.datetime | None = None,
           mode: str = "paper") -> list[dict]:
    """Seal every finished day not yet in the chain; returns the new lines.

    The first call needs `start` (the day the clock starts). Later calls
    continue from the last sealed day and refuse a `start`, and refuse to
    extend a chain that no longer verifies: a new link on a broken chain
    would hide the break.
    """
    chain = read_chain(chain_path)
    if chain:
        if start is not None:
            raise ChainError(f"the record already started on {chain[0]['date']}; "
                             "a chain has one start")
        problems = verify(db_path, chain_path, mode)
        if problems:
            raise ChainError("the existing chain does not verify:\n  "
                             + "\n  ".join(problems))
        day = dt.date.fromisoformat(chain[-1]["date"]) + dt.timedelta(days=1)
        prev = chain[-1]["hash"]
    else:
        if start is None:
            raise ChainError("no record yet: pass the start date (--start YYYY-MM-DD)")
        day, prev = start, GENESIS
    last = last_sealable_day(now or dt.datetime.now(dt.timezone.utc))
    if not chain and day > last:
        ready = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(),
                                    dt.timezone.utc) + SETTLE
        raise NotStartedYet(
            f"the record would start on {day}, which can be sealed from "
            f"{ready:%Y-%m-%d %H:%M} UTC; nothing was written, so run this same "
            "command (with --start) again then")
    new = []
    with contextlib.closing(_connect_ro(db_path)) as conn:
        while day <= last:
            line = seal(day_payload(conn, day, mode), prev)
            new.append(line)
            prev, day = line["hash"], day + dt.timedelta(days=1)
    if new:
        path = Path(chain_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for line in new:
                fh.write(_canonical(line) + "\n")
    return new


def render(chain_path: str | Path = DEFAULT_CHAIN, doc_path: str | Path = DEFAULT_DOC,
           recent: int = 30) -> str:
    """Rewrite the generated block of docs/TRACK_RECORD.md from the chain."""
    chain = read_chain(chain_path)
    if not chain:
        block = "*Not started: no day has been sealed yet.*"
    else:
        equities = [line["end_equity"] for line in chain if line["end_equity"] is not None]
        rows = "\n".join(
            f"| {line['date']} | {line['trades']} | {line['pnl']:+,.2f} | "
            f"{line['flows']:+,.2f} | "
            + (f"{line['end_equity']:,.2f}" if line["end_equity"] is not None else "—")
            + f" | `{line['hash'][:16]}` |"
            for line in reversed(chain[-recent:]))
        block = "\n".join([
            f"- **Started:** {chain[0]['date']} (UTC days)",
            f"- **Sealed through:** {chain[-1]['date']} ({len(chain)} days)",
            f"- **Closed trades:** {sum(line['trades'] for line in chain)}",
            f"- **Realized P&L:** {sum(line['pnl'] for line in chain):+,.2f}",
            f"- **Deposits − withdrawals:** {sum(line['flows'] for line in chain):+,.2f}",
            "- **Latest equity:** "
            + (f"{equities[-1]:,.2f}" if equities else "no equity point yet"),
            f"- **Head hash:** `{chain[-1]['hash']}`",
            "",
            f"Most recent {min(recent, len(chain))} days (all of them are in "
            "[`track_record/paper.jsonl`](../track_record/paper.jsonl)):",
            "",
            "| Day (UTC) | Trades | P&L | Flows | End equity | Hash |",
            "|---|---:|---:|---:|---:|---|",
            rows,
        ])
    doc = Path(doc_path)
    text = doc.read_text()
    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    if not rest:
        raise ChainError(f"{doc} has no {BEGIN} … {END} block")
    doc.write_text(f"{head}{BEGIN}\n{block}\n{END}{tail}")
    return block
