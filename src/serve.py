# serve.py — local retry logger + single-card queue with theme filter
# ------------------------------------------------------------------
# How to run:
#   macOS:    PYTHONPATH=src source .venv/bin/activate && python src/serve.py
#   Windows:  set PYTHONPATH=src & .\.venv\Scripts\Activate.ps1 & python src\serve.py
#
# SECTION MAP (search these tags):
# [1] Imports
# [2] Config / env
# [3] Optional compute_srs import
# [4] JSON helper
# [5] DB helpers (ensure_user, insert_attempt, dedup)
# [6] Recompute SRS (single-puzzle tolerant caller)
# [7] Query due rows
# [8] HTML builder (_queue_html)  ├─ [H-1] Head/Styles
#                                 ├─ [H-2] Toolbar
#                                 ├─ [H-3] Card (single puzzle view)
#                                 ├─ [H-4] Client data boot
#                                 ├─ [H-5] Local state & stats
#                                 ├─ [H-6] Theme filter
#                                 ├─ [H-7] Render & actions
#                                 └─ [H-8] Keyboard bindings
# [9]  HTTP handler (routes + / → /queue redirect)
# [10] Entrypoint (port-in-use help)
# ─────────────────────────────── [1] Imports ───────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import os
import traceback

# Always import from the src package (relative import ensures it works with -m src.serve)
from .config import (
    get_db_path,             # function to resolve DB path
    LOCAL_LOG_PORT,          # int
    INCLUDE_OVERDUE,         # bool
    QUEUE_CAP,               # int
    HIDE_TODAY_DONE,         # bool
    LOCAL_LOG_DEDUP_SECONDS, # int
    LICHESS_USERNAME,        # str
)

from .db import open_sqlite   # local db helper

# ─────────────────────────────── [2] Config / env ──────────────────────────
# Directly use constants/functions from config.py
# No need to re-import under new names — keep usage consistent everywhere.

PORT = LOCAL_LOG_PORT
USERNAME = LICHESS_USERNAME

# ──────────────────────── [3] Optional compute_srs import ──────────────────
# Prefer package-style import (when serve.py is inside src/). Fall back for legacy.
try:
    from . import compute_srs as _compute       # running as `python -m src.serve`
except Exception:
    try:
        import compute_srs as _compute          # running as `python serve.py` with PYTHONPATH=src
    except Exception:
        _compute = None

# ───────────────────────────────── [4] JSON helper ─────────────────────────
def _json(h: BaseHTTPRequestHandler, status=200, body=None):
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)

# ─────────────────────────────── [5] DB helpers ─────────────────────────────
def _ensure_user(conn):
    conn.execute("INSERT OR IGNORE INTO users(id, username) VALUES (1, ?)", (USERNAME,))

def _insert_attempt(conn, puzzle_id: str, result: str):
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()]
    if "source" in cols:
        conn.execute(
            "INSERT INTO attempts(user_id,puzzle_id,attempted_at,result,time_ms,puzzle_rating_after,source) "
            "VALUES (1, ?, ?, ?, NULL, NULL, 'local')",
            (puzzle_id, iso, result),
        )
    else:
        conn.execute(
            "INSERT INTO attempts(user_id,puzzle_id,attempted_at,result,time_ms,puzzle_rating_after) "
            "VALUES (1, ?, ?, ?, NULL, NULL)",
            (puzzle_id, iso, result),
        )

def _dedup_recent(conn, puzzle_id: str, result: str) -> bool:
    """
    Return True if a same (puzzle_id, result) attempt exists within LOCAL_LOG_DEDUP_SECONDS.
    """
    if LOCAL_LOG_DEDUP_SECONDS <= 0:
        return False
    sql = """
      SELECT attempted_at
      FROM attempts
      WHERE user_id=1 AND puzzle_id=? AND lower(result)=lower(?)
      ORDER BY attempted_at DESC
      LIMIT 1
    """
    row = conn.execute(sql, (puzzle_id, result)).fetchone()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row[0].replace("Z","+00:00"))
    except Exception:
        return False
    delta = datetime.now(timezone.utc) - (last if last.tzinfo else last.replace(tzinfo=timezone.utc))
    return delta.total_seconds() < LOCAL_LOG_DEDUP_SECONDS

# ─────────────────────────── [6] Recompute SRS (tolerant) ──────────────────
def _recompute_srs_single(puzzle_id: str | None):
    """
    Recompute SRS after a log. Prefer the modern signature:
        run(changed_pids=[pid])
    but gracefully fall back to older variants used earlier in this project.
    """
    if not _compute:
        return

    # Newest API: run(changed_pids=[...])
    try:
        _compute.run(changed_pids=[puzzle_id] if puzzle_id else None)
        return
    except TypeError:
        pass

    # Older variants used in this repo (keep these for compatibility):
    try:
        _compute.run(None, [puzzle_id] if puzzle_id else None)
        return
    except Exception:
        pass
    try:
        _compute.run([puzzle_id] if puzzle_id else None)
        return
    except Exception:
        pass
    try:
        _compute.run()
    except Exception:
        pass
