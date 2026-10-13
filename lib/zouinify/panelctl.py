"""`zouinify panel start|stop|status|url` and the `up` integration: one detached server per project (its
own state dir and port), never another project's. Lives outside lib/zouinify/panel/ on purpose: this is
the side that starts and stops the server PROCESS, run by the human from a shell or by the desk; the
panel package itself may not spawn anything (scripts/check_panel_guard.sh, scripts/check_panel_ast.py).
It also keeps the user-level list of running panels (~/.zouinify/projects.json). The port is probed
first: a server of this project is reused, another project's or a foreign process on it is refused,
never taken over."""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from zouinify import ZouinifyError
from zouinify import panelstate as state

PROJECTS_JSON = os.path.join("~", ".zouinify", "projects.json")
NO_TOKEN_MSG = ("panel.bind is intranet and no browser token is set: run `zouinify panel token rotate` in your OWN terminal "
                "first (it prints the token once; a Claude session never does), then start the panel")


def in_claude_session() -> bool:
    return bool(os.environ.get("CLAUDECODE")) or any(k.startswith("CLAUDE_CODE_") for k in os.environ)


def link_host(cfg) -> str:
    """The name the printed URL uses: panel.link_host, or this machine's FQDN when `auto`."""
    lh = str(cfg.panel.get("link_host") or "auto").strip()
    if lh and lh != "auto":
        return lh
    try:
        return socket.getfqdn().lower()
    except OSError:
        return socket.gethostname().lower()


def lan_hosts(cfg) -> set:
    """Every name and IPv4 address this machine may be reached by on the LAN (the Host check accepts these)."""
    names = {link_host(cfg)}
    try:
        hn = socket.gethostname().lower()
        names |= {hn, hn.split(".")[0], socket.getfqdn().lower()}
    except OSError:
        pass
    for name in list(names):
        try:
            for info in socket.getaddrinfo(name, None, socket.AF_INET):
                names.add(info[4][0])
        except OSError:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # no packet is sent: this only selects the outbound interface
        names.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {n for n in names if n and not n.startswith("127.")}


def public_url(cfg) -> str:
    host = link_host(cfg) if cfg.panel.get("bind") == "intranet" else "127.0.0.1"
    return f"http://{host}:{cfg.panel['port']}/"


def cmd_token_rotate(cfg, out=None) -> int:
    """Generate the browser token, keep only its hash, print the URL once: real TTY only, never in a Claude session."""
    out = out or sys.stdout
    if in_claude_session():
        raise ZouinifyError("the browser token is printed only in your own terminal, never inside a Claude session "
                            "(CLAUDECODE is set here): open a plain shell and run `zouinify panel token rotate` there")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ZouinifyError("the browser token is printed once, on a real terminal: run `zouinify panel token rotate` interactively")
    token = secrets.token_urlsafe(32)
    tid = state.set_token(cfg, token)
    url = f"{public_url(cfg)}#t={token}"
    print("", file=out)
    print(f"zouinify panel token (id {tid}) -- shown ONCE, only its hash is kept in {state.panel_json_path(cfg)}:", file=out)
    print("", file=out)
    print(f"    {url}", file=out)
    print("", file=out)
    print("Open that address in the browser you will use (phone or laptop on the lab network); the page stores the token "
          "and strips it from the address. The old token, if any, stops working now; running sessions need no restart.", file=out)
    if cfg.panel.get("bind") != "loopback":
        print("The LAN carries it in clear text: use it on the lab network only; from elsewhere, ssh -L.", file=out)
    try:
        input("Press Enter to clear the screen... ")
    except EOFError:
        pass
    print("\033[2J\033[H", end="", file=out)
    print(f"token id {tid} recorded; the address is no longer on screen. Panel: {public_url(cfg)} (rotate again any time).", file=out)
    return 0


