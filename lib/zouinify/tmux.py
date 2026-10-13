"""Thin wrappers around the tmux binary ($ZOUINIFY_TMUX overrides the name; the tests put a fake on PATH).

Only zouinify.launch may call new_session / kill_session. `send_keys` exists since T-0181 for ONE caller, the
terminal page (zouinify.term.server), which forwards the human's own keystrokes after the passphrase unlock into
worker panes only; nothing else types into a pane. The panel never imports this module: it reads through
zouinify.readonly, and scripts/check_panel_guard.sh fails CI on any other path.

Requires tmux 3.2+ (`new-session -e` sets the session's environment); an older tmux fails loudly.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time

from zouinify import ZouinifyError
from zouinify.config import DEFAULT_MODEL_RE, MODEL_WORDS

TMUX = os.environ.get("ZOUINIFY_TMUX", "tmux")
_RE_CACHE: dict = {}


class TmuxError(ZouinifyError):
    pass


class DuplicateSession(TmuxError):
    """`new-session` found the name already taken: somebody won the race between the check and the launch."""


def _run(args: list, timeout: float = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([TMUX, *args], capture_output=True, text=True, errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise TmuxError(f"{TMUX}: not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise TmuxError(f"tmux {args[0]} timed out after {timeout} s") from e


def has_session(name: str) -> bool:
    """Exact-name test (`=name`: tmux's -t matching is a prefix/fuzzy match otherwise)."""
    return _run(["has-session", "-t", f"={name}"]).returncode == 0


def new_session(name: str, cwd: str, env: dict, argv: list) -> None:
    """`tmux new-session -d -s <name> -c <cwd> -e K=V ... <argv>` (tmux 3.2+)."""
    cmd = ["new-session", "-d", "-s", name, "-c", cwd]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    r = _run(cmd + list(argv))
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "duplicate session" in msg:
            raise DuplicateSession(f"tmux session {name} already exists")
        if "unknown flag" in msg or "usage:" in msg:
            msg += "  (tmux 3.2+ is required for new-session -e)"
        raise TmuxError(f"tmux new-session {name} failed: {msg}")


def kill_session(name: str) -> None:
    r = _run(["kill-session", "-t", f"={name}"])
    if r.returncode != 0:
        raise TmuxError(f"tmux kill-session {name} failed: {(r.stderr or r.stdout).strip()}")


def capture_pane(name: str) -> str:
    """The visible screen of the session's active pane (no scrollback: Claude runs on the alternate screen).
    The target is `=name:` -- for a target-pane, a bare `=name` is read as a pane name, not an exact session."""
    r = _run(["capture-pane", "-p", "-t", f"={name}:"])
    return r.stdout if r.returncode == 0 else ""


PANE_FIELDS = ("pane_id", "pane_active", "window_active", "pane_current_command", "pane_pid", "window_index", "pane_index")
PANE_ID_RE = re.compile(r"^%\d+$")


def panes_of(name: str) -> list:
    """EVERY pane of the session across all its windows (`list-panes -s`), as [{pane_id, active, window_active, command,
    pane_pid, window, index}]; [] when no such session. The terminal page (T-0181) refuses a session with more than
    one pane: a check on "the" pane and a send to "the" pane must name the same %pane_id."""
    r = _run(["list-panes", "-s", "-t", f"={name}", "-F", "\t".join("#{%s}" % f for f in PANE_FIELDS)])
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(PANE_FIELDS):
            continue
        rows.append({"pane_id": parts[0], "active": parts[1] == "1", "window_active": parts[2] == "1", "command": parts[3],
                     "pane_pid": parts[4], "window": parts[5], "index": parts[6]})
    return rows


def pane_command(name: str):
    """The current command of the session's pane when it has exactly one (`claude`, `node`, `bash`, ...); None when
    there is no such session; "" when it has several panes (ambiguous: the terminal page refuses those)."""
    panes = panes_of(name)
    if not panes:
        return None
    return panes[0].get("command") or "" if len(panes) == 1 else ""


def _pane_target(pane_id: str) -> str:
    if not PANE_ID_RE.match(str(pane_id)):
        raise TmuxError(f"not a tmux pane id: {pane_id!r}")
    return str(pane_id)


def capture_pane_id(pane_id: str) -> str:
    """The visible screen of ONE pane, by its server-wide %id (never a session or window name)."""
    r = _run(["capture-pane", "-p", "-t", _pane_target(pane_id)])
    return r.stdout if r.returncode == 0 else ""


def send_keys(pane_id: str, ops: list) -> int:
    """Type into ONE pane by its %id: the terminal page's one write (T-0181), the human's own keystrokes after the
    unlock, into the pane zouinify.term.targets inspected a moment ago. Each op is ("t", text) -- sent literally with -l,
    after `--` so a leading dash is text -- or ("k", KeyName) from zouinify.term.keys's allowlist. One tmux call per op,
    in order; returns the number sent; a failure mid-batch raises TmuxError with .sent = how many went before it."""
    target = _pane_target(pane_id)
    n = 0
    for kind, value in ops:
        if kind == "t":
            r = _run(["send-keys", "-t", target, "-l", "--", value])
        else:
            r = _run(["send-keys", "-t", target, value])
        if r.returncode != 0:
            err = TmuxError(f"tmux send-keys into {target} failed after {n} op(s): {(r.stderr or r.stdout).strip()}")
            err.sent = n
            raise err
        n += 1
    return n


def list_panes() -> list:
    r = _run(["list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}\t#{pane_current_command}"])
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"session": parts[0], "pane_pid": parts[1], "command": parts[2]})
    return rows


