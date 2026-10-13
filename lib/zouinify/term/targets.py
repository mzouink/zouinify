"""Which panes the terminal page may reach: WORKER sessions of this project, and nothing else (user decision 2026-10-13,
"Z-11 yes workers"). A worker is a session the launcher registered with role `worker`, or a live Claude session whose
name matches the configured worker pattern (`t{num}-{slug}`) and whose cwd is under the project.

Every target is inspected as ONE concrete pane (security review of T-0181, B1): the session must have exactly one pane
across its windows, that pane's command must be claude, its process tree must be the registered agent (the pid `claude
agents --json` reports descends from the pane's pid), the agent's cwd must be under the project, and the %pane_id found
at open must be the same before every later screen and keystroke; the send and the capture then address that %pane_id,
never a session or window name. The configured role sessions (desk, manager, debug, chrome_debug) are refused by name
AND by tmux window, whatever the registry or the config says; so is a shell pane, a session of another project, a session
with a split or a second window (a worker could open a second pane that is a client of another session), and any name
in `terminal.exclude`."""
from __future__ import annotations

import os
import re
import threading
import time

from zouinify import agents, readonly, tmux
from zouinify.registry import Registry

ROLE_REFUSED = "the role sessions (desk, manager, debug, chrome_debug) are never reachable from the browser"
CLAUDE_COMMANDS = frozenset({"claude", "node"})     # tmux names Claude's pane `claude`, sometimes `node`
AGENTS_TTL_S = 2.0                                   # `claude agents --json` costs a second or two; pids do not move in two
_AGENTS: dict = {}                                   # project root -> {"at", "rows"}
_AGENTS_LOCK = threading.Lock()


def live_agents(cfg) -> list:
    """`claude agents --json`, cached for AGENTS_TTL_S per project inside one process."""
    with _AGENTS_LOCK:
        now = time.time()
        c = _AGENTS.setdefault(cfg.root, {"at": 0.0, "rows": []})
        if now - c["at"] > AGENTS_TTL_S:
            c["rows"] = agents.list_agents()
            c["at"] = now
        return list(c["rows"])


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


def descends(pid, ancestor, depth: int = 8) -> bool:
    """True when `pid` is `ancestor` or one of its descendants (at most `depth` levels of /proc parents)."""
    try:
        p, anc = int(pid), int(ancestor)
    except (TypeError, ValueError):
        return False
    for _ in range(depth):
        if p == anc:
            return True
        if p <= 1:
            return False
        p = readonly.proc_parent(p) or 0
    return False


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
    """The candidate worker sessions, before the live inspection: [{name, tmux, ticket, source}], sorted by name."""
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
    rows_a = live_agents(cfg)
    panes = tmux.list_panes() if rows_a else []
    for a in rows_a:
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


def inspect(cfg, target: dict, expect_pane: str | None = None) -> tuple:
    """(ok, reason, pane): the one concrete pane of the target, right now. Refused when: a role name or window; no tmux
    session; more than one pane across the session's windows; the pane is not `expect_pane` (the %id seen at open); the
    pane's command is not claude; no live agent of that name; the agent's cwd is outside the project; the agent's process
    does not descend from the pane's. `pane` = {pane_id, pane_pid, agent_pid, command}."""
    roles = role_names(cfg)
    if target["tmux"] in roles or target["name"] in roles:
        return False, ROLE_REFUSED, None
    if not tmux.has_session(target["tmux"]):
        return False, "no tmux session of that name", None
    panes = tmux.panes_of(target["tmux"])
    if not panes:
        return False, "no pane in that session", None
    if len(panes) != 1:
        return False, f"the session has {len(panes)} panes across its windows; the terminal page needs exactly one", None
    p = panes[0]
    if not tmux.PANE_ID_RE.match(str(p.get("pane_id") or "")):
        return False, "tmux gave no pane id", None
    if expect_pane and p["pane_id"] != expect_pane:
        return False, "the pane changed since the attachment was opened", None
    if p.get("command") not in CLAUDE_COMMANDS:
        return False, f"the pane runs {p.get('command') or 'nothing'!r}, not claude", None
    agent = next((a for a in live_agents(cfg) if str(a.get("name") or "") == target["name"]), None)
    if agent is None:
        return False, "no live claude agent of that name (claude agents --json)", None
    if not _under(str(agent.get("cwd") or ""), _roots(cfg)):
        return False, "the agent's cwd is outside this project", None
    if not descends(agent.get("pid"), p.get("pane_pid")):
        return False, "the pane's process tree is not the registered agent", None
    return True, "", {"pane_id": p["pane_id"], "pane_pid": p["pane_pid"], "agent_pid": agent.get("pid"), "command": p.get("command")}


def live(cfg, target: dict) -> tuple:
    """(ok, reason) for a listing."""
    ok, why, _pane = inspect(cfg, target)
    return ok, why


def resolve(cfg, name: str) -> tuple:
    """(target with its pane, reason): the inspected worker target for `name` (a session name or its tmux window), or
    (None, why)."""
    if not isinstance(name, str) or not name or "/" in name or len(name) > 120:
        return None, "not a session name"
    if name in role_names(cfg):
        return None, ROLE_REFUSED
    t = next((t for t in list_targets(cfg) if t["name"] == name or t["tmux"] == name), None)
    if t is None:
        return None, "not a worker session of this project (only workers are reachable)"
    ok, why, pane = inspect(cfg, t)
    if not ok:
        return None, why
    return dict(t, **pane), ""
