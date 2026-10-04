"""The strategy validator (bot/validator.py): other people's backtests in,
robust / fragile / likely overfit out."""
from __future__ import annotations

import datetime as dt
import json
import zipfile

import pandas as pd
import pytest

from bot import validator as v

START = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
EDGE = [3.0, 3.0, -2.0, 3.0, -2.0]          # PF 2.25
LOSER = [1.0, -2.0]                         # PF 0.5
THIN_EDGE = [2.2, -2.0]                     # PF 1.1, Sharpe per trade ~0.05


def _ft_trade(k: int, pnl: float, hours_apart: float = 80.0) -> dict:
    """One closed trade in freqtrade's backtest-export format."""
    opened = START + dt.timedelta(hours=k * hours_apart)
    closed = opened + dt.timedelta(hours=4)
    rate = 10_000.0
    return {"pair": "BTC/USDT", "stake_amount": 100.0, "amount": 0.01,
            "open_date": opened.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "close_date": closed.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "open_timestamp": int(opened.timestamp() * 1000),
            "close_timestamp": int(closed.timestamp() * 1000),
            "open_rate": rate, "close_rate": rate + pnl * 100, "fee_open": 0.001,
            "fee_close": 0.001, "profit_abs": pnl, "profit_ratio": pnl / 100.0,
            "exit_reason": "roi", "is_open": False, "is_short": False, "leverage": 1.0}


def _ft_export(strategies: dict[str, list[float]], n: int = 200) -> dict:
    out = {}
    for name, pattern in strategies.items():
        trades = [_ft_trade(k, pattern[k % len(pattern)]) for k in range(n)]
        trades.append({**_ft_trade(n, 50.0), "is_open": True})   # still open: ignored
        out[name] = {"trades": trades, "stake_currency": "USDT", "starting_balance": 1000}
    return {"metadata": {}, "strategy": out,
            "strategy_comparison": [{"key": k} for k in strategies]}


