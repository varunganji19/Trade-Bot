"""NSE (India) trading calendar — the session gate for LIVE paper entries.

The live paper engine consults this module for NEW ENTRIES ONLY (see
TradingEngine._process_market): opening an India book while the NSE is
closed would be acting on a market that cannot fill. The BACKTESTER never
calls it — backtest bars are historical NSE sessions by construction (a
bar exists iff the market was open), so a session gate there would be a
no-op at best and a deterministic-test breaker at worst.

The whole NSE session (09:15-15:30 IST) maps inside ONE UTC day, so the
kill-switch UTC-day semantics already align — no special handling needed.

India Standard Time is a FIXED offset, UTC+05:30, with no DST — a plain
timedelta conversion is exact; no tz database needed.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# IST = UTC+05:30, fixed offset, no DST
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# regular NSE cash session: 09:15-15:30 IST, Monday-Friday
NSE_SESSION = (time(9, 15), time(15, 30))

# NSE trading holidays, calendar year 2026.
# nseindia.com's holidays page was bot-blocked at authoring time (2026-09-10,
# three fetch attempts), so this is the STATIC FALLBACK: the certain
# nationwide closures. NSE published calendar — VERIFY ANNUALLY.
#   - 2026-01-26 Republic Day (Monday)
#   - 2026-05-01 Maharashtra Day (Friday)
#   - 2026-08-15 Independence Day (a Saturday in 2026 — weekend anyway; kept
#     for principle so the list stays correct in years where it lands
#     midweek)
#   - 2026-10-02 Gandhi Jayanti (Friday)
#   - 2026-12-25 Christmas (Friday)
# Missing from the certain-closures fallback: exchange-specific closures like
# Holi/Diwali (lunar calendars move them year to year) and special half-day
# sessions. The gate sits AFTER position management, so a missed holiday can
# only cost one cycle's worth of skipped entries — never a stuck exit.
NSE_HOLIDAYS_2026: frozenset[str] = frozenset({
    "2026-01-26",   # Republic Day
    "2026-05-01",   # Maharashtra Day
    "2026-08-15",   # Independence Day
    "2026-10-02",   # Gandhi Jayanti
    "2026-12-25",   # Christmas
})


def is_nse_session_open(now: datetime | None = None) -> bool:
    """True iff `now` (wall clock; default datetime.now(UTC) converted to
    IST) is inside the NSE cash session: Mon-Fri, 09:15-15:30 IST, not a
    listed holiday. A pure function of its input — unit-testable with
    explicit datetimes, no clock mocking needed. Naive datetimes are read
    as IST (the session is defined in IST)."""
    if now is None:
        now = datetime.now(timezone.utc)
    ist = now if now.tzinfo is None else now.astimezone(IST)
    if ist.weekday() >= 5:          # Sat/Sun
        return False
    if ist.date().isoformat() in NSE_HOLIDAYS_2026:
        return False
    start, end = NSE_SESSION
    t = ist.time()
    return start <= t <= end
