# report.py
import os
from datetime import date, datetime, timezone
from pathlib import Path
from collections import Counter
from sqlalchemy import text

# ------------ CONFIG ------------
THEME_COUNT = 4  # show up to N themes per row in tables
QUEUE_PERSIST = os.getenv("REPORT_QUEUE_PERSIST", "true").lower() in ("1","true","yes","on")
INCLUDE_OVERDUE = os.getenv("INCLUDE_OVERDUE", "false").lower() in ("1","true","yes","on")
PAGE_SIZE = int(os.getenv("REPORT_PAGE_SIZE", "10"))  # default rows per page

# ------------ SQL ------------
KPI = text("""
SELECT COUNT(*) AS attempts,
       ROUND(100.0*AVG(CASE WHEN result='win' THEN 1.0 ELSE 0.0 END),1) AS acc
FROM attempts WHERE user_id=1
""")
KPI7  = text("""SELECT ROUND(100.0*AVG(CASE WHEN result='win' THEN 1.0 ELSE 0.0 END),1) AS acc7
                FROM attempts WHERE user_id=1 AND attempted_at >= datetime('now','-7 day')""")
KPI30 = text("""SELECT ROUND(100.0*AVG(CASE WHEN result='win' THEN 1.0 ELSE 0.0 END),1) AS acc30
                FROM attempts WHERE user_id=1 AND attempted_at >= datetime('now','-30 day')""")

MISSED_30 = text("""
SELECT a.puzzle_id, p.themes, a.attempted_at,
       (SELECT COUNT(*) FROM attempts a2 WHERE a2.user_id=1 AND a2.puzzle_id=a.puzzle_id) AS total_attempts
FROM attempts a JOIN puzzles p ON p.puzzle_id=a.puzzle_id
WHERE a.user_id=1 AND a.result='loss' AND a.attempted_at >= datetime('now','-30 day')
ORDER BY a.attempted_at DESC LIMIT 1000
""")

THEME_ROWS_90 = text("""
SELECT p.themes AS themes_csv, a.result AS result
FROM attempts a JOIN puzzles p ON p.puzzle_id=a.puzzle_id
WHERE a.user_id=1 AND a.attempted_at >= datetime('now','-90 day')
""")
THEME_ROWS_ALL = text("""
SELECT p.themes AS themes_csv, a.result AS result
FROM attempts a JOIN puzzles p ON p.puzzle_id=a.puzzle_id
WHERE a.user_id=1
""")

RECENT_ATTEMPTS = text("""
SELECT a.puzzle_id, a.result, a.attempted_at, p.themes
FROM attempts a JOIN puzzles p ON p.puzzle_id=a.puzzle_id
WHERE a.user_id=1
ORDER BY a.attempted_at DESC
LIMIT 100
""")

# SRS queues — local-time aware
DUE_TPL = """
SELECT s.puzzle_id, p.themes, s.due_date,
       COUNT(a.id) AS attempts, MAX(a.attempted_at) AS last_attempt
FROM srs s
JOIN puzzles p ON p.puzzle_id = s.puzzle_id
LEFT JOIN attempts a ON a.user_id=1 AND a.puzzle_id=s.puzzle_id
WHERE s.user_id=1 AND {where}
GROUP BY s.puzzle_id, p.themes, s.due_date
ORDER BY s.due_date, s.puzzle_id
LIMIT 2000
"""
DUE_ONLY_TODAY = text(DUE_TPL.format(where="s.due_date = date('now','localtime')"))
DUE_OVERDUE    = text(DUE_TPL.format(where="s.due_date < date('now','localtime')"))
DUE_PLUS1      = text(DUE_TPL.format(where="s.due_date = date('now','localtime','+1 day')"))
DUE_2_7        = text(DUE_TPL.format(where="s.due_date BETWEEN date('now','localtime','+2 day') AND date('now','localtime','+7 day')"))
DUE_LATER      = text(DUE_TPL.format(where="s.due_date > date('now','localtime','+7 day')"))

