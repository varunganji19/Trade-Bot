# Contributing to Algo

Start with a small, reproducible problem. Describe the trigger, what happened,
what should happen, and how to reproduce it without credentials or a real account.
Use synthetic or public fixtures; remove emails, keys and private trade histories.

## Set up a development checkout

Use Python 3.11–3.13 for the supported-version path and Node.js 22 for JavaScript
checks. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt ruff==0.16.5
npm ci
make verify
```

For the fully pinned reproduction path, create a separate **Python 3.14** virtual
environment and install `requirements.lock` plus `ruff==0.16.5`, then run
`python3 -m pip check`, `npm ci` and `make verify`. The lock was resolved with
3.14; do not assume it is the resolved set for other interpreters. Both paths
are exercised in [CI](.github/workflows/ci.yml). Kronos model weights and
`requirements-kronos.txt` are optional and unnecessary for the core checks.

For a dashboard preview, use a disposable journal directory so writes and
generated artifacts stay away from your paper record:

```bash
export ALGO_DEV_DIR="$(mktemp -d)"
ALGO_SKIP_DOTENV=1 ALGO_NO_AUTO_RESUME=1 \
  BOT_DB_PATH="$ALGO_DEV_DIR/trading.db" \
  python3 -m uvicorn bot.dashboard:app --host 127.0.0.1 --port 8000
```

The engine starts stopped. Starting it or running market-data research can use
the network. Keep previews on loopback. The public landing page has its own
app and store; see [WEBSITE.md](docs/WEBSITE.md). Usage counters are opt-in;
see [USAGE.md](docs/USAGE.md). Never use your real waitlist for tests.

## Find the code before changing it

Use the Codebase Memory graph first: find the relevant symbols, follow their
callers/dependencies, and check index coverage for every file you rely on.
Then read those source files to verify that the graph matches the checkout.
For missing, excluded or stale coverage, use a targeted source search and note
the gap. Avoid a file-by-file repository scan.

[ARCHITECTURE.md](docs/ARCHITECTURE.md) follows a decision from candle to
journal. [SELF_CHECK.md](docs/SELF_CHECK.md) pairs key claims with code and tests.
Read the relevant path and its tests before editing it.

## Implement and verify one problem at a time

1. Make one focused change. Preserve unrelated work in the checkout.
2. Reproduce the original failure, then verify the changed behavior. Add a
   regression test when the failure could recur; for documentation or static
   layout changes, check the links or rendered result directly.
3. Run the affected tests, then `make verify` before committing. It runs the
   test suite, Python/JavaScript lint and the live/backtest parity check.
4. For UI work, use the real flow in a browser at desktop and phone widths;
   check keyboard operation and both dashboard themes where relevant.
5. Commit only that problem after verification succeeds. Report checks you
   could not run and why. Do not treat a passing test count as proof that the
   original failure is fixed.

Never bypass the promotion gate or enable production exchange endpoints to
make a test pass. Testnet operations require the setup and safeguards in
[TESTNET.md](docs/TESTNET.md). Leave the real journal, sealed track record,
credentials and subscriber stores out of development commits.

## Research changes need evidence

Declare an experiment in `experiments/*.toml` and commit the declaration
**before** running it. Register every tried variant; do not select only a
winner. Preserve the input window, provenance, costs and uncertainty. Historical
reports remain historical: add a clearly labelled rerun or reconstruction
rather than replacing evidence with a more convenient result.

`make evidence` runs research and writes artifacts; it is separate from the
offline verification command. Use an isolated `BOT_DB_PATH` and follow
[METHODOLOGY.md](docs/METHODOLOGY.md) and [RESULTS.md](docs/RESULTS.md).

## Submit a reviewable change

In the pull request, explain the concrete trigger, resulting behavior and
checks run. Include screenshots for visible changes and material limitations.
AI assistance is welcome, but the contributor must understand the affected
path, verify the result and explain the evidence. Never claim tests, users,
profits or advisor commitments that have not been verified.

Contributions use the repository's [MIT license](LICENSE). The editable
[team/advisors slide](docs/templates/team-advisors.pptx) and its
[editing guide](docs/templates/team-advisors.md) help present confirmed work
and specific gaps without inventing team credentials.
