#!/usr/bin/env python3
"""Build a handover brief for a Claude Code session from its transcript.

  handover.py <transcript.jsonl> [--out DIR]     write <sid>.md (instant, no LLM)
  handover.py <transcript.jsonl> --ai            also rewrite it via `claude -p` (slow; run detached)

The brief covers: goal, what was asked recently, files touched, git state,
open todos, scheduled/background work, and where the last turn stopped.
"""
import json
import os
import re
import subprocess
import sys

OUT_DIR = os.path.expanduser("~/.claude-monitor/handovers")
SCHEDULE_TOOLS = {"CronCreate", "ScheduleWakeup", "RemoteTrigger", "Workflow", "Monitor"}
NOISE = ("<system-reminder", "<command-name", "<local-command", "<task-notification", "[Request interrupted",
         "Caveat:", "<ide_", "<artifact-content", "This session is being continued", "UserPromptSubmit operation blocked")


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def digest(path):
    d = {"title": None, "cwd": None, "branch": None, "first_ts": None, "last_ts": None, "turns": 0,
         "prompts": [], "files": {}, "todos": [], "scheduled": [], "bg": [], "last_assistant": "", "sid": None}
    for line in open(path, errors="ignore"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        if e.get("timestamp"):
            d["first_ts"] = d["first_ts"] or e["timestamp"]
            d["last_ts"] = e["timestamp"]
        d["cwd"] = e.get("cwd") or d["cwd"]
        d["branch"] = e.get("gitBranch") or d["branch"]
        d["sid"] = e.get("sessionId") or d["sid"]
        if t == "ai-title":
            d["title"] = e.get("aiTitle") or d["title"]
        elif t == "custom-title":
            d["title"] = e.get("customTitle") or d["title"]
        msg = e.get("message") or {}
        if t == "user" and not e.get("isMeta"):
            txt = text_of(msg.get("content")).strip()
            if txt and not txt.startswith(NOISE):
                d["prompts"].append(txt)
        elif t == "assistant":
            d["turns"] += 1
            content = msg.get("content")
            txt = text_of(content).strip()
            if txt:
                d["last_assistant"] = txt
            for b in content if isinstance(content, list) else []:
                if b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input") or {}
                if name in ("Write", "Edit", "NotebookEdit") and inp.get("file_path"):
                    d["files"][inp["file_path"]] = name
                elif name == "TodoWrite":
                    d["todos"] = inp.get("todos") or d["todos"]
                elif name in SCHEDULE_TOOLS:
                    d["scheduled"].append((name, clip(json.dumps(inp, ensure_ascii=False), 220)))
                elif name == "Bash" and inp.get("run_in_background"):
                    d["bg"].append(clip(inp.get("description") or inp.get("command", ""), 140))
    return d


def git_state(cwd):
    if not cwd or not os.path.isdir(cwd):
        return ""
    def run(*a):
        try:
            return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=4).stdout.strip()
        except Exception:
            return ""
    status, log = run("status", "--short", "--branch"), run("log", "--oneline", "-5")
    return f"{status}\n\nRecent commits:\n{log}" if status else ""


def build(path):
    d = digest(path)
    L = [f"# Handover: {d['title'] or d['sid']}", "",
         f"- Session: `{d['sid']}`  ·  turns: {d['turns']}  ·  {d['first_ts']} → {d['last_ts']}",
         f"- Directory: `{d['cwd']}`  ·  branch: `{d['branch']}`", ""]
    if d["prompts"]:
        L += ["## Original goal", clip(d["prompts"][0], 600), "", "## Most recent requests (oldest → newest)"]
        L += [f"{i}. {clip(p, 350)}" for i, p in enumerate(d["prompts"][-8:], 1)]
        L.append("")
    if d["files"]:
        L += ["## Files written or edited (last 40)"] + [f"- {fp} ({how})" for fp, how in list(d["files"].items())[-40:]] + [""]
    g = git_state(d["cwd"])
    if g:
        L += ["## Git state right now", "```", g, "```", ""]
    open_todos = [t for t in d["todos"] if t.get("status") != "completed"]
    if d["todos"]:
        L += ["## Todos (last list)"] + [f"- [{'x' if t.get('status') == 'completed' else ' '}] {t.get('content')}" for t in d["todos"]] + [""]
    if d["scheduled"] or d["bg"]:
        L += ["## Scheduled / background work (verify it is still live)"]
        L += [f"- {n}: {a}" for n, a in d["scheduled"][-10:]] + [f"- background shell: {b}" for b in d["bg"][-10:]] + [""]
    if d["last_assistant"]:
        L += ["## Where the last turn stopped", clip(d["last_assistant"], 1500), ""]
    L += ["## Continue prompt (paste into the new session)",
          f"> Continue the work from this handover. Read `{path}` only if you need raw detail. "
          f"Start in `{d['cwd']}` on branch `{d['branch']}`, check `git status` first, "
          f"then pick up the open items above ({len(open_todos)} open todos)."]
    return "\n".join(L), d


def write(path, out_dir=OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    md, d = build(path)
    out = os.path.join(out_dir, f"{d['sid'] or os.path.basename(path)}.md")
    with open(out, "w") as f:
        f.write(md)
    return out


def ai_rewrite(md_path):
    """Ask a small model to turn the mechanical digest into Done / To do / Scheduled."""
    md = open(md_path).read()
    prompt = ("Below is an automatically extracted digest of a long Claude Code session. Rewrite it as a handover "
              "for a fresh session, in markdown, under 350 words, with exactly these sections: "
              "**Done**, **To do next**, **Scheduled / running in background**, **Gotchas**. "
              "Use only facts in the digest; say 'unknown' rather than guessing. Keep file paths and branch names exact.\n\n" + md)
    env = dict(os.environ, CLAUDE_GUARD_SKIP="1")
    r = subprocess.run(["claude", "-p", "--model", "haiku", prompt], capture_output=True, text=True, timeout=180, env=env)
    if r.returncode == 0 and r.stdout.strip():
        with open(md_path[:-3] + ".ai.md", "w") as f:
            f.write(r.stdout.strip() + "\n\n---\n_Raw digest: " + md_path + "_\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    out = write(args[0], args[args.index("--out") + 1] if "--out" in args else OUT_DIR)
    if "--ai" in args:
        ai_rewrite(out)
    print(out)
