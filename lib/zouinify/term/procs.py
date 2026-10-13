"""/proc reads for the terminal page's pane inspection (T-0181, security review B2): the real executable of every process
on the pane's tty or under the pane's shell, and the tty's foreground process group.

`pane_current_command` is argv[0] of the tty's foreground process, so a tmux client of the desk's session started by the
worker itself with `exec -a claude` would still read "claude" while that client forwards the human's keys elsewhere. Hence:
no process whose /proc/<pid>/exe resolves to tmux may hold the pane's tty or sit under the pane's pid, and the tty's
foreground process group (tpgid) must be the registered agent's own group (pgrp). Tests point ZOUINIFY_PROC at a fake tree
(<pid>/stat, <pid>/exe symlinks) and name the pane tty /dev/pts/<n> where <n> is the tty number."""
from __future__ import annotations

import os


def proc_root() -> str:
    return os.environ.get("ZOUINIFY_PROC") or "/proc"


def is_fake() -> bool:
    return proc_root() != "/proc"


def stat(pid) -> dict | None:
    """{pid, comm, state, ppid, pgrp, session, tty_nr, tpgid} of /proc/<pid>/stat, or None."""
    try:
        with open(os.path.join(proc_root(), str(int(pid)), "stat"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError, TypeError):
        return None
    try:
        head, _, rest = text.rpartition(")")       # comm may hold spaces and parentheses: split at the LAST ")"
        pid_s, _, comm = head.partition(" (")
        f = rest.split()
        return {"pid": int(pid_s), "comm": comm, "state": f[0], "ppid": int(f[1]), "pgrp": int(f[2]), "session": int(f[3]),
                "tty_nr": int(f[4]), "tpgid": int(f[5])}
    except (ValueError, IndexError):
        return None


def exe(pid) -> str | None:
    """The real executable of a process (readlink of /proc/<pid>/exe), " (deleted)" stripped; None when unreadable."""
    try:
        target = os.readlink(os.path.join(proc_root(), str(int(pid)), "exe"))
    except (OSError, ValueError, TypeError):
        return None
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    return target


def all_pids() -> list:
    try:
        return sorted(int(n) for n in os.listdir(proc_root()) if n.isdigit())
    except OSError:
        return []


def tty_nr_of(dev_path) -> int | None:
    """The tty_nr /proc/<pid>/stat uses for a terminal device: new_encode_dev of its major/minor. Under a fake /proc the
    pane tty is named /dev/pts/<n> and <n> is the number."""
    if not dev_path or not isinstance(dev_path, str):
        return None
    if is_fake():
        try:
            return int(os.path.basename(dev_path))
        except ValueError:
            return None
    try:
        st = os.stat(dev_path)
    except OSError:
        return None
    major, minor = os.major(st.st_rdev), os.minor(st.st_rdev)
    return (major << 8) | (minor & 0xFF) | ((minor & ~0xFF) << 12)


def is_tmux_exe(path) -> bool:
    return bool(path) and os.path.basename(str(path)) == "tmux"


def tmux_clients(tty_nr: int, pane_pid: int) -> list:
    """Pids whose real executable is tmux and which either hold the pane's tty or descend from the pane's pid."""
    stats = {}
    for pid in all_pids():
        s = stat(pid)
        if s:
            stats[pid] = s
    under: set = {int(pane_pid)}
    changed = True
    while changed:                                 # descendants of the pane pid, by ppid
        changed = False
        for pid, s in stats.items():
            if pid not in under and s["ppid"] in under:
                under.add(pid)
                changed = True
    found = []
    for pid, s in stats.items():
        if pid == int(pane_pid):
            continue
        if s["tty_nr"] == tty_nr or pid in under:
            if is_tmux_exe(exe(pid)):
                found.append(pid)
    return sorted(found)


def foreground_pgid(pane_pid) -> int | None:
    """The foreground process group of the pane's tty, read from the pane's own process."""
    s = stat(pane_pid)
    return s["tpgid"] if s else None


def pgid(pid) -> int | None:
    s = stat(pid)
    return s["pgrp"] if s else None
