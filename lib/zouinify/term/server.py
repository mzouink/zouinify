"""The terminal page's HTTP(S) server (T-0181): shows one WORKER pane of this project and forwards the human's own
keystrokes into it, after a passphrase unlock. stdlib only; GET/HEAD/POST; everything else 405.

Routes (all under /term/): `GET /term/health` (no secret), `GET /term/` and `/term/static/*` (the page: public code, no
data), `POST /term/unlock {passphrase}` -> the `zouinify_term` cookie (HttpOnly, SameSite=Strict, Path=/term, Secure on
TLS), then with the cookie only: `GET /term/me`, `GET /term/targets` (the live worker sessions), `POST /term/open
{target}` -> {tid}, `GET /term/<tid>/screen`, `POST /term/<tid>/keys {ops}`, `POST /term/<tid>/close`, `POST /term/lock`.

Rules, from the manager's conditions and the user's "yes workers" (2026-10-13): its OWN secret, never the panel's token
(PBKDF2 hash in term.json; the cookie is a random server-side unlock id, expiring after `unlock_idle_s` without a
keystroke and `unlock_max_s` regardless); the unlock is required from EVERY address, loopback included
(webauth.authorize with loopback_exempt=False); 5 wrong passphrases per minute per address -> 429 and a 15-minute
lockout; targets are worker sessions only, resolved and re-checked live before every batch (zouinify.term.targets; a
role session, a shell pane, another project's session: refused and audited); keys are an allowlist (zouinify.term.keys);
one JSON line per unlock, open, batch, close and refusal in term_audit.jsonl, never the passphrase; the Host/Origin
check and the limiter are zouinify.webauth's. The process side (start/stop, the passphrase, TLS, signals) is
zouinify.termctl; this module only serves."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from zouinify import ZouinifyError, panelstate, tmux, webauth
from zouinify.tmux import TmuxError
from zouinify.panel.redact import redact
from zouinify.term import keys as keysmod
from zouinify.term import targets as targetsmod

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml"}
COOKIE = "zouinify_term"
UNLOCK_BUDGET = 5            # wrong passphrases per window per address before 429 ...
UNLOCK_WINDOW_S = 60.0
LOCKOUT_S = 900              # ... and a lockout of that address
BODY_MAX = 65536
SOCKET_TIMEOUT_S = 30
UNLOCK_SLOTS = 2             # concurrent PBKDF2 verifications per process; beyond that an unlock attempt is told to retry (L3)
UNLOCK_SLOT_WAIT_S = 3.0
ALLOW = "GET, HEAD, POST"
CSP = webauth.CSP
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
QUERY_RE = re.compile(r"[?#][^ \"]*")
TID_RE = re.compile(r"^/term/([0-9a-f]{64})/(screen|keys|close)$")
NO_SECRET_MSG = ("no passphrase is set for the terminal page: run `zouinify term secret rotate` in your OWN terminal (it shows "
                 "the passphrase once; a Claude session never does)")


class TermServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg, bind: str, allowed_hosts, tls: bool):
        super().__init__(address, handler)
        self.cfg = cfg
        self.bind = bind
        self.tls = bool(tls)
        self.allowed_hosts = {str(h).strip().lower() for h in (allowed_hosts or ()) if str(h).strip()}
        self.started_at = panelstate.now_iso()
        # two budgets per address: wrong PASSPHRASES (5 a minute, then the 15-minute lockout) and, separately, requests without
        # a valid cookie (the panel's weights), so an unlocked browser is not cut off by someone else's wrong passphrases
        self.unlock_limiter = webauth.RateLimiter(budget=UNLOCK_BUDGET, window_s=UNLOCK_WINDOW_S, weights={"header": 1.0, "cookie": 1.0, None: 1.0})
        self.limiter = webauth.RateLimiter()
        self.lockout: dict = {}         # ip -> unix time until which unlock attempts are refused
        self.unlock_slots = threading.BoundedSemaphore(UNLOCK_SLOTS)
        self.audit_key = secrets.token_bytes(32)   # per process: typed text is audited as length + HMAC, never in plain (M1)
        self.unlocks: dict = {}         # unlock id -> {ip, ua, at, last}
        self.attachments: dict = {}     # tid -> {unlock, target, at, last}
        self.lock = threading.Lock()

    @property
    def times(self) -> dict:
        return self.cfg.terminal

    # -- unlock and attachment lifecycle ----------------------------------------------------------------
    def sweep(self) -> None:
        """Expire unlocks (idle / max) and attachments (idle / max / unlock gone); each expiry is audited."""
        now = time.time()
        t = self.times
        with self.lock:
            for uid, u in list(self.unlocks.items()):
                why = "idle" if now - u["last"] > t["unlock_idle_s"] else "max" if now - u["at"] > t["unlock_max_s"] else ""
                if why:
                    self.unlocks.pop(uid, None)
                    panelstate.term_audit(self.cfg, {"event": "lock", "unlock8": uid[:8], "reason": why, "ip": u["ip"]})
            for tid, a in list(self.attachments.items()):
                why = ("unlock gone" if a["unlock"] not in self.unlocks else "idle" if now - a["last"] > t["idle_close_s"]
                       else "max" if now - a["at"] > t["max_open_s"] else "")
                if why:
                    self.attachments.pop(tid, None)
                    panelstate.term_audit(self.cfg, {"event": "close", "tid8": tid[:8], "unlock8": a["unlock"][:8],
                                                     "target": a["target"]["name"], "reason": why})

    def unlock_for(self, presented):
        """The unlock id the presented cookie names (constant-time compare), or None."""
        if not isinstance(presented, str) or not presented:
            return None
        self.sweep()
        with self.lock:
            for uid in list(self.unlocks):
                if hmac.compare_digest(uid, presented):
                    return uid
        return None

    def unlock_matches(self, presented) -> bool:
        return self.unlock_for(presented) is not None

    def new_unlock(self, ip: str, ua: str) -> str:
        uid = secrets.token_hex(32)
        now = time.time()
        with self.lock:
            self.unlocks[uid] = {"ip": ip, "ua": ua, "at": now, "last": now}
        return uid

    def end_unlock(self, uid: str) -> list:
        """Drop the unlock and every attachment under it; returns the closed attachments' (tid, target name)."""
        with self.lock:
            self.unlocks.pop(uid, None)
            gone = [(tid, a["target"]["name"]) for tid, a in self.attachments.items() if a["unlock"] == uid]
            for tid, _ in gone:
                self.attachments.pop(tid, None)
        return gone

    def new_attachment(self, uid: str, target: dict) -> str:
        tid = secrets.token_hex(32)
        now = time.time()
        with self.lock:
            self.attachments[tid] = {"unlock": uid, "target": target, "at": now, "last": now}
        return tid

    def attachment(self, tid: str, uid: str):
        self.sweep()
        with self.lock:
            a = self.attachments.get(tid)
            return a if a and a["unlock"] == uid else None

    def drop_attachment(self, tid: str):
        with self.lock:
            return self.attachments.pop(tid, None)

    def touch(self, uid: str, tid: str | None = None) -> None:
        now = time.time()
        with self.lock:
            if uid in self.unlocks:
                self.unlocks[uid]["last"] = now
            if tid and tid in self.attachments:
                self.attachments[tid]["last"] = now

    def closes_in(self, tid: str) -> int:
        with self.lock:
            a = self.attachments.get(tid)
            if not a:
                return 0
            t = self.times
            now = time.time()
            return int(max(0, min(t["idle_close_s"] - (now - a["last"]), t["max_open_s"] - (now - a["at"]))))


