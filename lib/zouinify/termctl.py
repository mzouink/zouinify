"""The process side of the terminal page (T-0181): `zouinify term start|stop|status|url|secret rotate|audit`, the
detached process, the passphrase (shown ONCE on a real TTY, never inside a Claude session), TLS on the intranet,
the probe of /term/health, and `ensure_started` for `up`. Mirrors zouinify.panelctl; the server itself is
zouinify.term.server and may be imported only here."""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

from zouinify import ZouinifyError
from zouinify import panelstate as state
from zouinify.panelctl import in_claude_session, lan_hosts, link_host

NO_SECRET_MSG = ("no passphrase is set for the terminal page: run `zouinify term secret rotate` in your OWN terminal first "
                 "(it shows the passphrase once; a Claude session never does), then start it")
NOT_ENABLED_MSG = "terminal.enabled is false in .ops/config.yaml: set it to true to run the terminal page for this project"
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"       # no 0/o/1/l/i: typeable on a phone from a screen


def scheme(cfg) -> str:
    return "https" if (cfg.terminal.get("bind") == "intranet" and cfg.terminal.get("tls", "auto") == "auto") else "http"


def public_url(cfg) -> str:
    host = link_host(cfg) if cfg.terminal.get("bind") == "intranet" else "127.0.0.1"
    return f"{scheme(cfg)}://{host}:{cfg.terminal['port']}/term/"


def local_url(cfg, port: int | None = None) -> str:
    return f"{scheme(cfg)}://127.0.0.1:{port or cfg.terminal['port']}/term/"


