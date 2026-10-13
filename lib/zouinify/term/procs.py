"""/proc reads for the terminal page's pane inspection (T-0181, security review B2; hardened in T-0183): the real
executable of every process on the pane's tty or under the pane's shell, and the tty's foreground process group.

`pane_current_command` is argv[0] of the tty's foreground process, so a tmux client of the desk's session started by the
worker itself with `exec -a claude` would still read "claude" while that client forwards the human's keys elsewhere. Hence:
no process whose /proc/<pid>/exe is tmux may hold the pane's tty or sit under the pane's pid, and the tty's foreground
process group (tpgid) must be the registered agent's own group (pgrp). "Is tmux" is decided three ways (T-0183): the
executable's name, its identity (same st_dev and st_ino as the tmux zouinify runs: a hard link under any name) and its
content (same size and sha256: a byte-identical copy, `cp /usr/bin/tmux ~/x`); a renamed copy passed the name check alone.
Tests point ZOUINIFY_PROC at a fake tree (<pid>/stat, <pid>/exe symlinks) and name the pane tty /dev/pts/<n> where <n>
is the tty number."""
from __future__ import annotations

import hashlib
import os
import shutil

from zouinify.tmux import TMUX

HASH_CHUNK = 1 << 20
HASH_CACHE_MAX = 64
_HASHES: dict = {}                                   # (st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns) -> sha256 hex

HOW_NAMED = "named tmux"
HOW_LINKED = "the tmux binary under another name"
HOW_COPIED = "a copy of the tmux binary"


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


def all_stats() -> dict:
    """{pid: stat(pid)} for every process /proc lists right now."""
    stats = {}
    for pid in all_pids():
        s = stat(pid)
        if s:
            stats[pid] = s
    return stats


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


def real_tmux() -> dict | None:
    """The tmux binary zouinify itself runs ($ZOUINIFY_TMUX, else `tmux` on PATH), through its symlinks: {path, dev, ino,
    size, mtime}; None when there is none. Looked up on every call: the host may upgrade tmux while the page runs."""
    path = shutil.which(TMUX)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"path": os.path.realpath(path), "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime": st.st_mtime_ns}


def sha256_of(path, st=None) -> str | None:
    """sha256 of a file, cached by (dev, ino, size, mtime, ctime) so one binary is read once; None when unreadable."""
    try:
        st = st or os.stat(path)
    except OSError:
        return None
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)   # ctime: `touch -r` cannot reset it
    digest = _HASHES.get(key)
    if digest is None:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
                    h.update(chunk)
        except OSError:
            return None
        digest = h.hexdigest()
        if len(_HASHES) >= HASH_CACHE_MAX:
            _HASHES.clear()
        _HASHES[key] = digest
    return digest


def exe_is_tmux(pid) -> str | None:
    """How the real executable of `pid` is tmux, or None when it is not: HOW_NAMED (its name is tmux), HOW_LINKED (the
    very file zouinify's tmux resolves to, same st_dev and st_ino, whatever the name) or HOW_COPIED (same size and the
    same sha256). /proc/<pid>/exe is read as the file itself, which a same-uid process lets us stat and hash even after
    a rename or an unlink; another uid's process is unreadable and counts as not tmux (it cannot hold our terminal
    device either). Only a candidate of exactly tmux's size is hashed, so a 100 MB node binary is never read."""
    target = exe(pid)
    if target is None:
        return None
    if os.path.basename(target) == "tmux":
        return HOW_NAMED
    link = os.path.join(proc_root(), str(int(pid)), "exe")
    try:
        st = os.stat(link)
    except OSError:
        return None
    real = real_tmux()
    if not real or real["size"] <= 0:
        return None
    if (st.st_dev, st.st_ino) == (real["dev"], real["ino"]):
        return HOW_LINKED
    if st.st_size == real["size"]:
        want = sha256_of(real["path"])
        if want and sha256_of(link, st) == want:
            return HOW_COPIED
    return None


def descendants(pane_pid, stats: dict | None = None) -> set:
    """The pane's pid and every process under it, by /proc ppid."""
    stats = all_stats() if stats is None else stats
    under: set = {int(pane_pid)}
    changed = True
    while changed:
        changed = False
        for pid, s in stats.items():
            if pid not in under and s["ppid"] in under:
                under.add(pid)
                changed = True
    return under


def tmux_clients(tty_nr: int, pane_pid: int, stats: dict | None = None) -> list:
    """[(pid, how)] of every process whose real executable is tmux (exe_is_tmux) and which either holds the pane's tty or
    descends from the pane's pid; sorted by pid."""
    stats = all_stats() if stats is None else stats
    under = descendants(pane_pid, stats)
    found = []
    for pid, s in stats.items():
        if pid == int(pane_pid):
            continue
        if s["tty_nr"] == tty_nr or pid in under:
            how = exe_is_tmux(pid)
            if how:
                found.append((pid, how))
    return sorted(found)


def foreground_pgid(pane_pid) -> int | None:
    """The foreground process group of the pane's tty, read from the pane's own process."""
    s = stat(pane_pid)
    return s["tpgid"] if s else None


def pgid(pid) -> int | None:
    s = stat(pid)
    return s["pgrp"] if s else None
