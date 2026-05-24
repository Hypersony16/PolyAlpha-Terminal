from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Dict, Any

from bot.db import get_conn


def export_user_analytics(user_id: int) -> tuple[str, bytes]:
    """
    Export paper_auto_trades + paper_calibration as one JSON file.
    User can upload/send this later so the bot state can be restored/analyzed.
    """
    conn = get_conn()
    cur = conn.cursor()

    tables = {}
    for table in ["paper_auto_trades", "paper_calibration"]:
        try:
            cur.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in cur.fetchall()]
            cur.execute(f"SELECT * FROM {table} WHERE user_id = ? ORDER BY id ASC", (str(user_id),))
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            tables[table] = rows
        except Exception:
            tables[table] = []

    try:
        cur.execute("SELECT key, value FROM user_settings WHERE user_id = ?", (str(user_id),))
        tables["user_settings"] = [{"key": k, "value": v} for k, v in cur.fetchall() if str(k).startswith("paper_")]
    except Exception:
        tables["user_settings"] = []

    conn.close()

    payload = {
        "schema": "polyscalpbot_analytics_v1",
        "exported_at": datetime.utcnow().isoformat(),
        "user_id": str(user_id),
        "data": tables,
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    filename = f"polyscalp_analytics_{user_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    return filename, content


def import_user_analytics(user_id: int, raw: bytes) -> Dict[str, Any]:
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema") != "polyscalpbot_analytics_v1":
        raise ValueError("Unsupported analytics file")

    data = payload.get("data") or {}
    conn = get_conn()
    cur = conn.cursor()

    imported = {}
    for table in ["paper_auto_trades", "paper_calibration"]:
        rows = data.get(table) or []
        if not rows:
            imported[table] = 0
            continue

        # Replace IDs to avoid collisions; preserve all other fields that exist in current DB.
        cur.execute(f"PRAGMA table_info({table})")
        valid_cols = [r[1] for r in cur.fetchall() if r[1] != "id"]

        count = 0
        for row in rows:
            clean = {k: v for k, v in row.items() if k in valid_cols}
            clean["user_id"] = str(user_id)
            if not clean:
                continue
            cols = list(clean.keys())
            placeholders = ",".join(["?"] * len(cols))
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            cur.execute(sql, [clean[c] for c in cols])
            count += 1
        imported[table] = count

    for item in data.get("user_settings") or []:
        key = item.get("key")
        value = item.get("value")
        if key and str(key).startswith("paper_") and value is not None:
            cur.execute("""
                INSERT INTO user_settings (user_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
            """, (str(user_id), str(key), str(value)))

    conn.commit()
    conn.close()
    return imported


def analytics_text(user_id: int) -> str:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(pnl_usd),0),
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='closed' AND pnl_usd > 0 THEN 1 ELSE 0 END),
               COALESCE(AVG(edge),0), COALESCE(AVG(entry_price),0)
        FROM paper_auto_trades
        WHERE user_id = ?
    """, (str(user_id),))
    total, pnl, closed, wins, avg_edge, avg_entry = cur.fetchone()

    cur.execute("""
        SELECT side, COUNT(*), COALESCE(SUM(pnl_usd),0),
               SUM(CASE WHEN status='closed' AND pnl_usd > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END)
        FROM paper_auto_trades
        WHERE user_id = ?
        GROUP BY side
    """, (str(user_id),))
    side_rows = cur.fetchall()

    cur.execute("""
        SELECT market_slug, side, result, pnl_usd, entry_price, created_at
        FROM paper_auto_trades
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 8
    """, (str(user_id),))
    recent = cur.fetchall()

    conn.close()

    total = int(total or 0)
    closed = int(closed or 0)
    wins = int(wins or 0)
    winrate = wins / closed if closed else 0.0

    lines = [
        "<b>📊 ANALYTICS</b>",
        "<code>exportable paper-trade diagnostics</code>",
        "",
        f"Trades: <b>{total}</b>",
        f"Closed: <b>{closed}</b>",
        f"Win rate: <b>{winrate*100:.1f}%</b>",
        f"PnL: <b>${float(pnl or 0):.2f}</b>",
        f"Avg edge: <b>{float(avg_edge or 0)*100:.1f} pts</b>",
        f"Avg entry: <b>{float(avg_entry or 0):.3f}</b>",
        "",
        "<b>By side</b>",
    ]

    if side_rows:
        for side, n, side_pnl, side_wins, side_closed in side_rows:
            sr = (float(side_wins or 0) / float(side_closed or 1)) if side_closed else 0.0
            lines.append(f"• {side}: {int(n)} trades | {sr*100:.1f}% | ${float(side_pnl or 0):.2f}")
    else:
        lines.append("• no trades yet")

    lines.append("")
    lines.append("<b>Recent IDs</b>")
    if recent:
        for slug, side, result, pnl, entry, created in recent:
            short = str(slug or "no-slug").replace("btc-updown-15m-", "")
            lines.append(f"• {short} | {side}->{result or 'open'} | @ {float(entry or 0):.3f} | ${float(pnl or 0):.2f}")
    else:
        lines.append("• no recent trades")

    lines += [
        "",
        "Use /analytics_export to download JSON.",
        "Use /analytics_import then send/upload JSON to restore."
    ]
    return "\n".join(lines)