def new_passphrase() -> str:
    """Four groups of five characters from a 31-symbol alphabet: about 99 bits, readable from a screen, typeable on a phone."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4))


def cmd_secret_rotate(cfg, out=None) -> int:
    """Generate the passphrase, keep only its PBKDF2 hash, show it once: real TTY only, never in a Claude session."""
    out = out or sys.stdout
    if in_claude_session():
        raise ZouinifyError("the terminal passphrase is shown only in your own terminal, never inside a Claude session "
                            "(CLAUDECODE is set here): open a plain shell and run `zouinify term secret rotate` there")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ZouinifyError("the terminal passphrase is shown once, on a real terminal: run `zouinify term secret rotate` interactively")
    pp = new_passphrase()
    sid = state.set_term_secret(cfg, pp)
    print("", file=out)
    print(f"zouinify terminal passphrase (id {sid}) -- shown ONCE, only its hash is kept in {state.term_json_path(cfg)}:", file=out)
    print("", file=out)
    print(f"    {pp}", file=out)
    print("", file=out)
    print("Type it on the terminal page's unlock screen, once per browser session; it reaches WORKER sessions of this project only. "
          "The old passphrase, if any, stops working now and every open unlock ends.", file=out)
    if cfg.terminal.get("bind") == "intranet" and cfg.terminal.get("tls", "auto") != "auto":
        print("terminal.tls is off: on the lab network the passphrase crosses in clear text once per browser session.", file=out)
    try:
        input("Press Enter to clear the screen... ")
    except EOFError:
        pass
    print("\033[2J\033[3J\033[H", end="", file=out)
    print(f"passphrase id {sid} recorded; it is no longer on screen. Terminal page: {public_url(cfg)} (rotate again any time).", file=out)
    return 0


def probe(cfg, port: int | None = None, timeout: float = 1.5):
    """GET /term/health on 127.0.0.1. A dict with `root` = a zouinify terminal; {"foreign": True} = something else; None = nothing."""
    url = local_url(cfg, port) + "health"
    ctx = ssl._create_unverified_context() if url.startswith("https") else None  # noqa: SLF001 - our own self-signed cert, loopback only
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
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
        return {"foreign": True, "detail": "non-JSON /term/health"}
    if isinstance(d, dict) and d.get("service") == "zouinify-term" and d.get("root"):
        return d
    return {"foreign": True, "detail": "a /term/health that is not a zouinify terminal"}


def _cli_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")


def _preflight(cfg) -> None:
    if not cfg.terminal.get("enabled"):
        raise ZouinifyError(NOT_ENABLED_MSG)
    if not state.term_secret_configured(cfg):
        raise ZouinifyError(NO_SECRET_MSG)


def start_detached(cfg, out) -> int:
    os.makedirs(cfg.state_dir, exist_ok=True)
    log = open(state.term_log_path(cfg), "ab")
    try:
        proc = subprocess.Popen([sys.executable, "-I", _cli_path(), "--project", cfg.root, "term", "start", "--foreground"],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cfg.root, start_new_session=True, close_fds=True)
    finally:
        log.close()
    deadline = time.time() + 20
    while time.time() < deadline:
        h = probe(cfg, timeout=0.5)
        if isinstance(h, dict) and h.get("root") and state.same_root(h["root"], cfg.root):
            print(f"terminal page started: {public_url(cfg)}  (pid {h.get('pid')}, workers only, passphrase required from every address, "
                  f"{cfg.terminal.get('bind')}{', TLS' if h.get('tls') else ''}; log {state.term_log_path(cfg)})", file=out)
            if h.get("tls"):
                print("self-signed certificate: the browser warns once per device; accept it for this address only.", file=out)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = ""
    try:
        with open(state.term_log_path(cfg), encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-5:])
    except OSError:
        pass
    raise ZouinifyError(f"the terminal page did not answer on {local_url(cfg)} within 20 s; term.log says:\n{tail}")


def run_foreground(cfg) -> int:
    """Serve in this process until SIGTERM/SIGINT; TLS when bound to the intranet with terminal.tls auto."""
    import threading
    from zouinify.term import server
    _preflight(cfg)
    bind = cfg.terminal.get("bind", "loopback")
    hosts = lan_hosts(cfg) if bind == "intranet" else set()
    ctx = None
    if bind == "intranet" and cfg.terminal.get("tls", "auto") == "auto":
        from zouinify.term import tls
        cert, key = tls.ensure_cert(cfg, link_host(cfg), hosts)
        ctx = tls.context(cert, key)
    srv = server.make_server(cfg, allowed_hosts=hosts, ssl_context=ctx)

    def _term(signum, frame):  # noqa: ARG001
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    return server.run(srv, public_url(cfg))


def cmd_start(cfg, foreground: bool = False, out=None) -> int:
    out = out or sys.stdout
    _preflight(cfg)
    if cfg.terminal.get("bind") == "intranet" and cfg.terminal.get("tls", "auto") == "auto":
        from zouinify.term import tls
        tls.ensure_cert(cfg, link_host(cfg), lan_hosts(cfg))     # fail here, loudly, rather than in the detached process
    if cfg.terminal.get("bind") == "intranet" and cfg.terminal.get("tls", "auto") == "off":
        print("WARNING: terminal.tls is off: the passphrase crosses the lab network in clear text once per browser session, the "
              "unlock cookie on every request. Set terminal.tls: auto (default) to serve HTTPS with a self-signed certificate.", file=out)
    port = cfg.terminal["port"]
    h = probe(cfg)
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"terminal page already running: {public_url(cfg)}  (pid {h.get('pid')}, since {h.get('started_at')})", file=out)
            return 0
        raise ZouinifyError(f"port {port} is held by another project's terminal page ({h.get('project')}: {h['root']}); "
                            "pick another terminal.port in .ops/config.yaml -- a port is never taken over")
    if isinstance(h, dict) and h.get("foreign"):
        raise ZouinifyError(f"port {port} is held by a foreign process ({h.get('detail')}); pick another terminal.port -- never reused")
    if foreground:
        return run_foreground(cfg)
    return start_detached(cfg, out)


def is_our_term(pid: int, cfg) -> bool:
    """True when /proc/<pid>/cmdline is this project's foreground terminal server (pid reuse is never trusted)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0") if p]
    except OSError:
        return False
    return ("term" in argv and "--foreground" in argv and any(a.endswith("cli.py") for a in argv)
            and any(state.same_root(a, cfg.root) for a in argv if a.startswith("/")))


def cmd_stop(cfg, out=None) -> int:
    """The kill switch: the process goes, and with it every unlock and every attachment; the passphrase hash stays."""
    out = out or sys.stdout
    port = cfg.terminal["port"]
    h = probe(cfg)
    tj = state.read_term_json(cfg)
    if isinstance(h, dict) and h.get("root") and not state.same_root(h["root"], cfg.root):
        raise ZouinifyError(f"port {port} serves another project's terminal page ({h['root']}); zouinify never stops another project's server")
    pid = (h or {}).get("pid") if isinstance(h, dict) and h.get("root") else (tj or {}).get("pid")
    if not pid:
        print("terminal page not running", file=out)
        state.remove_term_json(cfg)
        return 0
    if not is_our_term(int(pid), cfg):
        print(f"terminal page not running: pid {pid} from term.json belongs to another process now (pid reuse); not killed; "
              "stale runtime fields removed", file=out)
        state.remove_term_json(cfg)
        return 0
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        print(f"terminal page not running (stale pid {pid} removed)", file=out)
        state.remove_term_json(cfg)
        return 0
    deadline = time.time() + 10
    while time.time() < deadline:
        if probe(cfg, timeout=0.3) is None and not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    state.remove_term_json(cfg, int(pid))
    print(f"terminal page stopped (pid {pid}); every unlock and attachment ended; the passphrase hash is kept", file=out)
    return 0


