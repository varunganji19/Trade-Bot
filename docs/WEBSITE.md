# Landing page and opt-in waitlist

Run the introduction/waitlist separately from the operator dashboard:

```sh
ALGO_SKIP_DOTENV=1 python3 -m uvicorn bot.website:app --host 127.0.0.1 --port 8001
```

Open `http://127.0.0.1:8001`. This service imports neither the dashboard nor
the trading engine and exposes no trading, account or subscriber-list API.
It serves the introduction, downloadable upstream sample report, privacy
notice, signup form and private deletion page. It does not send mail.

The default store is `data/waitlist.db` beside the configured trading
journal; it is a separate, ignored database. Override it with
`ALGO_WAITLIST_DB=/absolute/private/path/waitlist.db`. Database permissions
are restricted to the current OS user. Do not publish it or its backups.

Each signup needs an email and an explicitly true consent flag. It stores
the email, UTC creation/expiry times, consent-notice version and a hash of
a random deletion token. Tokens are returned once; their plaintext is not
stored. The browser shows a private deletion link whose token is in the
fragment, outside HTTP navigation/access logs. Removal is sent in a JSON
request body. Each submission has its own link; repeating an email does
not invalidate its earlier links. Operator exports group duplicate emails.
Email ownership is not verified, so counts represent expressed interest,
not verified people or paying customers.

Registrations expire after 30 days. Expired entries are excluded from
exports and purged during signup, export, deletion or maintenance:

```sh
python3 -m bot.waitlist stats    # unique active email addresses only
python3 -m bot.waitlist export  # local JSON, including email addresses
python3 -m bot.waitlist purge   # remove expired submissions
```

For a public deployment, set `ALGO_WEBSITE_HOSTS` to the exact serving
hostnames (comma separated), use HTTPS, and run this app independently of
the private dashboard. Requests are JSON-only with a 4 KiB body limit;
signup allows ten attempts per connection address per minute, per process.
The in-memory rate limiter is bounded to 2,048 addresses. Configure a
trusted reverse proxy and its access-log retention; multi-worker/global
rate limiting belongs at that boundary. No third-party browser assets or
analytics are loaded. The page uses a same-origin content-security policy.

Before accepting public registrations, the operator must fill in the
deployment identity/contact details and review the notice for the actual
hosting arrangement. This implementation supplies consent, minimisation,
expiry and deletion mechanics; it does not assert legal approval.
