from __future__ import annotations

from typing import Any, Dict, List, Tuple
from bot.db import get_conn
from bot.paper_auto import ensure_paper_auto_tables


def _fmt_money(x: Any) -> str:
    try:
        return f"${float(x):.2f}"
    except Exception:
        return "$0.00"


def _safe_pct(num: float, den: float) -> float:
    return (num / den) if den else 0.0


def _bucket_entry(price: float) -> str:
    if price < 0.40:
        return "35-40c"
    if price < 0.50:
        return "40-50c"
    if price < 0.60:
        return "50-60c"
    return "60c+"


def _bucket_edge(edge: float) -> str:
    e = edge * 100.0
    if e < 10:
        return "<10%"
    if e < 12:
        return "10-12%"
    if e < 15:
        return "12-15%"
    return "15%+"


def _bucket_model(prob: float) -> str:
    p = prob * 100.0
    if p < 65:
        return "62-65%"
    if p < 70:
        return "65-70%"
    if p < 80:
        return "70-80%"
    return "80%+"


def _query_group(cur, user_id: int, field_expr: str, where_extra: str = "") -> List[Tuple[Any, int, int, float, float, float]]:
    sql = f"""
        SELECT {field_expr} AS bucket,
               COUNT(*) AS n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(pnl_usd),0) AS pnl,
               COALESCE(AVG(pnl_usd),0) AS avg_pnl,
               COALESCE(AVG(entry_price),0) AS avg_entry
        FROM paper_auto_trades
        WHERE user_id = ? AND status='closed' {where_extra}
        GROUP BY bucket
        ORDER BY n DESC
    """
    cur.execute(sql, (str(user_id),))
    return cur.fetchall()


def strategy_breakdown(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COALESCE(trade_mode,'resolution') AS mode,
               COUNT(*) AS n,
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
               SUM(CASE WHEN status='closed' AND pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(pnl_usd),0) AS pnl,
               COALESCE(AVG(pnl_usd),0) AS avg_pnl,
               COALESCE(AVG(entry_price),0) AS avg_entry,
               COALESCE(AVG(edge),0) AS avg_edge,
               COALESCE(AVG(model_prob),0) AS avg_model,
               COALESCE(AVG(market_prob),0) AS avg_market
        FROM paper_auto_trades
        WHERE user_id=?
        GROUP BY COALESCE(trade_mode,'resolution')
        ORDER BY n DESC
    """, (str(user_id),))
    modes = cur.fetchall()

    cur.execute("""
        SELECT exit_reason, COUNT(*), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl_usd),0), COALESCE(AVG(pnl_usd),0),
               COALESCE(AVG(max_favorable),0), COALESCE(AVG(max_adverse),0)
        FROM paper_auto_trades
        WHERE user_id=? AND status='closed' AND COALESCE(trade_mode,'resolution')='scalp'
        GROUP BY exit_reason
        ORDER BY COUNT(*) DESC
    """, (str(user_id),))
    exits = cur.fetchall()

    # Raw rows for flexible buckets.
    cur.execute("""
        SELECT entry_price, edge, model_prob, pnl_usd, side, COALESCE(trade_mode,'resolution')
        FROM paper_auto_trades
        WHERE user_id=? AND status='closed'
    """, (str(user_id),))
    rows = cur.fetchall()
    conn.close()

    def bucketize(index_func):
        out: Dict[str, Dict[str, float]] = {}
        for entry, edge, model, pnl, side, mode in rows:
            b = index_func(float(entry or 0), float(edge or 0), float(model or 0), str(side or ''), str(mode or ''))
            d = out.setdefault(b, {"n":0,"wins":0,"pnl":0.0})
            d["n"] += 1
            d["wins"] += 1 if float(pnl or 0) > 0 else 0
            d["pnl"] += float(pnl or 0)
        return sorted(out.items(), key=lambda kv: (-kv[1]["n"], kv[0]))

    return {
        "modes": modes,
        "exits": exits,
        "entry_buckets": bucketize(lambda entry, edge, model, side, mode: _bucket_entry(entry)),
        "edge_buckets": bucketize(lambda entry, edge, model, side, mode: _bucket_edge(edge)),
        "model_buckets": bucketize(lambda entry, edge, model, side, mode: _bucket_model(model)),
        "side_buckets": bucketize(lambda entry, edge, model, side, mode: side or "?"),
    }


def strategy_breakdown_text(user_id: int) -> str:
    data = strategy_breakdown(user_id)
    lines = [
        "<b>🧩 STRATEGY BREAKDOWN</b>",
        "<code>scalp vs resolution + bucket diagnostics</code>",
        "",
        "<b>By mode</b>",
    ]

    if not data["modes"]:
        return "<b>🧩 STRATEGY BREAKDOWN</b>\nNo trades yet."

    for mode, n, closed, wins, pnl, avg_pnl, avg_entry, avg_edge, avg_model, avg_market in data["modes"]:
        closed = int(closed or 0)
        wins = int(wins or 0)
        wr = _safe_pct(wins, closed) * 100
        lines.append(
            f"• <b>{mode}</b>: {int(n or 0)} trades | {wr:.1f}% win | {_fmt_money(pnl)} PnL | avg {_fmt_money(avg_pnl)} | entry {float(avg_entry or 0):.3f} | edge {float(avg_edge or 0)*100:.1f} pts"
        )

    lines += ["", "<b>Scalp exits</b>"]
    if data["exits"]:
        for reason, n, wins, pnl, avg_pnl, avg_mfe, avg_mae in data["exits"]:
            wr = _safe_pct(float(wins or 0), float(n or 0)) * 100
            lines.append(f"• {reason or 'unknown'}: {int(n)} | {wr:.1f}% | {_fmt_money(pnl)} | avg {_fmt_money(avg_pnl)} | MFE {float(avg_mfe or 0)*100:.1f}c / MAE {float(avg_mae or 0)*100:.1f}c")
    else:
        lines.append("• no scalp exits yet")

    def add_bucket(title: str, items):
        lines.extend(["", f"<b>{title}</b>"])
        if not items:
            lines.append("• no data")
            return
        for name, d in items:
            n = int(d["n"])
            wr = _safe_pct(float(d["wins"]), float(n)) * 100
            lines.append(f"• {name}: {n} | {wr:.1f}% | {_fmt_money(d['pnl'])}")

    add_bucket("Entry buckets", data["entry_buckets"])
    add_bucket("Edge buckets", data["edge_buckets"])
    add_bucket("Model-prob buckets", data["model_buckets"])
    add_bucket("Side buckets", data["side_buckets"])

    lines += [
        "",
        "<b>Interpretation</b>",
        "• Cut buckets with negative PnL after 100+ samples.",
        "• If scalp exits lose but resolution wins, switch /mode resolution.",
        "• If resolution loses but scalp wins, switch /mode scalp."
    ]
    return "\n".join(lines)
