.PHONY: setup setup-locked setup-kronos test lint verify lock backtest validate battery run dashboard clean-cache ui hft-battery hft-status pinned shadow kronos

setup:
	python3 -m pip install -r requirements.txt

# Reproduce the fully pinned core environment used by the locked CI job.
setup-locked:
	python3 -m pip install -r requirements.lock

# Resolve every direct and transitive core/dev dependency. pip-tools is a
# maintainer tool rather than an application dependency:
#   python3 -m pip install pip-tools
# Run it on the same interpreter the locked CI job uses (see the lock header),
# otherwise the resolved set is not the set CI installs.
lock:
	python3 -m piptools compile --strip-extras \
		--output-file=requirements.lock requirements.txt

setup-kronos: setup
	python3 -m pip install -r requirements-kronos.txt
	# Reproducibility: pin the vendored model to a commit SHA after cloning
	# (e.g. `git -C models/kronos rev-parse HEAD >> models/kronos/SHA.pin`)
	# so Kronos evals stay byte-identical across machines.
	-git clone https://github.com/shiyu-coder/Kronos models/kronos

test:
	python3 -m pytest tests/ -q

lint:
	python3 -m ruff check bot main.py config.py run_battery.py tests scripts

# ONE command before you trust a change: tests + lint + a live-vs-backtest
# parity smoke on the same bar (the property this codebase is built on —
# the engine and the backtester must execute identical strategy code).
verify: test lint
	python3 scripts/parity_smoke.py

# one-off backtest (needs network; disk-cached per day after the first run)
backtest:
	python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 --strategy turtle_trend

# the honest-statistics battery + a generated REPORT.md artifact
# NOTE: append the BACKTESTS.md-documented trial Sharpes for the Deflated
# Sharpe correction, e.g. --trial-sharpes 0.82 1.05 0.64 — without them the
# validate run reports backtest stats but no selection-aware DSR verdict.
validate:
	python3 main.py validate --symbol BTC/USDT --timeframe 1h --days 730 \
		--strategy turtle_trend --report REPORT.md

# full battery across the watchlist (writes data/results/*.json — the
# Evidence tab reads these)
battery:
	python3 run_battery.py

run:
	python3 main.py run

dashboard:
	python3 main.py dashboard

# drop rolling parquet cache entries older than 14 days via the bot's own
# prune (same _prune_rolling_cache fetch_history runs on boot; pinned-window
# caches are never pruned). Needs network deps only for import, no fetch.
clean-cache:
	python3 -c "from bot.data import _prune_rolling_cache; _prune_rolling_cache(); print('[clean-cache] pruned rolling entries older than 14d')"

# alias for the muscle-memory command ("make ui")
ui: dashboard

hft-battery:
	python3 main.py hft-battery --days 14

hft-status:
	python3 main.py hft-status

# pinned Milestone-A windows (byte-identical reruns incl. forex+india legs)
pinned:
	python3 scripts/pinned_runs.py before

# Shadow Account: journal vs its own rules
shadow:
	python3 main.py shadow

# offline Kronos IC evaluation (tracked non-voter verdict)
kronos:
	python3 main.py kronos --days 60

.PHONY: hft-battery hft-status
