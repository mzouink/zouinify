"""The panel server's own two files: its pid file and its log, at <project root>/.ops/state/panel.json and
<project root>/.ops/state/server.log -- paths FIXED in code (desk condition, T-0160): never a config key, never
an argument, and only for the loaded Config of the project (a look-alike object is refused). The state dir may
not be a symlink and the log is opened with O_NOFOLLOW. This is the second and last zouinify module the panel
package may import (with zouinify.readonly; scripts/check_panel_ast.py). It writes nothing else: not a ticket,
not a session, not the registry, not the user-level projects list (that one is zouinify.panelctl's)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile

from zouinify import __version__
from zouinify.config import Config

STATE_REL = (".ops", "state")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def version() -> str:
    return __version__


def pid() -> int:
    return os.getpid()


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def url(cfg, port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or cfg.panel['port']}/"


def same_root(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _check(cfg) -> None:
    if not isinstance(cfg, Config):
        raise TypeError("panelstate takes the loaded Config of the project, nothing else")


def state_dir(cfg) -> str:
    """<root>/.ops/state, fixed; refused when it (or .ops) is a symlink or resolves outside the root."""
    _check(cfg)
    root = os.path.realpath(cfg.root)
    d = os.path.join(root, *STATE_REL)
    for p in (os.path.join(root, STATE_REL[0]), d):
        if os.path.islink(p):
            raise OSError(f"{p} is a symlink: the panel refuses to write through it")
    if os.path.exists(d) and not os.path.realpath(d).startswith(root + os.sep):
        raise OSError(f"{d} resolves outside the project: refused")
    return d


def panel_json_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "panel.json")


def server_log_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "server.log")


def log(cfg, text: str) -> None:
    """Append one line to the server log (control characters escaped, symlinks never followed); never raises."""
    try:
        d = state_dir(cfg)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "server.log")
        safe = CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {safe}\n")
    except (OSError, TypeError):
        pass


def _write_json(path: str, obj) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    if os.path.islink(path):
        os.remove(path)       # never write through a planted symlink
    os.replace(tmp, path)


def write_panel_json(cfg, port: int, pid_: int, started_at: str) -> None:
    _write_json(panel_json_path(cfg), {"port": port, "pid": pid_, "root": cfg.root, "project": cfg.name, "bind": "loopback",
                                       "url": url(cfg, port), "started_at": started_at, "version": __version__})


def read_panel_json(cfg):
    try:
        with open(panel_json_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def remove_panel_json(cfg, pid_: int | None = None) -> None:
    d = read_panel_json(cfg)
    if d is None:
        return
    if pid_ is None or d.get("pid") == pid_:
        try:
            os.remove(panel_json_path(cfg))
        except OSError:
            pass


def hook_installed() -> tuple:
    """Whether ~/.claude/settings.json already names the zouinify hook (T-0163): read-only, never edited."""
    path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return "zouinify-hook" in fh.read(), path
    except OSError:
        return False, path
