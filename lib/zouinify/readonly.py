"""The read-only facade the panel is allowed to use. lib/zouinify/panel/ may import this module, the
ticket readers, the status helpers, the agents listing and the registry reader -- never zouinify.tmux,
zouinify.launch or any process-spawning primitive (scripts/check_panel_guard.sh enforces the allowlist).
Everything here observes; nothing creates, kills, types, moves or deploys."""
from __future__ import annotations

import os
import subprocess

from zouinify import ZouinifyError
from zouinify import tickets as _tk
from zouinify import tmux as _tmux

READ_ONLY_TICKET_SH = {"human": {"--count", "--json"}, "board": {"--json", "--slow"}}


def capture_pane(name: str) -> str:
    """The visible screen of a tmux session's active pane (capture-pane -p; reads, never types)."""
    return _tmux.capture_pane(name)


def list_panes() -> list:
    return _tmux.list_panes()


def has_session(name: str) -> bool:
    return _tmux.has_session(name)


def model_on_screen(text: str, pattern: str | None = None) -> str | None:
    return _tmux.model_on_screen(text, pattern)


def model_word(label: str | None) -> str | None:
    return _tmux.model_word(label)


def alias_of(model: str) -> str | None:
    return _tmux.alias_of(model)


def trunk_sha(root: str) -> str:
    """`git rev-parse --short HEAD` of the project root; "" when unavailable."""
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_ticket_sh(cfg, args: list, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run the project's ticket.sh for a read-only output mode only: `human --count|--json`, `board --json
    [--slow]`. Anything else is refused here, before the script is invoked."""
    if not args or args[0] not in READ_ONLY_TICKET_SH:
        raise ZouinifyError(f"read_ticket_sh: {' '.join(args) or '(nothing)'} is not a read-only ticket.sh command")
    bad = [a for a in args[1:] if a not in READ_ONLY_TICKET_SH[args[0]]]
    if bad:
        raise ZouinifyError(f"read_ticket_sh: ticket.sh {args[0]} takes only {sorted(READ_ONLY_TICKET_SH[args[0]])} here, not {bad}")
    return _tk.ticket_sh(cfg, list(args), timeout=timeout)


def proc_parent(pid: int):
    """The parent pid from /proc, for mapping a Claude session's pid to its tmux pane."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().split(")")[-1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def path_exists(path: str) -> bool:
    return os.path.exists(path)
