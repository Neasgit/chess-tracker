# update.py — orchestrates: init DB -> sync puzzles -> sync attempts -> compute SRS -> render report
import os
import sys
import time
import importlib
from pathlib import Path
from datetime import datetime
import shutil, subprocess, os

def backup_db(db_path: str, backups_dir: str = "backups"):
    Path(backups_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    dst = Path(backups_dir) / f"lichess_{ts}.sqlite3"
    try:
        shutil.copy2(db_path, dst)
        print(f"[backup] Wrote {dst}")
    except Exception as e:
        print(f"[backup] WARN: {e}")
    # rotate (best-effort)
    rot = Path("scripts/rotate_backups.sh")
    if rot.exists():
        try:
            subprocess.run(["bash", str(rot)], check=False)
        except Exception as e:
            print(f"[backup] rotate WARN: {e}")

# local modules
from db import init_db, get_engine

# ---------- tiny .env loader (no dependency on python-dotenv) ----------
def load_dotenv(path: str | os.PathLike = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and (os.getenv(k) is None):
            os.environ[k] = v

# ---------- imports to pluggable steps ----------
def _get_steps():
    sync_puzzles = importlib.import_module("sync_puzzles").run
    sync_attempts = importlib.import_module("sync_attempts").run
    compute_srs  = importlib.import_module("compute_srs").run   # expects (engine) or no args
    report       = importlib.import_module("report").run        # expects (engine, outdir="reports")
    return sync_puzzles, sync_attempts, compute_srs, report

# ---------- main workflow ----------
def main() -> None:
    t0 = time.perf_counter()

    # env
    load_dotenv(".env")
    DB           = os.getenv("DB_PATH", "./db/lichess_puzzles.sqlite3")
    PUZZLES_URL  = os.getenv("PUZZLE_CSV_URL", "https://database.lichess.org/lichess_db_puzzle.csv.zst")
    USER         = os.getenv("LICHESS_USERNAME", "")
    TOKEN        = os.getenv("LICHESS_TOKEN", "")
    OUTDIR       = "reports"

    # database
    init_db(DB, "src/schema.sql")
    eng = get_engine(DB)

    # dynamic steps
    sync_puzzles, sync_attempts, compute_srs, report = _get_steps()

    # 1) puzzles
    print("==> Syncing puzzles", flush=True)
    try:
        sync_puzzles(PUZZLES_URL, eng)
    except TypeError:
        # older version that only takes (engine)
        sync_puzzles(eng)

    # 2) attempts
    print("==> Syncing attempts", flush=True)
    sync_attempts(USER, TOKEN, eng)

    # 3) SRS
    print("==> Computing SRS", flush=True)
    try:
        compute_srs(eng)
    except TypeError:
        # older version that takes no args
        compute_srs()

    # 4) report(s)
    print("==> Generating report", flush=True)
    out = report(eng, OUTDIR)
    # report() returns a Path in some versions; just informational:
    if out:
        print(f"Report written to {out}", flush=True)

    print(f"Done in {time.perf_counter() - t0:.1f}s", flush=True)

# ---------- entrypoint for launchd & manual runs ----------
if __name__ == "__main__":
    print(f"[launcher] start {datetime.now().astimezone().isoformat()}", flush=True)
    try:
        main()
        print(f"[launcher] done  {datetime.now().astimezone().isoformat()}", flush=True)
    except Exception as e:
        # mirror to stderr so it appears in launchd.err.log
        print(f"[launcher] error {e!r}", file=sys.stderr, flush=True)
        raise
