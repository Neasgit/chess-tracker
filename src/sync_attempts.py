# sync_attempts.py — robust Lichess attempts sync with retries/backoff and env knobs
from __future__ import annotations

import os
import sys
import time
import json
import math
import typing as t
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy import text
from sqlalchemy.engine import Engine

API = "https://lichess.org/api/puzzle/activity"  # NDJSON stream

# ---------- env knobs ----------
def _getenv_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default

ATTEMPTS_MAX        = _getenv_int("ATTEMPTS_MAX", 1_000_000)
ATTEMPTS_RETRY_MAX  = _getenv_int("ATTEMPTS_RETRY_MAX", 5)
ATTEMPTS_RETRY_WAIT = _getenv_int("ATTEMPTS_RETRY_WAIT", 10)  # seconds between outer-loop retries
ATTEMPTS_TIMEOUT    = _getenv_int("ATTEMPTS_TIMEOUT", 30)     # per-request timeout seconds

# ---------- helpers ----------
def _iso_from_ms(ms: int) -> str:
    # Lichess gives milliseconds since epoch
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")

def _session_with_retries() -> requests.Session:
    # Automatic retries for transient failures (429/5xx and connection errors)
    retry = Retry(
        total=ATTEMPTS_RETRY_MAX,
        connect=ATTEMPTS_RETRY_MAX,
        read=ATTEMPTS_RETRY_MAX,
        backoff_factor=1.5,                 # 0s, 1.5s, 3s, 4.5s, ...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    # Be polite and explicit
    s.headers.update({"User-Agent": "sean-chess-sync/1.0 (+requests)"})
    return s

def _get_last_attempt_ms(engine: Engine) -> int | None:
    sql = text("SELECT MAX(strftime('%s', attempted_at))*1000 AS last_ms FROM attempts WHERE user_id=1")
    with engine.begin() as conn:
        row = conn.execute(sql).one()
        return int(row[0]) if row and row[0] is not None else None

def _ensure_user(engine: Engine, username: str) -> int:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO users (id, username) VALUES (1, ?)",
            (username or "me",),
        )
    return 1

def _upsert_attempt(engine: Engine, user_id: int, pid: str, when_iso: str, win: bool):
    # Matches your schema: unique (user_id, puzzle_id, attempted_at)
    # time_ms and puzzle_rating_after unknown here -> NULL
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT OR IGNORE INTO attempts(user_id, puzzle_id, attempted_at, result, time_ms, puzzle_rating_after)
            VALUES (?, ?, ?, ?, NULL, NULL)
            """,
            (user_id, pid, when_iso, "win" if win else "loss"),
        )

# ---------- core fetch ----------
def _fetch_and_upsert(engine: Engine, headers: dict[str, str], since_ms: int | None) -> tuple[int, int | None, set[str]]:
    """
    Returns: (inserted_count, latest_ms_seen, changed_puzzle_ids)
    """
    params = {"max": ATTEMPTS_MAX}
    # NOTE: Lichess puzzle-activity API does not have since param; we’ll still
    # guard with UNIQUE insert and just ignore older duplicates quickly.
    changed: set[str] = set()
    inserted = 0
    latest_ms = since_ms or 0

    s = _session_with_retries()

    try:
        with s.get(API, headers=headers, params=params, stream=True, timeout=ATTEMPTS_TIMEOUT) as r:
            r.raise_for_status()
            # stream in modest chunks: iterate line by line
            for line in r.iter_lines(decode_unicode=True, chunk_size=1024):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ms = int(obj.get("date", 0))  # ms since epoch
                if ms <= (since_ms or 0):
                    # Older than or equal to what we’ve seen; safe to skip quickly
                    continue

                puzzle = obj.get("puzzle") or {}
                pid = puzzle.get("id")
                if not pid:
                    continue
                win = bool(obj.get("win", False))

                when_iso = _iso_from_ms(ms)
                _upsert_attempt(engine, 1, pid, when_iso, win)
                inserted += 1
                changed.add(pid)
                if ms > latest_ms:
                    latest_ms = ms

    finally:
        s.close()

    return inserted, (latest_ms if inserted > 0 else since_ms), changed

# ---------- public entry ----------
def run(username: str, token: str, engine: Engine) -> None:
    user_id = _ensure_user(engine, username)

    # Build headers (token required)
    headers = {"Accept": "application/x-ndjson"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # find last seen attempt timestamp to skip ancient rows faster (still safe due to UNIQUE)
    last_ms = _get_last_attempt_ms(engine)

    # Outer guard: if even the retrying Session fails, do a small manual retry loop
    tries = 0
    inserted_total = 0
    changed_pids_total: set[str] = set()
    while True:
        tries += 1
        try:
            ins, latest, changed = _fetch_and_upsert(engine, headers, last_ms)
            inserted_total += ins
            changed_pids_total |= changed
            # If nothing new, we’re done
            print(f"[sync_attempts] Upserted {ins} attempts.")
            break
        except requests.exceptions.RequestException as e:
            # Automatic retries were exhausted; do a short manual pause then try again
            if tries <= 3:
                wait = ATTEMPTS_RETRY_WAIT
                print(f"[sync_attempts] Network error: {e!r}. Retrying in {wait}s...", flush=True)
                try:
                    time.sleep(wait)
                    continue
                except KeyboardInterrupt:
                    raise
            else:
                print(f"[sync_attempts] Giving up after {tries} tries: {e!r}", file=sys.stderr, flush=True)
                break
        except KeyboardInterrupt:
            # Let Ctrl-C abort immediately
            raise

    # Nothing else to do here; compute_srs will read attempts normally