def _compiled(pattern: str | None):
    pat = pattern or DEFAULT_MODEL_RE
    rx = _RE_CACHE.get(pat)
    if rx is None:
        try:
            rx = re.compile(pat)
        except re.error:
            rx = re.compile(DEFAULT_MODEL_RE)
        _RE_CACHE[pat] = rx
    return rx


def model_on_screen(text: str, pattern: str | None = None) -> str | None:
    """The model label in the status bar (group 1 of `model_re`, e.g. `Fable 5.1`), the last match on the
    screen, or None when no status bar is visible."""
    last = None
    for m in _compiled(pattern).finditer(text or ""):
        last = m
    if not last:
        return None
    label = last.group(1) if last.re.groups else last.group(0)
    return label.strip() or None


def model_word(label: str | None) -> str | None:
    """`Fable 5.1` -> `fable`."""
    return label.split()[0].lower() if label else None


def alias_of(model: str) -> str | None:
    """The family word of a configured model (`fable`, `claude-opus-5-5` -> `opus`), or None if unknown."""
    low = (model or "").lower()
    for w in MODEL_WORDS:
        if w in low:
            return w
    return None


def wait_for_model(name: str, timeout_s: float, poll_s: float = 1.0, pattern: str | None = None) -> str | None:
    """Poll the status bar until a model label shows, up to timeout_s; None if it never does."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        label = model_on_screen(capture_pane(name), pattern)
        if label:
            return label
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_s, remaining))


def launch_line(name: str, cwd: str, env: dict, argv: list, brief_path: str | None = None) -> str:
    """The exact shell line a human could paste from anywhere; with brief_path (absolute) the prompt is
    shown as "$(cat <brief>)" instead of its full text."""
    parts = ["tmux", "new-session", "-d", "-s", name, "-c", cwd]
    for k, v in env.items():
        parts += ["-e", f"{k}={v}"]
    quoted = [shlex.quote(p) for p in parts]
    body = list(argv)
    if brief_path is not None and body:
        quoted += [shlex.quote(p) for p in body[:-1]] + [f'"$(cat {shlex.quote(brief_path)})"']
    else:
        quoted += [shlex.quote(p) for p in body]
    return " ".join(quoted)