# ------------ helpers ------------
LABELS = {
    "rookEndgame":"Rook Endgame","endgame":"Endgame","middlegame":"Middlegame","opening":"Opening",
    "mateIn1":"Mate in 1","mateIn2":"Mate in 2","mateIn3":"Mate in 3","smotheredMate":"Smothered Mate",
    "fork":"Fork","pin":"Pin","skewer":"Skewer","clearance":"Clearance","deflection":"Deflection",
    "queensideAttack":"Queenside Attack","kingsideAttack":"Kingside Attack",
    "short":"Short Tactic","long":"Long Tactic","oneMove":"One Move","crushing":"Crushing",
    "advantage":"Advantage","master":"Master","masterVsMaster":"Master vs Master",
    "defensiveMove":"Defensive Move","queenRookEndgame":"Queen+Rook Endgame","hangingPiece":"Hanging Piece",
    "backRankMate":"Back-Rank Mate","exposedKing":"Exposed King"
}
def _label(t: str) -> str: return LABELS.get(t, t)

def _local_tz(): return datetime.now().astimezone().tzinfo

def _fmt_ts(iso_ts: str) -> str:
    if not iso_ts: return "—"
    dt = datetime.fromisoformat(iso_ts.replace("Z","+00:00"))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")

def _ago(iso_ts: str) -> str:
    if not iso_ts: return ""
    dt = datetime.fromisoformat(iso_ts.replace("Z","+00:00"))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now().astimezone() - dt.astimezone(_local_tz())
    secs = int(delta.total_seconds())
    d, h, m = secs//86400, (secs%86400)//3600, (secs%3600)//60
    return f"{d}d ago" if d else (f"{h}h ago" if h else f"{m}m ago")

def _first_themes(themes_csv: str, n=THEME_COUNT) -> str:
    if not themes_csv: return ""
    parts = [p.strip() for p in themes_csv.replace(",", " ").split() if p.strip()]
    return ", ".join(_label(p) for p in parts[:n]) if parts else ""

def _acc(attempts, wins): return round(100.0 * wins / attempts, 1) if attempts else None

# ---------- HTML table renderers ----------
def _html_table(headers, rows, col_specs, table_id=None, extra_classes="", page_size=PAGE_SIZE):
    colgroup = "".join(f'<col style="width:{w}">' for w in col_specs)
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead>"
    body_rows = []
    for r in rows:
        tds = [f"<td>{'' if v is None else v}</td>" for v in r]
        body_rows.append("<tr>" + "".join(tds) + "</tr>")
    tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
    tid = f' id="{table_id}"' if table_id else ""
    classes = (extra_classes or "")
    ds  = f' data-page-size="{page_size}"' if "paged" in classes else ""
    cls = f' class="{classes}"' if classes else ""
    return f'<table{tid}{cls}{ds}><colgroup>{colgroup}</colgroup>{thead}{tbody}</table><div class="pager"></div>'

def _html_due(rows, table_id=None):
    headers = ["Puzzle","Theme","Attempts","Last Attempt","Due"]
    # widen Last Attempt to keep single line
    col_specs = ["6ch","auto","5ch","28ch","12ch"]
    if not rows:
        return _html_table(headers, [["—","—","0","—","—"]], col_specs, table_id, "filterable paged")
    out = []
    for pid, themes, due, attempts, last_attempt in rows:
        last = f"{_fmt_ts(last_attempt)} ({_ago(last_attempt)})" if last_attempt else "—"
        out.append([
            f'<a href="https://lichess.org/training/{pid}" target="_blank" rel="noopener">{pid}</a>',
            _first_themes(themes),
            str(attempts or 0),
            last,
            due
        ])
    return _html_table(headers, out, col_specs, table_id, "filterable paged")

def _html_missed(rows, table_id=None):
    headers = ["Puzzle","Theme","Attempts","When"]
    # widen When to keep single line
    col_specs = ["6ch","auto","5ch","28ch"]
    if not rows:
        return _html_table(headers, [["—","—","0","—"]], col_specs, table_id, "filterable paged")
    out = []
    for pid, themes, ts, tot in rows:
        out.append([
            f'<a href="https://lichess.org/training/{pid}" target="_blank" rel="noopener">{pid}</a>',
            _first_themes(themes),
            str(tot),
            f"{_fmt_ts(ts)} ({_ago(ts)})"
        ])
    return _html_table(headers, out, col_specs, table_id, "filterable paged")

def _html_themes(agg, title, table_id=None):
    headers = ["Theme","Attempts","Accuracy"]
    col_specs = ["auto","6ch","8ch"]
    rows = []
    for theme, (n,w) in sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0])):
        acc = _acc(n,w)
        rows.append([_label(theme), str(n), f"{acc:.1f}%" if acc is not None else "—"])
    if not rows: rows = [["_none yet_", "0", "—"]]
    caption = f"<h2>{title}</h2>"
    return caption + _html_table(headers, rows, col_specs, table_id, "filterable paged")

