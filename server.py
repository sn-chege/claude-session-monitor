#!/usr/bin/env python3
"""Local dashboard: live Claude Code processes + their session stats.
Serves http://127.0.0.1:8787 — open and pin this in a browser tab.
"""
import json
import os
import re
import subprocess
import glob
import datetime
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

# elapsed-hours / turn-count thresholds that flag a session as "should /clear"
RISK_HOURS = 3.0
RISK_TURNS = 300

# incremental scan cache: path -> {offset, turns, in_tok, out_tok, cread, ccreate, first_ts, last_ts}
_scan_cache = {}


SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
STATE_FILE = os.path.expanduser("~/.claude-monitor/state/alerts.json")
ALERT_REPEAT_MIN = 30   # re-alert this often while a session stays flagged
SNOOZE_MIN = 60
_title_cache = {}       # transcript path -> (mtime, title)
_lock = threading.Lock()


def load_alert_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def get_claude_processes():
    """Live sessions, from ~/.claude/sessions/<pid>.json (has name, cwd, status)."""
    procs = []
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            pid = int(d["pid"])
            os.kill(pid, 0)  # raises if the process is gone
        except Exception:
            continue
        procs.append(d)
    return procs


def find_transcript(session_id):
    matches = glob.glob(os.path.join(PROJECTS_ROOT, "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def read_title(path):
    """Latest ai-title / custom title in the transcript, cached by mtime."""
    mtime = os.stat(path).st_mtime
    hit = _title_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    title = hit[1] if hit else None
    with open(path, "r", errors="ignore") as f:
        for line in f:
            if '"ai-title"' in line or '"custom-title"' in line:
                try:
                    d = json.loads(line)
                    title = d.get("aiTitle") or d.get("customTitle") or title
                except Exception:
                    pass
    _title_cache[path] = (mtime, title)
    return title


def scan_transcript(path):
    st = os.stat(path)
    cache = _scan_cache.get(path)
    if cache is None or cache["offset"] > st.st_size:
        cache = {"offset": 0, "turns": 0, "in_tok": 0, "out_tok": 0, "cread": 0, "ccreate": 0,
                 "first_ts": None, "last_ts": None, "model": None, "context": 0}
        _scan_cache[path] = cache

    with open(path, "r", errors="ignore") as f:
        f.seek(cache["offset"])
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp")
            if ts:
                if cache["first_ts"] is None:
                    cache["first_ts"] = ts
                cache["last_ts"] = ts
            if d.get("type") == "assistant":
                msg = d.get("message", {})
                usage = msg.get("usage")
                if usage:
                    cache["turns"] += 1
                    cache["in_tok"] += usage.get("input_tokens", 0) or 0
                    cache["out_tok"] += usage.get("output_tokens", 0) or 0
                    cache["cread"] += usage.get("cache_read_input_tokens", 0) or 0
                    cache["ccreate"] += usage.get("cache_creation_input_tokens", 0) or 0
                    # size of the prompt on the latest turn = current context window use
                    cache["context"] = ((usage.get("input_tokens", 0) or 0)
                                        + (usage.get("cache_read_input_tokens", 0) or 0)
                                        + (usage.get("cache_creation_input_tokens", 0) or 0))
                    cache["model"] = msg.get("model") or cache["model"]
        cache["offset"] = f.tell()

    return cache


def human_elapsed(first_ts):
    if not first_ts:
        return "?", 0
    try:
        start = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
    except Exception:
        return "?", 0
    hours = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds() / 3600
    return f"{int(hours)}h {int((hours - int(hours)) * 60)}m", hours


def human_ago(ts):
    if not ts:
        return "?"
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return "?"
    s = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    return f"{int(s // 3600)}h {int(s % 3600 // 60)}m ago"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def notify(title, message):
    """Real macOS notification with a sound."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)


def maybe_alert(rows):
    """Fire a notification when a session first flags, then every ALERT_REPEAT_MIN."""
    now = time.time()
    with _lock:
        st = load_alert_state()
        changed = False
        for r in rows:
            s = st.setdefault(r["session_id"], {})
            r["snoozed"] = s.get("snooze_until", 0) > now
            r["last_alert"] = s.get("last_alert", 0)
            if not r["risk"]:
                if s.get("last_alert"):
                    s["last_alert"] = 0
                    changed = True
                continue
            if r["snoozed"] or now - s.get("last_alert", 0) < ALERT_REPEAT_MIN * 60:
                continue
            reason = f"{r['elapsed']} / {r['turns']} turns / {r['total_tokens']} tokens"
            notify(f"Clear this session: {r['name']}", f"{reason}. Run /clear in that window.")
            s["last_alert"] = now
            r["last_alert"] = now
            changed = True
        if changed:
            save_alert_state(st)


def recommend(r):
    """Rule-based next step for a session. level: bad | warn | ok | info; btn: the card action to highlight."""
    p = max(r["elapsed_hours"] / RISK_HOURS, r["turns"] / RISK_TURNS)
    idle_min = r["idle_sec"] / 60
    busy = str(r["status"]).lower() in ("busy", "running", "working")
    if busy:
        if r["risk"]:
            return {"level": "warn", "title": "Let it finish, then hand over",
                    "why": "Over the limit but mid-task. Ending now could lose work.", "btn": None}
        return {"level": "info", "title": "Working: leave it", "why": "Active right now.", "btn": None}
    if r["risk"] and idle_min >= 10:
        return {"level": "bad", "title": "Hand over and end it",
                "why": f"Over the limit ({r['elapsed']}, {r['turns']} turns) and idle {int(idle_min)}m. Every new turn re-reads the whole history.",
                "btn": "handover"}
    if r["risk"]:
        return {"level": "bad", "title": "Hand over, then /clear",
                "why": f"Over the limit ({r['elapsed']}, {r['turns']} turns).", "btn": "handover"}
    if p >= 0.75:
        return {"level": "warn", "title": "Wrap up this task, then hand over",
                "why": f"{int(p * 100)}% of the way to the limit. Finish the current thread, don't start a new one.", "btn": "handover"}
    if idle_min >= 120:
        return {"level": "warn", "title": "Stale: hand over and end it",
                "why": f"Idle {int(idle_min // 60)}h. Nothing is gained by keeping it open.", "btn": "handover"}
    if r["context_raw"] >= 500_000:
        return {"level": "warn", "title": "Context is huge: /compact or hand over",
                "why": f"Context is {r['context']} per turn, so each reply is expensive.", "btn": "handover"}
    if idle_min >= 30:
        return {"level": "info", "title": "Idle: resume or end",
                "why": f"Idle {int(idle_min)}m. Fine to leave, but end it if the task is done.", "btn": None}
    return {"level": "ok", "title": "Healthy: no action", "why": f"{int(p * 100)}% of limit.", "btn": None}


def make_handover(sid, ai=True):
    """Write the handover brief for a session; returns its path or None."""
    tp = find_transcript(sid)
    if not tp:
        return None
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import handover as h
    out = h.write(tp)
    if ai:
        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "handover.py"), tp, "--ai"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return out


def build_status():
    rows = []
    for p in get_claude_processes():
        sid = p.get("sessionId")
        cwd = p.get("cwd") or ""
        row = {"pid": p["pid"], "session_id": sid, "project": os.path.basename(cwd) or "?", "cwd": cwd,
               "name": p.get("name") or (sid or "")[:8], "status": p.get("status", "?"),
               "entrypoint": p.get("entrypoint", "?"), "version": p.get("version", "?"),
               "elapsed": "?", "elapsed_hours": 0, "turns": 0, "total_tokens": "0", "total_tokens_raw": 0,
               "context": "0", "context_raw": 0, "cache_read": "0", "output": "0", "model": "?",
               "last_active": human_ago(datetime.datetime.fromtimestamp(
                   p.get("updatedAt", 0) / 1000, datetime.timezone.utc).isoformat()) if p.get("updatedAt") else "?",
               "risk": False, "idle_sec": 0}
        if p.get("updatedAt"):
            row["idle_sec"] = max(0, time.time() - p["updatedAt"] / 1000)
        tp = find_transcript(sid) if sid else None
        if tp:
            c = scan_transcript(tp)
            title = read_title(tp)
            if title:
                row["name"] = title
            row["elapsed"], hours = human_elapsed(c["first_ts"])
            total = c["in_tok"] + c["out_tok"] + c["cread"] + c["ccreate"]
            row.update({
                "elapsed_hours": round(hours, 2), "turns": c["turns"],
                "total_tokens": fmt_tokens(total), "total_tokens_raw": total,
                "context": fmt_tokens(c["context"]), "context_raw": c["context"],
                "cache_read": fmt_tokens(c["cread"]), "output": fmt_tokens(c["out_tok"]),
                "model": c["model"] or "?", "last_active": human_ago(c["last_ts"]),
                "risk": hours >= RISK_HOURS or c["turns"] >= RISK_TURNS,
            })
        rows.append(row)
    maybe_alert(rows)
    for row in rows:
        row["rec"] = recommend(row)
    rows.sort(key=lambda r: -r["total_tokens_raw"])
    return {"rows": rows, "generated_at": datetime.datetime.now().strftime("%H:%M:%S"),
            "risk_hours": RISK_HOURS, "risk_turns": RISK_TURNS}


def do_action(action, sid):
    rows = {r["session_id"]: r for r in build_status()["rows"]}
    r = rows.get(sid)
    if not r:
        return 404, {"error": "session not running"}
    if action == "test":
        notify(f"Test alert: {r['name']}", f"{r['elapsed']} / {r['turns']} turns / {r['total_tokens']} tokens")
    elif action in ("snooze", "unsnooze"):
        with _lock:
            st = load_alert_state()
            st.setdefault(sid, {})["snooze_until"] = time.time() + SNOOZE_MIN * 60 if action == "snooze" else 0
            save_alert_state(st)
    elif action == "handover":
        path = make_handover(sid)
        if not path:
            return 404, {"error": "no transcript found for that session"}
        return 200, {"ok": True, "handover": path, "ai": path[:-3] + ".ai.md"}
    elif action == "stop":
        path = None
        try:
            path = make_handover(sid)  # brief first, so ending never loses the context
        except Exception:
            pass
        os.kill(int(r["pid"]), signal.SIGTERM)
        return 200, {"ok": True, "handover": path, "ai": path[:-3] + ".ai.md" if path else None}
    else:
        return 400, {"error": "unknown action"}
    return 200, {"ok": True}


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Sessions</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --card:#fff; --line:rgba(128,128,128,.25); }
  @media (prefers-color-scheme: dark) { :root { --card:#1f2228; } body { background:#16181c; color:#e6e6e6; } }
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin:0; padding:20px; background:#fafafa; color:#1a1a1a; }
  h1 { font-size:18px; margin:0 0 4px; }
  .sub { font-size:12px; opacity:.6; margin-bottom:16px; }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .card.risk { border-color:#d32f2f; box-shadow:0 0 0 1px #d32f2f; }
  .top { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
  .name { font-weight:600; font-size:14px; line-height:1.3; word-break:break-word; }
  .proj { font-size:11px; opacity:.55; margin-top:2px; }
  .badge { padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap; }
  .badge.risk { background:#d32f2f; color:#fff; }
  .badge.ok { background:rgba(60,160,60,.18); color:#2e7d32; }
  .badge.snz { background:rgba(128,128,128,.25); }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px 12px; margin:12px 0; }
  .stats div span { display:block; font-size:10px; text-transform:uppercase; letter-spacing:.04em; opacity:.55; }
  .stats div b { font-size:14px; font-weight:600; }
  .meta { font-size:11px; opacity:.55; margin-bottom:10px; word-break:break-all; }
  .bar { height:5px; border-radius:3px; background:var(--line); margin:-4px 0 10px; overflow:hidden; }
  .bar i { display:block; height:100%; background:#2e7d32; }
  .bar i.hot { background:#d32f2f; }
  .btns { display:flex; gap:6px; flex-wrap:wrap; }
  button { font:inherit; font-size:12px; padding:4px 10px; border-radius:6px; border:1px solid var(--line);
           background:transparent; color:inherit; cursor:pointer; }
  button:hover { background:rgba(128,128,128,.15); }
  button.danger { color:#d32f2f; border-color:#d32f2f66; }
  .empty { opacity:.5; padding:30px 0; text-align:center; }
</style>
</head>
<body>
<h1>Claude Code — live sessions</h1>
<div class="sub" id="sub">loading…</div>
<div id="grid"></div>
<script>
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function act(action, sid, label) {
  if (action === 'stop' && !confirm('End session "' + label + '"? Unsaved work in that window is lost.')) return;
  const r = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body: JSON.stringify({action, session_id: sid})});
  if (!r.ok) alert((await r.json()).error || 'failed');
  refresh();
}
function copyResume(sid) { navigator.clipboard.writeText('claude --resume ' + sid); }
async function refresh() {
  try {
    const d = await (await fetch('/api/status')).json();
    document.getElementById('sub').textContent =
      `${d.rows.length} session(s) · updated ${d.generated_at} · flags at ${d.risk_hours}h or ${d.risk_turns} turns · macOS alert on flag`;
    const g = document.getElementById('grid');
    if (!d.rows.length) { g.innerHTML = '<div class="empty">No Claude sessions running</div>'; return; }
    g.innerHTML = d.rows.map(r => {
      const pct = Math.min(100, Math.max(r.elapsed_hours / d.risk_hours, r.turns / d.risk_turns) * 100);
      const badge = r.risk ? '<span class="badge risk">/clear me</span>' : '<span class="badge ok">ok</span>';
      const snz = r.snoozed ? ' <span class="badge snz">snoozed</span>' : '';
      const id = esc(r.session_id), nm = esc(r.name).replace(/'/g, '&#39;');
      return `<div class="card ${r.risk ? 'risk' : ''}">
        <div class="top"><div><div class="name">${esc(r.name)}</div><div class="proj">${esc(r.project)} · ${esc(r.status)}</div></div><div>${badge}${snz}</div></div>
        <div class="stats">
          <div><span>Elapsed</span><b>${esc(r.elapsed)}</b></div><div><span>Turns</span><b>${r.turns}</b></div>
          <div><span>Context now</span><b>${esc(r.context)}</b></div><div><span>Total tokens</span><b>${esc(r.total_tokens)}</b></div>
          <div><span>Cache reads</span><b>${esc(r.cache_read)}</b></div><div><span>Output</span><b>${esc(r.output)}</b></div>
          <div><span>Last active</span><b>${esc(r.last_active)}</b></div><div><span>Model</span><b>${esc(r.model)}</b></div>
        </div>
        <div class="bar"><i class="${r.risk ? 'hot' : ''}" style="width:${pct}%"></i></div>
        <div class="meta">${esc(r.entrypoint)} · v${esc(r.version)} · pid ${r.pid}<br>${esc(r.cwd)}</div>
        <div class="btns">
          <button onclick="act('test','${id}')">Send alert</button>
          <button onclick="act('${r.snoozed ? 'unsnooze' : 'snooze'}','${id}')">${r.snoozed ? 'Unsnooze' : 'Snooze 1h'}</button>
          <button onclick="copyResume('${id}')">Copy resume</button>
          <button class="danger" onclick="act('stop','${id}','${nm}')">End</button>
        </div></div>`;
    }).join('');
  } catch (e) {
    document.getElementById('sub').textContent = 'server unreachable — is it running?';
  }
}
refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        # reject cross-site requests: only the dashboard's own origin may trigger actions
        origin = self.headers.get("Origin", "")
        if origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}") or self.path != "/api/action":
            self.send_response(403)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
            code, payload = do_action(req.get("action"), req.get("session_id"))
        except Exception as e:
            code, payload = 500, {"error": str(e)}
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            try:
                body = json.dumps(build_status()).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e), "rows": [], "generated_at": "", "risk_hours": RISK_HOURS, "risk_turns": RISK_TURNS}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claude session monitor on http://127.0.0.1:{PORT}")
    server.serve_forever()
