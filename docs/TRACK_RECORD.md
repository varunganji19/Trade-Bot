# Forward track record

*Roadmap V1. A backtest can be rerun until it looks good. A forward record
can't, as long as each day is sealed shortly after it ends and the seal
lands somewhere the author doesn't control.*

## What is sealed

For each finished **UTC day**, `python3 main.py track-record append`
reads the paper book (`mode='paper'` journal rows only; demo and fast-book
rows are never included) and records:

- every trade **closed** that day (id, symbol, side, quantity, entry and
  exit price, P&L, fees, strategy, timeframe, open and close time, exit
  reason);
- every equity point written that day;
- every deposit, withdrawal and account reset that day.

These are serialised as canonical JSON and hashed (SHA-256). The day's line
in [`track_record/paper.jsonl`](../track_record/paper.jsonl) holds a short
summary (trades, P&L, flows, end equity), that hash, and the previous
line's hash. A hash over the whole line closes it. Days with no
activity are sealed too, so a trade can't later be slipped into a quiet day.
A day is sealed only once it has been over for an hour, so a trade closed at
23:59:59 is never cut off mid-write.

The journal is opened **read-only**: sealing and verifying never write to it.

## How to check it

```bash
python3 main.py track-record verify
```

This recomputes every sealed day from the journal and reports, by date, any
day whose rows changed, any line edited after sealing, and any break or gap
in the chain. Changing one trade in a sealed day breaks that day and every
link after it.

## Daily routine

```bash
python3 main.py track-record append
git add track_record/paper.jsonl docs/TRACK_RECORD.md
git commit -m "track record: seal through <date>"
```

Then push. The first `append` takes `--start YYYY-MM-DD`, the day the
clock starts. A chain has one start, and `append` refuses to extend a
chain that no longer verifies.

## What this does and does not prove

- **It proves** that the sealed days in the journal have not changed since
  the line was sealed.
- **It does not prove when** a line was sealed. Someone could rewrite the
  journal and the chain together, and a local git commit's date can be set to
  anything. A day is fixed in time only when its line reaches a place the
  author doesn't control: a push to GitHub records when it arrived. Seal
  and push daily; a long gap between a day and its push weakens that day.
- **It is paper trading.** Fills are simulated from candles (see
  [METHODOLOGY.md](METHODOLOGY.md)); a forward paper record measures the
  decision process honestly, not the fills an exchange would give.
- **A paper-account reset deletes the book's rows.** Resetting after the
  clock has started breaks every sealed day before it, and `verify` will say
  so. The backup the reset writes still verifies against the chain
  (`BOT_DB_PATH=<backup> python3 main.py track-record verify`). Start
  the clock after the reset (roadmap item 0.1), not before.
- **It is not a performance claim.** Nothing in the paper book currently
  passes the promotion gate; this record exists so that if something ever
  does, its forward results can be trusted.

## Record

<!-- track-record:begin -->
*Not started: no day has been sealed yet.*
<!-- track-record:end -->
