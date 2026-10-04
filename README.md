# claude-session-monitor

A local dashboard for Claude Code sessions. It lists running Claude Code processes with their turns, elapsed time and token usage, alerts when a session runs too long, and writes handover briefs so work can continue in a fresh session.

- `server.py` serves the dashboard at http://127.0.0.1:8787
- `index.html` is the dashboard page
- `session_guard.py` is a UserPromptSubmit hook that blocks a prompt once a session has run too long
- `handover.py` writes the handover brief

Run it with `python3 server.py`. It reads session data from `~/.claude` and keeps its own state and handovers next to the scripts (not committed).