def cmd_status(cfg, out=None) -> int:
    out = out or sys.stdout
    h = probe(cfg)
    secret = "set" if state.term_secret_configured(cfg) else "NOT set"
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"terminal page running: {public_url(cfg)}  pid {h.get('pid')}  since {h.get('started_at')}  bind {h.get('bind')}"
                  f"{'  TLS' if h.get('tls') else ''}  targets workers  passphrase {secret}  version {h.get('version')}", file=out)
            return 0
        print(f"port {cfg.terminal['port']} serves ANOTHER project's terminal page: {h['root']}", file=out)
        return 1
    if isinstance(h, dict):
        print(f"port {cfg.terminal['port']} is held by a foreign process ({h.get('detail')})", file=out)
        return 1
    print(f"terminal page not running (would serve {public_url(cfg)}; enabled: {bool(cfg.terminal.get('enabled'))}; passphrase {secret}; "
          "start: zouinify term start)", file=out)
    return 1


def cmd_url(cfg, out=None) -> int:
    out = out or sys.stdout
    print(public_url(cfg), file=out)
    print(f"(passphrase {'set' if state.term_secret_configured(cfg) else 'NOT set'}, required from every address and never shown here: "
          "`zouinify term secret rotate` shows it once in your own terminal; workers only)", file=out)
    return 0


def parse_since(text: str) -> float:
    """`10m`, `2h`, `1d`, `90s` -> seconds."""
    t = str(text or "10m").strip().lower()
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(t[-1:], None)
    if unit is None:
        raise ZouinifyError(f"--since: a number with s, m, h or d (got {text!r})")
    try:
        return float(t[:-1]) * unit
    except ValueError as e:
        raise ZouinifyError(f"--since: a number with s, m, h or d (got {text!r})") from e


def cmd_audit(cfg, since: str = "10m", target: str | None = None, out=None) -> int:
    """Print the recent audit lines (read-only), so a session may cite `term <tid8>` for an answer the human typed."""
    out = out or sys.stdout
    horizon = dt.datetime.now().astimezone() - dt.timedelta(seconds=parse_since(since))
    path = state.term_audit_path(cfg)
    n = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    at = dt.datetime.fromisoformat(str(rec.get("at")))
                except (ValueError, TypeError):
                    continue
                if at < horizon:
                    continue
                if target and rec.get("target") != target:
                    continue
                n += 1
                keys = " ".join(rec.get("keys") or []) if isinstance(rec.get("keys"), list) else ""
                print(f"{rec.get('at')} {rec.get('event', '?'):8s} tid={rec.get('tid8', '-'):8s} unlock={rec.get('unlock8', '-'):8s} "
                      f"ip={rec.get('ip', '-')} target={rec.get('target', '-')}"
                      + (f" text={rec.get('text')!r}" if rec.get("text") else "") + (f" keys={keys}" if keys else "")
                      + (f" reason={rec.get('reason')}" if rec.get("reason") else ""), file=out)
    except OSError:
        pass
    print(f"{n} audit line(s) in the last {since}" + (f" for {target}" if target else "") + f" ({path})", file=out)
    return 0


def ensure_started(cfg, dry_run: bool = False, out=None) -> bool:
    """Called by `up` when terminal.enabled and terminal.autostart: report a running terminal page, start a missing
    one, refuse a held port; never kill."""
    out = out or sys.stdout
    if not state.term_secret_configured(cfg):
        print(f"terminal      not started: {NO_SECRET_MSG}", file=out)
        return False
    h = probe(cfg)
    if isinstance(h, dict) and h.get("root"):
        if state.same_root(h["root"], cfg.root):
            print(f"terminal      running  {public_url(cfg)}  (pid {h.get('pid')}, workers only)", file=out)
            return True
        print(f"terminal      REFUSED: port {cfg.terminal['port']} serves another project's terminal page ({h['root']}); change terminal.port", file=out)
        return False
    if isinstance(h, dict):
        print(f"terminal      REFUSED: port {cfg.terminal['port']} is held by a foreign process ({h.get('detail')}); change terminal.port", file=out)
        return False
    if dry_run:
        print(f"terminal      would start on {public_url(cfg)} (workers only, {cfg.terminal.get('bind', 'loopback')})", file=out)
        return True
    try:
        start_detached(cfg, out)
        return True
    except ZouinifyError as e:
        print(f"terminal      not started: {e}", file=out)
        return False
