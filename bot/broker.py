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

from dataclasses import dataclass

from config import CONFIG, MarketSpec, CostConfig, parse_utc, infer_kind


# Tick quantization: resting stop/target levels must sit on a tradable tick.
# Per-kind decimals (crypto 2dp, forex 5dp, india 2dp/paise). Crypto spans
# many magnitudes (BTC 80000 vs sub-penny alts), so the helper never collapses
# a non-zero price to 0.0 — it falls back to 8dp rather than zeroing dust.
TICK_DPS: dict[str, int] = {"crypto": 2, "forex": 5, "india": 2}


def quantize_price(price: float, kind: str) -> float:
    """Round `price` to the per-kind tick (see TICK_DPS)."""
    dps = TICK_DPS.get(kind, 2)
    q = round(float(price), dps)
    if q == 0 and price:
        return round(float(price), 8)
    return q


def quantize_qty(qty: float, kind: str) -> float:
    """Round `qty` to a tradable granularity (mirrors RiskManager.size_position:
    whole units for forex/india, 6dp for crypto). The broker re-applies this
    defensively so callers that bypass risk sizing still book sane quantities."""
    if kind in ("forex", "india"):
        return round(float(qty), 0)
    return round(float(qty), 6)


def limit_fill_price(side: str, limit: float, bar, penetration_bps: float = 0.0) -> float | None:
    """Simulated maker-limit fill on an OHLCV bar (the HFT book's entry leg).

    A buy limit at P fills when the market trades at/below P: an OPEN below P
    gapped through it (fill at the open — the better price a real resting
    limit gets); otherwise the fill is AT P when the bar's low reaches it.
    penetration_bps approximates queue priority — the level must be
    penetrated by that many bps, not just touched (0.0 = optimistic touch
    fills; raise HFT_PENETRATION_BPS for an honest adverse-selection sim).
    Sell limits mirrored. Returns the fill price, or None when unfilled.
    Shared by the backtester and the live engine (one fill model, no drift)."""
    open_ = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    if side == "LONG":
        if open_ <= limit:
            return open_
        if low <= limit * (1.0 - penetration_bps / 1e4):
            return limit
        return None
    if open_ >= limit:
        return open_
    if high >= limit * (1.0 + penetration_bps / 1e4):
        return limit
    return None


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
    initial_stop: float | None = None  # fill-time stop level, never trailed (R ground truth)


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
        # iterate a snapshot: API threads call this while the engine cycle
        # mutates the positions dict (same race positions_snapshot() covers)
        for pos in self.positions_snapshot():
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
                      decision_bar_ts: float | None = None,
                      maker_entry: bool = False) -> Position:
        side = "long" if decision.action == "LONG" else "short"
        if maker_entry:
            # a resting limit that filled: the fill IS the quoted level (or a
            # better open when the bar gapped through it, passed by the
            # caller) — liquidity was PROVIDED, so no slippage and the maker
            # fee. This is the HFT book's entry leg (bot/strategies/hft.py).
            fill = price
        else:
            fill = self._fill_price(price, side, opening=True, kind=spec.kind)
        stop = fill - decision.stop_distance if side == "long" else fill + decision.stop_distance
        target = None
        if decision.target_rr:
            risk = decision.stop_distance
            target = fill + decision.target_rr * risk if side == "long" else fill - decision.target_rr * risk

        # resting levels sit on the venue tick; qty re-rounded defensively
        # (risk already rounds — this covers direct callers). risk_per_unit
        # keeps the raw decision distance so R math stays exact. Qty is
        # rounded BEFORE the fee so the charged fee matches the booked qty.
        qty = quantize_qty(qty, spec.kind)
        stop = quantize_price(stop, spec.kind)
        if target is not None:
            target = quantize_price(target, spec.kind)
        fee = self._fee(fill * qty, spec.kind, maker=maker_entry)
        self.cash -= fee
        self.fees_paid += fee
        pos = Position(
            trade_id=trade_id, symbol=spec.symbol, side=side, qty=qty,
            entry_price=fill, stop=stop, target=target,
            strategy=decision.strategy_name or "orchestrator",
            timeframe=spec.timeframe, rationale=decision.rationale, opened_ts=ts,
            risk_per_unit=decision.stop_distance, entry_fee=fee,
            entry_bar_ts=decision_bar_ts or 0.0, initial_stop=stop,
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
        legs (taker fee + slippage). The position is removed only AFTER pricing
        succeeds — a pricing exception leaves the position intact (no orphan)."""
        key = self.position_key(spec.symbol, spec.timeframe)
        pos = self.positions.get(key)
        if pos is None:
            raise KeyError(key)
        maker = self._maker_exit(reason)
        exit_fill = self._fill_price(price, pos.side, opening=False,
                                     kind=spec.kind, maker=maker)
        direction = 1.0 if pos.side == "long" else -1.0
        gross = (exit_fill - pos.entry_price) * direction * pos.qty
        fee = self._fee(exit_fill * pos.qty, spec.kind, maker=maker)
        # pricing succeeded — now remove and settle cash
        self.positions.pop(key)
        self.cash += gross - fee
        self.fees_paid += fee
        entry_fee = pos.entry_fee if pos.entry_fee is not None else 0.0
        pnl = gross - fee - entry_fee
        self.realized_pnl += pnl
        pnl_pct = (exit_fill / pos.entry_price - 1.0) * direction * 100.0
        return pos, pnl, pnl_pct, fee + entry_fee, exit_fill

    # ------------------------------------------------------------------ exits
    def restore_position(self, row: dict, kind: str | None = None, timeframe: str = "1h") -> Position:
        """Rebuild an open Position from a journal row after a restart.

        Cash is NOT touched here: the caller restores broker cash from the
        journal's last equity point, so re-charging the entry fee would
        double-count it (the live engine's cash path already paid it). The
        fee is still recorded on the position so the close PnL reports the
        full round trip.

        `kind` defaults to infer_kind(symbol): callers that cannot resolve a
        spec (off-watchlist symbols) must not silently price everything as
        crypto — the 3-way cost model differs 5x+ by kind. An explicit kind
        still wins when given.

        bars_held starts at 0: the wall-clock estimate (now - opened_ts) /
        bar_seconds overstated holds across downtime (a weekend offline read
        as dozens of 1h bars). The engine recomputes bars_held from BAR
        timestamps on the next managed cycle (entry_bar_ts vs bar_epoch),
        which is the only clock time stops understand."""
        if not kind:
            kind = infer_kind(row["symbol"])
        opened = row["opened_ts"]
        entry_bar_ts = 0.0
        dt = parse_utc(opened)
        if dt is not None:
            entry_bar_ts = dt.timestamp()
        stop = row["stop_price"]
        # initial-risk ground truth: prefer the latched initial stop; fall back
        # to the final (possibly BE-trailed) stop for legacy rows — their true
        # initial distance is unrecoverable, and R math on them is documented
        # as approximate (see journal._migrate).
        initial = row["initial_stop_price"] if "initial_stop_price" in row.keys() else None
        risk_src = initial if initial is not None else stop
        risk = abs(row["entry_price"] - risk_src) if risk_src else 0.0
        pos = Position(
            trade_id=row["id"], symbol=row["symbol"], side=row["side"], qty=row["qty"],
            entry_price=row["entry_price"], stop=stop, target=row["target_price"],
            strategy=row["strategy"], timeframe=row.get("timeframe") or timeframe,
            rationale=row["rationale_open"] or "", opened_ts=opened or "",
            risk_per_unit=risk, bars_held=0,
            entry_fee=(row["entry_fee"] if "entry_fee" in row.keys() and row["entry_fee"] is not None
                       else self._fee(row["entry_price"] * row["qty"], kind)),
            entry_bar_ts=entry_bar_ts,
            initial_stop=initial,
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
