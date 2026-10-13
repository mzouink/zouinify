"""`claude agents --json`: the documented listing of live Claude Code sessions (name, cwd, pid, status)."""
from __future__ import annotations

import json
import os
import subprocess

CLAUDE = os.environ.get("ZOUINIFY_CLAUDE", "claude")


def list_agents(timeout: float = 20) -> list:
    """Every live session, or [] when the CLI is missing, slow or prints something unexpected."""
    try:
        r = subprocess.run([CLAUDE, "agents", "--json"], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("agents") or data.get("sessions") or []
    return [a for a in data if isinstance(a, dict)]


def find(agents: list, name: str, cwd: str | None = None) -> dict | None:
    """The session named `name`; when several share the name, the one whose cwd is the project."""
    rows = [a for a in agents if a.get("name") == name]
    if cwd:
        real = os.path.realpath(cwd)
        for a in rows:
            if os.path.realpath(str(a.get("cwd") or "")) == real:
                return a
    return rows[0] if rows else None