# ─────────────────────────────── [7] Query due rows ────────────────────────
def _due_rows(limit=2000):
    """
    Load the queue with optional filters:
      - INCLUDE_OVERDUE
      - HIDE_TODAY_DONE
      - QUEUE_CAP (applied last)
    """
    where = []
    where.append("s.user_id=1")
    if INCLUDE_OVERDUE:
        where.append("s.due_date <= date('now','localtime')")
    else:
        where.append("s.due_date = date('now','localtime')")
    # hide items already attempted today (any result)
    if HIDE_TODAY_DONE:
        where.append("""
          NOT EXISTS (
            SELECT 1 FROM attempts a
            WHERE a.user_id=1
              AND a.puzzle_id = s.puzzle_id
              AND date(a.attempted_at) = date('now','localtime')
          )
        """)
    sql = f"""
      SELECT s.puzzle_id, p.themes, s.due_date,
             (SELECT COUNT(*) FROM attempts a WHERE a.user_id=1 AND a.puzzle_id=s.puzzle_id) AS attempts,
             (SELECT MAX(a.attempted_at) FROM attempts a WHERE a.user_id=1 AND a.puzzle_id=s.puzzle_id) AS last_attempt
      FROM srs s
      JOIN puzzles p ON p.puzzle_id = s.puzzle_id
      WHERE {' AND '.join(where)}
      ORDER BY s.due_date, s.puzzle_id
      LIMIT ?
    """
    cap = max(1, min(QUEUE_CAP or 2000, 2000))
    with open_sqlite() as conn:
        return conn.execute(sql, (cap,)).fetchall()

# ─────────────────────────────── [8] HTML builder ──────────────────────────
def _queue_html():
    try:
        # Build items from DB
        raw = _due_rows(limit=2000)
        items = [{
            "puzzle_id": pid,
            "themes": (themes or ""),
            "attempts": int(attempts or 0),
            "last": last or "",
            "due": due or ""
        } for (pid, themes, due, attempts, last) in raw]

        # Official-like labels (subset you actually see in your data)
        THEME_MAP = {
            # Phases
            "opening": "Opening",
            "middlegame": "Middlegame",
            "endgame": "Endgame",
            "rookEndgame": "Rook endgame",
            "bishopEndgame": "Bishop endgame",
            "pawnEndgame": "Pawn endgame",
            "knightEndgame": "Knight endgame",
            "queenEndgame": "Queen endgame",
            "queenRookEndgame": "Queen and Rook",
            # Motifs (popular)
            "pin": "Pin", "fork": "Fork", "skewer": "Skewer",
            "clearance": "Clearance", "deflection": "Deflection",
            "discoveredAttack": "Discovered attack",
            "hangingPiece": "Hanging piece",
            "kingsideAttack": "Kingside attack", "queensideAttack": "Queenside attack",
            "exposedKing": "Exposed king",
            # Mates (common)
            "checkmate": "Checkmate",
            "mateIn1": "Mate in 1", "mateIn2": "Mate in 2", "mateIn3": "Mate in 3", "mateIn4": "Mate in 4",
            "backRankMate": "Back rank mate", "smotheredMate": "Smothered mate",
            # Lengths/Goals
            "oneMove": "One-move puzzle", "short": "Short puzzle", "long": "Long puzzle",
            "advantage": "Advantage", "crushing": "Crushing", "equality": "Equality",
        }

        # Collect keys present
        present_keys = set()
        for it in items:
            for t in (it["themes"] or "").replace(",", " ").split():
                if t in THEME_MAP:
                    present_keys.add(t)

        theme_options = sorted(
            ({"key": k, "label": THEME_MAP[k]} for k in present_keys),
            key=lambda d: d["label"].lower()
        )

        payload = json.dumps({"items": items, "themes": theme_options, "themeLabels": THEME_MAP}, ensure_ascii=False)

        # Raw string with placeholders; replaced at the end
        html = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Queue — Lichess Puzzles</title>