class Handler(BaseHTTPRequestHandler):
    server_version = "zouinify-term/" + panelstate.version()
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S

    # -- plumbing ---------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - one line per request into term.log, never a secret
        msg = QUERY_RE.sub(lambda m: m.group()[0] + "<query-redacted>", CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), fmt % args))
        panelstate.term_log(self.server.cfg, f"{self.address_string()} {msg}")

    def ip(self) -> str:
        return self.client_address[0]            # the socket's peer, never a forwarded header

    def ua(self) -> str:
        return CONTROL_RE.sub("", self.headers.get("User-Agent") or "")[:80]

    def audit(self, event: str, **fields) -> None:
        rec = {"event": event, "ip": self.ip(), "user_agent": self.ua()}
        rec.update(fields)
        panelstate.term_audit(self.server.cfg, rec)

    def text_digest(self, text: str) -> str:
        """16 hex characters of HMAC-SHA256(per-process key, text): correlates batches, never reveals a typed password."""
        return hmac.new(self.server.audit_key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def send_bytes(self, status: int, body: bytes, ctype: str, extra: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, status: int, obj, head: bool = False, extra: dict | None = None) -> None:
        self.send_bytes(status, json.dumps(obj, indent=1, default=str).encode("utf-8"), "application/json; charset=utf-8", extra, head=head)

    def cookie(self, uid: str, max_age: int) -> str:
        return f"{COOKIE}={uid}; Path=/term; HttpOnly; SameSite=Strict; Max-Age={max_age}" + ("; Secure" if self.server.tls else "")

    def expired_cookie(self) -> str:
        return f"{COOKIE}=; Path=/term; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self.server.tls else "")

    def host_ok(self) -> bool:
        return webauth.host_allowed(self.headers, self.server.server_address[1], self.server.allowed_hosts,
                                    "https" if self.server.tls else "http")

    def drain(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(min(length, 1 << 20))
        except ValueError:
            pass

    def body_json(self):
        """The JSON object of a POST body, or None (no/oversized body, not application/json, not an object)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > BODY_MAX:
            self.drain()
            return None
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = self.rfile.read(length)
        if ct != "application/json":
            return None
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return d if isinstance(d, dict) else None

    # -- methods ----------------------------------------------------------------------------------------
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("HEAD")

    def do_POST(self):
        self.route("POST")

    def refuse_method(self):
        self.drain()
        self.audit("refused", reason="method", method=self.command)
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed", "allow": ALLOW}, extra={"Allow": ALLOW})

    def do_PUT(self):
        self.refuse_method()

    def do_DELETE(self):
        self.refuse_method()

    def do_PATCH(self):
        self.refuse_method()

    def do_OPTIONS(self):
        self.refuse_method()

    # -- routing ----------------------------------------------------------------------------------------
    def route(self, method: str) -> None:
        head = method == "HEAD"
        srv: TermServer = self.server
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        if not self.host_ok():
            self.drain()
            self.audit("refused", reason="host-or-origin", path=path[:120])
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "host or origin not allowed: the terminal page answers only to its own "
                                                                  "names with no foreign Origin"}, head)
        try:
            if method in ("GET", "HEAD"):
                if path == "/term/health":
                    return self.send_json(HTTPStatus.OK, {"ok": True, "service": "zouinify-term", "version": panelstate.version(),
                                                          "root_sha": root_sha(srv.cfg.root), "project": srv.cfg.name, "bind": srv.bind,
                                                          "tls": srv.tls, "port": srv.server_address[1], "pid": panelstate.pid(),
                                                          "started_at": srv.started_at, "targets": "workers", "unlock_required": True}, head)
                if path in ("/term", "/term/", "/term/index.html"):
                    return self.serve_static("term.html", head)
                if path.startswith("/term/static/"):
                    return self.serve_static(path[len("/term/static/"):], head)
            if method == "POST" and path == "/term/unlock":
                return self.unlock()
            # everything else needs the unlock cookie: from every address, loopback included
            status, reason, _source = webauth.authorize(self.ip(), self.headers, True, srv.unlock_matches, srv.limiter, COOKIE,
                                                        loopback_exempt=False)
            if status != 200:
                self.drain()
                self.audit("refused", reason=reason, path=path[:120])
                if status == 429:
                    return self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many refused requests from this address: wait a minute"},
                                          head, {"Retry-After": str(int(UNLOCK_WINDOW_S))})
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unlock first: the terminal page needs its passphrase"}, head)
            tok, _ = webauth.presented_token(self.headers, COOKIE)
            uid = srv.unlock_for(tok)
            if uid is None:
                self.drain()
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unlock first: the terminal page needs its passphrase"}, head)
            m = TID_RE.match(path)
            if method in ("GET", "HEAD"):
                if path == "/term/me":
                    srv.touch(uid)
                    return self.send_json(HTTPStatus.OK, {"unlocked": True, "unlock8": uid[:8], "targets": "workers",
                                                          "idle_s": srv.times["unlock_idle_s"], "max_s": srv.times["unlock_max_s"],
                                                          "project": srv.cfg.name}, head)
                if path == "/term/targets":
                    rows = []
                    for t in targetsmod.list_targets(srv.cfg):
                        ok, why = targetsmod.live(srv.cfg, t)
                        rows.append({"name": t["name"], "tmux": t["tmux"], "ticket": t.get("ticket", ""), "live": ok, "reason": why})
                    srv.touch(uid)
                    return self.send_json(HTTPStatus.OK, {"targets": rows, "note": targetsmod.ROLE_REFUSED}, head)
                if m and m.group(2) == "screen":
                    return self.screen(uid, m.group(1), head)
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)
            if path == "/term/lock":
                self.drain()
                gone = srv.end_unlock(uid)
                for tid, name in gone:
                    self.audit("close", tid8=tid[:8], unlock8=uid[:8], target=name, reason="lock")
                self.audit("lock", unlock8=uid[:8], reason="user")
                return self.send_json(HTTPStatus.OK, {"ok": True}, extra={"Set-Cookie": self.expired_cookie()})
            if path == "/term/open":
                return self.open_target(uid)
            if m and m.group(2) == "keys":
                return self.keys(uid, m.group(1))
            if m and m.group(2) == "close":
                return self.close(uid, m.group(1))
            self.drain()
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ZouinifyError as e:
            self.log_message("refused on %s: %s", path, e)
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(e)[:300]}, head)
        except Exception as e:  # noqa: BLE001 - never take the server down for one request
            self.log_message("error on %s: %r", path, e)
            return self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"[:300]}, head)

    # -- handlers ---------------------------------------------------------------------------------------
    def unlock(self) -> None:
        srv: TermServer = self.server
        ip = self.ip()
        now = time.time()
        until = srv.lockout.get(ip, 0)
        if until > now:
            self.drain()
            self.audit("refused", reason="unlock-lockout")
            return self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": f"too many wrong passphrases from this address: locked out for {int(until - now)} s"},
                                  extra={"Retry-After": str(int(until - now))})
        if srv.unlock_limiter.rate_limited(ip):
            srv.lockout[ip] = now + LOCKOUT_S
            self.drain()
            self.audit("refused", reason="unlock-lockout")
            return self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": f"too many wrong passphrases from this address: locked out for {LOCKOUT_S} s"},
                                  extra={"Retry-After": str(LOCKOUT_S)})
        body = self.body_json()
        pp = body.get("passphrase") if body else None
        if not isinstance(pp, str) or not pp or len(pp) > 256:
            self.audit("refused", reason="unlock-bad-request")
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "a JSON body {\"passphrase\": \"...\"} is required"})
        if not panelstate.term_secret_configured(srv.cfg):
            self.audit("refused", reason="unlock-no-secret")
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": NO_SECRET_MSG})
        if not srv.unlock_slots.acquire(timeout=UNLOCK_SLOT_WAIT_S):
            self.audit("refused", reason="unlock-busy")
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "too many unlock attempts are being checked right now: try again in a moment"},
                                  extra={"Retry-After": "2"})
        try:
            matched = panelstate.term_secret_matches(srv.cfg, pp)
        finally:
            srv.unlock_slots.release()
        if matched:
            uid = srv.new_unlock(ip, self.ua())
            self.audit("unlock", unlock8=uid[:8], secret_id=panelstate.term_secret_id(srv.cfg))
            return self.send_json(HTTPStatus.OK, {"ok": True, "unlock8": uid[:8], "idle_s": srv.times["unlock_idle_s"], "max_s": srv.times["unlock_max_s"]},
                                  extra={"Set-Cookie": self.cookie(uid, int(srv.times["unlock_max_s"]))})
        srv.unlock_limiter.note_failure(ip, 1.0)
        locked = srv.unlock_limiter.rate_limited(ip)
        if locked:
            srv.lockout[ip] = now + LOCKOUT_S
        self.audit("refused", reason="unlock-wrong-lockout" if locked else "unlock-wrong")
        if locked:
            return self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": f"too many wrong passphrases from this address: locked out for {LOCKOUT_S} s"},
                                  extra={"Retry-After": str(LOCKOUT_S)})
        return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "wrong passphrase"})

    def open_target(self, uid: str) -> None:
        srv: TermServer = self.server
        body = self.body_json()
        name = body.get("target") if body else None
        if not isinstance(name, str) or not name:
            self.audit("refused", reason="open-bad-request", unlock8=uid[:8])
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "a JSON body {\"target\": \"<worker session>\"} is required"})
        target, why = targetsmod.resolve(srv.cfg, name)
        if target is None:
            self.audit("refused", reason=f"target: {why}", target=name[:120], unlock8=uid[:8])
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "target not offered", "reason": why})
        tid = srv.new_attachment(uid, target)
        srv.touch(uid)
        self.audit("open", tid8=tid[:8], unlock8=uid[:8], target=target["name"], tmux=target["tmux"], pane=target["pane_id"],
                   ticket=target.get("ticket", ""))
        return self.send_json(HTTPStatus.OK, {"tid": tid, "tid8": tid[:8], "target": target["name"], "tmux": target["tmux"], "pane": target["pane_id"],
                                              "ticket": target.get("ticket", ""), "closes_in_s": srv.closes_in(tid)})

    def attached(self, uid: str, tid: str):
        """(attachment, None) or (None, reason) -- the target is re-inspected every time, against the %pane_id seen at open."""
        srv: TermServer = self.server
        a = srv.attachment(tid, uid)
        if not a:
            return None, "closed: unknown attachment, or it expired (idle or maximum time)"
        ok, why, pane = targetsmod.inspect(srv.cfg, a["target"], expect_pane=a["target"]["pane_id"])
        if not ok or pane["pane_id"] != a["target"]["pane_id"]:
            srv.drop_attachment(tid)
            self.audit("close", tid8=tid[:8], unlock8=uid[:8], target=a["target"]["name"], pane=a["target"]["pane_id"], reason=why or "pane changed")
            return None, f"closed: {why or 'the pane changed'}"
        return a, None

    def screen(self, uid: str, tid: str, head: bool) -> None:
        srv: TermServer = self.server
        a, err = self.attached(uid, tid)
        if err:
            return self.send_json(HTTPStatus.GONE, {"error": err}, head)
        text = redact(tmux.capture_pane_id(a["target"]["pane_id"]))
        srv.touch(uid)                          # a screen poll keeps the unlock alive, not the attachment: idle means no keystroke
        return self.send_json(HTTPStatus.OK, {"text": text, "at": panelstate.now_iso(), "tid8": tid[:8], "target": a["target"]["name"],
                                              "tmux": a["target"]["tmux"], "pane": a["target"]["pane_id"], "closes_in_s": srv.closes_in(tid)}, head)

    def keys(self, uid: str, tid: str) -> None:
        srv: TermServer = self.server
        a, err = self.attached(uid, tid)
        if err:
            self.drain()
            self.audit("refused", reason=err, tid8=tid[:8], unlock8=uid[:8])
            return self.send_json(HTTPStatus.GONE, {"error": err})
        body = self.body_json()
        try:
            ops = keysmod.parse_ops(body if body is not None else {})
        except ValueError as e:
            self.audit("refused", reason=f"keys: {e}", tid8=tid[:8], unlock8=uid[:8], target=a["target"]["name"])
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        text = "".join(v for k, v in ops if k == "t")
        names = [v for k, v in ops if k == "k"]
        try:
            n = tmux.send_keys(a["target"]["pane_id"], ops)
        except TmuxError as e:
            sent = int(getattr(e, "sent", 0) or 0)
            srv.touch(uid, tid)
            self.audit("keys", tid8=tid[:8], unlock8=uid[:8], target=a["target"]["name"], tmux=a["target"]["tmux"], pane=a["target"]["pane_id"],
                       text_len=len(text), text_hmac=self.text_digest(text), keys=names, n=sent, m=len(ops), partial=True, reason=str(e)[:200])
            return self.send_json(HTTPStatus.BAD_GATEWAY, {"error": f"tmux refused mid-batch after {sent} of {len(ops)} ops: {e}"[:300], "sent": sent})
        srv.touch(uid, tid)
        self.audit("keys", tid8=tid[:8], unlock8=uid[:8], target=a["target"]["name"], tmux=a["target"]["tmux"], pane=a["target"]["pane_id"],
                   text_len=len(text), text_hmac=self.text_digest(text), keys=names, n=n, m=len(ops))
        return self.send_json(HTTPStatus.OK, {"ok": True, "sent": n, "closes_in_s": srv.closes_in(tid)})

    def close(self, uid: str, tid: str) -> None:
        srv: TermServer = self.server
        self.drain()
        a = srv.attachment(tid, uid)
        if a:
            srv.drop_attachment(tid)
            self.audit("close", tid8=tid[:8], unlock8=uid[:8], target=a["target"]["name"], reason="user")
        return self.send_json(HTTPStatus.OK, {"ok": True})

    def serve_static(self, rel: str, head: bool) -> None:
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        return self.send_bytes(HTTPStatus.OK, body, ctype, head=head)


def root_sha(root: str) -> str:
    """16 hex characters identifying the project root without exposing the path (L4): what /term/health shows and
    what the process side compares."""
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]


def bind_address(bind: str) -> str:
    if bind == "intranet":
        return "0.0.0.0"
    if bind == "loopback":
        return "127.0.0.1"
    raise ZouinifyError(f"terminal.bind: {bind!r} is not loopback or intranet")


def make_server(cfg, port: int | None = None, bind: str | None = None, allowed_hosts=None, address: str | None = None,
                ssl_context=None) -> TermServer:
    """The server, bound and (with `ssl_context`) wrapped in TLS; refused without a passphrase. `address` narrows the
    bind for tests, which must never expose even a test server on the LAN; production never passes it."""
    bind = bind or cfg.terminal.get("bind", "loopback")
    port = cfg.terminal["port"] if port is None else port
    if not panelstate.term_secret_configured(cfg):
        raise ZouinifyError(NO_SECRET_MSG)
    srv = TermServer((address or bind_address(bind), port), Handler, cfg, bind, allowed_hosts, ssl_context is not None)
    if ssl_context is not None:
        srv.socket = ssl_context.wrap_socket(srv.socket, server_side=True)
    return srv


def run(srv: TermServer, url_: str) -> int:
    """Serve until shutdown() (zouinify.termctl installs the signal handlers); term.json's runtime fields exist exactly
    while the server runs."""
    cfg = srv.cfg
    port = srv.server_address[1]
    panelstate.write_term_json(cfg, port, panelstate.pid(), srv.started_at, srv.bind, srv.tls, url_)
    panelstate.term_log(cfg, f"zouinify term {panelstate.version()} serving {cfg.name} on {srv.server_address[0]}:{port} "
                             f"({srv.bind}{', TLS' if srv.tls else ''}, workers only, passphrase required from every address) pid {panelstate.pid()}")
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        panelstate.remove_term_json(cfg, panelstate.pid())
        panelstate.term_log(cfg, "zouinify term stopped")
    return 0