def probe(port: int, timeout: float = 1.5):
    """GET /health on 127.0.0.1:port. A dict with `root` = a zouinify panel; {"foreign": True} = something
    else answers; None = nothing listens."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            body = r.read(1 << 16)
    except urllib.error.HTTPError as e:
        return {"foreign": True, "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
            return None
        return {"foreign": True, "detail": str(reason)[:120]}
    try:
        d = json.loads(body)
    except ValueError:
        return {"foreign": True, "detail": "non-JSON /health"}
    if isinstance(d, dict) and d.get("service") == "zouinify-panel" and d.get("root"):
        return d
    return {"foreign": True, "detail": "a /health that is not a zouinify panel"}


# -- the user-level list of running panels ----------------------------------------------------------------

def projects_path() -> str:
    return os.path.expanduser(PROJECTS_JSON)


def load_projects() -> list:
    try:
        with open(projects_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write_projects(rows: list) -> None:
    path = projects_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".projects.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def register(cfg, port: int, pid: int) -> None:
    rows = [p for p in load_projects() if not state.same_root(str(p.get("root", "")), cfg.root)]
    rows.append({"name": cfg.name, "short": cfg.short, "root": cfg.root, "port": port, "pid": pid, "url": state.url(cfg, port),
                 "updated_at": dt.datetime.now().astimezone().replace(microsecond=0).isoformat()})
    _write_projects(rows)


def unregister(cfg) -> None:
    rows = load_projects()
    keep = [p for p in rows if not state.same_root(str(p.get("root", "")), cfg.root)]
    if len(keep) != len(rows):
        _write_projects(keep)


# -- start / stop ---------------------------------------------------------------------------------------

def _cli_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")


def start_detached(cfg, out) -> int:
    """Spawn `zouinify --project <root> panel start --foreground` in its own session, logging to server.log."""
    os.makedirs(cfg.state_dir, exist_ok=True)
    log = open(state.server_log_path(cfg), "ab")
    try:
        proc = subprocess.Popen([sys.executable, "-I", _cli_path(), "--project", cfg.root, "panel", "start", "--foreground"],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cfg.root, start_new_session=True, close_fds=True)
    finally:
        log.close()
    deadline = time.time() + 15
    while time.time() < deadline:
        h = probe(cfg.panel["port"], timeout=0.5)
        if isinstance(h, dict) and h.get("root") and state.same_root(h["root"], cfg.root):
            mode = "intranet, token required on every request from the LAN" if cfg.panel.get("bind") == "intranet" else "loopback"
            print(f"panel started: {public_url(cfg)}  (pid {h.get('pid')}, read-only, {mode}; log {state.server_log_path(cfg)})", file=out)
            if cfg.panel.get("bind") == "intranet":
                print("the token is not shown here: `zouinify panel token rotate` prints it once in your own terminal", file=out)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = ""
    try:
        with open(state.server_log_path(cfg), encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-5:])
    except OSError:
        pass
    raise ZouinifyError(f"the panel did not answer on {state.url(cfg)} within 15 s; server.log says:\n{tail}")


def run_foreground(cfg) -> int:
    """Serve in this process until SIGTERM/SIGINT. The signal handling and the projects-list entry live
    here, outside the panel package, which may import neither signal nor socket."""
    import threading
    from zouinify.panel import server
    hosts = lan_hosts(cfg) if cfg.panel.get("bind") == "intranet" else set()
    srv = server.make_server(cfg, allowed_hosts=hosts)

    def _term(signum, frame):  # noqa: ARG001
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    register(cfg, srv.server_address[1], os.getpid())
    try:
        return server.run(srv)
    finally:
        unregister(cfg)


def cmd_start(cfg, foreground: bool = False, out=None) -> int:
    out = out or sys.stdout
    port = cfg.panel["port"]
    if cfg.panel.get("bind", "loopback") == "intranet" and not state.token_configured(cfg):
        raise ZouinifyError(NO_TOKEN_MSG)
    h = probe(port)
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"panel already running: {state.url(cfg)}  (pid {h.get('pid')}, since {h.get('started_at')})", file=out)
            return 0
        raise ZouinifyError(f"port {port} is held by another project's panel ({h.get('project')}: {h['root']}); "
                            "pick another panel.port in .ops/config.yaml -- a port is never taken over")
    if isinstance(h, dict) and h.get("foreign"):
        raise ZouinifyError(f"port {port} is held by a foreign process ({h.get('detail')}); pick another panel.port -- never reused")
    if foreground:
        return run_foreground(cfg)
    return start_detached(cfg, out)


def is_our_panel(pid: int, cfg) -> bool:
    """True when /proc/<pid>/cmdline is this project's foreground panel server (pid reuse is never trusted)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0") if p]
    except OSError:
        return False
    return ("panel" in argv and "--foreground" in argv and any(a.endswith("cli.py") for a in argv)
            and any(state.same_root(a, cfg.root) for a in argv if a.startswith("/")))


