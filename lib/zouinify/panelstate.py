"""The panel server's own two files: its pid file (<state_dir>/panel.json) and its log
(<state_dir>/server.log). This is the second and last zouinify module the panel package may import (with
zouinify.readonly; scripts/check_panel_ast.py). It writes ONLY those two files, at paths derived from the
config and never taken from an argument; it never touches a ticket, a session, the registry or the
user-level projects list (that one is zouinify.panelctl's, on the process side)."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile

from zouinify import __version__

def version() -> str:
    return __version__


def pid() -> int:
    return os.getpid()


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def url(cfg, port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or cfg.panel['port']}/"


def panel_json_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "panel.json")


def server_log_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "server.log")


def same_root(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def log(cfg, text: str) -> None:
    """Append one line to the server log; never raises."""
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(server_log_path(cfg), "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


def _write_json(path: str, obj, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_panel_json(cfg, port: int, pid_: int, started_at: str) -> None:
    _write_json(panel_json_path(cfg), {"port": port, "pid": pid_, "root": cfg.root, "project": cfg.name, "bind": "loopback",
                                       "url": url(cfg, port), "started_at": started_at, "version": __version__})


def read_panel_json(cfg):
    try:
        with open(panel_json_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
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
