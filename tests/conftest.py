"""Session start: reap the test servers a killed run left behind (T-0225).

The process-side tests start real `zouinify term start --foreground` / `panel start --foreground` processes for a throwaway
world under /tmp/zouinify-test-*; they stop them in tearDown, but a run that dies (a killed pytest, a lost terminal) leaves the
detached server running for hours, listening on a port, with the world directory beside it. Every world writes the pid of the
pytest process that owns it into `<world>/.test-runner`; a server whose world has no such marker, or whose runner is dead, is
nobody's and is stopped here before the session starts (printed, so a reviewer sees it); a server whose runner is alive is a
concurrent run of the same user and is left alone. Only this user's processes are looked at (the others cannot be signalled).
"""
import os
import shutil
import signal
import time

import pytest

WORLD_PREFIXES = ("/tmp/zouinify-test-", "/tmp/zouinify-init-")
RUNNER_MARKER = ".test-runner"


def _cmdline(pid: str) -> list:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _uid(pid: str):
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def leftover_test_servers() -> list:
    """[(pid, project, world)] of this user's zouinify test servers whose runner is gone."""
    out = []
    if not os.path.isdir("/proc"):
        return out
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        args = _cmdline(pid)
        if "--foreground" not in args or "start" not in args or "--project" not in args:
            continue
        if not any(a.endswith("cli.py") for a in args):
            continue
        project = args[args.index("--project") + 1] if args.index("--project") + 1 < len(args) else ""
        if not project.startswith(WORLD_PREFIXES):
            continue
        if _uid(pid) != me:
            continue
        world = os.path.dirname(project.rstrip("/"))
        marker = os.path.join(world, RUNNER_MARKER)
        runner = None
        try:
            with open(marker, encoding="utf-8") as fh:
                runner = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            runner = None
        if runner and _alive(runner):
            continue                                   # a concurrent run of this user: not ours to stop
        out.append((int(pid), project, world))
    return out


def reap_leftovers(out=None) -> int:
    n = 0
    for pid, project, world in leftover_test_servers():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.2)
        if _alive(pid):
            pytest.exit(f"a leftover zouinify test server (pid {pid}, project {project}) cannot be stopped: stop it yourself, then rerun", returncode=3)
        shutil.rmtree(world, ignore_errors=True)
        print(f"reaped a leftover test server from a dead run: pid {pid} ({project}); its world directory removed", file=out)
        n += 1
    return n


def pytest_sessionstart(session):
    reap_leftovers()