<!-- [H-1] Styles -->
<style>
:root {
  --bg: #f6f7fb; --fg: #0f172a; --muted:#64748b; --border:#e2e8f0; --card:#ffffff;
  --row:#f8fafc; --accent:#2563eb; --danger:#ef4444; --ok:#16a34a; --open:#22c55e;
}
* { box-sizing:border-box; }
body {
  margin:0; color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,"Noto Sans";
  background: linear-gradient(180deg, #eef2ff 0%, #f6f7fb 30%, #ffffff 100%);
}
.wrap { max-width:860px; margin:0 auto; padding:24px; }
h1 { margin:0 0 10px; font-size:24px; letter-spacing:.2px; }
.sub { color:var(--muted); font-size:12px; }

.toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:14px 0 18px; }
.toolbar .stat { background:#ffffffaa; border:1px solid var(--border); border-radius:999px; padding:6px 10px; }

select, button {
  padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:#fff;
  cursor:pointer; transition:filter .12s ease, transform .12s ease;
}
button:hover { filter:brightness(1.05); transform: translateY(-1px); }

button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
button.primary:hover { background:var(--accent); filter:brightness(1.1); }

button.danger  { background:var(--danger); color:#fff; border-color:var(--danger); }
button.danger:hover  { background:var(--danger); filter:brightness(1.1); }

button.openbtn { background:var(--open); color:#fff; border-color:var(--open); }
button.openbtn:hover { background:var(--open); filter:brightness(1.1); }

button.ghost   { background:#f1f5f9; color:var(--fg); border-color:var(--border); }
button.ghost:hover { background:#e2e8f0; }

.card {
  border:1px solid var(--border); background:var(--card); border-radius:16px; padding:18px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .06);
}
.topline { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.badge { display:inline-block; background:#eef2ff; padding:2px 8px; border-radius:999px; font-size:12px; }
.kv { color:var(--muted); font-size:12px; }

.meta { display:grid; grid-template-columns: 1fr 1fr; gap:8px; margin:10px 0 2px; }
.meta .box { background:var(--row); border:1px solid var(--border); border-radius:10px; padding:8px 10px; min-height:40px; }
.meta .label { font-size:11px; color:var(--muted); margin-bottom:2px; }

.actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
.notice-ok { color:var(--ok); font-weight:600; }
.notice-err { color:var(--danger); font-weight:600; }
hr { border:none; border-top:1px solid var(--border); margin:12px 0; }
.small { font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <h1>Due Queue</h1>
  <div class="sub">Includes overdue: <strong>__INCLUDE__</strong></div>

  <!-- [H-2] Toolbar -->
  <div class="toolbar">
    <label class="kv">Theme
      <select id="themeSel">
        <option value="">All themes</option>
      </select>
    </label>
    <button class="ghost" id="resetIdx">Reset position</button>
    <button class="ghost" id="resetStats">Reset today</button>
    <span class="stat" id="today">Today: 0 win / 0 loss</span>
    <span class="kv" id="meta"></span>
  </div>

  <!-- [H-3] Card (single puzzle view) -->
  <div class="card">
    <div class="topline">
      <div>
        <strong id="pid">—</strong>
        <span class="badge" id="due">—</span>
      </div>
      <div class="kv" id="msg"></div>
    </div>

    <div class="meta">
      <div class="box">
        <div class="label">Themes</div>
        <div id="themes">—</div>
      </div>
      <div class="box">
        <div class="label">Attempts</div>
        <div><span id="attempts">0</span></div>
      </div>
      <div class="box">
        <div class="label">Last Attempt</div>
        <div id="last">—</div>
      </div>
      <div class="box">
        <div class="label">Next Due</div>
        <div id="due2">—</div>
      </div>
    </div>

    <hr/>
    <div class="actions">
      <button id="prev">Prev (P)</button>
      <button class="openbtn" id="open">Open (O)</button>
      <button class="primary" id="win">Win (W)</button>
      <button class="danger" id="loss">Loss (L)</button>
      <button id="skip">Skip (N)</button>
    </div>
    <div class="small" style="margin-top:6px;">Keyboard: O open • W win • L loss • N skip • P prev</div>
  </div>
</div>

<script>
// [H-4] Client data boot
const DATA = __PAYLOAD__; // { items: [...], themes: [{key,label}], themeLabels: {key:label} }
let items = DATA.items || [];
const allThemes = DATA.themes || [];
const THEME_LABELS = DATA.themeLabels || {};

// [H-5] Local state & today stats
const $ = (id) => document.getElementById(id);
let idx = parseInt(localStorage.getItem("queue_idx") || "0", 10);
if (isNaN(idx) || idx < 0) idx = 0;
if (idx >= items.length) idx = 0;

function dayKey() {
  const d = new Date();
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,'0')+"-"+String(d.getDate()).padStart(2,'0');
}
let statsDay = localStorage.getItem("queue_stats_daykey") || dayKey();
let stats = JSON.parse(localStorage.getItem("queue_stats_today") || '{}');
if (!stats || typeof stats !== "object") stats = {win:0, loss:0};
if (typeof stats.win !== "number") stats.win = 0;
if (typeof stats.loss !== "number") stats.loss = 0;

function ensureToday() {
  const k = dayKey();
  if (k !== statsDay) { stats = {win:0, loss:0}; statsDay = k; saveStats(); }
}
function saveStats() {
  localStorage.setItem("queue_stats_today", JSON.stringify(stats));
  localStorage.setItem("queue_stats_daykey", statsDay);
}
function bump(result) {
  ensureToday();
  if (result === "win") stats.win++; else if (result === "loss") stats.loss++;
  saveStats(); render();
}

// [H-6] Theme filter (official labels)
const themeSel = $("themeSel");
(function fillThemes() {
  for (const opt of allThemes) {
    const el = document.createElement("option");
    el.value = opt.key;         // filter by KEY
    el.textContent = opt.label; // show LABEL
    themeSel.appendChild(el);
  }
})();

function filterByTheme(themeKey) {
  if (!themeKey) return;
  items = DATA.items.filter(x => (x.themes || "").split(/[ ,]+/).includes(themeKey));
  idx = 0;
  localStorage.setItem("queue_idx", "0");
  render();
}
themeSel.addEventListener("change", (e) => {
  const key = e.target.value || "";
  if (!key) {
    items = DATA.items.slice();
    idx = 0; localStorage.setItem("queue_idx", "0");
    render();
  } else {
    filterByTheme(key);
  }
});

// [H-7] Render & actions
const pidEl = $("pid"), themesEl = $("themes"), attemptsEl = $("attempts"),
      lastEl = $("last"), dueEl = $("due"), due2El = $("due2"),
      metaEl = $("meta"), msgEl = $("msg"), todayEl = $("today");

function pad(n) { return String(n).padStart(2,'0'); }
function fmt(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+" "+
           pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());
  } catch(e) { return ts; }
}
function firstThemes(csv) {
  if (!csv) return "";
  return csv
    .split(/[ ,]+/)
    .filter(Boolean)
    .map(k => THEME_LABELS[k] || k)
    .slice(0, 4)
    .join(", ");
}
function url(pid) { return "https://lichess.org/training/" + pid; }

function render() {
  ensureToday();
  todayEl.textContent = "Today: " + (stats.win||0) + " win / " + (stats.loss||0) + " loss";
  metaEl.textContent = items.length ? ("Showing " + (idx+1) + " of " + items.length) : "Showing 0 of 0";

  if (!items.length) {
    pidEl.textContent = "—"; themesEl.textContent = "—"; attemptsEl.textContent = "0";
    lastEl.textContent = "—"; dueEl.textContent = "—"; due2El.textContent = "—";
    msgEl.className = "kv notice-ok"; msgEl.textContent = "🎉 All done!";
    return;
  }
  const x = items[idx];
  pidEl.textContent = x.puzzle_id;
  themesEl.textContent = firstThemes(x.themes);
  attemptsEl.textContent = String(x.attempts||0);
  lastEl.textContent = fmt(x.last);
  dueEl.textContent = x.due || "—";
  due2El.textContent = x.due || "—";
  msgEl.className = "kv"; msgEl.textContent = "";
}

function step(delta) {
  if (!items.length) return;
  idx = Math.max(0, Math.min(items.length-1, idx + delta));
  localStorage.setItem("queue_idx", String(idx));
  render();
}

function removeCurrent() {
  if (!items.length) return;
  items.splice(idx, 1);
  if (idx >= items.length) idx = Math.max(0, items.length-1);
  localStorage.setItem("queue_idx", String(idx));
}

function openCurrent() {
  if (!items.length) return;
  window.open(url(items[idx].puzzle_id), "_blank", "noopener");
}

function log(result) {
  if (!items.length) return;
  const id = items[idx].puzzle_id;
  const u = "http://127.0.0.1:__PORT__/log?puzzle_id="+encodeURIComponent(id)+"&result="+encodeURIComponent(result);
  fetch(u).then(r => r.json()).then(j => {
    if (j && j.ok) {
      msgEl.className = "kv notice-ok";
      msgEl.textContent = "Logged " + result + " ✓";
      bump(result);
      removeCurrent();
      render();
    } else {
      msgEl.className = "kv notice-err";
      msgEl.textContent = "Log failed: " + (j && j.error ? j.error : "unknown");
    }
  }).catch(() => {
    msgEl.className = "kv notice-err";
    msgEl.textContent = "Server not running?";
  });
}

// Buttons + keys
$("open").onclick = openCurrent;
$("win").onclick  = () => log("win");
$("loss").onclick = () => log("loss");
$("skip").onclick = () => { step(+1); };
$("prev").onclick = () => { step(-1); };
$("resetIdx").onclick = () => { idx = 0; localStorage.setItem("queue_idx","0"); render(); };
$("resetStats").onclick = () => {
  stats = {win:0, loss:0}; statsDay = dayKey(); saveStats(); render();
};

// [H-8] Keyboard bindings
document.addEventListener("keydown", (e) => {
  const k = (e.key||"").toLowerCase();
  if (k === "o") openCurrent();
  else if (k === "w") log("win");
  else if (k === "l") log("loss");
  else if (k === "n") step(+1);
  else if (k === "p") step(-1);
});

render();
</script>
</body></html>
"""
        return (
            html
            .replace("__INCLUDE__", "yes" if INCLUDE_OVERDUE else "no")
            .replace("__PORT__", str(PORT))
            .replace("__PAYLOAD__", payload)
        )
    except Exception as e:
        print("[/queue] ERROR:\n" + "".join(traceback.format_exception(e)))
        safe = (
            "<!doctype html><meta charset='utf-8'>"
            "<style>body{font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial}</style>"
            "<h1>Queue error</h1>"
            "<p>See the terminal for details.</p>"
        )
        return safe

# ─────────────────────────────── [9] HTML builder ──────────────────────────
def _queue_html():
    try:
        # Build items from DB
        raw = _due_rows(limit=2000)
        items = [{
            "puzzle_id": pid,
            "themes": (themes or ""),
            "attempts": int(attempts or 0),
            "last": last or "",
            "due": due or ""
        } for (pid, themes, due, attempts, last) in raw]

        # ───────── Official Lichess themes → nice labels (for dropdown + per-card) ─────────
        THEME_MAP = {
            # Phases
            "opening": "Opening",
            "middlegame": "Middlegame",
            "endgame": "Endgame",
            "rookEndgame": "Rook endgame",
            "bishopEndgame": "Bishop endgame",
            "pawnEndgame": "Pawn endgame",
            "knightEndgame": "Knight endgame",
            "queenEndgame": "Queen endgame",
            "queenRookEndgame": "Queen and Rook",

            # Motifs
            "advancedPawn": "Advanced pawn",
            "attackingF2F7": "Attacking f2 or f7",
            "captureDefender": "Capture the defender",
            "discoveredAttack": "Discovered attack",
            "doubleCheck": "Double check",
            "exposedKing": "Exposed king",
            "fork": "Fork",
            "hangingPiece": "Hanging piece",
            "kingsideAttack": "Kingside attack",
            "queensideAttack": "Queenside attack",
            "pin": "Pin",
            "sacrifice": "Sacrifice",
            "skewer": "Skewer",
            "trappedPiece": "Trapped piece",

            # Advanced
            "attraction": "Attraction",
            "clearance": "Clearance",
            "defensiveMove": "Defensive move",
            "deflection": "Deflection",
            "interference": "Interference",
            "intermezzo": "Intermezzo",
            "quietMove": "Quiet move",
            "xRayAttack": "X-Ray attack",
            "zugzwang": "Zugzwang",

            # Mates
            "checkmate": "Checkmate",
            "mateIn1": "Mate in 1",
            "mateIn2": "Mate in 2",
            "mateIn3": "Mate in 3",
            "mateIn4": "Mate in 4",
            "mateIn5": "Mate in 5 or more",
            "arabianMate": "Arabian mate",
            "anastasiaMate": "Anastasia's mate",
            "backRankMate": "Back rank mate",
            "bodenMate": "Boden's mate",
            "doubleBishopMate": "Double bishop mate",
            "dovetailMate": "Dovetail mate",
            "hookMate": "Hook mate",
            "killBoxMate": "Kill box mate",
            "smotheredMate": "Smothered mate",
            "vukovicMate": "Vukovic mate",

            # Special moves
            "castling": "Castling",
            "enPassant": "En passant",
            "promotion": "Promotion",
            "underPromotion": "Underpromotion",

            # Goals
            "equality": "Equality",
            "advantage": "Advantage",
            "crushing": "Crushing",

            # Lengths
            "oneMove": "One-move puzzle",
            "short": "Short puzzle",
            "long": "Long puzzle",
            "veryLong": "Very long puzzle",

            # Origin
            "master": "Master games",
            "superGM": "Super GM games",
            "playerGames": "Player games",
        }

        # Collect only the official theme KEYS present in your data (prevents empty options)
        present_keys = set()
        for it in items:
            for t in (it["themes"] or "").replace(",", " ").split():
                if t in THEME_MAP:
                    present_keys.add(t)

        # Dropdown options: [{key, label}], sorted by label
        theme_options = sorted(
            ({"key": k, "label": THEME_MAP[k]} for k in present_keys),
            key=lambda d: d["label"].lower()
        )

        # Include THEME_MAP so client can render nice names inside the card
        payload = json.dumps(
            {"items": items, "themes": theme_options, "themeLabels": THEME_MAP},
            ensure_ascii=False
        )

        # IMPORTANT: raw string with placeholders; we .replace(...) at the end (avoid JS ${} issues)
        html = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Queue — Lichess Puzzles</title>

<!-- [H-1] Styles -->
<style>
:root {
  --bg: #f6f7fb; --fg: #0f172a; --muted:#64748b; --border:#e2e8f0; --card:#ffffff;
  --row:#f8fafc; --accent:#2563eb; --danger:#ef4444; --ok:#16a34a; --open:#22c55e;
}
* { box-sizing:border-box; }
body {
  margin:0; color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,"Noto Sans";
  background: linear-gradient(180deg, #eef2ff 0%, #f6f7fb 30%, #ffffff 100%);
}
.wrap { max-width:860px; margin:0 auto; padding:24px; }
h1 { margin:0 0 10px; font-size:24px; letter-spacing:.2px; }
.sub { color:var(--muted); font-size:12px; }

.toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:14px 0 18px; }
.toolbar .stat { background:#ffffffaa; border:1px solid var(--border); border-radius:999px; padding:6px 10px; }

select, button {
  padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:#fff;
  cursor:pointer; transition:filter .12s ease, transform .12s ease;
}
button:hover { filter:brightness(1.05); transform: translateY(-1px); }

button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
button.primary:hover { background:var(--accent); filter:brightness(1.1); }

button.danger  { background:var(--danger); color:#fff; border-color:var(--danger); }
button.danger:hover  { background:var(--danger); filter:brightness(1.1); }

button.openbtn { background:var(--open); color:#fff; border-color:var(--open); }
button.openbtn:hover { background:var(--open); filter:brightness(1.1); }

button.ghost   { background:#f1f5f9; color:var(--fg); border-color:var(--border); }
button.ghost:hover { background:#e2e8f0; }

.card {
  border:1px solid var(--border); background:var(--card); border-radius:16px; padding:18px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .06);
}
.topline { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.badge { display:inline-block; background:#eef2ff; padding:2px 8px; border-radius:999px; font-size:12px; }
.kv { color:var(--muted); font-size:12px; }

.meta { display:grid; grid-template-columns: 1fr 1fr; gap:8px; margin:10px 0 2px; }
.meta .box { background:var(--row); border:1px solid var(--border); border-radius:10px; padding:8px 10px; min-height:40px; }
.meta .label { font-size:11px; color:var(--muted); margin-bottom:2px; }

.actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
.notice-ok { color:var(--ok); font-weight:600; }
.notice-err { color:var(--danger); font-weight:600; }
hr { border:none; border-top:1px solid var(--border); margin:12px 0; }
.small { font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <h1>Due Queue</h1>
  <div class="sub">Includes overdue: <strong>__INCLUDE__</strong></div>

  <!-- [H-2] Toolbar -->
  <div class="toolbar">
    <label class="kv">Theme
      <select id="themeSel">
        <option value="">All themes</option>
      </select>
    </label>
    <button class="ghost" id="resetIdx">Reset position</button>
    <button class="ghost" id="resetStats">Reset today</button>
    <span class="stat" id="today">Today: 0 win / 0 loss</span>
    <span class="kv" id="meta"></span>
  </div>

  <!-- [H-3] Card (single puzzle view) -->
  <div class="card">
    <div class="topline">
      <div>
        <strong id="pid">—</strong>
        <span class="badge" id="due">—</span>
      </div>
      <div class="kv" id="msg"></div>
    </div>

    <div class="meta">
      <div class="box">
        <div class="label">Themes</div>
        <div id="themes">—</div>
      </div>
      <div class="box">
        <div class="label">Attempts</div>
        <div><span id="attempts">0</span></div>
      </div>
      <div class="box">
        <div class="label">Last Attempt</div>
        <div id="last">—</div>
      </div>
      <div class="box">
        <div class="label">Next Due</div>
        <div id="due2">—</div>
      </div>
    </div>

    <hr/>
    <div class="actions">
      <button id="prev">Prev (P)</button>
      <button class="openbtn" id="open">Open (O)</button>
      <button class="primary" id="win">Win (W)</button>
      <button class="danger" id="loss">Loss (L)</button>
      <button id="skip">Skip (N)</button>
    </div>
    <div class="small" style="margin-top:6px;">Keyboard: O open • W win • L loss • N skip • P prev</div>
  </div>
</div>

<script>
// [H-4] Client data boot
const DATA = __PAYLOAD__; // { items: [...], themes: [{key,label}], themeLabels: {key:label} }
let items = DATA.items || [];
const allThemes = DATA.themes || [];
const THEME_LABELS = DATA.themeLabels || {};

// [H-5] Local state & today stats
const $ = (id) => document.getElementById(id);
let idx = parseInt(localStorage.getItem("queue_idx") || "0", 10);
if (isNaN(idx) || idx < 0) idx = 0;
if (idx >= items.length) idx = 0;

function dayKey() {
  const d = new Date();
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,'0')+"-"+String(d.getDate()).padStart(2,'0');
}
let statsDay = localStorage.getItem("queue_stats_daykey") || dayKey();
let stats = JSON.parse(localStorage.getItem("queue_stats_today") || '{}');
if (!stats || typeof stats !== "object") stats = {win:0, loss:0};
if (typeof stats.win !== "number") stats.win = 0;
if (typeof stats.loss !== "number") stats.loss = 0;

function ensureToday() {
  const k = dayKey();
  if (k !== statsDay) { stats = {win:0, loss:0}; statsDay = k; saveStats(); }
}
function saveStats() {
  localStorage.setItem("queue_stats_today", JSON.stringify(stats));
  localStorage.setItem("queue_stats_daykey", statsDay);
}
function bump(result) {
  ensureToday();
  if (result === "win") stats.win++; else if (result === "loss") stats.loss++;
  saveStats(); render();
}

// [H-6] Theme filter (official labels)
const themeSel = $("themeSel");
(function fillThemes() {
  for (const opt of allThemes) {
    const el = document.createElement("option");
    el.value = opt.key;         // filter by KEY
    el.textContent = opt.label; // show LABEL
    themeSel.appendChild(el);
  }
})();

function filterByTheme(themeKey) {
  if (!themeKey) return;
  items = DATA.items.filter(x => (x.themes || "").split(/[ ,]+/).includes(themeKey));
  idx = 0;
  localStorage.setItem("queue_idx", "0");
  render();
}

themeSel.addEventListener("change", (e) => {
  const key = e.target.value || "";
  if (!key) {
    items = DATA.items.slice();
    idx = 0; localStorage.setItem("queue_idx", "0");
    render();
  } else {
    filterByTheme(key);
  }
});

// [H-7] Render & actions
const pidEl = $("pid"), themesEl = $("themes"), attemptsEl = $("attempts"),
      lastEl = $("last"), dueEl = $("due"), due2El = $("due2"),
      metaEl = $("meta"), msgEl = $("msg"), todayEl = $("today");

function pad(n) { return String(n).padStart(2,'0'); }
function fmt(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+" "+
           pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());
  } catch(e) { return ts; }
}
// Show first 4 themes, using official labels
function firstThemes(csv) {
  if (!csv) return "";
  return csv
    .split(/[ ,]+/)
    .filter(Boolean)
    .map(k => THEME_LABELS[k] || k)
    .slice(0, 4)
    .join(", ");
}
function url(pid) { return "https://lichess.org/training/" + pid; }

function render() {
  ensureToday();
  todayEl.textContent = "Today: " + (stats.win||0) + " win / " + (stats.loss||0) + " loss";
  metaEl.textContent = items.length ? ("Showing " + (idx+1) + " of " + items.length) : "Showing 0 of 0";

  if (!items.length) {
    pidEl.textContent = "—"; themesEl.textContent = "—"; attemptsEl.textContent = "0";
    lastEl.textContent = "—"; dueEl.textContent = "—"; due2El.textContent = "—";
    msgEl.className = "kv notice-ok"; msgEl.textContent = "🎉 All done!";
    return;
  }
  const x = items[idx];
  pidEl.textContent = x.puzzle_id;
  themesEl.textContent = firstThemes(x.themes);
  attemptsEl.textContent = String(x.attempts||0);
  lastEl.textContent = fmt(x.last);
  dueEl.textContent = x.due || "—";
  due2El.textContent = x.due || "—";
  msgEl.className = "kv"; msgEl.textContent = "";
}

function step(delta) {
  if (!items.length) return;
  idx = Math.max(0, Math.min(items.length-1, idx + delta));
  localStorage.setItem("queue_idx", String(idx));
  render();
}

function removeCurrent() {
  if (!items.length) return;
  items.splice(idx, 1);
  if (idx >= items.length) idx = Math.max(0, items.length-1);
  localStorage.setItem("queue_idx", String(idx));
}

function openCurrent() {
  if (!items.length) return;
  window.open(url(items[idx].puzzle_id), "_blank", "noopener");
}

function log(result) {
  if (!items.length) return;
  const id = items[idx].puzzle_id;
  const u = "http://127.0.0.1:__PORT__/log?puzzle_id="+encodeURIComponent(id)+"&result="+encodeURIComponent(result);
  fetch(u).then(r => r.json()).then(j => {
    if (j && j.ok) {
      msgEl.className = "kv notice-ok";
      msgEl.textContent = "Logged " + result + " ✓";
      bump(result);
      removeCurrent();
      render();
    } else {
      msgEl.className = "kv notice-err";
      msgEl.textContent = "Log failed: " + (j && j.error ? j.error : "unknown");
    }
  }).catch(() => {
    msgEl.className = "kv notice-err";
    msgEl.textContent = "Server not running?";
  });
}

// Buttons + keys
$("open").onclick = openCurrent;
$("win").onclick  = () => log("win");
$("loss").onclick = () => log("loss");
$("skip").onclick = () => { step(+1); };
$("prev").onclick = () => { step(-1); };
$("resetIdx").onclick = () => { idx = 0; localStorage.setItem("queue_idx","0"); render(); };
$("resetStats").onclick = () => {
  stats = {win:0, loss:0}; statsDay = dayKey(); saveStats(); render();
};

// [H-8] Keyboard bindings
document.addEventListener("keydown", (e) => {
  const k = (e.key||"").toLowerCase();
  if (k === "o") openCurrent();
  else if (k === "w") log("win");
  else if (k === "l") log("loss");
  else if (k === "n") step(+1);
  else if (k === "p") step(-1);
});

render();
</script>
</body></html>
"""
        return (
            html
            .replace("__INCLUDE__", "yes" if INCLUDE_OVERDUE else "no")
            .replace("__PORT__", str(PORT))
            .replace("__PAYLOAD__", payload)
        )
    except Exception as e:
        # Print full traceback to your terminal, and render a simple error page
        print("[/queue] ERROR:\n" + "".join(traceback.format_exception(e)))
        safe = (
            "<!doctype html><meta charset='utf-8'>"
            "<style>body{font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial}</style>"
            "<h1>Queue error</h1>"
            "<p>See the terminal for details.</p>"
        )
        return safe

# ───────────────────────── [10] HTTP handler ───────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)

            # Redirect root to /queue (so opening http://127.0.0.1:PORT goes to the UI)
            if parsed.path in ("/", ""):
                self.send_response(302)
                self.send_header("Location", "/queue")
                self.end_headers()
                return

            # Ignore favicon requests (prevents noisy 404s)
            if parsed.path == "/favicon.ico":
                self.send_response(204)  # No Content
                self.end_headers()
                return

            if parsed.path == "/health":
                return _json(self, 200, {"ok": True, "time": datetime.now().astimezone().isoformat()})

            if parsed.path == "/queue":
                html = _queue_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if parsed.path == "/log":
                qs = parse_qs(parsed.query)
                pid = (qs.get("puzzle_id") or qs.get("p") or [""])[0].strip()
                result = (qs.get("result") or qs.get("r") or [""])[0].strip().lower()
                if not pid or result not in ("win", "loss"):
                    return _json(self, 400, {"ok": False, "error": "need puzzle_id and result=win|loss"})

                # Write attempt directly to SQLite
                db_path = _resolve_db_path() if "_resolve_db_path" in globals() else get_db_path()
                with sqlite3.connect(db_path) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    _ensure_user(conn)
                    _insert_attempt(conn, pid, result)
                    conn.commit()

                _recompute_srs_single(pid)
                return _json(self, 200, {"ok": True, "puzzle_id": pid, "result": result})

            # Not found
            return _json(self, 404, {"ok": False, "error": "not found"})

        except Exception as e:
            print("[request] ERROR:\n" + "".join(traceback.format_exception(e)))
            return _json(self, 500, {"ok": False, "error": repr(e)})

# ───────────────────────────── [11] Entrypoint ─────────────────────────────
def main():
    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        # macOS: errno 48 means "Address already in use"
        if getattr(e, "errno", None) == 48:
            print(f"[serve] Port {PORT} is busy. Stop the other server or use another port:")
            print(f"       lsof -iTCP:{PORT} -sTCP:LISTEN -n -P")
            print(f"       kill -9 <PID>")
            print(f"       or: LOCAL_LOG_PORT={PORT+1} PYTHONPATH=src python -m src.serve")
            raise
        else:
            raise

    print(f"[serve] listening on http://127.0.0.1:{PORT}")
    print("[serve] pages: /queue   •   /log?puzzle_id=XXXX&result=win|loss   •   /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] bye")

if __name__ == "__main__":
    main()
