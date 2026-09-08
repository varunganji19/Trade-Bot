"""
Validation report renderer — turns a `main.py validate` report dict into a
standalone Markdown artifact (REPORT.md).

The validate command already writes the full JSON; this renders the SAME
numbers for humans: the statistic, its verdict, and the caveats that keep it
honest. Committed alongside the repo, it is the one results artifact a judge
can trust in two minutes — and it is generated, never hand-edited.
"""
from __future__ import annotations


def render_validation_report(r: dict) -> str:
    lines: list[str] = []
    b = r.get("backtest", {}) or {}
    lines.append(f"# Validation report — {r.get('symbol', '?')} "
                 f"{r.get('timeframe', '?')} · `{r.get('strategy', '?')}`")
    window = f"last {r.get('days')}d" if not r.get("start") else f"{r.get('start')} → {r.get('end')}"
    lines.append(f"_{window} · {r.get('bars', '?')} bars · generated {r.get('generated_at', '?')}_")
    lines.append("")
    lines.append("## Backtest (full costs: taker fees + slippage on every fill)")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    for k, label in (("return_pct", "return"), ("total_pnl", "total PnL ($, on $10k)"),
                     ("trades", "closed trades"), ("win_rate_pct", "win rate"),
                     ("profit_factor", "profit factor"), ("max_drawdown_pct", "max drawdown"),
                     ("sharpe", "Sharpe (annualized, kind-aware)"), ("fees", "fees paid ($)")):
        if b.get(k) is not None:
            lines.append(f"| {label} | {b[k]} |")
    lines.append("")

    cv = r.get("purged_cv")
    if cv:
        lines.append("## Purged-CV out-of-sample path distribution")
        lines.append("")
        lines.append(f"{cv.get('n_active_paths', 0)}/{cv.get('n_paths', 0)} paths traded · "
                     f"mean {cv.get('mean_return_pct')}% ± {cv.get('std_return_pct')}% · "
                     f"t≈{cv.get('t_stat')} · {cv.get('pct_paths_profitable')}% of traded "
                     f"paths profitable · purged {cv.get('purged_trades')}/{cv.get('total_trades')}")
        lines.append("")
        lines.append("| path | trades | return % | win % |")
        lines.append("|---:|---:|---:|---:|")
        for i, p in enumerate(cv.get("paths", []), 1):
            if p.get("trades"):
                lines.append(f"| {i} | {p['trades']} | {p['return_pct']:+.2f} | {p['win_rate_pct']} |")
        lines.append("")

    pbo = r.get("pbo")
    if pbo:
        lines.append(f"## PBO (CSCV, {pbo.get('n_configs')} configs × {pbo.get('n_paths')} aligned paths)")
        lines.append("")
        lines.append(f"**{pbo.get('pbo')}** — {pbo.get('verdict')} "
                     f"({pbo.get('n_sims')} random IS/OOS splits; ≥0.5 = the family's "
                     f"IS winner is IS-luck).")
        lines.append("")

    dsr = r.get("deflated_sharpe")
    if dsr:
        lines.append("## Deflated Sharpe (Bailey & Lopez de Prado 2014)")
        lines.append("")
        lines.append(f"best trial Sharpe {dsr.get('best_sharpe')} over {dsr.get('n_trials')} "
                     f"documented trials → **DSR {dsr.get('deflated_sharpe')}** — "
                     f"{dsr.get('verdict')}. Trial Sharpes must be the annualized Sharpes of "
                     "every configuration tried on this window (BACKTESTS.md rounds).")
        lines.append("")

    mc = r.get("monte_carlo")
    if mc and mc.get("n_sims"):
        lines.append(f"## Monte Carlo ({mc.get('n_sims')} resampled trade orders)")
        lines.append("")
        lines.append(f"terminal equity p5 ${mc.get('terminal_p5'):,} · p50 ${mc.get('terminal_p50'):,} "
                     f"· p95 ${mc.get('terminal_p95'):,} · loses money in {mc.get('p_lose_money')}% "
                     f"of sims · DD beyond −10% in {mc.get('p_dd_beyond_10pct')}% of sims.")
        lines.append("")

    mt = r.get("min_trl")
    if mt and mt.get("min_bars"):
        lines.append("## Minimum Track Record Length")
        lines.append("")
        lines.append(f"{mt.get('min_years')} years ≈ {mt.get('min_bars'):,} bars of OOS "
                     "track record needed for 95% confidence that SR > 0.")
        lines.append("")

    lines.append("## Caveats (read before the numbers)")
    lines.append("")
    lines.append("- Backtests are not promises: regime shifts kill edges, and live results ")
    lines.append("  should be expected to be worse than these (RESEARCH.md §4).")
    lines.append("- Parameters are fixed in config, never fitted on the test window — but the")
    lines.append("  strategy set itself was chosen on overlapping history; DSR/PBO above are")
    lines.append("  the honest counterweight, not a clean pass.")
    lines.append("- Thin samples stay thin: a verdict built on <30 trades is indicative only.")
    return "\n".join(lines) + "\n"
