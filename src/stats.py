# src/stats.py
from __future__ import annotations
from sqlite3 import Connection

def today_attempt_stats(conn: Connection, user_id: int = 1) -> dict:
    sql = """
      SELECT
        SUM(CASE WHEN date(attempted_at) = date('now','localtime') THEN 1 ELSE 0 END) AS attempts_today,
        SUM(CASE WHEN date(attempted_at) = date('now','localtime') AND lower(result)='win'  THEN 1 ELSE 0 END) AS wins_today,
        SUM(CASE WHEN date(attempted_at) = date('now','localtime') AND lower(result)='loss' THEN 1 ELSE 0 END) AS losses_today
      FROM attempts
      WHERE user_id = ?
    """
    row = conn.execute(sql, (user_id,)).fetchone()
    attempts = int(row[0] or 0)
    wins = int(row[1] or 0)
    losses = int(row[2] or 0)
    return {"attempts_today": attempts, "wins_today": wins, "losses_today": losses}
