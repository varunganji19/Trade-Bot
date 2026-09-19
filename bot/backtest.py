"""
Event-driven backtester.

Fidelity rules (see RESEARCH.md §4):
  - Decisions are made on CLOSED bars only (no look-ahead): the indicator frame
    is truncated to `i` before strategies evaluate, then fills happen at bar
    i+1's open with slippage, or stop/target fills inside bar i+1's range.
  - The FILL bar (bar i+1, where the entry filled at the open) is scanned for
    stop/target too — its whole range is post-fill there, and skipping it
    diverged from the live engine, which does manage that bar (parity bug
    fixed 2026-09: backtest now scans it, so a same-bar stop-out is seen by
    both paths).
  - Market legs pay taker fees + slippage (crypto 0.10% + 0.05%, forex
    0.02%+0.01%); bracket take-profit exits are resting limits and fill at
    their level with no slippage, paying the maker fee.
  - A strategy exit decided at bar i's close FILLS at bar i+1's open BEFORE
    that bar's stop/target scan — an already-filled market order cannot lose
    to a same-bar bracket level, and only when no signal fired does the bar
    i+1 scan run (with the bar-i-trailed stop as the active level, matching
    the live engine's trail-then-manage cycle; audit Fix 1.2).
  - Stops are checked before targets within a bar (conservative).
  - Same Orchestrator/RiskManager/PaperBroker code as the live engine.

Modes:
  single  — one strategy per spec (pass strategy=name)
  ensemble — orchestrator decision per bar. NOTE: preferred_timeframes are
             disjoint across strategies (turtle 1h, meanrev 4h/1d, scalper
             5m/15m), so each spec is evaluated by exactly ONE strategy and
             the regime blend reduces to that strategy's confidence — the
             blend/conflict guard only engage if strategies ever share a
             timeframe (they currently don't).

run_walk_forward() splits history into sequential folds, each traded from a
fresh engine state (positions, cooldowns, equity reset) — approximating
periodic live restarts, NOT classic parameter walk-forward: parameters are
fixed in config and never fitted on data, so there is no train/test parameter
split that could leak. Per-fold + aggregate stats are reported honestly.

Determinism: no wall-clock or RNG touches the simulated path — the kill
switch day is derived from bar timestamps (RiskManager.note_equity ts), so
identical inputs always produce identical results (see the determinism test).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import pandas as pd

from bot.broker import PaperBroker, limit_fill_price
from bot.indicators import add_all_indicators
from bot.orchestrator import Orchestrator
from bot.risk import RiskManager
from config import CONFIG, MarketSpec, bars_per_year
from bot.strategies import get_strategy


@dataclass
class BTResult:
    spec: MarketSpec
    strategy: str
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        closed = [t for t in self.trades if t["status"] == "CLOSED"]
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        total_pnl = sum(t["pnl"] for t in closed)
        fees = sum(t.get("fees", 0) for t in closed)

        eq = pd.Series([p["equity"] for p in self.equity_curve]) if self.equity_curve else pd.Series(dtype=float)
        max_dd = 0.0
        if len(eq):
            peak = eq.cummax()
            max_dd = float(((eq - peak) / peak).min())

        # annualized Sharpe on per-bar equity returns
        sharpe = None
        if len(eq) > 20:
            rets = eq.pct_change().dropna()
            if rets.std() > 0:
                sharpe = float(rets.mean() / rets.std() * math.sqrt(
                    bars_per_year(self.spec.timeframe, self.spec.kind)))

        start = self.start_equity or (self.equity_curve[0]["equity"] if self.equity_curve else 0)
        end = self.end_equity or (eq.iloc[-1] if len(eq) else start)
        return {
            "symbol": self.spec.symbol, "timeframe": self.spec.timeframe,
            "strategy": self.strategy, "trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_pnl": round(total_pnl, 2), "fees": round(fees, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "return_pct": round((end / start - 1) * 100, 2) if start else 0.0,
            "start_equity": round(start, 2), "end_equity": round(float(end), 2),
        }


class Backtester:
    def __init__(self, cfg=None, starting_capital: float | None = None):
        self.cfg = cfg or CONFIG
        self.starting_capital = starting_capital or self.cfg.paper_capital
        self._alloc_warned = False   # allocation failures are warned ONCE per run

    # ------------------------------------------------------------------ core
    def run(self, spec: MarketSpec, df: pd.DataFrame, strategy: str | None = None,
            warmup_bars: int = 220,
            peer_histories: dict[str, pd.DataFrame] | None = None) -> BTResult:
        """
        Trade `df` bar-by-bar. strategy=None -> ensemble (orchestrator).

        peer_histories: {symbol: closed-bar frame} for the OTHER symbols sharing
        this timeframe — enables cross-symbol risk-budget allocation inside the
        backtest (each peer is truncated at the current bar timestamp first, so
        no peer future leaks into the allocator). None -> single-symbol mode
        with the default unscaled risk.
        """
        broker = PaperBroker(self.starting_capital, costs=self.cfg.costs)
        risk = RiskManager(self.cfg)
        orchestrator = Orchestrator(self.cfg.params, llm_client=None, sentiment_overlay=None, cfg=self.cfg)

        # slice the spec to one timeframe (orchestrator filters by preferred tf)
        single = get_strategy(strategy, self.cfg.params) if strategy else None

        result = BTResult(spec=spec, strategy=strategy or "ensemble")
        n = len(df)
        if n < warmup_bars + 10:
            raise ValueError(f"need >= {warmup_bars + 10} bars, got {n}")

        # Pre-compute indicators ONCE on the full frame (rolling ops are causal:
        # every value at i depends only on bars <= i), then re-verify causality
        # by never reading past i in the loop.
        ind = add_all_indicators(df, self.cfg.params)

        risk.note_equity(self.starting_capital)
        # set at entry: the NEXT iteration's `cur` bar is the fill bar (the
        # entry filled at its open), which must be scanned for stop/target —
        # skipping it was the live/backtest parity bug
        fill_scan_pending = False
        # resting maker limit (HFT book): an unfilled entry order awaiting a
        # touch — {decision, qty, limit, side, strategy_name, waited,
        # decision_bar_ts}. None when no order is resting.
        pending_limit: dict | None = None

        for i in range(warmup_bars, n - 1):
            cur_bar = ind.iloc[i]
            next_open = float(ind["open"].iloc[i + 1])
            next_bar = ind.iloc[i + 1]
            next_ts = str(ind.index[i + 1])
            cur_ts = str(ind.index[i])
            bar_epoch = float(ind.index[i].timestamp())

            # kill switch follows simulated bar time, not the wall clock
            risk.note_equity(broker.equity({spec.symbol: float(cur_bar["close"])}), ts=next_ts)

            # ---- manage open position on this symbol -------------------------
            pos = broker.positions.get(broker.position_key(spec.symbol, spec.timeframe))
            if pos is not None:
                pos.bars_held += 1
                # Event ordering (audit Fix 1.2, FLAW_VALIDATION correction #1):
                # the strategy exit decided at bar i's CLOSE fills at bar i+1's
                # OPEN — that market fill is already on the tape BEFORE bar i+1
                # trades, so it must execute before any bar-i+1 stop/target scan.
                # The old scan-both-bars-first loop let a same-bar stop/target
                # "win" over an order that had already filled at the open
                # (mixed-direction bias: winners stolen by targets, losers saved
                # by stops, ~2-3% of trades). Time order is now:
                #   (a) fill bar FIRST — its whole range is post-fill and
                #       PRE-decision, so it is unaffected by this fix;
                #   (b) the strategy exit at the next open;
                #   (c) bar i+1's stop/target scan, ONLY if (b) did not fire —
                #       with the newly-trailed stop active (live parity: the
                #       engine trails at bar i's close and the next bar's scan
                #       uses the new level).
                reason, exit_price, exit_idx = None, None, None
                if fill_scan_pending:
                    reason, exit_price = broker.scan_bar_exits(spec, cur_bar)
                    if reason:
                        exit_idx = i
                    fill_scan_pending = False
                if not reason:
                    exit_reason, new_stop = single.check_exit(ind, i, pos) if single else (
                        orchestrator_strat_exit(orchestrator, ind, i, pos))
                    if new_stop is not None and new_stop != pos.stop:
                        pos.stop = new_stop
                    if exit_reason:
                        # the exit signal was computed on bar i's close, which
                        # was not tradable at decision time -> fill at bar i+1's
                        # open, and bar i+1's intra-bar stop/target scan never
                        # happens — the position is gone at the open
                        reason, exit_price, exit_idx = exit_reason, next_open, i + 1
                    else:
                        # no signal exit: bar i+1's range is the next stop/target
                        # opportunity, now with the trailed stop as the level
                        reason, exit_price = broker.scan_bar_exits(spec, next_bar)
                        if reason:
                            exit_idx = i + 1
                if reason:
                    closed_pos, pnl, pnl_pct, fee, exit_fill = broker.close_position(
                        spec, exit_price, reason)
                    # cooldown after ANY exit (stops longest): one closed trade
                    # must not immediately re-trigger and churn fees — the
                    # shared policy also blocks re-entry inside risk.approve
                    risk.apply_exit_cooldown(spec, closed_pos, reason, bar_epoch)
                    result.trades.append(_trade_dict(closed_pos, exit_fill, reason, pnl, pnl_pct, fee, ind, exit_idx))
                    result.equity_curve.append({"ts": str(ind.index[exit_idx]),
                                                "equity": round(broker.equity({spec.symbol: exit_fill}), 2)})
                    continue

                # mark to market
                result.equity_curve.append({"ts": next_ts,
                                            "equity": round(broker.equity({spec.symbol: float(next_bar['close'])}), 2)})
                continue

            # ---- no position: resting maker limit first, then new entries --
            if pending_limit is not None:
                fill_price = limit_fill_price(pending_limit["side"],
                                              pending_limit["limit"], cur_bar,
                                              self.cfg.hft.maker_penetration_bps)
                if fill_price is not None:
                    if risk.halted:
                        pending_limit = None   # kill switch: order dies unfilled
                    else:
                        fill_decision = pending_limit["decision"]
                        fill_decision.price = fill_price
                        broker.open_position(spec, fill_decision, pending_limit["qty"],
                                             fill_price, trade_id=-1, ts=cur_ts,
                                             decision_bar_ts=pending_limit["decision_bar_ts"],
                                             maker_entry=True)
                        # the fill bar's whole range is post-fill: scan it NOW
                        # (its stop/target may already have been touched);
                        # later bars flow through the normal manage path
                        reason, exit_price = broker.scan_bar_exits(spec, cur_bar)
                        if reason:
                            closed_pos, pnl, pnl_pct, fee, exit_fill = broker.close_position(
                                spec, exit_price, reason)
                            risk.apply_exit_cooldown(spec, closed_pos, reason, bar_epoch)
                            result.trades.append(_trade_dict(closed_pos, exit_fill, reason,
                                                             pnl, pnl_pct, fee, ind, i))
                            result.equity_curve.append(
                                {"ts": cur_ts, "equity": round(broker.equity({spec.symbol: exit_fill}), 2)})
                        else:
                            result.equity_curve.append(
                                {"ts": next_ts,
                                 "equity": round(broker.equity({spec.symbol: float(next_bar["close"])}), 2)})
                    pending_limit = None
                    continue
                pending_limit["waited"] += 1
                if pending_limit["waited"] > self.cfg.hft.limit_wait_bars:
                    pending_limit = None       # expired unfilled: no trade happened
                else:
                    continue                   # still resting — no new decisions

            # ---- look for an entry on the CURRENT closed bar ----------------
            if single is not None:
                sig = single.evaluate(ind, i)
                decision = _decision_from_signal(sig, float(cur_bar["close"]))
                strategy_name = single.name
            else:
                decision = orchestrator.decide(ind, i, spec, include_sentiment=False)
                strategy_name = decision.strategy_name or "orchestrator"

            if decision.action == "HOLD":
                continue

            # synthetic Decision wrapper for risk approval
            decision.strategy_name = strategy_name
            # portfolio allocation: divide the risk budget across the symbols
            # that could hold a position on this timeframe — same allocator as
            # the live engine (inverse-vol/HRP over the trailing window,
            # truncated at bar i so no peer's future bars leak in)
            if self.cfg.portfolio.enabled and peer_histories:
                try:
                    from bot.allocator import allocation_weights
                    ts = ind.index[i]
                    hist = {spec.symbol: ind.iloc[: i + 1]}
                    for peer_spec in self.cfg.watchlist:
                        if peer_spec.timeframe == spec.timeframe and peer_spec.symbol in peer_histories:
                            d = peer_histories[peer_spec.symbol]
                            hist[peer_spec.symbol] = d.loc[d.index <= ts]
                    peer_specs = [s for s in self.cfg.watchlist if s.symbol in hist]
                    risk.set_allocation(allocation_weights(peer_specs, hist))
                except Exception as exc:
                    # silent pass used to hide allocator bugs as quietly DOUBLED
                    # risk (unscaled 1%-per-symbol). Warn once, keep trading.
                    if not self._alloc_warned:
                        print(f"[backtest] allocation hook failed ({type(exc).__name__}: "
                              f"{exc}) — risk budget runs unscaled for this run")
                        self._alloc_warned = True
            approval = risk.approve(
                decision, spec, broker.cash, len(broker.positions),
                # cross-TF one-symbol rule (engine parity: broker.has_position
                # spans timeframes — a 4h book never opens under a live 1h
                # book on the same symbol, and vice versa)
                has_position_on_symbol=broker.has_position(spec.symbol),
                # real marked gross notional (engine parity): the open book
                # marked at the decision bar's close for this spec's symbol,
                # at entry for anything else (the only mark a single-frame
                # backtest can price) — the 0.0 default understated exposure
                open_gross_notional=sum(
                    p.qty * (float(cur_bar["close"]) if p.symbol == spec.symbol
                             else p.entry_price)
                    for p in broker.positions_snapshot()),
                bar_epoch=bar_epoch)
            if not approval.approved:
                continue

            # maker entry (HFT book): the order RESTS at the quoted level and
            # fills when a later bar's range reaches it (maker fee, no
            # slippage), expiring after cfg.hft.limit_wait_bars — checked at
            # the top of this loop, never filled at the next open
            if getattr(decision, "limit_price", None):
                pending_limit = {"decision": decision, "qty": approval.qty,
                                 "limit": float(decision.limit_price),
                                 "side": decision.action,
                                 "strategy_name": strategy_name,
                                 "waited": 0, "decision_bar_ts": bar_epoch}
                continue

            # fill at NEXT bar's open with slippage+fee
            fill_decision = decision
            fill_decision.price = next_open
            pos = broker.open_position(spec, fill_decision, approval.qty, next_open,
                                       trade_id=-1, ts=next_ts,
                                       decision_bar_ts=bar_epoch)
            pos.strategy = strategy_name
            fill_scan_pending = True

        # close any remaining position at the last close — but scan the fill
        # bar first when the position opened on the final iteration (its fill
        # bar is the last bar and the loop never managed it)
        pos = broker.positions.get(broker.position_key(spec.symbol, spec.timeframe))
        if pos is not None:
            if fill_scan_pending:
                reason, exit_price = broker.scan_bar_exits(spec, ind.iloc[n - 1])
                if reason:
                    closed_pos, pnl, pnl_pct, fee, exit_fill = broker.close_position(
                        spec, exit_price, reason)
                    result.trades.append(_trade_dict(closed_pos, exit_fill, reason,
                                                    pnl, pnl_pct, fee, ind, n - 1))
                    pos = None
            if pos is not None:
                last_close = float(ind["close"].iloc[-1])
                closed_pos, pnl, pnl_pct, fee, exit_fill = broker.close_position(
                    spec, last_close, "end of backtest")
                result.trades.append(_trade_dict(closed_pos, exit_fill, "end of backtest",
                                                pnl, pnl_pct, fee, ind, n - 1))

        result.start_equity = self.starting_capital
        result.end_equity = round(broker.equity({spec.symbol: float(ind['close'].iloc[-1])}), 2)
        if not result.equity_curve:
            result.equity_curve = [{"ts": str(ind.index[0]), "equity": self.starting_capital},
                                   {"ts": str(ind.index[-1]), "equity": result.end_equity}]
        return result

    # -------------------------------------------------------- walk-forward
    def run_walk_forward(self, spec: MarketSpec, df: pd.DataFrame, folds: int = 4,
                         strategy: str | None = None,
                         progress: bool = True) -> dict:
        n = len(df)
        fold_len = n // folds
        results = []
        for k in range(folds):
            lo = k * fold_len
            hi = min((k + 1) * fold_len, n)
            if hi - lo < 300:
                continue
            res = self.run(spec, df.iloc[lo:hi], strategy=strategy, warmup_bars=220)
            results.append(res)
            if progress:
                s = res.stats()
                print(f"  fold {k + 1}/{folds} {spec.symbol} {s['trades']} trades "
                      f"ret {s['return_pct']}% dd {s['max_drawdown_pct']}%")
        if not results:
            raise ValueError("not enough data for walk-forward folds")
        agg = _aggregate(results, spec)
        return {"folds": [r.stats() for r in results], "aggregate": agg,
                "equity_curve": [p for r in results for p in r.equity_curve],
                "trades": [t for r in results for t in r.trades]}


# ---------------------------------------------------------------------- helpers
def orchestrator_strat_exit(orchestrator, df, i, pos):
    """In ensemble mode, ask the position's owning strategy for exits."""
    strat = get_strategy(pos.strategy, orchestrator.cfg.params) if pos.strategy else None
    if strat is None:
        return None, None
    return strat.check_exit(df, i, pos)


