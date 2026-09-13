"""Triangular arbitrage monitor — ETH/USDT vs (ETH/BTC x BTC/USDT).

The implied cross: implied(ETH/USDT) = ETH/BTC x BTC/USDT. When the actual
ETH/USDT print deviates from the implied cross beyond the FULL 3-leg taker
cost, a USDT -> ETH -> BTC -> USDT round trip is (theoretically) profitable.

Muck & Schmidl (2025, Finance Research Letters 73, 106508) measured single-
venue crypto triangular mispricings at 1-5bp lasting seconds — far below the
~3-leg cost, and gone before a 1m bar even closes. So the honest expectation
for this module is a MEASURED ABSENCE of exploitable arbitrage: it journals
every observation (the monitor IS the deliverable) and only fires an atomic
3-leg paper round trip when the edge clears the full cost + buffer, which
the backtest harness shows essentially never happens on real data.

All fills are NEXT-BAR opens with taker fees + slippage on each of the three
legs — the same conservative fill rules the rest of the bot uses (no
same-bar fantasy fills).
"""
from __future__ import annotations

import pandas as pd

from config import TRIANGULAR_LEGS, CostConfig


def tri_cost_rate(costs: CostConfig, kind: str = "crypto") -> float:
    """Full round-trip cost rate for the 3 legs (taker fee + adverse
    slippage on EACH leg — the arb crosses the spread three times)."""
    return 3.0 * (costs.fee(kind) + costs.slippage(kind))


def triangular_edge(df_ethusdt: pd.DataFrame, df_ethbtc: pd.DataFrame,
                    df_btcusdt: pd.DataFrame) -> pd.DataFrame:
    """d = ETH/USDT / (ETH/BTC x BTC/USDT) - 1 on SYNCHRONIZED closes
    (inner join: a bar missing from any leg is skipped — stale-leg 'edges'
    are the classic fake-arb artifact)."""
    px = pd.concat({
        "eth_usdt": df_ethusdt["close"],
        "eth_btc": df_ethbtc["close"],
        "btc_usdt": df_btcusdt["close"],
    }, axis=1, join="inner").dropna()
    if px.empty:
        return pd.DataFrame(columns=["d"])
    d = px["eth_usdt"] / (px["eth_btc"] * px["btc_usdt"]) - 1.0
    return d.to_frame(name="d")


def tri_backtest(dfs: dict[str, pd.DataFrame], costs: CostConfig,
                 min_edge_bps: float = 5.0) -> dict:
    """Replay the arb over aligned 1m history. A round trip fires when
    |d| clears the 3-leg cost + buffer; fills happen at the NEXT bar's
    synchronized open (no same-bar fills). Returns trades + a summary."""
    legs = list(TRIANGULAR_LEGS)
    frames = [dfs.get(sym) for sym in legs]
    if any(f is None or f.empty for f in frames):
        return {"trades": [], "error": "missing leg data"}

    # synchronized closes AND next-bar opens for all three legs
    closes = pd.concat({sym: f["close"] for sym, f in zip(legs, frames)},
                       axis=1, join="inner").dropna()
    opens = pd.concat({sym: f["open"] for sym, f in zip(legs, frames)},
                      axis=1, join="inner").dropna()
    d = closes["ETH/USDT"] / (closes["ETH/BTC"] * closes["BTC/USDT"]) - 1.0
    cost = tri_cost_rate(costs)
    threshold = cost + min_edge_bps / 1e4

    trades = []
    equity = 1.0
    d_prev = d.shift(1)  # decision on bar i's close, fill at i+1's open
    for ts, d_i in d_prev.dropna().items():
        if abs(d_i) <= threshold:
            continue
        if ts not in opens.index:
            continue
        o = opens.loc[ts]
        implied_open = o["ETH/BTC"] * o["BTC/USDT"]
        # realized edge at fill: actual open vs implied cross at the fill bar
        realized = (o["ETH/USDT"] / implied_open) - 1.0
        direction = "sell-eth-via-usdt" if d_i > 0 else "buy-eth-via-usdt"
        signed = realized if d_i > 0 else -realized
        pnl_rate = signed - cost
        equity *= (1.0 + pnl_rate)
        trades.append({
            "symbol": "TRI-ETH", "side": "long" if d_i > 0 else "short",
            "strategy": "hft_triangular_arb", "status": "CLOSED",
            "entry_ts": str(ts), "exit_ts": str(ts),
            "entry_price": float(closes.loc[ts, "ETH/USDT"]),
            "exit_price": float(o["ETH/USDT"]),
            "pnl_pct": round(pnl_rate * 100.0, 4),
            "edge_bps": round(abs(d_i) * 1e4, 2),
            "cost_bps": round(cost * 1e4, 2),
            "direction": direction,
            "exit_reason": "triangular round trip",
        })
    return {
        "trades": trades,
        "summary": {
            "bars_aligned": len(d),
            "opportunities": int((d.abs() > threshold).sum()),
            "gross_opportunities": int((d.abs() > cost).sum()),
            "fired": len(trades),
            "max_abs_edge_bps": round(float(d.abs().max() * 1e4), 2) if len(d) else 0.0,
            "p95_abs_edge_bps": round(float(d.abs().quantile(0.95) * 1e4), 2) if len(d) else 0.0,
            "equity_multiple": round(equity, 6),
            "cost_bps": round(cost * 1e4, 2),
            "threshold_bps": round(threshold * 1e4, 2),
        },
    }
