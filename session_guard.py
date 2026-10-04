#!/usr/bin/env python3
"""UserPromptSubmit hook: blocks a prompt once its session has run too long,
forcing a /clear before continuing. Avoids runaway cache-read token costs
from marathon sessions (see memory: feedback on long-lived Claude sessions).

On block it also writes a handover brief (see handover.py) so the next session can pick up the work.
"""
import json
import os
import subprocess
import sys
import time

RISK_HOURS = 3.0
RISK_TURNS = 300
AI_REFRESH_SEC = 30 * 60
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
sys.path.insert(0, HERE)


def handover(payload):
    """Returns (path, ai_path_or_None). Never raises: the guard must still block."""
    tp = payload.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return None, None
    try:
        import handover as h
        out = h.write(tp)
        ai = out[:-3] + ".ai.md"
        if not os.path.exists(ai) or time.time() - os.path.getmtime(ai) > AI_REFRESH_SEC:
            subprocess.Popen([sys.executable, os.path.join(HERE, "handover.py"), tp, "--ai"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return out, ai
    except Exception:
        return None, None


def main():
    if os.environ.get("CLAUDE_GUARD_SKIP"):
        return  # headless helper sessions spawned by handover.py
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input, fail open

    session_id = payload.get("session_id")
    if not session_id:
        return

    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, f"{session_id}.json")

    now = time.time()
    state = {"start": now, "count": 0, "warned": False}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except Exception:
            pass

    count = state["count"] + 1
    elapsed_hours = (now - state["start"]) / 3600

    if elapsed_hours >= RISK_HOURS or count >= RISK_TURNS:
        # blocked prompts are not turns, so the count is not saved
        out, ai = handover(payload)
        reason = (
            f"This session has been running {elapsed_hours:.1f}h across {state['count']} turns. "
            f"Each turn re-sends the full accumulated history as cache-read tokens, so long-lived "
            f"sessions silently burn large amounts of usage. Run /clear (or start a fresh session) "
            f"before continuing."
        )
        if out:
            reason += (
                f"\n\nHandover brief written: {out}\n"
                f"A tidier Done / To do / Scheduled version lands in ~30-60s at: {ai}\n"
                f"In the new session, say: \"Read {ai if os.path.exists(ai) else out} and continue the work.\""
            )
        print(json.dumps({"decision": "block", "reason": reason}))
        return

    state["count"] = count
    with open(state_path, "w") as f:
        json.dump(state, f)
    sys.exit(0)


if __name__ == "__main__":
    main()
