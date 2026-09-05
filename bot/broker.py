"""
Paper broker — simulated execution with fees and slippage.

Market legs (entries, stop/signal/manual exits) fill at the current price
+/- adverse slippage and pay the taker fee on notional. A bracket
take-profit is a resting LIMIT: it fills at its own level (no slippage
crossed) and pays the maker fee. Positions carry their strategy, stop,
target, and the reasoning that opened them — everything the dashboard
needs for attribution.

The same class is reused by the backtester (bar-priced fills) and the live paper
engine (tick-priced fills).

Bracket orders behave as OCO: when one side (stop or target) fills, the other
is cancelled — closing the position is that cancellation. Within a single bar
we check the stop first (conservative when both sides fall inside the bar's
range), and fills account for gaps through the level:
  - a stop (market order) triggered by a gap fills at the bar's open, never
    better than the stop level;
  - a target (resting limit) gapped through fills at the bar's open, which is
    the favorable fill a real limit order would get.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import CONFIG, MarketSpec, CostConfig


@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str                 # 'long' | 'short'
    qty: float
    entry_price: float
    stop: float | None
    target: float | None
    strategy: str
    timeframe: str = "1h"     # owning spec's timeframe (positions key on this)
    rationale: str = ""
    opened_ts: str = ""
    risk_per_unit: float = 0.0   # initial stop distance per unit (for R math)
    bars_held: int = 0
    entry_fee: float | None = None  # entry-leg fee, deferred into close_position pnl
    entry_bar_ts: float = 0.0   # epoch of the DECISION bar (bar-time clock for bars_held)
    meta: dict = field(default_factory=dict)


class PaperBroker:
    def __init__(self, starting_capital: float | None = None, costs: CostConfig | None = None):
        self.cfg = CONFIG
        self.cash = starting_capital if starting_capital is not None else CONFIG.paper_capital
        self.costs = costs or CONFIG.costs
        # positions are keyed by (symbol, timeframe): one symbol can be traded
        # by several specs (BTC 1h turtle + 15m scalper + 4h meanrev) and each
        # book must manage only its own position — never each other's.
        self.positions: dict[tuple[str, str], Position] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0

    # ------------------------------------------------------------------ info
    def equity(self, price_map: dict[str, float] | None = None) -> float:
        eq = self.cash
        for pos in self.positions.values():
            price = (price_map or {}).get(pos.symbol, pos.entry_price)
            eq += self.unrealized(pos, price)
        return eq

    @staticmethod
    def position_key(symbol: str, timeframe: str) -> tuple[str, str]:
        return (symbol, timeframe)

    def has_position(self, symbol: str) -> bool:
        """True if ANY timeframe of this symbol holds a position (the risk
        manager's one-position-per-symbol gate)."""
        return any(key[0] == symbol for key in self.positions)

    def positions_snapshot(self) -> list[Position]:
        """Copy of open positions for callers on OTHER threads (dashboard API
        handlers): iterating .positions directly races the engine's
        open/close mutations and raises 'dictionary changed size during
        iteration' -> sporadic 500s on the stats poll."""
        return list(self.positions.values())

    @staticmethod
    def unrealized(pos: Position, price: float) -> float:
        direction = 1.0 if pos.side == "long" else -1.0
        return (price - pos.entry_price) * direction * pos.qty

    # ------------------------------------------------------------------ fills
    # A bracket take-profit is a RESTING LIMIT order: it sits at its level until
    # price trades through it, fills at the level (or the better open on a gap),
    # and — because it adds liquidity rather than crossing the spread — pays
    # the maker fee and NO slippage. Every other exit (stop, signal, manual,
    # end-of-data) is a market order: taker fee, adverse slippage. The engine
    # and backtester pass the exit reason verbatim, so the broker can price
    # the leg from the reason with no caller changes.
    MAKER_EXIT_REASONS = frozenset({"take profit"})

    def _maker_exit(self, reason: str) -> bool:
        return (self.costs.maker_pricing
                and reason.strip().lower() in self.MAKER_EXIT_REASONS)

    def _fill_price(self, price: float, side: str, opening: bool, kind: str,
                    maker: bool = False) -> float:
        slip = self.costs.slippage(kind, maker=maker)
        # pay slippage in the adverse direction: buy higher, sell lower
        adverse = (side == "long") == opening
        return price * (1 + slip) if adverse else price * (1 - slip)

    def _fee(self, notional: float, kind: str, maker: bool = False) -> float:
        return notional * self.costs.fee(kind, maker=maker)

    def open_position(self, spec: MarketSpec, decision, qty: float, price: float,
                      trade_id: int, ts: str = "",
                      decision_bar_ts: float | None = None) -> Position:
        side = "long" if decision.action == "LONG" else "short"
        fill = self._fill_price(price, side, opening=True, kind=spec.kind)
        stop = fill - decision.stop_distance if side == "long" else fill + decision.stop_distance
        target = None
        if decision.target_rr:
            risk = decision.stop_distance
            target = fill + decision.target_rr * risk if side == "long" else fill - decision.target_rr * risk

        fee = self._fee(fill * qty, spec.kind)
        self.cash -= fee
        self.fees_paid += fee
        pos = Position(
            trade_id=trade_id, symbol=spec.symbol, side=side, qty=qty,
            entry_price=fill, stop=stop, target=target,
            strategy=decision.strategy_name or "orchestrator",
            timeframe=spec.timeframe, rationale=decision.rationale, opened_ts=ts,
            risk_per_unit=decision.stop_distance, entry_fee=fee,
            entry_bar_ts=decision_bar_ts or 0.0,
        )
        self.positions[self.position_key(spec.symbol, spec.timeframe)] = pos
        return pos

    def close_position(self, spec: MarketSpec, price: float,
                       reason: str) -> tuple[Position, float, float, float, float]:
        """Close at `price`, returning (position, net_pnl, pnl_pct, round_trip_fees, exit_fill).

        net_pnl is net of BOTH legs' fees so journal per-trade PnL reconciles with
        cash; exit_fill is the actual post-slippage price the position closed at —
        journals record fills, not decision prices. Take-profit exits are priced
        as resting limits (maker fee, no slippage); all other exits are market
        legs (taker fee + slippage)."""
        pos = self.positions.pop(self.position_key(spec.symbol, spec.timeframe))
        maker = self._maker_exit(reason)
        exit_fill = self._fill_price(price, pos.side, opening=False,
                                     kind=spec.kind, maker=maker)
        direction = 1.0 if pos.side == "long" else -1.0
        gross = (exit_fill - pos.entry_price) * direction * pos.qty
        fee = self._fee(exit_fill * pos.qty, spec.kind, maker=maker)
        self.cash += gross - fee
        self.fees_paid += fee
        entry_fee = pos.entry_fee if pos.entry_fee is not None else 0.0
        pnl = gross - fee - entry_fee
        self.realized_pnl += pnl
        pnl_pct = (exit_fill / pos.entry_price - 1.0) * direction * 100.0
        return pos, pnl, pnl_pct, fee + entry_fee, exit_fill

    # ------------------------------------------------------------------ exits
    def restore_position(self, row: dict, kind: str, timeframe: str = "1h") -> Position:
        """Rebuild an open Position from a journal row after a restart.

        Cash is NOT touched here: the caller restores broker cash from the
        journal's last equity point, so re-charging the entry fee would
        double-count it (the live engine's cash path already paid it). The
        fee is still recorded on the position so the close PnL reports the
        full round trip."""
        from config import TIMEFRAME_SECONDS
        opened = row["opened_ts"]
        bars = 0
        entry_bar_ts = 0.0
        if opened:
            try:
                dt = datetime.fromisoformat(opened)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                entry_bar_ts = dt.timestamp()
                bar_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)
                bars = max(0, int((datetime.now(timezone.utc).timestamp() - entry_bar_ts)
                                  / bar_seconds))
            except Exception:
                bars = 0
        stop = row["stop_price"]
        risk = abs(row["entry_price"] - stop) if stop else 0.0
        pos = Position(
            trade_id=row["id"], symbol=row["symbol"], side=row["side"], qty=row["qty"],
            entry_price=row["entry_price"], stop=stop, target=row["target_price"],
            strategy=row["strategy"], timeframe=row.get("timeframe") or timeframe,
            rationale=row["rationale_open"] or "", opened_ts=opened or "",
            risk_per_unit=risk, bars_held=bars,
            entry_fee=self._fee(row["entry_price"] * row["qty"], kind),
            entry_bar_ts=entry_bar_ts,
        )
        self.positions[self.position_key(pos.symbol, pos.timeframe)] = pos
        return pos

    def scan_bar_exits(self, spec: MarketSpec, bar) -> tuple[str | None, float | None]:
        """
        OCO bracket check of hard stop/target against a bar's high/low/open.

        Returns (reason, exit_price) or (None, None). Fill realism:
          - stop checked first when both levels fall inside the bar (conservative);
          - a stop gapped through (open beyond the level) fills at the open —
            you get the market, not the level;
          - a target gapped through fills at the open (favorable, like a real
            resting limit order).
          - both levels beyond the open (extreme gap) resolve to whichever the
            open touched first in price terms: for a long, an open below the
            stop is a stop fill; an open above the target is a target fill.
        """
        pos = self.positions.get(self.position_key(spec.symbol, spec.timeframe))
        if pos is None:
            return None, None
        high, low = float(bar["high"]), float(bar["low"])
        try:
            open_ = float(bar["open"])
        except (KeyError, TypeError, ValueError):
            open_ = None

        def gap_fill(level: float, is_stop: bool) -> float:
            # fill at the open when the bar gapped past the level
            if open_ is None:
                return level
            if pos.side == "long":
                gapped_through = open_ <= level if is_stop else open_ >= level
                fill = open_ if gapped_through else level
                if is_stop:
                    fill = min(fill, level)  # never better than the stop
                return fill
            gapped_through = open_ >= level if is_stop else open_ <= level
            fill = open_ if gapped_through else level
            if is_stop:
                fill = max(fill, level)  # never better than the stop
            return fill

        if pos.side == "long":
            if pos.stop is not None and low <= pos.stop:
                return "stop loss", gap_fill(pos.stop, is_stop=True)
            if pos.target is not None and high >= pos.target:
                return "take profit", gap_fill(pos.target, is_stop=False)
        else:
            if pos.stop is not None and high >= pos.stop:
                return "stop loss", gap_fill(pos.stop, is_stop=True)
            if pos.target is not None and low <= pos.target:
                return "take profit", gap_fill(pos.target, is_stop=False)
        return None, None
