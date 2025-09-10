# compute_srs.py
import os
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from sqlalchemy import text

try:
    from .db import get_engine
except ImportError:
    from db import get_engine
# ---------------- env helpers ----------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).lower()
    return v in ("1", "true", "yes", "on")

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _parse_cadence(s: str, fallback: List[int]) -> List[int]:
    out: List[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or fallback

# --------------- config ----------------
TRACK_WINS      = _env_bool("SRS_TRACK_WINS", False)  # <-- key switch
LOSS_CADENCE    = _parse_cadence(os.getenv("SRS_LOSS_CADENCE"), [1,2,4,7,14,30,60,90])
WIN_CADENCE     = _parse_cadence(os.getenv("SRS_WIN_CADENCE"),  [2,4,7,14,30,60,90])  # only used if TRACK_WINS=true
RESET_ON_FAIL   = _env_bool("SRS_RESET_ON_FAIL", True)

SEED_MODE        = _env_str("SRS_SEED_MODE", "tomorrow")   # "tomorrow" | "stagger"
def _env_int(name: str, default: int) -> int:
    v = os.getenv(name, str(default))
    v = v.split("#", 1)[0].strip()   # drop inline comments if present
    try:
        return int(v)
    except Exception:
        return default

STAGGER_BUCKETS  = _env_int("SRS_STAGGER_BUCKETS", 7)
# --------------- SQL ----------------
SQL_LAST_ATTEMPT = text("""
SELECT a.puzzle_id,
       MAX(a.attempted_at) AS last_attempt,
       (SELECT result FROM attempts a2
         WHERE a2.user_id=1 AND a2.puzzle_id=a.puzzle_id
         ORDER BY a2.attempted_at DESC LIMIT 1) AS last_result
FROM attempts a
WHERE a.user_id=1
GROUP BY a.puzzle_id
""")

SQL_GET_EXISTING = text("""
SELECT puzzle_id, success_streak, interval_days, last_result, last_reviewed, due_date
FROM srs WHERE user_id=1 AND puzzle_id=:pid
""")

SQL_UPSERT_SRS = text("""
INSERT INTO srs (user_id, puzzle_id, last_result, success_streak, interval_days, due_date, last_reviewed)
VALUES (1, :puzzle_id, :last_result, :streak, :interval_days, :due_date, :last_reviewed)
ON CONFLICT(user_id, puzzle_id) DO UPDATE SET
  last_result    = excluded.last_result,
  success_streak = excluded.success_streak,
  interval_days  = excluded.interval_days,
  due_date       = excluded.due_date,
  last_reviewed  = excluded.last_reviewed
""")

SQL_DELETE_SRS = text("DELETE FROM srs WHERE user_id=1 AND puzzle_id=:pid")

# --------------- helpers ----------------
def _local_date_of_iso(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z","+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")

def _next_interval_days(streak: int, for_win: bool) -> int:
    cad = WIN_CADENCE if (for_win and TRACK_WINS) else LOSS_CADENCE
    idx = min(max(streak,1) - 1, len(cad)-1)
    return cad[idx]

def _seed_offset_days(puzzle_id: str) -> int:
    if SEED_MODE == "stagger":
        base = 0
        for ch in puzzle_id:
            base = (base * 131 + ord(ch)) & 0x7fffffff
        return (base % max(STAGGER_BUCKETS,1)) or 1
    return 1  # tomorrow

def _calc_update(existing: Optional[dict], last_result: str, last_attempt_local_date: str, puzzle_id: str):
    """Return (streak, interval_days, base_date, plus_expr, for_win)"""
    is_win = (last_result == "win")
    if existing:
        streak = existing["success_streak"] or 0
        if is_win:
            # won last attempt
            if TRACK_WINS:
                streak += 1
            else:
                # we don't track wins -> caller will delete row
                return None
        else:
            # lost last attempt
            streak = 0 if RESET_ON_FAIL else max(streak-1, 0)
        interval = _next_interval_days(streak if (is_win and TRACK_WINS) else 1 if is_win else (streak or 1),
                                       for_win=is_win)
        return (streak, interval, last_attempt_local_date, f"+{interval} day", is_win)
    else:
        # no SRS row yet: create only if loss, or win if TRACK_WINS=true
        if is_win and not TRACK_WINS:
            return None
        streak = 1 if is_win else 0
        # First due: tomorrow (or staggered)
        interval = _seed_offset_days(puzzle_id) if not is_win else _next_interval_days(streak or 1, for_win=True)
        return (streak, interval, last_attempt_local_date, f"+{interval} day", is_win)

# --------------- main ----------------
def run(engine=None, changed_pids: Optional[list]=None):
    """
    Recompute/seed SRS schedule.
    Accepts optional changed_pids for compatibility with update.py.
    """
    eng = engine or get_engine(os.getenv("DB_PATH", "./db/lichess_puzzles.sqlite3"))
    updated = 0
    deleted = 0

    with eng.begin() as conn:
        rows = conn.execute(SQL_LAST_ATTEMPT).mappings().all()
        for r in rows:
            pid = r["puzzle_id"]
            if changed_pids is not None and pid not in changed_pids:
                continue

            last_result = (r["last_result"] or "loss").lower()
            last_attempt_date = _local_date_of_iso(r["last_attempt"])
            existing = conn.execute(SQL_GET_EXISTING, {"pid": pid}).mappings().first()

            # If we don't track wins and latest result is win: remove existing row if any.
            if last_result == "win" and not TRACK_WINS:
                if existing:
                    conn.execute(SQL_DELETE_SRS, {"pid": pid})
                    deleted += 1
                continue

            res = _calc_update(existing, last_result, last_attempt_date, pid)
            if res is None:
                # means: latest is win and TRACK_WINS=false (handled above), or other non-trackables
                continue

            streak, interval, base_date, plus, is_win = res
            due_val = conn.execute(text("SELECT date(:base, :plus) AS d"),
                                   {"base": base_date, "plus": plus}).mappings().one()["d"]

            conn.execute(SQL_UPSERT_SRS, {
                "puzzle_id": pid,
                "last_result": last_result,
                "streak": streak,
                "interval_days": interval,
                "due_date": due_val,
                "last_reviewed": base_date
            })
            updated += 1

    print(f"[compute_srs] Updated SRS for {updated} puzzles. Removed {deleted} non-tracked rows.")

if __name__ == "__main__":
    run()