def _decision_from_signal(sig, price: float):
    """Adapt a raw Signal to the Decision interface used by risk/broker."""
    class _D:  # lightweight stand-in
        pass
    d = _D()
    d.action = sig.action
    d.confidence = sig.confidence
    d.stop_distance = sig.stop_distance
    d.target_rr = sig.target_rr
    d.price = price
    d.rationale = sig.rationale
    d.strategy_name = sig.strategy
    d.limit_price = getattr(sig, "limit_price", None)
    return d


def _trade_dict(pos, exit_price, reason, pnl, pnl_pct, fee, ind, i) -> dict:
    return {
        "symbol": pos.symbol, "side": pos.side, "qty": pos.qty,
        "entry_price": pos.entry_price, "exit_price": exit_price,
        "stop": pos.stop, "target": pos.target, "strategy": pos.strategy,
        "status": "CLOSED", "entry_ts": pos.opened_ts, "exit_ts": str(ind.index[i]),
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 3), "fees": round(fee, 4),
        "exit_reason": reason, "rationale": pos.rationale,
        # initial-stop ground truth for R math in shadow.behavior_profile
        "initial_stop": pos.initial_stop if pos.initial_stop is not None else pos.stop,
    }


def _aggregate(results: list, spec) -> dict:
    merged = BTResult(spec=spec, strategy=results[0].strategy if results else "ensemble")
    merged.trades = [t for r in results for t in r.trades]
    merged.equity_curve = [p for r in results for p in r.equity_curve]
    if merged.equity_curve:
        merged.start_equity = merged.equity_curve[0]["equity"]
        merged.end_equity = merged.equity_curve[-1]["equity"]
    return merged.stats()


def results_to_json(res, path: str):
    if isinstance(res, dict):  # walk-forward payload
        payload = {"stats": res["aggregate"], "folds": res["folds"],
                   "trades": res.get("trades", []), "equity_curve": res.get("equity_curve", [])}
    else:
        payload = {"stats": res.stats(), "trades": res.trades, "equity_curve": res.equity_curve}
    with open(path, "w") as f:
        json.dump(payload, f, indent=1, default=str)
