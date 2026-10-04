"""
Event-driven backtester.

Fidelity rules (see docs/archive/RESEARCH.md §4):
  - Decisions read indicator values only through CLOSED bar i (no look-ahead).
    Market fills happen at bar i+1's open with slippage; a resting maker
    order waits for a later bar to reach its limit.
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
  - Each held bar is scanned exactly once. Every scored bar has one closing
    equity observation, including flat bars and final liquidation fees.
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
split that could leak. Aggregate equity adds each fold's fixed-capital PnL
onto one starting balance. Warmup gaps between folds are excluded from the
per-bar Sharpe observations rather than treated as one synthetic return.

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
from bot.risk import RiskManager, correlation_cluster
from config import CONFIG, MarketSpec, bars_per_year, infer_kind
from bot.strategies import get_strategy
from bot.strategies.base import strategy_applies


METRICS_VERSION = 3


@dataclass
class BTResult:
    spec: MarketSpec
    strategy: str
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    # Each restarted fold begins a new observed return segment. Warmup gaps
    # between folds are context, not one synthetic trading-bar return.
    segment_starts: list[int] = field(default_factory=list)

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
        rets = eq.pct_change().dropna().drop(index=self.segment_starts, errors="ignore")
        if len(rets) >= 20:
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
            "starting_capital": round(start, 2), "metrics_version": METRICS_VERSION,
            "capital_model": "fixed_starting_capital",
            "return_basis": "net_pnl_over_starting_capital",
            "n_return_observations": len(rets),
        }


class Backtester:
    def __init__(self, cfg=None, starting_capital: float | None = None,
                 book: str = "standard"):
        self.cfg = cfg or CONFIG
        # which book's strategies the ENSEMBLE may vote with (see
        # BaseStrategy.book) — both books trade 5m now, so the backtester
        # must be told which one it is replaying, exactly like the engine
        self.book = book
        self.starting_capital = self.cfg.paper_capital if starting_capital is None else starting_capital
        if not math.isfinite(self.starting_capital) or self.starting_capital <= 0:
            raise ValueError("starting_capital must be finite and positive")
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
        orchestrator = Orchestrator(self.cfg.params, cfg=self.cfg, book=self.book)

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

        # Orders decided at a close execute on a later bar. Replay each bar
        # once: queued market fills at open, resting maker fills, brackets,
        # strategy decisions at close, then the equity observation.
        pending_entry: dict | None = None
        pending_limit: dict | None = None
        pending_exit: str | None = None
        key = broker.position_key(spec.symbol, spec.timeframe)
        risk.note_equity(self.starting_capital, ts=str(ind.index[warmup_bars]))
        self._alloc_warned = False

        def approve(decision, mark: float, epoch: float, qty_ceiling=None):
            positions = broker.positions_snapshot()
            gross = sum(p.qty * (mark if p.symbol == spec.symbol else p.entry_price)
                        for p in positions)
            cluster = sum(p.qty * (mark if p.symbol == spec.symbol else p.entry_price)
                          for p in positions
                          if correlation_cluster(p.symbol, infer_kind(p.symbol))
                          == correlation_cluster(spec.symbol, spec.kind))
            return risk.approve(
                decision, spec, broker.equity({spec.symbol: mark}), len(positions),
                has_position_on_symbol=broker.has_position(spec.symbol),
                open_gross_notional=gross, cluster_gross_notional=cluster,
                bar_epoch=epoch, qty_ceiling=qty_ceiling)

        def close(price: float, reason: str, i: int, cooldown_epoch: float | None = None):
            closed_pos, pnl, pnl_pct, fee, exit_fill = broker.close_position(spec, price, reason)
            risk.apply_exit_cooldown(
                spec, closed_pos, reason,
                float(ind.index[i].timestamp()) if cooldown_epoch is None else cooldown_epoch)
            result.trades.append(_trade_dict(closed_pos, exit_fill, reason,
                                             pnl, pnl_pct, fee, ind, i))

        for i in range(warmup_bars, n):
            bar = ind.iloc[i]
            ts = str(ind.index[i])
            epoch = float(ind.index[i].timestamp())
            mark = float(bar["close"])
            exited = False
            maker_filled = False
            risk.note_equity(broker.equity({spec.symbol: float(bar["open"])}), ts=ts)

            # An exit decided on the preceding closed bar fills before this
            # bar can touch a resting bracket.
            if pending_exit is not None and key in broker.positions:
                # Live starts the cooldown when the preceding close decides
                # the exit. Preserve that clock while recording the actual
                # next-open fill; this candle's close may decide a new entry.
                close(float(bar["open"]), pending_exit, i,
                      cooldown_epoch=float(ind.index[i - 1].timestamp()))
            pending_exit = None

            if pending_entry is not None:
                decision = pending_entry["decision"]
                decision.price = float(bar["open"])
                broker.open_position(spec, decision, pending_entry["qty"], decision.price,
                                     trade_id=-1, ts=ts,
                                     decision_bar_ts=pending_entry["decision_bar_ts"])
                pending_entry = None

            if pending_limit is not None and key not in broker.positions:
                # Re-check the account at the executable price. A stale maker
                # authorization cannot survive a halt, cooldown, or risk veto,
                # and fresh sizing can only shrink its authorized quantity.
                fill = limit_fill_price(pending_limit["side"], pending_limit["limit"], bar,
                                        self.cfg.hft.maker_penetration_bps)
                if fill is not None:
                    decision = pending_limit["decision"]
                    decision.price = fill
                    approval = approve(decision, fill, epoch, qty_ceiling=pending_limit["qty"])
                    if approval.approved:
                        broker.open_position(spec, decision, approval.qty, fill, trade_id=-1,
                                             ts=ts, maker_entry=True,
                                             decision_bar_ts=pending_limit["decision_bar_ts"])
                        maker_filled = True
                    pending_limit = None
                else:
                    pending_limit["waited"] += 1
                    if risk.halted or pending_limit["waited"] > self.cfg.hft.limit_wait_bars:
                        pending_limit = None

            pos = broker.positions.get(key)
            if pos is not None:
                if not maker_filled:
                    pos.bars_held += 1
                reason, exit_price = broker.scan_bar_exits(spec, bar)
                if reason:
                    close(exit_price, reason, i)
                    exited = True
                elif i == n - 1:
                    close(mark, "end of backtest", i)
                    exited = True
                elif not maker_filled:
                    exit_reason, new_stop = single.check_exit(ind, i, pos) if single else (
                        orchestrator_strat_exit(orchestrator, ind, i, pos))
                    if new_stop is not None and new_stop != pos.stop:
                        pos.stop = new_stop
                    pending_exit = exit_reason

            # ts is the input bar label; equity is the account after that
            # closed bar, including all fills, fees and terminal settlement.
            equity = broker.equity({spec.symbol: mark})
            risk.note_equity(equity, ts=ts)
            result.equity_curve.append({"ts": ts, "equity": equity})

            if (i == n - 1 or exited or maker_filled or key in broker.positions
                    or pending_limit is not None):
                continue

            if single is not None:
                if not strategy_applies(single, spec.symbol):
                    continue
                sig = single.evaluate(ind, i)
                decision = _decision_from_signal(sig, mark)
                strategy_name = single.name
            else:
                decision = orchestrator.decide(ind, i, spec)
                strategy_name = decision.strategy_name or "orchestrator"
            if decision.action not in ("LONG", "SHORT"):
                continue
            decision.strategy_name = strategy_name
            if self.cfg.portfolio.enabled and peer_histories:
                try:
                    from bot.allocator import allocation_weights
                    hist = {spec.symbol: ind.iloc[:i + 1]}
                    for peer_spec in self.cfg.watchlist:
                        if peer_spec.timeframe == spec.timeframe and peer_spec.symbol in peer_histories:
                            d = peer_histories[peer_spec.symbol]
                            hist[peer_spec.symbol] = d.loc[d.index <= ind.index[i]]
                    peer_specs = [s for s in self.cfg.watchlist if s.symbol in hist]
                    risk.set_allocation(allocation_weights(peer_specs, hist))
                except Exception as exc:
                    if not self._alloc_warned:
                        print(f"[backtest] allocation hook failed ({type(exc).__name__}: "
                              f"{exc}) — risk budget runs unscaled for this run")
                        self._alloc_warned = True
            approval = approve(decision, mark, epoch)
            if not approval.approved:
                continue
            order = {"decision": decision, "qty": approval.qty,
                     "decision_bar_ts": epoch}
            if getattr(decision, "limit_price", None):
                order.update(limit=float(decision.limit_price), side=decision.action, waited=0)
                pending_limit = order
            else:
                pending_entry = order

        result.start_equity = self.starting_capital
        result.end_equity = result.equity_curve[-1]["equity"]
        return result

    # -------------------------------------------------------- walk-forward
    def run_walk_forward(self, spec: MarketSpec, df: pd.DataFrame, folds: int = 4,
                         strategy: str | None = None,
                         progress: bool = True, warmup_bars: int = 220) -> dict:
        n = len(df)
        fold_len = n // folds
        results = []
        for k in range(folds):
            lo = k * fold_len
            hi = min((k + 1) * fold_len, n)
            if hi - lo < 300:
                continue
            res = self.run(spec, df.iloc[lo:hi], strategy=strategy,
                           warmup_bars=warmup_bars)
            results.append(res)
            if progress:
                s = res.stats()
                print(f"  fold {k + 1}/{folds} {spec.symbol} {s['trades']} trades "
                      f"ret {s['return_pct']}% dd {s['max_drawdown_pct']}%")
        if not results:
            raise ValueError("not enough data for walk-forward folds")
        merged = _merge_folds(results, spec)
        agg = merged.stats()
        agg["aggregation_method"] = "fixed_capital_additive_pnl"
        return {"folds": [r.stats() for r in results], "aggregate": agg,
                "equity_curve": merged.equity_curve,
                "trades": merged.trades, "segment_starts": merged.segment_starts,
                "metrics_version": METRICS_VERSION,
                "aggregation_method": "fixed_capital_additive_pnl"}


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
        # Keep account amounts at broker precision. Per-trade rounding loses
        # small fees and can turn net losses into breakeven CV observations.
        "pnl": pnl, "pnl_pct": round(pnl_pct, 3), "fees": fee,
        "exit_reason": reason, "rationale": pos.rationale,
        # initial-stop ground truth for R math in shadow.behavior_profile
        "initial_stop": pos.initial_stop if pos.initial_stop is not None else pos.stop,
    }


def _merge_folds(results: list, spec) -> BTResult:
    merged = BTResult(spec=spec, strategy=results[0].strategy if results else "ensemble")
    merged.trades = [t for r in results for t in r.trades]
    if not results:
        return merged
    merged.start_equity = results[0].start_equity
    balance = merged.start_equity
    for result in results:
        merged.segment_starts.append(len(merged.equity_curve))
        merged.equity_curve.extend(
            {"ts": p["ts"], "equity": balance + p["equity"] - result.start_equity}
            for p in result.equity_curve)
        balance += result.end_equity - result.start_equity
    merged.end_equity = balance
    return merged


def _aggregate(results: list, spec) -> dict:
    stats = _merge_folds(results, spec).stats()
    stats["aggregation_method"] = "fixed_capital_additive_pnl"
    return stats


def results_to_json(res, path: str):
    if isinstance(res, dict):  # walk-forward payload
        payload = {"stats": res["aggregate"], "folds": res["folds"],
                   "trades": res.get("trades", []), "equity_curve": res.get("equity_curve", []),
                   "segment_starts": res.get("segment_starts", []),
                   "aggregation_method": "fixed_capital_additive_pnl"}
    else:
        payload = {"stats": res.stats(), "trades": res.trades, "equity_curve": res.equity_curve}
    payload["metrics_version"] = METRICS_VERSION
    with open(path, "w") as f:
        json.dump(payload, f, indent=1, default=str)
