"""The panel server's own two files: its pid file and its log, at <project root>/.ops/state/panel.json and
<project root>/.ops/state/server.log -- paths FIXED in code (desk condition, T-0160): never a config key, never
an argument, and only for the loaded Config of the project (a look-alike object is refused). The state dir may
not be a symlink and the log is opened with O_NOFOLLOW. This is the second and last zouinify module the panel
package may import (with zouinify.readonly; scripts/check_panel_ast.py). It writes nothing else: not a ticket,
not a session, not the registry, not the user-level projects list (that one is zouinify.panelctl's).

panel.json also keeps the browser token's sha256 hash and a 4-hex token id (T-0165): the panel package may only
ask `token_matches()` / `token_id()`; it never sees the hash, and `set_token()` is the process side's (panelctl)."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile

from zouinify import __version__
from zouinify.config import Config

STATE_REL = (".ops", "state")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
TOKEN_FIELDS = ("token_hash", "token_id", "token_rotated_at")
TERM_FIELDS = ("secret_hash", "secret_salt", "secret_rounds", "secret_id", "secret_rotated_at")
PBKDF2_ROUNDS = 600_000


def version() -> str:
    return __version__


def pid() -> int:
    return os.getpid()


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def url(cfg, port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or cfg.panel['port']}/"


def same_root(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _check(cfg) -> None:
    if not isinstance(cfg, Config):
        raise TypeError("panelstate takes the loaded Config of the project, nothing else")


def state_dir(cfg) -> str:
    """<root>/.ops/state, fixed; refused when it (or .ops) is a symlink or resolves outside the root."""
    _check(cfg)
    root = os.path.realpath(cfg.root)
    d = os.path.join(root, *STATE_REL)
    for p in (os.path.join(root, STATE_REL[0]), d):
        if os.path.islink(p):
            raise OSError(f"{p} is a symlink: the panel refuses to write through it")
    if os.path.exists(d) and not os.path.realpath(d).startswith(root + os.sep):
        raise OSError(f"{d} resolves outside the project: refused")
    return d


def panel_json_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "panel.json")


def server_log_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "server.log")


def log(cfg, text: str) -> None:
    """Append one line to the server log (control characters escaped, symlinks never followed); never raises."""
    try:
        d = state_dir(cfg)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "server.log")
        safe = CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {safe}\n")
    except (OSError, TypeError):
        pass


@contextlib.contextmanager
def _locked(cfg):
    """One writer of panel.json at a time: a token rotation racing a start or a stop must not lose a field."""
    d = state_dir(cfg)
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, ".panel.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_json(path: str, obj) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    if os.path.islink(path):
        os.remove(path)       # never write through a planted symlink
    os.replace(tmp, path)


def write_panel_json(cfg, port: int, pid_: int, started_at: str, hook_port: int | None = None, hook_nonce: str | None = None) -> None:
    """The runtime fields of a running server; the token fields already there are kept. `hook_port` and `hook_nonce`
    are what the hook script reads (T-0163, T-0182): the port of the loopback listener and the per-start secret it
    sends back as the X-Zouinify-Hook header (the file is 0600, so only this user's processes can read it)."""
    with _locked(cfg):
        cur = read_panel_json(cfg) or {}
        d = {"port": port, "pid": pid_, "root": cfg.root, "project": cfg.name, "bind": cfg.panel.get("bind", "loopback"),
             "url": url(cfg, port), "started_at": started_at, "version": __version__}
        if hook_port:
            d["hook_port"] = int(hook_port)
        if hook_nonce:
            d["hook_nonce"] = str(hook_nonce)
        for k in TOKEN_FIELDS:
            if cur.get(k):
                d[k] = cur[k]
        _write_json(panel_json_path(cfg), d)


def read_panel_json(cfg):
    try:
        with open(panel_json_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def remove_panel_json(cfg, pid_: int | None = None) -> None:
    """Drop the runtime fields (pid, port, started_at) of a stopped server; the token hash survives restarts.
    The file itself goes only when it holds no token."""
    try:
        with _locked(cfg):
            d = read_panel_json(cfg)
            if d is None:
                return
            if pid_ is not None and d.get("pid") != pid_:
                return
            keep = {k: d[k] for k in TOKEN_FIELDS if d.get(k)}
            if keep:
                keep.update({"root": cfg.root, "project": cfg.name})
                _write_json(panel_json_path(cfg), keep)
            else:
                os.remove(panel_json_path(cfg))
    except OSError:
        pass


# -- the browser token (T-0165): hash on disk, compared in constant time; the panel never sees the hash ----------

def set_token(cfg, token: str) -> str:
    """Record sha256(token) and a 4-hex id in panel.json (0600). Called by zouinify.panelctl on a real TTY only;
    the plain token is printed there once and never stored. Returns the token id."""
    _check(cfg)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("a token is at least 32 characters")
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _locked(cfg):
        cur = read_panel_json(cfg) or {}
        cur.update({"token_hash": h, "token_id": h[-4:], "token_rotated_at": now_iso(), "root": cfg.root, "project": cfg.name})
        _write_json(panel_json_path(cfg), cur)
    return h[-4:]


def token_configured(cfg) -> bool:
    d = read_panel_json(cfg) or {}
    return bool(d.get("token_hash"))


def token_id(cfg):
    d = read_panel_json(cfg) or {}
    return d.get("token_id") or None


def token_matches(cfg, presented) -> bool:
    """Constant-time comparison of sha256(presented) with the stored hash; False when no token is set."""
    d = read_panel_json(cfg) or {}
    h = d.get("token_hash")
    if not h or not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(hashlib.sha256(presented.encode("utf-8")).hexdigest(), str(h))


def hook_installed() -> tuple:
    """Whether ~/.claude/settings.json already names the zouinify hook (T-0163): read-only, never edited."""
    path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return "zouinify-hook" in fh.read(), path
    except OSError:
        return False, path


def new_hook_nonce() -> str:
    """The per-start secret of the hook listener (T-0182): written to panel.json (0600) by the server, read by the
    hook script, sent back as X-Zouinify-Hook. Random, never logged."""
    return secrets.token_urlsafe(18)


def hook_nonce_matches(cfg, presented) -> bool:
    """Constant-time comparison with the nonce in panel.json; False when none is set or the shape is wrong."""
    d = read_panel_json(cfg) or {}
    want = d.get("hook_nonce")
    if not isinstance(want, str) or not want or not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(want, presented)


HOOK_SCRIPT = "~/.claude/skills/zouinify/templates/hooks/zouinify-hook.sh"
HOOK_EVENTS = ("Stop", "Notification", "SessionStart", "SessionEnd")


def hook_snippet() -> str:
    """The ~/.claude/settings.json block the HUMAN adds (a session never edits that file): the four events, the
    Notification matcher for the two prompt kinds, a 5 s timeout."""
    def entry(ev):
        h = {"hooks": [{"type": "command", "command": HOOK_SCRIPT, "timeout": 5}]}
        if ev == "Notification":
            h = {"matcher": "permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input", **h}
        return [h]
    return json.dumps({"hooks": {ev: entry(ev) for ev in HOOK_EVENTS}}, indent=1)


EVENTS_KEEP_DAYS = 14
EVENTS_MAX_BYTES = 20 * 1024 * 1024    # per day; beyond it events are still shown live but no longer appended


def events_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "events.jsonl")


def append_event(cfg, event: dict) -> None:
    """Append one hook event (already allowlisted by the listener) to <state_dir>/events.jsonl: 0600, O_NOFOLLOW,
    under the state lock; the file rotates to events.<date>.jsonl when the day changes and files older than
    EVENTS_KEEP_DAYS go. Never raises: a logging failure must not fail a hook."""
    try:
        with _locked(cfg):
            path = events_path(cfg)
            today = dt.date.today()
            try:                                        # housekeeping first; a failure here never costs the append
                st = os.lstat(path)
                if not os.path.islink(path):
                    day = dt.date.fromtimestamp(st.st_mtime)
                    if day != today:
                        os.replace(path, os.path.join(state_dir(cfg), f"events.{day.isoformat()}.jsonl"))
                else:
                    os.remove(path)        # never write through a planted symlink
            except OSError:
                pass
            try:
                names = os.listdir(state_dir(cfg))
            except OSError:
                names = []
            for name in names:
                if name.startswith("events.") and name.endswith(".jsonl") and name != "events.jsonl":
                    try:
                        day = dt.date.fromisoformat(name[len("events."):-len(".jsonl")])
                        if (today - day).days > EVENTS_KEEP_DAYS:
                            os.remove(os.path.join(state_dir(cfg), name))
                    except (ValueError, OSError):       # not a date, or a planted directory: skip it
                        continue
            line = json.dumps(event, sort_keys=True) + "\n"
            try:
                if os.lstat(path).st_size + len(line) > EVENTS_MAX_BYTES:
                    return                      # the day's file is full: a flood must not fill the disk
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
    except (OSError, TypeError, ValueError):
        pass
# -- the terminal page (T-0181): its own files next to the panel's, its own secret, never the panel's token ----------
# term.json: pid, port, url, bind, tls of the running `zouinify term` process + the passphrase hash (PBKDF2-HMAC-SHA256,
# 600k rounds, 16-byte salt) and a 4-hex id; term.log: one line per request; term_audit.jsonl: one JSON line per unlock,
# open, batch of keys, close and refusal. The panel package may call `term_info()` only (no secret in it).

def term_json_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "term.json")


