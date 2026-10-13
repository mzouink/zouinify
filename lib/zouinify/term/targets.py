"""Which panes the terminal page may reach: WORKER sessions of this project, and nothing else (user decision 2026-10-13,
"Z-11 yes workers"). A worker is a session the launcher registered with role `worker`, or a live Claude session whose
name matches the configured worker pattern (`t{num}-{slug}`) and whose cwd is under the project; it is offered only
while its tmux session exists and its pane runs claude. The configured role sessions (desk, manager, debug,
chrome_debug) are refused by name AND by tmux window, whatever the registry or the config says, and so is any pane that
is not a Claude pane (a shell), any session of another project and any name in `terminal.exclude`."""
from __future__ import annotations

import os
import re

from zouinify import agents, readonly, tmux
from zouinify.registry import Registry

ROLE_REFUSED = "the role sessions (desk, manager, debug, chrome_debug) are never reachable from the browser"
CLAUDE_COMMANDS = frozenset({"claude", "node"})     # tmux names Claude's pane `claude`, sometimes `node`


def role_names(cfg) -> frozenset:
    """Every configured role session's `-n` name and tmux window name."""
    names = set()
    for s in cfg.role_sessions().values():
        for v in (getattr(s, "name", None), getattr(s, "tmux", None)):
            if v:
                names.add(str(v))
    return frozenset(names)


def worker_pattern(cfg):
    """The configured worker tmux pattern as a regex: `t{num}-{slug}` -> ^t(\\d{4})-([a-z0-9][a-z0-9-]*)$."""
    w = cfg.sessions.get("worker")
    pat = str(getattr(w, "tmux", None) or getattr(w, "name", None) or "t{num}-{slug}")
    rx = re.escape(pat).replace(r"\{num\}", r"(\d{4})").replace(r"\{slug\}", r"([a-z0-9][a-z0-9-]*)")
    return re.compile("^" + rx + "$")


def _roots(cfg) -> set:
    roots = {os.path.realpath(cfg.root)}
    for r in getattr(cfg, "repos", []) or []:
        try:
            roots.add(os.path.realpath(cfg.path(r["path"])))
        except (KeyError, TypeError):
            pass
    return roots


def _under(cwd: str, roots: set) -> bool:
    rc = os.path.realpath(cwd or "")
    return any(rc == r or rc.startswith(r + os.sep) for r in roots)


def pane_session_of(pid, panes: list):
    """The tmux session whose pane is an ancestor of `pid` (at most 6 levels up /proc), or None: a session named by the
    worker pattern may live in a window of another name (sessions from before the launcher existed)."""
    by_pid: dict = {}
    for p in panes:
        try:
            by_pid[int(p["pane_pid"])] = p["session"]
        except (KeyError, ValueError, TypeError):
            pass
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return None
    for _ in range(6):
        if not p:
            return None
        if p in by_pid:
            return by_pid[p]
        p = readonly.proc_parent(p)
    return None


def list_targets(cfg) -> list:
    """The candidate worker sessions, before the live checks: [{name, tmux, ticket, source}], sorted by name."""
    roles = role_names(cfg)
    excluded = set(cfg.terminal.get("exclude") or ())
    out: dict = {}
    for e in Registry(cfg).load():
        name, win = str(e.get("name") or ""), str(e.get("tmux") or "")
        if e.get("role") != "worker" or not name or not win or name in roles or win in roles:
            continue
        out[win] = {"name": name, "tmux": win, "ticket": str(e.get("ticket") or ""), "source": "registry"}
    pat = worker_pattern(cfg)
    roots = _roots(cfg)
    live_agents = agents.list_agents()
    panes = tmux.list_panes() if live_agents else []
    for a in live_agents:
        name = str(a.get("name") or "")
        m = pat.match(name)
        if not m or name in roles or not _under(str(a.get("cwd") or ""), roots):
            continue
        win = pane_session_of(a.get("pid"), panes) or name
        if win in roles or any(t["tmux"] == win for t in out.values()):
            continue
        out.setdefault(name, {"name": name, "tmux": win, "ticket": f"T-{m.group(1)}" if m.groups() else "", "source": "agents"})
    rows = [t for t in out.values() if t["name"] not in excluded and t["tmux"] not in excluded]
    rows.sort(key=lambda t: t["name"])
    return rows


def live(cfg, target: dict) -> tuple:
    """(ok, reason): the target's tmux session exists and its pane runs claude, right now."""
    if target["tmux"] in role_names(cfg) or target["name"] in role_names(cfg):
        return False, ROLE_REFUSED
    if not tmux.has_session(target["tmux"]):
        return False, "no tmux session of that name"
    cmd = tmux.pane_command(target["tmux"])
    if cmd not in CLAUDE_COMMANDS:
        return False, f"the pane runs {cmd or 'nothing'!r}, not claude"
    return True, ""


def resolve(cfg, name: str) -> tuple:
    """(target, reason): the live worker target for `name` (a session name or its tmux window), or (None, why)."""
    if not isinstance(name, str) or not name or "/" in name or len(name) > 120:
        return None, "not a session name"
    if name in role_names(cfg):
        return None, ROLE_REFUSED
    t = next((t for t in list_targets(cfg) if t["name"] == name or t["tmux"] == name), None)
    if t is None:
        return None, "not a worker session of this project (only workers are reachable)"
    ok, why = live(cfg, t)
    if not ok:
        return None, why
    return t, ""