def _write(tmp_path, doc, name="backtest-result.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


@pytest.fixture
def all_regimes(monkeypatch):
    """Daily labels that cycle through the three regimes every few days."""
    import bot.evidence_stats as es
    idx = pd.date_range("2023-01-01", "2026-01-01", freq="D", tz="UTC")
    labels = pd.Series([es.REGIMES[(i // 5) % 3] for i in range(len(idx))], index=idx)
    monkeypatch.setattr(es, "label_regimes", lambda daily, **k: labels)
    return pd.DataFrame({"high": 1.0, "low": 1.0, "close": 1.0}, index=idx)


def test_freqtrade_export_is_read_with_fees_and_open_trades_dropped(tmp_path):
    source, by = v.load_trades(_write(tmp_path, _ft_export({"A": EDGE})))
    assert source == "freqtrade" and list(by) == ["A"]
    trades = by["A"]
    assert len(trades) == 200
    t = trades[0]
    assert t["pnl"] == 3.0 and t["pair"] == "BTC/USDT"
    # 0.01 x 10,000 x 0.1% in + 0.01 x 10,300 x 0.1% out
    assert t["fee"] == pytest.approx(0.1 + 0.103)
    assert t["open"].tzinfo is not None


def test_the_zip_newer_freqtrade_versions_write_is_read(tmp_path):
    p = tmp_path / "backtest-result-2024.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("backtest-result-2024_config.json", json.dumps({"stake": 1}))
        zf.writestr("backtest-result-2024.json", json.dumps(_ft_export({"A": EDGE})))
    assert len(v.load_trades(p)[1]["A"]) == 200


def test_a_csv_with_its_own_column_names_is_read(tmp_path):
    p = tmp_path / "mytrades.csv"
    p.write_text("Entry_Time,Exit_Time,PnL,Commission,Symbol\n"
                 "2024-01-02 10:00,2024-01-02 12:00,5.5,0.2,ETH/USDT\n"
                 "2024-01-01 10:00,2024-01-01 12:00,-1.5,0.2,ETH/USDT\n")
    source, by = v.load_trades(p)
    assert source == "trade list (CSV)"
    assert [t["pnl"] for t in by["mytrades"]] == [-1.5, 5.5]      # close order
    assert by["mytrades"][0]["fee"] == 0.2


def test_a_csv_without_pnl_is_refused_with_the_columns_it_needs(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("time,price\n2024-01-01,1\n")
    with pytest.raises(v.ValidatorError, match="open time, a close time and a net P&L"):
        v.load_trades(p)


def test_this_repos_backtest_json_is_read(tmp_path):
    doc = {"stats": {"strategy": "ts_momentum"},
           "trades": [{"symbol": "BTC/USDT", "qty": 0.5, "entry_price": 100.0,
                       "entry_ts": "2024-01-01 00:00:00+00:00",
                       "exit_ts": "2024-01-02 00:00:00+00:00", "pnl": 4.0, "fees": 0.1}]}
    _, by = v.load_trades(_write(tmp_path, doc, "bt.json"))
    t = by["ts_momentum"][0]
    assert t["notional"] == 50.0 and t["fee"] == 0.1


def test_a_strong_edge_with_every_check_run_is_robust(tmp_path, all_regimes):
    report = v.validate(_write(tmp_path, _ft_export({"A": EDGE})), trials=10,
                        daily=all_regimes)
    r = report["strategies"]["A"]
    assert r["verdict"] == v.ROBUST, r["reasons"]
    assert r["intervals"]["pf_lo"] >= 1.0
    assert [row["fee_mult"] for row in r["costs"]["rows"]] == [0.5, 1.0, 2.0]
    assert r["regimes"]["covered"] == 3
    assert r["stability"]["profitable"] == 4


def test_doubling_fees_moves_each_trade_by_its_fee(tmp_path):
    r = v.validate(_write(tmp_path, _ft_export({"A": EDGE})))["strategies"]["A"]
    rows = {row["fee_mult"]: row["net"] for row in r["costs"]["rows"]}
    fees = r["costs"]["fees_total"]
    assert rows[2.0] == pytest.approx(rows[1.0] - fees, abs=0.01)
    assert rows[0.5] == pytest.approx(rows[1.0] + fees / 2, abs=0.01)


def test_a_check_that_could_not_run_caps_the_verdict_at_fragile(tmp_path):
    r = v.validate(_write(tmp_path, _ft_export({"A": EDGE})))["strategies"]["A"]
    assert r["verdict"] == v.FRAGILE
    assert any("regime coverage not checked" in x for x in r["reasons"])
    assert any("--trials" in x for x in r["reasons"])


def test_a_backtest_that_loses_is_likely_overfit(tmp_path):
    r = v.validate(_write(tmp_path, _ft_export({"A": LOSER})), trials=1)["strategies"]["A"]
    assert r["verdict"] == v.OVERFIT
    assert "below 1.0" in r["reasons"][0]


def test_a_short_losing_backtest_is_named_as_having_no_edge(tmp_path):
    doc = {"strategy": {"A": {"trades": [_ft_trade(k, [2.0, -2.2][k % 2])
                                         for k in range(30)]}}}
    r = v.validate(_write(tmp_path, doc), trials=1)["strategies"]["A"]
    assert r["intervals"]["pf_hi"] > 1.0              # the interval alone would not say it
    assert r["verdict"] == v.OVERFIT
    assert r["reasons"][0].startswith("no edge: it loses money in its own backtest")


def test_a_thin_edge_found_among_many_variants_is_likely_overfit(tmp_path, all_regimes):
    r = v.validate(_write(tmp_path, _ft_export({"A": THIN_EDGE})), trials=1000,
                   daily=all_regimes)["strategies"]["A"]
    assert r["intervals"]["pf"] > 1.0                 # profitable on its face...
    assert r["verdict"] == v.OVERFIT                  # ...and explained by selection
    assert any("Deflated Sharpe" in x for x in r["reasons"])


def test_several_strategies_in_one_file_are_measured_for_pbo(tmp_path):
    report = v.validate(_write(tmp_path, _ft_export({"A": EDGE, "B": LOSER,
                                                     "C": THIN_EDGE})))
    assert set(report["strategies"]) == {"A", "B", "C"}
    pb = report["strategies"]["A"]["pbo"]
    assert pb["pbo"] is not None and pb["configs"] == 3
    single = v.validate(_write(tmp_path, _ft_export({"A": EDGE}), "one.json"))
    assert single["strategies"]["A"]["pbo"]["pbo"] is None


def test_stability_names_a_strategy_that_stopped_working(tmp_path):
    trades = [_ft_trade(k, 3.0 if k < 150 else -2.0) for k in range(200)]
    doc = {"strategy": {"A": {"trades": trades}}}
    r = v.validate(_write(tmp_path, doc), trials=1)["strategies"]["A"]
    pfs = [s["pf"] for s in r["stability"]["segments"]]
    assert pfs[-1] is not None and pfs[-1] < 1.0 and pfs[0] > 1.0
    assert r["verdict"] != v.ROBUST


def test_the_cli_writes_a_markdown_report(tmp_path, capsys):
    from bot.cli.research import cmd_validate_trades
    src = _write(tmp_path, _ft_export({"A": EDGE, "B": LOSER}))
    md, js = tmp_path / "report.md", tmp_path / "report.json"
    args = type("A", (), {"file": str(src), "trials": 3, "strategy": None, "prices": None,
                          "regime_market": None, "report": str(md), "json": str(js),
                          "record": False})()
    cmd_validate_trades(args)
    text = md.read_text()
    assert "## A: **FRAGILE**" in text and "## B: **LIKELY OVERFIT**" in text
    assert json.loads(js.read_text())["strategies"]["B"]["verdict"] == v.OVERFIT
    assert "Cost sensitivity" in text and "×2" in text


@pytest.mark.parametrize("value", [False, 0, "false", "FALSE", "0", "no", " No ", "", None])
def test_explicit_false_open_flags_are_closed_trades(value):
    row = {**_ft_trade(0, 3.0), "is_open": value}
    row = {f" {key.upper()} ": val for key, val in row.items()}
    assert len(v._from_rows([row])) == 1


@pytest.mark.parametrize("value", [True, 1, "true", "TRUE", "1", "yes", " YES "])
def test_explicit_true_flags_skip_open_trades_even_with_uppercase_columns(value):
    assert v._from_rows([{" IS_OPEN ": value}]) == []


@pytest.mark.parametrize("value", ["maybe", "0.0", "2", 2, float("nan"), [], {}])
def test_invalid_open_flags_are_rejected(value):
    with pytest.raises(v.ValidatorError, match="is_open"):
        v._from_rows([{**_ft_trade(0, 3.0), "is_open": value}])


def test_csv_epoch_strings_and_false_flags_are_normalized_before_filtering(tmp_path):
    path = tmp_path / "epochs.csv"
    path.write_text(" OPEN_TIME , CLOSE_TIME , PNL , IS_OPEN \n"
                    "1704067200,1704081600000,3,false\n"
                    "1704153600000,1704168000,-2,0\n"
                    ",,50,yes\n")
    rows = v.load_trades(path)[1]["epochs"]
    assert [row["pnl"] for row in rows] == [3.0, -2.0]
    assert rows[0]["open"] == pd.Timestamp("2024-01-01", tz="UTC")
    assert rows[0]["close"] == pd.Timestamp("2024-01-01T04:00:00Z")


@pytest.mark.parametrize("value", [1704067200, 1704067200000, "1704067200",
                                    "1704067200000", "1.7040672e9",
                                    "2024-01-01T05:30:00+05:30", "2024-01-01"])
def test_numeric_and_iso_timestamps_use_the_same_utc_clock(value):
    assert v._ts(value) == pd.Timestamp("2024-01-01", tz="UTC")


@pytest.mark.parametrize("value, expected", [("20240101", "2024-01-01"),
                                             ("20240229", "2024-02-29"),
                                             (" 19991231 ", "1999-12-31")])
def test_compact_date_strings_are_calendar_dates(value, expected):
    assert v._ts(value) == pd.Timestamp(expected, tz="UTC")


@pytest.mark.parametrize("value", ["20240230", "20231301", "20240001",
                                    "20240100", "00000101", "20230229"])
def test_invalid_compact_dates_are_not_silently_epoch_seconds(value):
    with pytest.raises(v.ValidatorError, match="invalid timestamp"):
        v._ts(value)


@pytest.mark.parametrize("value", [20240101, "20240101.0", "2.0240101e7", "+20240101"])
def test_numeric_values_and_explicit_epoch_strings_retain_second_semantics(value):
    assert v._ts(value) == pd.Timestamp(20240101, unit="s", tz="UTC")


def test_csv_accepts_compact_dates_alongside_epoch_timestamps(tmp_path):
    path = tmp_path / "compact.csv"
    path.write_text("open_date,close_date,pnl\n"
                    "20240101,20240102,3\n"
                    "1704153600,1704240000000,-2\n")
    rows = v.load_trades(path)[1]["compact"]
    assert [row["open"] for row in rows] == [pd.Timestamp("2024-01-01", tz="UTC"),
                                             pd.Timestamp("2024-01-02", tz="UTC")]
    assert [row["close"] for row in rows] == [pd.Timestamp("2024-01-02", tz="UTC"),
                                              pd.Timestamp("2024-01-03", tz="UTC")]


@pytest.mark.parametrize("value", [None, True, "", "NaT", "NaN", "inf", "-inf",
                                    float("nan"), float("inf"), "not a date", "1e300"])
def test_invalid_timestamps_raise_a_validator_error(value):
    with pytest.raises(v.ValidatorError, match="invalid timestamp"):
        v._ts(value)


def test_an_explicit_zero_close_fee_is_preserved_in_cost_sensitivity():
    rows = v._from_rows([{**_ft_trade(0, 3.0), "fee_close": 0.0}])
    assert rows[0]["fee"] == pytest.approx(0.1)
    costs = {row["fee_mult"]: row["net"] for row in v.cost_sensitivity(rows)["rows"]}
    assert costs[2.0] == pytest.approx(2.9)


@pytest.mark.parametrize("value", [None, ""])
def test_only_missing_close_fee_uses_the_open_fee(value):
    rows = v._from_rows([{**_ft_trade(0, 3.0), "fee_close": value}])
    assert rows[0]["fee"] == pytest.approx(0.203)


def test_an_invalid_present_close_fee_is_not_replaced_with_the_open_fee():
    with pytest.raises(v.ValidatorError, match="fee_close"):
        v._from_rows([{**_ft_trade(0, 3.0), "fee_close": "nan"}])
