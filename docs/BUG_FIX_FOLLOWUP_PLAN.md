# Verified follow-up plan

## Report verification

The original 13 fixes are supported by the source, regressions and saved
research evidence. The usefulness rankings are opinions. Two qualifications:
hard-bracket replay catches missed breaches once usable history returns, and
maker reapproval parity does not establish parity for every lifecycle event.

| Report observation | Verification | Action |
| --- | --- | --- |
| Signal/time exit delays re-entry | Reproduced exactly: the same 300-bar, always-long, two-bar-exit fixture produces 27 trades in original HEAD and 20 in the v2 working tree; gaps are one versus two bars | Fix the decision-time cooldown and permit the next close's entry decision; preserve next-open exit execution |
| Stops/targets should prevent immediate re-entry | Correct; these exits occur inside the current candle, unlike a queued exit already executed at its open | Keep the current stop/target skip and cooldown behavior; regress both cases |
| UNKNOWN requests have no operator escape | Correct; repeated absent lookups block the intent and symbol | Add a leased, audited operator resolution path; confirmed exchange evidence settles once, and any assertion that a request never reached the exchange requires explicit operator confirmation, reason and evidence |
| Reject UNKNOWN after a timer | Unsupported: timeouts and 5XX responses can still represent successful matching-engine execution; API data sources can lag | Preserve UNKNOWN after absence alone; never resubmit or infer zero fill from elapsed time |
| Compact date strings become epoch seconds | Reproduced: `"20240101"` becomes `1970-08-23T06:15:01Z` | Give string-only `YYYYMMDD` explicit calendar semantics, reject invalid compact dates, preserve numeric JSON and ordinary epoch strings |
| Every recovery rewrites the order log | Correct, including a duplicate export after settlement | Avoid unchanged exports and retain failed-export retry; keep book reloads needed for journal cursor/stop changes |
| Backup sidecars are unignored | Correct | Add exact ignore patterns and preserve the files |
| Backup proves an intentional completed reset | The backup was created October 3, 2026 at 21:13:41 IST; this chat began 21:35:48, with both sidecars already present. Backup creation precedes the reset transaction | Record the evidence and limits. It predates this chat and does not prove a committed reset, actor or intent. Do not open, reset, restore or delete the real journal |

Binance's [REST documentation](https://developers.binance.com/en/docs/products/spot/rest-api)
supports the UNKNOWN-order rule. The backup audit used filesystem metadata,
reset source and this chat's captured history, without opening SQLite files.

## Implementation sequence

1. Add failing signal/time re-entry regressions, including longer Scalper
   cooldowns, and live/backtest lifecycle parity coverage. Anchor a queued
   exit's cooldown to its decision candle; retain its actual next-open fill
   timestamp and stop/target precedence. Advance new evidence to metrics v3.
2. Add `testnet resolve` with the book lease and durable audit records.
   Validate order identity, settle terminal results idempotently, preserve
   existing kill switches, and permit an operator's explicitly documented
   never-accepted assertion only after a fresh absent lookup and zero known
   fills. Test disagreement, insufficient evidence, restarts and cash/quantity
   invariance. Avoid unchanged exports without hiding journal updates.
3. Add compact-date parsing and compatibility regressions; ignore backup
   sidecars without modifying data.
4. Refresh graph discovery/coverage per batch, verify current source, run
   targeted regressions, then `make verify` with temporary journals and fake
   exchanges. Check lifecycle parity explicitly, since the existing smoke
   primarily compares strategy decisions.
5. Preserve all v2 and historical reports. Replay every affected pinned and
   experiment case into `docs/research/verified_metrics_v3`. Pin the original
   baseline Git revision. Reuse an original result only when its source,
   configuration and exact input hashes match; compute all v3 results afresh.
   Compare v3 against v2 and independently reconcile exported equity, fees,
   net P&L, return, drawdown and Sharpe. Record the missing EURUSD cache.
6. Organize the original fixes and this follow-up into self-contained verified
   commits, honoring dependency order. Keep shared position/journal changes
   together where splitting would produce a broken intermediate revision.
   Preserve the sidecar files and all unrelated local data.

## Acceptance

- The two-bar exit fixture returns 27 trades and one-bar exit-to-entry gaps;
  cooldowns remain correct for longer policies and stop/target exits.
- UNKNOWN requests are never resubmitted automatically. Resolution is
  evidence-based, audited and retry-safe; existing halt states remain intact.
- Compact dates normalize to UTC, invalid dates fail explicitly, and epoch
  numeric compatibility and explicit zero exit fees remain covered.
- All existing and new regressions, lint and parity checks pass.
- New research exports agree with their underlying trades/equity and include
  source/input provenance. Old reports and real journals remain untouched.

Status: complete. The complete code passes `make verify` with 659 Python
tests, Python/JavaScript lint and expanded lifecycle parity. Eight coherent
implementation commit snapshots passed independently. The v3 replay finished
seven available pinned windows and all 225 experiment cases; exact pinned
EURUSD remains unavailable. The independent export audit passed, with
experiment summary limitations recorded. See [the follow-up report](BUG_FIX_FOLLOWUP.md)
for changes, evidence and the backup audit's limits.
