.PHONY: setup setup-kronos test lint backtest validate battery report run dashboard demo clean-cache

setup:
	python3 -m pip install -r requirements.txt

setup-kronos: setup
	python3 -m pip install -r requirements-kronos.txt
	-git clone https://github.com/shiyu-coder/Kronos models/kronos

test:
	python3 -m pytest tests/ -q

lint:
	python3 -m ruff check bot main.py config.py run_battery.py tests

# one-off backtest (needs network; disk-cached per day after the first run)
backtest:
	python3 main.py backtest --symbol BTC/USDT --timeframe 1h --days 365 --strategy turtle_trend

# the honest-statistics battery + a generated REPORT.md artifact
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

# seed the demo journal (real backtest replay, mode='demo') and open the UI
demo:
	python3 main.py seed-demo
	python3 main.py dashboard

# drop parquet cache entries older than 14 days (they re-fetch on demand)
clean-cache:
	find data/cache -name '*.parquet' -mtime +14 -delete

# alias for the muscle-memory command ("make ui")
ui: dashboard
