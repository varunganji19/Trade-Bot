# Opt-in local usage counters

Usage measurement is disabled by default, independent of the opt-in
verdict-summary store. It counts completed validations, consenting clients,
and clients that have completed at least two validations. One multi-strategy
report counts as one validation. Failed validations are not counted.

In the dashboard's Validate tab, check **Count completed validations and
repeat use locally** for runs you want counted. A random browser UUID is
saved only after opt-in; reloading the page leaves the checkbox unchecked.
**Delete my usage counters** removes that browser's server-side count and
its browser ID. Unchecking stops future counting. If browser storage is
unavailable, validation still runs without usage measurement.

For the command line, opt in per invocation:

```sh
python3 main.py validate-trades trades.json --usage
python3 -m bot.usage stats
python3 -m bot.usage forget
```

The first `--usage` creates `usage-client.json` beside the configured
journal. The ID stays stable across explicit opt-in invocations. `forget`
removes that installation's counts and ID. `stats` on a fresh installation
does not create a counter database or identifier.

`usage.db` is separate from the journal, ignored by Git and restricted to
the current OS user. It stores a keyed hash of the client UUID, the first
and last active UTC day and its run count. The HMAC key is local to that
database; raw browser UUIDs are not stored in it. The CLI's raw installation
UUID stays in its separate owner-only client file. No email, filename,
market, trade, P&L, IP address or report contents enter the counter store.
Nothing is uploaded to another service.

Clients inactive for 90 UTC days are removed on the next count or stats
request. Counts for active clients include their retained run history;
this is not a rolling 90-day total. Explicit deletion also reduces the
totals. Browser and CLI installations count separately, storage resets can
create new clients, and people who decline are absent. These are
**pseudonymous consenting-client metrics**, not a count of verified people,
sessions, customers or all validator usage.

The existing dashboard authentication/localhost guards also cover
`GET /api/usage` and `POST /api/usage/forget`. The separate public landing
service exposes neither route. Optional counter-write failure returns the
completed report with a warning rather than losing the validation result.
