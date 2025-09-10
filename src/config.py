# src/config.py
from __future__ import annotations
import os
from pathlib import Path

# Load .env if present (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Project root (two levels up from src/config.py)
ROOT = Path(__file__).resolve().parent.parent

# ---------------- env readers ----------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "on")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _env_csv_int(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for p in raw.split(","):
        p = p.strip()
        if p.isdigit():
            out.append(int(p))
    return out or list(default)

# ---------------- Core paths ----------------
DB_PATH = _env_str("DB_PATH", "./db/lichess_puzzles.sqlite3")

def get_db_path() -> str:
    """
    Return the DB path, making it absolute if it’s relative to project ROOT.
    """
    p = DB_PATH
    if not p.startswith("/"):
        return str((ROOT / p).resolve())
    return p

# ---------------- Identity / user ----------------
LICHESS_USERNAME = _env_str("LICHESS_USERNAME", "me")

# ---------------- Local server ----------------
LOCAL_LOG_PORT = _env_int("LOCAL_LOG_PORT", 8765)
INCLUDE_OVERDUE = _env_bool("INCLUDE_OVERDUE", True)
QUEUE_CAP = _env_int("QUEUE_CAP", 60)
HIDE_TODAY_DONE = _env_bool("HIDE_TODAY_DONE", True)
LOCAL_LOG_DEDUP_SECONDS = _env_int("LOCAL_LOG_DEDUP_SECONDS", 2)

# ---------------- SRS ----------------
SRS_TRACK_WINS = _env_bool("SRS_TRACK_WINS", False)
SRS_RESET_ON_FAIL = _env_bool("SRS_RESET_ON_FAIL", True)
SRS_LOSS_CADENCE = _env_csv_int("SRS_LOSS_CADENCE", [1,2,4,7,14,30,60,90])
SRS_WIN_CADENCE  = _env_csv_int("SRS_WIN_CADENCE",  [2,4,7,14,30,60,90])
SRS_SEED_MODE = _env_str("SRS_SEED_MODE", "tomorrow")  # or "stagger"
SRS_STAGGER_BUCKETS = _env_int("SRS_STAGGER_BUCKETS", 7)

# ---------------- Reports ----------------
REPORT_HTML = _env_bool("REPORT_HTML", True)

# ---------------- Compat helpers for serve.py ----------------
# These mimic the old helper names so serve.py works unchanged.
get_bool = _env_bool
get_int  = _env_int
get_str  = _env_str
# NOTE: get_db_path is already defined above — don’t redefine it here.
