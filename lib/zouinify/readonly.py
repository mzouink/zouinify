"""The read-only facade the panel is allowed to use. lib/zouinify/panel/ may import this module and
nothing else of zouinify's internals: never zouinify.tmux, zouinify.launch, zouinify.tickets directly, nor
any process-spawning primitive (scripts/check_panel_guard.sh and the AST allowlist enforce it).
Everything here observes; nothing creates, kills, types, moves or deploys. The modules it delegates to are
imported inside the functions so that this module re-exports none of them as attributes."""
from __future__ import annotations

import json
import os

from zouinify import ZouinifyError

READ_ONLY_TICKET_SH = {"human": {"--count", "--json"}, "board": {"--json", "--slow"}}


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