# ------------ main ------------
def run(engine, outdir: str = "reports"):
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)

    with engine.begin() as conn:
        kpi   = conn.execute(KPI).mappings().one()
        k7    = conn.execute(KPI7).mappings().one()
        k30   = conn.execute(KPI30).mappings().one()
        missed = conn.execute(MISSED_30).all()
        due_today = conn.execute(DUE_ONLY_TODAY).all()
        due_over  = conn.execute(DUE_OVERDUE).all() if INCLUDE_OVERDUE else []
        due_p1    = conn.execute(DUE_PLUS1).all()
        due_p7    = conn.execute(DUE_2_7).all()
        due_later = conn.execute(DUE_LATER).all()
        rows90    = conn.execute(THEME_ROWS_90).all()
        rows_all  = conn.execute(THEME_ROWS_ALL).all()
        recent    = conn.execute(RECENT_ATTEMPTS).all()

    # theme aggregates
    attempts90 = Counter(); wins90 = Counter()
    for r in rows90:
        for t in (r.themes_csv or "").replace(",", " ").split():
            attempts90[t] += 1
            if r.result == "win": wins90[t] += 1
    agg90 = {t:(attempts90[t], wins90[t]) for t in attempts90}

    attempts_all = Counter(); wins_all = Counter()
    for r in rows_all:
        for t in (r.themes_csv or "").replace(",", " ").split():
            attempts_all[t] += 1
            if r.result == "win": wins_all[t] += 1
    agg_all = {t:(attempts_all[t], wins_all[t]) for t in attempts_all}

    # ---------- DAILY HTML ----------
    if (os.getenv("REPORT_HTML","false").lower() in ("1","true","yes","on")):
        daily_head = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lichess Puzzle Report — {date.today().isoformat()}</title>
