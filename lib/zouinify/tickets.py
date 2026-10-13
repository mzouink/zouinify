"""Read-only access to the project's tickets: front matter, the FREEZE file, counts, and the project's
own scripts/ticket.sh for its read-only output modes (`human --count`, `human --json`, `board --json`).

The lifecycle is ticket.sh's. Nothing here writes a ticket; the only mutating call anywhere in zouinify
is `ticket.sh log` from `launch --receipt`, run on the desk's behalf and signed by its TICKET_AS.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess

from zouinify import ZouinifyError

ID_RE = re.compile(r"^T-\d{4}$")
TERMINAL = ("done", "dropped")
FREEZE_FILE_NAME = "FREEZE"


def check_id(tid: str) -> str:
    if not ID_RE.match(tid or ""):
        raise ZouinifyError(f"{tid!r} is not a ticket id (T-xxxx)")
    return tid


def tickets_dir(cfg) -> str:
    return cfg.path(cfg.tickets_dir)


def ticket_files(cfg) -> list:
    return sorted(glob.glob(os.path.join(tickets_dir(cfg), "T-[0-9][0-9][0-9][0-9]-*.md")))


def file_of(cfg, tid: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(tickets_dir(cfg), f"{tid}-*.md")))
    return hits[0] if hits else None


def front_matter(path: str) -> dict:
    """The `key: value` lines between the first two `---` lines; a trailing `# comment` is dropped,
    the first occurrence of a key wins (the same rules as ticket.sh's `field`)."""
    out: dict = {}
    inside = False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            s = line.rstrip("\n")
            if i == 0 and s.strip() == "---":
                inside = True
                continue
            if not inside:
                break
            if s.strip() == "---":
                break
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", s)
            if m and m.group(1) not in out:
                out[m.group(1)] = re.sub(r"\s*#.*$", "", m.group(2)).strip()
    return out


def slug_of(path: str) -> str:
    """`T-0155-h6-sitting-protocol.md` -> `t0155-h6-sitting-protocol` (ticket.sh's worktree/branch slug)."""
    base = os.path.basename(path)
    if base.endswith(".md"):
        base = base[:-3]
    return re.sub(r"^T-(\d+)-", r"t\1-", base)[:40].rstrip("-")


def worker_names(cfg, path: str) -> tuple:
    """(session name, tmux name) for a worker, from the config's patterns and the ticket's slug."""
    slug = slug_of(path)
    m = re.match(r"^t(\d+)-?(.*)$", slug)
    num, rest = (m.group(1), m.group(2)) if m else ("", slug)
    w = cfg.sessions["worker"]
    return (w.name.replace("{num}", num).replace("{slug}", rest),
            w.tmux.replace("{num}", num).replace("{slug}", rest))


def freeze_path(cfg) -> str:
    return os.path.join(tickets_dir(cfg), FREEZE_FILE_NAME)


def freeze_on(cfg) -> bool:
    return os.path.isfile(freeze_path(cfg))


def _freeze_text(cfg) -> str:
    try:
        with open(freeze_path(cfg), encoding="utf-8") as fh:
            return "\n".join(re.sub(r"#.*$", "", line) for line in fh)
    except OSError:
        return ""


def freeze_ids(cfg) -> list:
    return sorted(set(re.findall(r"T-\d{4}", _freeze_text(cfg))))


def freeze_lists(cfg, tid: str) -> bool:
    return tid in freeze_ids(cfg)


def counts(cfg) -> tuple:
    """({status: n}, number of open P0 tickets) from the front matter alone (no git, sub-second)."""
    by: dict = {}
    p0 = 0
    for f in ticket_files(cfg):
        fm = front_matter(f)
        st = fm.get("status", "")
        by[st] = by.get(st, 0) + 1
        if fm.get("severity") == "P0" and st not in TERMINAL:
            p0 += 1
    return by, p0


def ticket_sh(cfg, args: list, capture: bool = True, timeout: float = 180) -> subprocess.CompletedProcess:
    """Run the project's ticket.sh from the project root, with the caller's environment (TICKET_AS)."""
    script = cfg.path(cfg.tickets_script)
    if not os.access(script, os.X_OK):
        raise ZouinifyError(f"{script}: not found or not executable (tickets.script in .ops/config.yaml)")
    try:
        return subprocess.run([script, *args], cwd=cfg.root, capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ZouinifyError(f"{cfg.tickets_script} {' '.join(args)} timed out after {timeout:g} s") from e


def human_count(cfg) -> int | None:
    """`ticket.sh human --count`, or None when the script cannot answer."""
    try:
        r = ticket_sh(cfg, ["human", "--count"], timeout=120)
    except ZouinifyError:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"(\d+)\s*$", r.stdout or "")
    return int(m.group(1)) if m else None
