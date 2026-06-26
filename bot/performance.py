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
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(pnl_usd),0) AS pnl
        FROM paper_auto_trades
        WHERE user_id = ?
        GROUP BY mode
    """, (str(user_id),))
    mode_rows = cur.fetchall()

    by_entry = _query_group(cur, user_id, "CASE WHEN entry_price < 0.40 THEN '35-40c' WHEN entry_price < 0.50 THEN '40-50c' WHEN entry_price < 0.60 THEN '50-60c' ELSE '60c+' END")
    by_edge = _query_group(cur, user_id, "CASE WHEN edge < 0.10 THEN '<10%' WHEN edge < 0.12 THEN '10-12%' WHEN edge < 0.15 THEN '12-15%' ELSE '15%+' END")
    by_model = _query_group(cur, user_id, "CASE WHEN model_prob < 0.65 THEN '62-65%' WHEN model_prob < 0.70 THEN '65-70%' WHEN model_prob < 0.80 THEN '70-80%' ELSE '80%+' END")

    conn.close()
    return {
        "by_mode": mode_rows,
        "by_entry": by_entry,
        "by_edge": by_edge,
        "by_model": by_model,
    }


def strategy_breakdown_text(user_id: int) -> str:
    data = strategy_breakdown(user_id)
    lines = ["📊 <b>Strategy Performance Breakdown</b>"]

    mode_rows = data.get("by_mode", [])
    if mode_rows:
        lines.append("\n<b>By Mode:</b>")
        for row in mode_rows:
            mode, n, closed, wins, pnl = row
            winrate = _safe_pct(wins, closed) * 100 if closed else 0
            lines.append(f"  {mode}: {n} trades | {winrate:.0f}% win | PnL {_fmt_money(pnl)}")

    by_entry = data.get("by_entry", [])
    if by_entry:
        lines.append("\n<b>By Entry Price:</b>")
        for row in by_entry:
            bucket, n, wins, pnl, avg_pnl, avg_entry = row
            winrate = _safe_pct(wins, n) * 100
            lines.append(f"  {bucket}: {n} trades | {winrate:.0f}% win | PnL {_fmt_money(pnl)}")

    by_edge = data.get("by_edge", [])
    if by_edge:
        lines.append("\n<b>By Edge:</b>")
        for row in by_edge:
            bucket, n, wins, pnl, avg_pnl, avg_entry = row
            winrate = _safe_pct(wins, n) * 100
            lines.append(f"  {bucket}: {n} trades | {winrate:.0f}% win | PnL {_fmt_money(pnl)}")

    by_model = data.get("by_model", [])
    if by_model:
        lines.append("\n<b>By Model Prob:</b>")
        for row in by_model:
            bucket, n, wins, pnl, avg_pnl, avg_entry = row
            winrate = _safe_pct(wins, n) * 100
            lines.append(f"  {bucket}: {n} trades | {winrate:.0f}% win | PnL {_fmt_money(pnl)}")

    if len(lines) == 1:
        lines.append("No closed trades yet.")

    return "\n".join(lines)
