"""`zouinify config --env [--write [PATH]]`: tickets/ticket.env, the flat KEY=value file the vendored ticket.sh core PARSES
(never sources) for this project's values (T-0188, the grammar agreed with the core's owner for T-0186).

Printed to stdout by default. `--write` writes it to <root>/<tickets dir>/ticket.env or the path given, and refuses when that
path lies in a repository's MAIN worktree (nobody develops there, and the file is tracked: a ticket commits it from a ticket
worktree). The writer never commits or pushes.

Grammar the core reads (desk decision, T-0188 review): bare `KEY=value` lines, no quotes, no escapes; every value is made of
the alphabet [A-Za-z0-9._/@:+ -] (the space only as the list separator), anything else is refused here and by the parser,
so the file needs no quoting. It is for parsing only: sourced by a shell, a list value `KEY=a b` would run `b` as a command
(the parser never does that; the desk has the finding). Comments and blank lines are ignored. The repo
list keeps the configured names; a per-repo key is REPO_<NAME>_PATH|_TRUNK|_PROD with NAME = the name uppercased and `-`
turned into `_`, so every key fits `^[A-Z][A-Z0-9_]*$`.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

from zouinify import ZouinifyError, __version__

REPO_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
VALUE_RE = re.compile(r"^[A-Za-z0-9._/@:+ -]*$")           # the whole alphabet a value may use; a list item may not hold the space
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
    if not VALUE_RE.match(v):
        raise ZouinifyError(f"{key}: a ticket.env value is made of letters, digits and . _ / @ : + - (space only between list items); "
                            f"refused: {v!r}")
    return v


def render(cfg) -> str:
    """The file text. Every list is space-separated; every value is written bare, KEY=value, from the allowlisted alphabet.
    REPOS names the default repo FIRST (the core takes the first name as the repo the tickets live in), then the others in
    config order."""
    default = next((r for r in cfg.repos if r.get("default")), cfg.repos[0])
    repos = [default] + [r for r in cfg.repos if r is not default]
    names = [str(r["name"]) for r in repos]
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
    pairs = [("PROJECT", cfg.name), ("TRUNK", default["trunk"]), ("TICKETS_DIR", cfg.tickets_dir), ("WORKTREES", cfg.worktrees),
             ("REPOS", " ".join(names)), ("PROTECTED_BRANCHES_EXTRA", " ".join(cfg.protected_branches_extra)),
             ("ASK_VERBS", " ".join(cfg.ask_verbs)), ("TICKET_TYPES", " ".join(cfg.ticket_types)),
             ("DEPLOY_PLUGIN", cfg.deploy_plugin), ("DEPLOY_KINDS", " ".join(cfg.deploy_kinds))]
    for r in repos:
        k = repo_key(str(r["name"]))
        pairs += [(f"REPO_{k}_PATH", r["path"]), (f"REPO_{k}_TRUNK", r["trunk"]), (f"REPO_{k}_PROD", r.get("prod") or "")]
    lines = [f"# generated-from: .ops/config.yaml sha256 {config_sha256(cfg)}",
             f"# generated-by: zouinify {__version__} -- regenerate with: zouinify config --env --write",
             "# Parsed by scripts/ticket.sh (never sourced): bare KEY=value lines, values from [A-Za-z0-9._/@:+ -], lists space-separated,",
             "# exported TICKET_<KEY> overrides win."]
    lines += [f"{k}={_value(k, v)}" for k, v in pairs]
    return "\n".join(lines) + "\n"


def worktree_kind(directory: str) -> str:
    """'main' for a repository's main worktree (a bare repository and a submodule checkout count as main: nothing is
    written there either), 'linked' for a `git worktree add` checkout, 'none' only when git itself says the directory
    is not in a repository. Any other failure (no git, a timeout, "dubious ownership" on a shared filesystem, odd
    output) raises: the check fails CLOSED, because the main-worktree refusal rests on it. GIT_DIR/GIT_WORK_TREE and
    friends are dropped from the environment so a caller's variables cannot redirect the answer."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", directory, "rev-parse", "--is-bare-repository", "--git-dir", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise ZouinifyError(f"could not ask git whether {directory} is a main worktree ({e}); refusing to write") from None
    if r.returncode != 0:
        if "not a git repository" in r.stderr.lower():
            return "none"
        raise ZouinifyError(f"git could not classify {directory} (exit {r.returncode}: {r.stderr.strip()}); refusing to write")
    parts = r.stdout.split("\n")
    if len(parts) < 3:
        raise ZouinifyError(f"unexpected git output for {directory}; refusing to write")
    if parts[0].strip() == "true":
        return "main"
    git_dir = os.path.realpath(os.path.join(directory, parts[1]))
    common = os.path.realpath(os.path.join(directory, parts[2]))
    return "main" if git_dir == common else "linked"


def write_atomic(path: str, text: str) -> None:
    """A temp file beside the target, then os.replace: no partial file, and a hard link to a file elsewhere (another
    worktree's tracked copy) is broken rather than written through."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    write_atomic(path, text)
    print(f"wrote      {path}  ({'linked worktree' if kind == 'linked' else 'outside git'}; a ticket commits it, this command never does)", file=out)
    return 0
