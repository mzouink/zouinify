"""The read-only facade the panel is allowed to use. lib/zouinify/panel/ may import this module (and
zouinify.panelstate for the server's own pid file and log) and nothing else of zouinify's internals: never
zouinify.tmux, zouinify.launch, zouinify.tickets, zouinify.agents, zouinify.status or the registry
directly, nor any process-spawning primitive (scripts/check_panel_guard.sh and scripts/check_panel_ast.py
enforce it). Everything here observes; nothing creates, kills, types, moves or deploys. The modules it
delegates to are imported inside the functions so that this module re-exports none of them as attributes."""
from __future__ import annotations

import json
import os

from zouinify import ZouinifyError
from zouinify.config import ROLE_SESSIONS  # noqa: F401 - a tuple of role names, re-exported for the panel

READ_ONLY_TICKET_SH = {"human": {"--count", "--json"}, "board": {"--json", "--slow"}}
TERMINAL = ("done", "dropped")


def fail(message: str):
    """Raise the CLI's user-facing error from code that may not import zouinify itself."""
    raise ZouinifyError(message)


def version() -> str:
    from zouinify import __version__
    return __version__


# -- tmux, reading only ---------------------------------------------------------------------------------

def capture_pane(name: str) -> str:
    """The visible screen of a tmux session's active pane (capture-pane -p; reads, never types)."""
    from zouinify import tmux as t
    return t.capture_pane(name)


def list_panes() -> list:
    from zouinify import tmux as t
    return t.list_panes()


def has_session(name: str) -> bool:
    from zouinify import tmux as t
    return t.has_session(name)


def model_on_screen(text: str, pattern: str | None = None) -> str | None:
    from zouinify import tmux as t
    return t.model_on_screen(text, pattern)


def model_word(label: str | None) -> str | None:
    from zouinify import tmux as t
    return t.model_word(label)


def alias_of(model: str) -> str | None:
    from zouinify import tmux as t
    return t.alias_of(model)


# -- git and processes, reading only ----------------------------------------------------------------------

def trunk_sha(root: str) -> str:
    """`git rev-parse --short HEAD` of the project root; "" when unavailable."""
    import subprocess as sp
    try:
        r = sp.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, sp.TimeoutExpired):
        return ""


def proc_parent(pid: int):
    """The parent pid from /proc, for mapping a Claude session's pid to its tmux pane."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().split(")")[-1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def path_exists(path: str) -> bool:
    return os.path.exists(path)


def list_agents() -> list:
    """`claude agents --json`: every live Claude Code session (name, cwd, pid, status), or []."""
    from zouinify import agents as a
    return a.list_agents()


def find_agent(agents: list, name: str, cwd: str | None = None):
    from zouinify import agents as a
    return a.find(agents, name, cwd)


# -- the project's tickets, reading only ------------------------------------------------------------------

def is_ticket_id(s: str) -> bool:
    from zouinify import tickets as t
    return bool(t.ID_RE.match(s or ""))


def ticket_files(cfg) -> list:
    from zouinify import tickets as t
    return t.ticket_files(cfg)


def file_of(cfg, tid: str) -> str | None:
    from zouinify import tickets as t
    return t.file_of(cfg, tid)


def front_matter(path: str) -> dict:
    from zouinify import tickets as t
    return t.front_matter(path)


def freeze(cfg) -> dict:
    from zouinify import tickets as t
    on = t.freeze_on(cfg)
    return {"on": on, "ids": t.freeze_ids(cfg) if on else []}


def counts(cfg) -> tuple:
    from zouinify import tickets as t
    return t.counts(cfg)


def tickets_dir(cfg) -> str:
    from zouinify import tickets as t
    return t.tickets_dir(cfg)


# -- status helpers (the same numbers `zouinify status` prints) ---------------------------------------------

def now_local(cfg):
    from zouinify import status as s
    return s.now_local(cfg)


def creds(cfg, now=None, use_cache: bool = True) -> dict:
    """The credentials state, from the cached read-only `aws sts get-caller-identity` (use_cache=False re-probes now: the
    SSO button's click, T-0195)."""
    from zouinify import status as s
    return s.creds(cfg, now, use_cache=use_cache)


def waits_on(cfg, token: str = "aws-sso") -> list:
    """The open tickets whose newest log line is `waits-on: <token> -- resume: <step>` (T-0195): who waits on SSO."""
    from zouinify import tickets as t
    return t.waits_on(cfg, token)


# -- the launcher registry, reading only --------------------------------------------------------------------

def registry_entries(cfg) -> list:
    from zouinify.registry import Registry
    return Registry(cfg).load()


def registry_entry(cfg, name: str):
    from zouinify.registry import Registry
    return Registry(cfg).get(name)


# -- ticket.sh, read-only output modes only ---------------------------------------------------------------

def read_ticket_sh(cfg, args: list, timeout: float = 120):
    """Run the project's ticket.sh for a read-only output mode only: `human --count|--json`, `board --json
    [--slow]`. Anything else is refused here, before the script is invoked. Returns the CompletedProcess."""
    if not args or args[0] not in READ_ONLY_TICKET_SH:
        raise ZouinifyError(f"read_ticket_sh: {' '.join(args) or '(nothing)'} is not a read-only ticket.sh command")
    bad = [a for a in args[1:] if a not in READ_ONLY_TICKET_SH[args[0]]]
    if bad:
        raise ZouinifyError(f"read_ticket_sh: ticket.sh {args[0]} takes only {sorted(READ_ONLY_TICKET_SH[args[0]])} here, not {bad}")
    from zouinify import tickets as t
    return t.ticket_sh(cfg, list(args), timeout=timeout)


def read_human_json(cfg, timeout: float = 120) -> list | None:
    """`ticket.sh human --json` parsed: a list of item dicts, or None when the script fails or prints
    something that is not a JSON list (never an exception)."""
    try:
        r = read_ticket_sh(cfg, ["human", "--json"], timeout=timeout)
    except ZouinifyError:
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "")
    except ValueError:
        return None
    return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else None


def read_board_json(cfg, timeout: float = 60) -> dict | None:
    """`ticket.sh board --json` parsed: a dict with a `tickets` list, or None when the project's script
    does not have that mode yet (it prints its usage or the text board, which is not JSON) or fails."""
    try:
        r = read_ticket_sh(cfg, ["board", "--json"], timeout=timeout)
    except ZouinifyError:
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tickets"), list) else None