def term_log_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "term.log")


def term_audit_path(cfg) -> str:
    return os.path.join(state_dir(cfg), "term_audit.jsonl")


def term_log(cfg, text: str) -> None:
    """Append one line to term.log (control characters escaped, symlinks never followed); never raises."""
    try:
        d = state_dir(cfg)
        os.makedirs(d, exist_ok=True)
        safe = CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))
        fd = os.open(os.path.join(d, "term.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {safe}\n")
    except (OSError, TypeError):
        pass


def term_audit(cfg, event: dict) -> None:
    """Append one JSON line to term_audit.jsonl (0600, O_NOFOLLOW): {at, event, unlock8, tid8, target, ip, user_agent,
    text, keys, reason, ...}; strings have their control characters escaped; never the passphrase; never raises."""
    try:
        d = state_dir(cfg)
        os.makedirs(d, exist_ok=True)
        rec = {"at": now_iso()}
        for k, v in event.items():
            rec[str(k)] = CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), v) if isinstance(v, str) else v
        fd = os.open(os.path.join(d, "term_audit.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_term_json(cfg):
    try:
        with open(term_json_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def write_term_json(cfg, port: int, pid_: int, started_at: str, bind: str, tls: bool, url_: str) -> None:
    """The runtime fields of a running terminal process; the secret fields already there are kept."""
    with _locked(cfg):
        cur = read_term_json(cfg) or {}
        d = {"port": port, "pid": pid_, "root": cfg.root, "project": cfg.name, "bind": bind, "tls": bool(tls), "url": url_,
             "started_at": started_at, "version": __version__, "targets": "workers"}
        for k in TERM_FIELDS:
            if cur.get(k):
                d[k] = cur[k]
        _write_json(term_json_path(cfg), d)


def remove_term_json(cfg, pid_: int | None = None) -> None:
    """Drop the runtime fields of a stopped terminal process; the secret survives restarts."""
    try:
        with _locked(cfg):
            d = read_term_json(cfg)
            if d is None:
                return
            if pid_ is not None and d.get("pid") != pid_:
                return
            keep = {k: d[k] for k in TERM_FIELDS if d.get(k)}
            if keep:
                keep.update({"root": cfg.root, "project": cfg.name})
                _write_json(term_json_path(cfg), keep)
            else:
                os.remove(term_json_path(cfg))
    except OSError:
        pass


def set_term_secret(cfg, passphrase: str) -> str:
    """Record PBKDF2-HMAC-SHA256(passphrase) with a fresh 16-byte salt in term.json (0600). Called by zouinify.termctl
    on a real TTY only; the passphrase is shown there once and never stored. Returns the 4-hex secret id."""
    _check(cfg)
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError("a passphrase is at least 12 characters")
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()
    with _locked(cfg):
        cur = read_term_json(cfg) or {}
        cur.update({"secret_hash": h, "secret_salt": salt.hex(), "secret_rounds": PBKDF2_ROUNDS, "secret_id": h[-4:],
                    "secret_rotated_at": now_iso(), "root": cfg.root, "project": cfg.name})
        _write_json(term_json_path(cfg), cur)
    return h[-4:]


def term_secret_configured(cfg) -> bool:
    d = read_term_json(cfg) or {}
    return bool(d.get("secret_hash") and d.get("secret_salt"))


def term_secret_id(cfg):
    d = read_term_json(cfg) or {}
    return d.get("secret_id") or None


def term_secret_matches(cfg, presented) -> bool:
    """Constant-time comparison of PBKDF2(presented, stored salt) with the stored hash; False when none is set."""
    d = read_term_json(cfg) or {}
    h, salt = d.get("secret_hash"), d.get("secret_salt")
    if not h or not salt or not isinstance(presented, str) or not presented or len(presented) > 256:
        return False
    try:
        rounds = int(d.get("secret_rounds") or PBKDF2_ROUNDS)
        got = hashlib.pbkdf2_hmac("sha256", presented.encode("utf-8"), bytes.fromhex(str(salt)), rounds).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, str(h))


def term_info(cfg) -> dict:
    """What the panel may know about this project's terminal page (T-0181): whether it is enabled and running, and its
    address, so the waiting rows of WORKER sessions can link to it. Never a secret, never the hash."""
    t = getattr(cfg, "terminal", None) or {}
    d = read_term_json(cfg) or {}
    pid_ = d.get("pid")
    running = bool(pid_) and os.path.exists(f"/proc/{pid_}") and bool(d.get("url"))
    return {"enabled": bool(t.get("enabled")), "running": running, "url": str(d.get("url") or "") if running else "", "targets": "workers"}