<style>
:root {{ --bg:#fff; --fg:#0f172a; --muted:#64748b; --border:#e2e8f0; --row:#f8fafc; --accent:#2563eb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,"Noto Sans"; }}
.layout {{ display:grid; grid-template-columns: 220px 1fr; min-height:100vh; }}
aside {{ position:sticky; top:0; height:100vh; border-right:1px solid var(--border); padding:16px; }}
aside h2 {{ margin:0 0 8px; font-size:14px; color:var(--muted); }}
nav a {{ display:block; padding:6px 8px; border-radius:6px; text-decoration:none; color:inherit; }}
nav a:hover {{ background:#f1f5f9; }}
main {{ padding:24px; max-width:1200px; margin:0 auto; }}
h1 {{ font-size:28px; margin:0 0 12px; }}
h2 {{ font-size:20px; margin:22px 0 8px; }}
.kpis{{display:grid; grid-template-columns:repeat(4, minmax(220px,1fr)); gap:12px; margin:16px 0;}}
.kpi{{border:1px solid var(--border); border-radius:10px; padding:12px;}}
.kpi .label{{color:var(--muted); font-size:12px;}} .kpi .value{{font-size:22px; font-weight:600;}}
.controls{{display:flex; gap:8px; align-items:center; margin:8px 0 16px; flex-wrap:wrap;}}
.controls input[type="text"]{{padding:6px 8px; border:1px solid var(--border); border-radius:6px;}}
.controls button, .controls select, .controls label{{padding:6px 8px; border:1px solid var(--border); border-radius:6px; background:#fff; cursor:pointer;}}
.controls button:hover{{background:#f8fafc;}}
table{{ width:100%; border-collapse:collapse; margin:8px 0 16px; table-layout:auto; }}
th,td{{ border:1px solid var(--border); padding:8px 10px; vertical-align:top; }}
thead th{{ background:#f1f5f9; position:sticky; top:0; z-index:1; }}
tbody tr:nth-child(even){{ background:var(--row); }}
a{{ color:var(--accent); text-decoration:none; }} a:hover{{ text-decoration:underline; }}
.small{{font-size:12px; color:var(--muted);}}
td,th{{ font-variant-numeric: tabular-nums; }}
.pager{{display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:-6px 0 12px;}}
.pager button{{padding:4px 8px; border:1px solid var(--border); border-radius:6px; background:#fff; cursor:pointer;}}
.pager button.active{{background:#e2e8f0;}}
.pager button:disabled{{opacity:.5; cursor:not-allowed;}}

/* center numeric Attempts, keep Theme left/wrapped */
#due-today table th:nth-child(3),  #due-today table td:nth-child(3),
#due-plus1 table th:nth-child(3),  #due-plus1 table td:nth-child(3),
#due-p7    table th:nth-child(3),  #due-p7    table td:nth-child(3),
#due-later table th:nth-child(3),  #due-later table td:nth-child(3),
#missed    table th:nth-child(3),  #missed    table td:nth-child(3) {{ text-align:center; }}
#due-today table td:nth-child(2),
#due-plus1 table td:nth-child(2),
#due-p7    table td:nth-child(2),
#due-later table td:nth-child(2),
#missed    table td:nth-child(2) {{ text-align:left; white-space:normal; overflow-wrap:anywhere; }}
</style>
</head>
<body>
<div class="layout">
  <aside>
    <h2>Navigation</h2>
    <nav>
      <a href="index.html">Overall Tracker →</a>
      <a href="#kpis">Stats</a>
      <a href="#due-today">Due Today</a>
      {"<a href=\"#overdue\">Overdue</a>" if (INCLUDE_OVERDUE and __import__('builtins').len(due_over)>0) else ""}
      <a href="#themes-top">Top Themes (90d)</a>
      <a href="#themes-struggle">Struggle Themes (90d)</a>
      <a href="#missed">Missed (30d)</a>
      <a href="#due-plus1">+1 Day</a>
      <a href="#due-p7">+2–7 Days</a>
      <a href="#due-later">Later</a>
    </nav>
  </aside>
  <main>
    <section id="kpis">
      <h1>Lichess Puzzle Report — {date.today().isoformat()}</h1>
      <div class="kpis">
        <div class="kpi"><div class="label">All-time attempts</div><div class="value">{kpi['attempts']}</div></div>
        <div class="kpi"><div class="label">All-time accuracy</div><div class="value">{kpi['acc'] if kpi['acc'] is not None else 'n/a'}%</div></div>
        <div class="kpi"><div class="label">Last 7 days accuracy</div><div class="value">{k7['acc7'] if k7['acc7'] is not None else 'n/a'}%</div></div>
        <div class="kpi"><div class="label">Last 30 days accuracy</div><div class="value">{k30['acc30'] if k30['acc30'] is not None else 'n/a'}%</div></div>
      </div>
    </section>

    <section id="due-today">
      <h2>Due Today ({len(due_today)})</h2>
      <div class="controls" data-for="due-today">
        <input type="text" placeholder="Filter (theme or ID)"/>
        <label><input type="checkbox" id="shuffle-due"> Shuffle</label>
        <select id="batch-n"><option>5</option><option selected>10</option><option>20</option><option>50</option></select>
        <button data-open="first">Start</button>
        <button data-open="next">Next (from report)</button>
        <button data-open="batch">Open batch</button>
        <button data-reset>Reset</button>
      </div>
      {_html_due(due_today, table_id="due-today-table")}
    </section>

    {("<section id=\"overdue\"><h2>Overdue (" + str(len(due_over)) + ")</h2>" + _html_due(due_over, table_id="overdue-table") + "</section>") if (INCLUDE_OVERDUE and len(due_over)>0) else ""}

    <section id="themes-top">{_html_themes(agg90, "Top Themes (last 90 days)", table_id="themes-top-table")}</section>
    <section id="themes-struggle">{_html_themes({t:v for t,v in agg90.items() if v[0]>=8 and (_acc(*v) is None or _acc(*v) <= 60.0)}, "Struggle Themes (last 90 days)", table_id="themes-struggle-table")}</section>

    <section id="missed">
      <h2>Missed (last 30 days)</h2>
      <div class="controls" data-for="missed">
        <input type="text" placeholder="Filter (theme or ID)"/>
      </div>
      {_html_missed(missed, table_id="missed-table")}
    </section>

    <section id="due-plus1"><h2>+1 Day ({len(due_p1)})</h2>{_html_due(due_p1, table_id="plus1-table")}</section>
    <section id="due-p7"><h2>+2–7 Days ({len(due_p7)})</h2>{_html_due(due_p7, table_id="p7-table")}</section>
    <section id="due-later"><h2>Later ({len(due_later)})</h2>{_html_due(due_later, table_id="later-table")}</section>

    <div class="small">Generated {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}</div>
  </main>
</div>
"""
        # Daily JS — fixed pagination (separate filter vs paging)
        JS_TEMPLATE = r"""
<script>
(function(){
  const REPORT_DATE = "__REPORT_DATE__";
  const LS_KEY = "dueQueueState"; // { date, ids[], idx }
  const PERSIST_ACROSS_DAYS = __QUEUE_PERSIST__;

  function rowsOf(table){ return Array.from(table.querySelectorAll('tbody tr')); }
  function textOfRow(tr){ return tr.innerText.toLowerCase(); }

  // ---- filtering marks rows instead of removing from pagination set
  function applyFilter(table, query){
    const q = (query || "").trim().toLowerCase();
    rowsOf(table).forEach(tr => {
      const hide = q && !textOfRow(tr).includes(q);
      tr.dataset.filtered = hide ? "1" : "0";             // <-- mark filtered
      tr.style.display = hide ? "none" : "";              // for immediate UX
    });
    paginateTable(table); // re-page based on filtered set
  }

  // ---- pagination uses only non-filtered rows
  function paginateTable(table){
    const ps = parseInt(table.dataset.pageSize || "10", 10);
    const body = table.tBodies[0];
    const all = Array.from(body.rows);
    const eligible = all.filter(r => r.dataset.filtered !== "1");   // <-- key fix
    const pager = (table.nextElementSibling && table.nextElementSibling.classList.contains('pager'))
      ? table.nextElementSibling : null;
    if(!pager) return;

    // current page
    let cur = parseInt(table.dataset.pageIndex || "0", 10);
    const pages = Math.max(Math.ceil(eligible.length / ps), 1);
    if(cur >= pages) cur = pages - 1;
    table.dataset.pageIndex = String(cur);

    // show eligible rows on current page; hide the rest
    eligible.forEach((tr, i) => {
      const page = Math.floor(i / ps);
      tr.style.visibility = (page === cur) ? "" : "hidden";
      tr.style.display    = (page === cur) ? "" : "none";
    });
    // filtered rows remain hidden
    all.forEach(tr => {
      if(tr.dataset.filtered === "1"){
        tr.style.display = "none";
        tr.style.visibility = "hidden";
      }
    });

    // build pager
    pager.innerHTML = "";
    function btn(label, disabled, onClick, active=false){
      const b = document.createElement('button');
      b.type = "button";
      b.textContent = label;
      if(disabled) b.disabled = true;
      if(active) b.classList.add('active');
      b.addEventListener('click', onClick);
      pager.appendChild(b);
    }
    btn("«", cur===0, ()=>{ table.dataset.pageIndex="0"; paginateTable(table); });
    btn("‹", cur===0, ()=>{ table.dataset.pageIndex=String(cur-1); paginateTable(table); });

    const maxButtons = 7;
    const start = Math.max(0, cur - Math.floor(maxButtons/2));
    const end = Math.min(pages, start + maxButtons);
    for(let i=start;i<end;i++){
      btn(String(i+1), false, ()=>{ table.dataset.pageIndex=String(i); paginateTable(table); }, i===cur);
    }

    btn("›", cur>=pages-1, ()=>{ table.dataset.pageIndex=String(cur+1); paginateTable(table); });
    btn("»", cur>=pages-1, ()=>{ table.dataset.pageIndex=String(pages-1); paginateTable(table); });
  }

  // init filter + pagination for every paged/filterable table
  document.querySelectorAll('table.paged').forEach(t => {
    // mark all rows as not filtered initially
    rowsOf(t).forEach(tr => tr.dataset.filtered = tr.dataset.filtered || "0");
    paginateTable(t);
  });

  document.querySelectorAll('.controls').forEach(ctrl => {
    const section = ctrl.closest('section');
    const table = section.querySelector('table');
    const input = ctrl.querySelector('input[type="text"]');
    if(input && table){
      input.addEventListener('input', () => applyFilter(table, input.value));
    }
  });

  // ---- due-today queue controls ----
  const dueTable = document.getElementById('due-today-table');
  const shuffleEl = document.getElementById('shuffle-due');
  const batchSel  = document.getElementById('batch-n');

  function visibleIdsFrom(table){
    return rowsOf(table)
      .filter(tr => tr.dataset.filtered !== "1" && tr.style.display !== 'none')
      .map(tr => tr.querySelector('td a')?.textContent.trim())
      .filter(Boolean);
  }
  let queue = visibleIdsFrom(dueTable).map(id => 'https://lichess.org/training/' + id);
  let idx   = 0;

  function saveState(){
    try {
      const ids = queue.map(u => u.split('/').pop());
      localStorage.setItem(LS_KEY, JSON.stringify({ date: REPORT_DATE, ids, idx }));
    } catch(e) {}
  }
  function loadState(){
    try {
      const raw = localStorage.getItem(LS_KEY);
      if(!raw) return null;
      const obj = JSON.parse(raw);
      if(!obj) return null;
      if(!PERSIST_ACROSS_DAYS && obj.date !== REPORT_DATE) return null;
      return obj;
    } catch(e) { return null; }
  }
  function arraysEqual(a,b){ if(a.length!==b.length) return false; for(let i=0;i<a.length;i++) if(a[i]!==b[i]) return false; return true; }

  (function initQueue(){
    const idsNow = visibleIdsFrom(dueTable);
    const state = loadState();
    if(state && arraysEqual(state.ids, idsNow)){
      queue = state.ids.map(id => 'https://lichess.org/training/' + id);
      idx = Math.min(state.idx||0, queue.length?queue.length-1:0);
    } else {
      queue = idsNow.map(id => 'https://lichess.org/training/' + id);
      idx = 0;
      saveState();
    }
  })();

  function rebuildQueue(preserveCurrent=false){
    let ids = visibleIdsFrom(dueTable);
    if(shuffleEl && shuffleEl.checked){
      for(let i=ids.length-1;i>0;--i){ const j=Math.floor(Math.random()*(i+1)); [ids[i],ids[j]]=[ids[j],ids[i]]; }
    }
    const oldCurrent = queue[idx]?.split('/').pop() || null;
    queue = ids.map(id => 'https://lichess.org/training/' + id);
    if(preserveCurrent && oldCurrent){
      const newIdx = queue.findIndex(u => u.endsWith('/'+oldCurrent));
      idx = newIdx>=0 ? newIdx : 0;
    } else idx = 0;
    saveState();
  }

  function openUrl(u){ window.open(u, '_blank', 'noopener'); }
  function openWithHashQueue(){
    rebuildQueue(false);
    if(queue.length===0) return;
    const ids = queue.map(u => u.split('/').pop());
    const first = ids[0];
    const rest  = ids.slice(1).join('.');
    const url = 'https://lichess.org/training/' + first + (rest ? ('#q=' + rest) : '');
    idx = 0; saveState();
    openUrl(url);
  }
  function openNext(){ if(queue.length){ idx = Math.min(idx+1, queue.length-1); saveState(); openUrl(queue[idx]); } }
  function openBatch(n){
    if(queue.length===0) return;
    const start = idx;
    for(let k=0; k<n && (start+k)<queue.length; k++){ setTimeout(() => openUrl(queue[start+k]), 120*k); }
    idx = Math.min(start + n - 1, queue.length-1);
    saveState();
  }

  const ctrls = document.querySelector('#due-today .controls');
  if(ctrls){
    const filterEl = ctrls.querySelector('input[type="text"]');
    if(filterEl) filterEl.addEventListener('input', () => rebuildQueue(true));
    if(shuffleEl) shuffleEl.addEventListener('change', () => rebuildQueue(false));
    ctrls.querySelector('[data-open="first"]').addEventListener('click', openWithHashQueue);
    ctrls.querySelector('[data-open="next"]').addEventListener('click',  openNext);
    ctrls.querySelector('[data-open="batch"]').addEventListener('click', () => {
      const n = parseInt(batchSel.value, 10) || 10;
      openBatch(n);
    });
    ctrls.querySelector('[data-reset"]').addEventListener('click', () => {
      queue = visibleIdsFrom(dueTable).map(id => 'https://lichess.org/training/' + id); idx = 0; saveState();
    });
  }

  window.addEventListener('resize', () => {
    document.querySelectorAll('table.paged').forEach(t => paginateTable(t));
  });
})();
</script>
"""
        daily_js = JS_TEMPLATE.replace("__REPORT_DATE__", date.today().isoformat()) \
                              .replace("__QUEUE_PERSIST__", "true" if QUEUE_PERSIST else "false")

        daily_path = out / f"{date.today().isoformat()}.html"
        daily_path.write_text(daily_head + daily_js, encoding="utf-8")
        print(f"[report] Wrote {daily_path}")

        # ---------- TRACKER (index.html) ----------
        tracker_head = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lichess Puzzle Tracker</title>
<style>
:root {{ --bg:#fff; --fg:#0f172a; --muted:#64748b; --border:#e2e8f0; --row:#f8fafc; --accent:#2563eb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,"Noto Sans"; }}
.layout {{ display:grid; grid-template-columns: 220px 1fr; min-height:100vh; }}
aside {{ position:sticky; top:0; height:100vh; border-right:1px solid var(--border); padding:16px; }}
aside h2 {{ margin:0 0 8px; font-size:14px; color:var(--muted); }}
nav a {{ display:block; padding:6px 8px; border-radius:6px; text-decoration:none; color:inherit; }}
nav a:hover {{ background:#f1f5f9; }}
main {{ padding:24px; max-width:1200px; margin:0 auto; }}
h1 {{ font-size:28px; margin:0 0 12px; }}
h2 {{ font-size:20px; margin:22px 0 8px; }}
.kpis{{display:grid; grid-template-columns:repeat(4, minmax(220px,1fr)); gap:12px; margin:16px 0;}}
.kpi{{border:1px solid var(--border); border-radius:10px; padding:12px;}}
.kpi .label{{color:var(--muted); font-size:12px;}} .kpi .value{{font-size:22px; font-weight:600;}}
table{{ width:100%; border-collapse:collapse; margin:8px 0 16px; }}
th,td{{ border:1px solid var(--border); padding:8px 10px; vertical-align:top; }}
thead th{{ background:#f1f5f9; position:sticky; top:0; z-index:1; }}
tbody tr:nth-child(even){{ background:var(--row); }}
a{{ color:var(--accent); text-decoration:none; }} a:hover{{ text-decoration:underline; }}
.small{{font-size:12px; color:var(--muted);}}
td,th{{ font-variant-numeric: tabular-nums; }}
.pager{{display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:-6px 0 12px;}}
.pager button{{padding:4px 8px; border:1px solid var(--border); border-radius:6px; background:#fff; cursor:pointer;}}
.pager button.active{{background:#e2e8f0;}}
.pager button:disabled{{opacity:.5; cursor:not-allowed;}}
</style>
</head>
<body>
<div class="layout">
  <aside>
    <h2>Navigation</h2>
    <nav>
      <a href="{date.today().isoformat()}.html">Today’s Report →</a>
      <a href="#kpis">Stats</a>
      <a href="#queues">SRS Queues</a>
      <a href="#recent">Recent Attempts</a>
      <a href="#themes-all">Top Themes (all-time)</a>
      <a href="#themes-90">Top Themes (90d)</a>
    </nav>
  </aside>
  <main>
    <section id="kpis">
      <h1>Lichess Puzzle Tracker</h1>
      <div class="kpis">
        <div class="kpi"><div class="label">All-time attempts</div><div class="value">{kpi['attempts']}</div></div>
        <div class="kpi"><div class="label">All-time accuracy</div><div class="value">{kpi['acc'] if kpi['acc'] is not None else 'n/a'}%</div></div>
        <div class="kpi"><div class="label">Last 7 days accuracy</div><div class="value">{k7['acc7'] if k7['acc7'] is not None else 'n/a'}%</div></div>
        <div class="kpi"><div class="label">Last 30 days accuracy</div><div class="value">{k30['acc30'] if k30['acc30'] is not None else 'n/a'}%</div></div>
      </div>
    </section>

    <section id="queues">
      <h2>SRS Queues</h2>
      <table>
        <colgroup><col style="width:10ch"><col style="width:10ch"><col style="width:12ch"><col style="width:10ch"></colgroup>
        <thead><tr><th>Due Today</th><th>+1 Day</th><th>+2–7 Days</th><th>Later</th></tr></thead>
        <tbody><tr><td>{len(due_today)}</td><td>{len(due_p1)}</td><td>{len(due_p7)}</td><td>{len(due_later)}</td></tr></tbody>
      </table>
    </section>

    <section id="recent">
      <h2>Recent Attempts</h2>
      <table id="recent-table" class="paged" data-page-size="{PAGE_SIZE}">
        <colgroup><col style="width:6ch"><col style="width:8ch"><col style="width:auto"><col style="width:28ch"></colgroup>
        <thead><tr><th>Puzzle</th><th>Result</th><th>Themes</th><th>When</th></tr></thead>
        <tbody>
          {"".join(f"<tr><td><a href='https://lichess.org/training/{r.puzzle_id}' target='_blank' rel='noopener'>{r.puzzle_id}</a></td><td>{r.result.title()}</td><td>{_first_themes(r.themes)}</td><td>{_fmt_ts(r.attempted_at)} ({_ago(r.attempted_at)})</td></tr>" for r in recent)}
        </tbody>
      </table>
      <div class="pager"></div>
    </section>

    <section id="themes-all">{_html_themes(agg_all, "Top Themes (all-time)", table_id="themes-all-table")}</section>
    <section id="themes-90">{_html_themes(agg90, "Top Themes (last 90 days)", table_id="themes-90-table")}</section>

    <div class="small">Generated {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}</div>
  </main>
</div>
"""
        TRACKER_JS = r"""
<script>
(function(){
  function rowsOf(t){ return Array.from(t.querySelectorAll('tbody tr')); }
  function textOfRow(tr){ return tr.innerText.toLowerCase(); }

  function paginateTable(table){
    const ps = parseInt(table.dataset.pageSize || "10", 10);
    const body = table.tBodies[0];
    const all  = Array.from(body.rows);
    const eligible = all.filter(r => r.dataset.filtered !== "1");
    const pager = (table.nextElementSibling && table.nextElementSibling.classList.contains('pager'))
      ? table.nextElementSibling : null;
    if(!pager) return;

    let cur = parseInt(table.dataset.pageIndex || "0", 10);
    const pages = Math.max(Math.ceil(eligible.length / ps), 1);
    if(cur >= pages) cur = pages - 1;
    table.dataset.pageIndex = String(cur);

    eligible.forEach((tr, i) => {
      const page = Math.floor(i / ps);
      tr.style.visibility = (page === cur) ? "" : "hidden";
      tr.style.display    = (page === cur) ? "" : "none";
    });
    all.forEach(tr => {
      if(tr.dataset.filtered === "1"){
        tr.style.display = "none";
        tr.style.visibility = "hidden";
      }
    });

    pager.innerHTML = "";
    function btn(label, disabled, onClick, active=false){
      const b = document.createElement('button');
      b.type = "button";
      b.textContent = label;
      if(disabled) b.disabled = true;
      if(active) b.classList.add('active');
      b.addEventListener('click', onClick);
      pager.appendChild(b);
    }
    btn("«", cur===0, ()=>{ table.dataset.pageIndex="0"; paginateTable(table); });
    btn("‹", cur===0, ()=>{ table.dataset.pageIndex=String(cur-1); paginateTable(table); });

    const maxButtons = 7;
    const start = Math.max(0, cur - Math.floor(maxButtons/2));
    const end = Math.min(pages, start + maxButtons);
    for(let i=start;i<end;i++){
      btn(String(i+1), false, ()=>{ table.dataset.pageIndex=String(i); paginateTable(table); }, i===cur);
    }

    btn("›", cur>=pages-1, ()=>{ table.dataset.pageIndex=String(cur+1); paginateTable(table); });
    btn("»", cur>=pages-1, ()=>{ table.dataset.pageIndex=String(pages-1); paginateTable(table); });
  }

  // mark initial filter state and paginate all paged tables
  document.querySelectorAll('table.paged').forEach(t => {
    rowsOf(t).forEach(tr => tr.dataset.filtered = tr.dataset.filtered || "0");
    paginateTable(t);
  });

  window.addEventListener('resize', () => {
    document.querySelectorAll('table.paged').forEach(t => paginateTable(t));
  });
})();
</script>
</body>
</html>
"""
        (out / "index.html").write_text(tracker_head + TRACKER_JS, encoding="utf-8")
        print(f"[report] Wrote {out / 'index.html'}")

    # minimal Markdown (kept for continuity)
    md_path = out / f"{date.today().isoformat()}.md"
    md = [
        f"# Lichess Puzzle Report — {date.today().isoformat()}",
        f"- All-time attempts: {kpi['attempts']}",
        f"- All-time accuracy: {kpi['acc'] if kpi['acc'] is not None else 'n/a'}%",
        f"- Last 7 days accuracy: {k7['acc7'] if k7['acc7'] is not None else 'n/a'}%",
        f"- Last 30 days accuracy: {k30['acc30'] if k30['acc30'] is not None else 'n/a'}%",
        ""
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[report] Wrote {md_path}")
    return md_path
