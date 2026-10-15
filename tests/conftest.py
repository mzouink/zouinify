"""Session start: reap the test servers a killed run left behind (T-0225), failing closed before anything is deleted.

The process-side tests start real `zouinify term start --foreground` / `panel start --foreground` processes for a throwaway
world under the temp dir (`zouinify-test-*`, `zouinify-init-*`); they stop them in tearDown, but a run that dies (a killed
pytest, a lost terminal) leaves the detached server running for hours with its world beside it. Every world writes the pid
of the pytest that owns it to `<world>/.test-runner`. At session start, every test server of this user is looked at and:

- reaped (stopped, its world removed, printed) only when its world is a plain test directory directly under the temp dir
  (the project's real path is `<tempdir>/zouinify-(test|init)-*/<child>`, given without `..` and not through a symlinked
  world) AND the marker names a pid that is provably dead; the pid is re-read from /proc right before each signal, so a
  reused pid is never hit, and only that world is removed, never its parent;
- kept, and printed with the reason, on any doubt: no marker (a run of test code older than the marker, maybe another
  session's, still in progress), an unreadable or garbled marker, a runner that is alive or cannot be checked, a path that
  is not a plain test world. A check that gates `rm -rf` keeps and explains on any doubt (the desk's rule).

Only this user's processes are looked at (the others cannot be signalled). A server that is reaped but will not die ends
the session with a message naming it.
"""
import os
import re
import shutil
import signal
import tempfile
import time

import pytest

TMP = os.path.realpath(tempfile.gettempdir())
WORLD_RE = re.compile(r"^zouinify-(test|init)-[A-Za-z0-9_-]+$")
LOOKS_LIKE_A_TEST_WORLD = re.compile(r"zouinify-(test|init)-")
RUNNER_MARKER = ".test-runner"


def _cmdline(pid) -> list:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _uid(pid):
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _alive(pid: int) -> bool:
    """True unless the pid provably does not exist (a pid of another user that we may not signal counts as alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True


def _project_of(args: list):
    """The --project value of a zouinify server's command line, or None when the line is not one (`cli.py`, `start`,
    `--foreground`, `--project <path>`; the uid is checked by the callers)."""
    if "--foreground" not in args or "start" not in args or "--project" not in args:
        return None
    if not any(a.endswith("cli.py") for a in args):
        return None
    i = args.index("--project")
    return args[i + 1] if i + 1 < len(args) else None


def same_server(pid: int, project: str) -> bool:
    """Re-read from /proc now: pid is still this user's zouinify server for exactly that --project path."""
    return _uid(pid) == os.getuid() and _project_of(_cmdline(pid)) == project


def world_of(project: str):
    """(world, "") for a server's --project whose real path is `<tempdir>/zouinify-(test|init)-*/<child>`, reached without
    `..` and not through a symlinked world; (None, reason) for anything else. The real path is what is checked, so a
    path under another temp dir, a world deeper than one level, the bare world with no child, or an escape through `..`
    is refused; the world is the real path, so a reap removes exactly one test directory under the temp dir and never
    its parent."""
    given = str(project or "").rstrip("/")
    if not given or not os.path.isabs(given):
        return None, "not an absolute path"
    if os.path.normpath(given) != given:
        return None, "the path has `..` or redundant separators"
    real = os.path.realpath(given)
    world = os.path.dirname(real)
    if not os.path.basename(real) or os.path.dirname(world) != TMP:
        return None, f"not <world>/<project> with the world exactly one directory under {TMP}"
    if not WORLD_RE.match(os.path.basename(world)):
        return None, "not a zouinify test world name"
    if os.path.islink(os.path.dirname(given)) or os.path.islink(given):
        return None, "the world or the project is a symlink"
    if not os.path.isdir(world) or os.path.islink(world):
        return None, "the world is not a plain directory"
    return world, ""


def runner_of(world: str):
    """("dead", pid) when the marker names a pid that provably does not exist; ("alive", pid) when it does; ("none", None)
    without a readable marker naming a possible pid (1 .. 2**22, Linux's largest pid_max)."""
    try:
        with open(os.path.join(world, RUNNER_MARKER), encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return "none", None
    if not 0 < pid <= 2 ** 22:
        return "none", None
    return ("alive" if _alive(pid) else "dead"), pid


def test_servers() -> list:
    """[(pid, project, world or None, verdict, reason)] for every zouinify test server of this user (a server whose
    --project mentions a test world name anywhere; the real panel's never does); verdict is "reap" or "keep"."""
    out = []
    if not os.path.isdir("/proc"):
        return out
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        project = _project_of(_cmdline(pid))
        if project is None or not LOOKS_LIKE_A_TEST_WORLD.search(project) or _uid(pid) != me:
            continue
        world, why = world_of(project)
        if world is None:
            out.append((int(pid), project, None, "keep", why))
            continue
        state, runner = runner_of(world)
        if state == "dead":
            out.append((int(pid), project, world, "reap", f"its runner {runner} is dead"))
        elif state == "alive":
            out.append((int(pid), project, world, "keep", f"its runner {runner} is alive (a run in progress)"))
        else:
            out.append((int(pid), project, world, "keep", "no readable runner marker: maybe a run of older test code, still in progress"))
    return out


def leftover_test_servers() -> list:
    """[(pid, project, world)] of the servers that may be reaped."""
    return [(pid, project, world) for pid, project, world, verdict, _ in test_servers() if verdict == "reap"]


def _signal(pid: int, project: str, sig) -> bool:
    """Send sig only if the pid, re-read now, is still that server."""
    if not same_server(pid, project):
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def reap_leftovers(out=None) -> int:
    n = 0
    for pid, project, world, verdict, why in test_servers():
        if verdict != "reap":
            print(f"kept a zouinify test server: pid {pid} ({project}): {why}", file=out)
            continue
        if not _signal(pid, project, signal.SIGTERM):
            print(f"kept pid {pid}: no longer the server for {project}", file=out)
            continue
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            _signal(pid, project, signal.SIGKILL)
            for _ in range(20):                        # a process paged in from a slow disk can outlive the kill for a moment
                if not same_server(pid, project):
                    break
                time.sleep(0.1)
        if same_server(pid, project):
            pytest.exit(f"a leftover zouinify test server (pid {pid}, project {project}) cannot be stopped: stop it yourself, then rerun", returncode=3)
        # the delete, re-checked at the moment of acting: still exactly that world, still a dead runner's
        if not os.path.exists(world):
            print(f"stopped pid {pid} ({project}): its world was already gone", file=out)
            n += 1
            continue
        if world_of(project)[0] != world or runner_of(world)[0] != "dead" or os.path.islink(world):
            print(f"stopped pid {pid} ({project}) but kept its world: it changed under us", file=out)
            n += 1
            continue
        shutil.rmtree(world, ignore_errors=True)
        print(f"reaped a leftover test server from a dead run: pid {pid} ({project}, {why}); its world directory removed", file=out)
        n += 1
    return n


def pytest_sessionstart(session):
    reap_leftovers()
