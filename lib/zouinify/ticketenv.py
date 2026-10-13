"""`zouinify config --env [--write [PATH]]`: tickets/ticket.env, the flat KEY=value file the vendored ticket.sh core PARSES
(never sources) for this project's values (T-0188, the grammar agreed with the core's owner for T-0186).

Printed to stdout by default. `--write` writes it to <root>/<tickets dir>/ticket.env or the path given, and refuses when that
path lies in a repository's MAIN worktree (nobody develops there, and the file is tracked: a ticket commits it from a ticket
worktree). The writer never commits or pushes.

Grammar the core reads: `^[A-Z][A-Z0-9_]*=.*$` lines, the value literal with one pair of surrounding double quotes removed,
comments and blank lines ignored. Lists are space-separated. The repo list keeps the configured names; a per-repo key is
REPO_<NAME>_PATH|_TRUNK|_PROD with NAME = the name uppercased and `-` turned into `_`, so every key fits the grammar.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

from zouinify import ZouinifyError, __version__

REPO_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
BAD_VALUE_RE = re.compile(r'["\\]|[\x00-\x1f\x7f]')
KEYS = ("PROJECT", "TRUNK", "TICKETS_DIR", "WORKTREES", "REPOS", "PROTECTED_BRANCHES_EXTRA", "ASK_VERBS", "TICKET_TYPES",
        "DEPLOY_PLUGIN", "DEPLOY_KINDS")
HEADER_RE = re.compile(r"^# generated-from: \.ops/config\.yaml sha256 ([0-9a-f]{64})$", re.M)


def repo_key(name: str) -> str:
    return name.upper().replace("-", "_")


def config_sha256(cfg) -> str:
    with open(cfg.config_path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _value(key: str, v) -> str:
    v = "" if v is None else str(v)
    if BAD_VALUE_RE.search(v):
        raise ZouinifyError(f"{key}: a value for ticket.env may not contain a double quote, a backslash or a control character ({v!r})")
    return v


def render(cfg) -> str:
    """The file text. Every list is space-separated; every value is written as KEY="value"."""
    names = [str(r["name"]) for r in cfg.repos]
    bad = [n for n in names if not REPO_NAME_RE.match(n)]
    if bad:
        raise ZouinifyError("repos: a name for ticket.env is lowercase letters, digits, _ and - starting with a letter: " + ", ".join(bad))
    seen = {}
    for n in names:
        k = repo_key(n)
        if k in seen:
            raise ZouinifyError(f"repos: {seen[k]} and {n} both map to REPO_{k}_* in ticket.env; rename one")
        seen[k] = n
    for lst, where in ((cfg.ask_verbs, "tickets.ask_verbs"), (cfg.ticket_types, "tickets.types"), (cfg.deploy_kinds, "tickets.deploy_kinds"),
                       (cfg.protected_branches_extra, "tickets.protected_branches_extra")):
        if any(" " in x or not x for x in lst):
            raise ZouinifyError(f"{where}: ticket.env lists are space-separated, so an item may not contain a space or be empty")
    default = next((r for r in cfg.repos if r.get("default")), cfg.repos[0])
    pairs = [("PROJECT", cfg.name), ("TRUNK", default["trunk"]), ("TICKETS_DIR", cfg.tickets_dir), ("WORKTREES", cfg.worktrees),
             ("REPOS", " ".join(names)), ("PROTECTED_BRANCHES_EXTRA", " ".join(cfg.protected_branches_extra)),
             ("ASK_VERBS", " ".join(cfg.ask_verbs)), ("TICKET_TYPES", " ".join(cfg.ticket_types)),
             ("DEPLOY_PLUGIN", cfg.deploy_plugin), ("DEPLOY_KINDS", " ".join(cfg.deploy_kinds))]
    for r in cfg.repos:
        k = repo_key(str(r["name"]))
        pairs += [(f"REPO_{k}_PATH", r["path"]), (f"REPO_{k}_TRUNK", r["trunk"]), (f"REPO_{k}_PROD", r.get("prod") or "")]
    lines = [f"# generated-from: .ops/config.yaml sha256 {config_sha256(cfg)}",
             f"# generated-by: zouinify {__version__} -- regenerate with: zouinify config --env --write",
             "# Parsed by scripts/ticket.sh (never sourced): KEY=\"value\" lines, lists space-separated, exported TICKET_<KEY> overrides win."]
    lines += [f'{k}="{_value(k, v)}"' for k, v in pairs]
    return "\n".join(lines) + "\n"


def worktree_kind(directory: str) -> str:
    """'main' for a repository's main worktree, 'linked' for a `git worktree add` checkout, 'none' outside git."""
    try:
        r = subprocess.run(["git", "-C", directory, "rev-parse", "--git-dir", "--git-common-dir"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "none"
    if r.returncode != 0:
        return "none"
    parts = r.stdout.split("\n")
    if len(parts) < 2:
        return "none"
    git_dir = os.path.realpath(os.path.join(directory, parts[0]))
    common = os.path.realpath(os.path.join(directory, parts[1]))
    return "main" if git_dir == common else "linked"


def cmd_env(cfg, write=None, out=None) -> int:
    """write: None = print to stdout; "" = the default path under the project; a string = that path."""
    out = out or sys.stdout
    text = render(cfg)
    if write is None:
        out.write(text)
        return 0
    given = os.path.expanduser(write or os.path.join(cfg.root, cfg.tickets_dir, "ticket.env"))
    parent = os.path.realpath(os.path.dirname(os.path.abspath(given)))      # the directory resolved, the file itself NOT (a link is refused)
    path = os.path.join(parent, os.path.basename(given))
    if not os.path.isdir(parent):
        raise ZouinifyError(f"{parent} is not a directory; create the tickets directory first")
    if os.path.islink(path):
        raise ZouinifyError(f"{path} is a symlink; refusing to write through it")
    if os.path.lexists(path) and not os.path.isfile(path):
        raise ZouinifyError(f"{path} exists and is not a regular file")
    kind = worktree_kind(parent)
    if kind == "main":
        raise ZouinifyError(f"{path} lies in a repository's main worktree, where a write would dirty it; run this from a ticket "
                            "worktree (zouinify config --env --write there), or print it (no --write) and commit it through a ticket")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            if fh.read() == text:
                print(f"unchanged  {path}", file=out)
                return 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote      {path}  ({'linked worktree' if kind == 'linked' else 'outside git'}; a ticket commits it, this command never does)", file=out)
    return 0