def cmd_stop(cfg, out=None) -> int:
    out = out or sys.stdout
    port = cfg.panel["port"]
    h = probe(port)
    pj = state.read_panel_json(cfg)
    if isinstance(h, dict) and h.get("root") and not state.same_root(h["root"], cfg.root):
        raise ZouinifyError(f"port {port} serves another project's panel ({h['root']}); zouinify never stops another project's server")
    pid = (h or {}).get("pid") if isinstance(h, dict) and h.get("root") else (pj or {}).get("pid")
    if not pid:
        print("panel not running", file=out)
        state.remove_panel_json(cfg)
        unregister(cfg)
        return 0
    if not is_our_panel(int(pid), cfg):
        print(f"panel not running: pid {pid} from panel.json belongs to another process now (pid reuse); not killed; "
              "stale panel.json removed", file=out)
        state.remove_panel_json(cfg)
        unregister(cfg)
        return 0
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        print(f"panel not running (stale pid {pid} removed)", file=out)
        state.remove_panel_json(cfg)
        unregister(cfg)
        return 0
    deadline = time.time() + 10
    while time.time() < deadline:
        if probe(port, timeout=0.3) is None and not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    state.remove_panel_json(cfg, int(pid))
    unregister(cfg)
    print(f"panel stopped (pid {pid})", file=out)
    return 0


def cmd_status(cfg, out=None) -> int:
    out = out or sys.stdout
    h = probe(cfg.panel["port"])
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"panel running: {state.url(cfg)}  pid {h.get('pid')}  since {h.get('started_at')}  bind {h.get('bind')}  "
                  f"version {h.get('version')}", file=out)
            return 0
        print(f"port {cfg.panel['port']} serves ANOTHER project's panel: {h['root']}", file=out)
        return 1
    if isinstance(h, dict):
        print(f"port {cfg.panel['port']} is held by a foreign process ({h.get('detail')})", file=out)
        return 1
    print(f"panel not running (would serve {state.url(cfg)}; start: zouinify panel start)", file=out)
    return 1


def cmd_url(cfg, out=None) -> int:
    out = out or sys.stdout
    print(public_url(cfg), file=out)
    if cfg.panel.get("bind") == "intranet":
        print(f"(token required from the LAN; it is {'set' if state.token_configured(cfg) else 'NOT set'} and never shown here: "
              "`zouinify panel token rotate` prints it once in your own terminal)", file=out)
    return 0


def ensure_started(cfg, dry_run: bool = False, out=None) -> bool:
    """Called by `up`: report a running panel, start a missing one, refuse a held port; never kill."""
    out = out or sys.stdout
    port = cfg.panel["port"]
    if cfg.panel.get("bind", "loopback") == "intranet" and not state.token_configured(cfg):
        print(f"panel         not started: {NO_TOKEN_MSG}", file=out)
        return False
    h = probe(port)
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"panel         running  {public_url(cfg)}  (pid {h.get('pid')})", file=out)
            return True
        print(f"panel         REFUSED: port {port} serves another project's panel ({h['root']}); change panel.port", file=out)
        return False
    if isinstance(h, dict):
        print(f"panel         REFUSED: port {port} is held by a foreign process ({h.get('detail')}); change panel.port", file=out)
        return False
    if dry_run:
        print(f"panel         would start on {public_url(cfg)} (read-only, {cfg.panel.get('bind', 'loopback')})", file=out)
        return True
    try:
        start_detached(cfg, out)
        return True
    except ZouinifyError as e:
        print(f"panel         FAILED: {e}", file=out)
        return False
