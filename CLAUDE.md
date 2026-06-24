# CLAUDE.md

This project's agent guide is **[AGENTS.md](AGENTS.md)** — read it first
(architecture, golden rules, conventions, gotchas, how to run/test).

Past-session memory and hard-won lessons: **[docs/agent-memory.md](docs/agent-memory.md)**.

Quick start: `run.bat` → console on `http://127.0.0.1:8000`. Verify with
`python -m pytest tests -q` + `python -m scripts.dry_run` + `node --check web/app.js`.
