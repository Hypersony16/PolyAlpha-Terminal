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
        rows = data.get(table, [])
        if not rows:
            imported[table] = 0
            continue
        try:
            cur.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in cur.fetchall()]
            if not cols:
                imported[table] = 0
                continue
            count = 0
            for row in rows:
                vals = [row.get(c) for c in cols if c != "id"]
                col_names = [c for c in cols if c != "id"]
                placeholders = ",".join(["?"] * len(col_names))
                try:
                    cur.execute(
                        f"INSERT OR IGNORE INTO {table} ({','.join(col_names)}) VALUES ({placeholders})",
                        vals,
                    )
                    count += 1
                except Exception:
                    pass
            imported[table] = count
        except Exception:
            imported[table] = 0

    try:
        for item in data.get("user_settings", []):
            key = item.get("key", "")
            value = item.get("value", "")
            if key.startswith("paper_"):
                cur.execute(
                    "INSERT OR REPLACE INTO user_settings(user_id, key, value) VALUES (?, ?, ?)",
                    (str(user_id), key, value),
                )
    except Exception:
        pass

    conn.commit()
    conn.close()
    return {"imported": imported}


def analytics_text(user_id: int) -> str:
    conn = get_conn()
    cur = conn.cursor()

    lines = ["📈 <b>Analytics Export</b>"]

    try:
        cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(pnl_usd),0), COALESCE(AVG(pnl_usd),0) "
            "FROM paper_auto_trades WHERE user_id = ? AND status='closed'",
            (str(user_id),),
        )
        row = cur.fetchone()
        if row and row[0]:
            n, wins, total_pnl, avg_pnl = row
            winrate = (wins / n * 100) if n else 0
            lines.append(f"Closed trades: {n}")
            lines.append(f"Win rate: {winrate:.1f}%")
            lines.append(f"Total PnL: ${total_pnl:.2f}")
            lines.append(f"Avg PnL: ${avg_pnl:.2f}")
        else:
            lines.append("No closed trades yet.")
    except Exception:
        lines.append("No trade data available.")

    conn.close()
    return "\n".join(lines)
