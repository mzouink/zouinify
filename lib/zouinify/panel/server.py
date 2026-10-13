"""The HTTP server of the panel: stdlib only, GET/HEAD only -- any other method is 405. Routes: `/` and
`/static/*` (the page), `/health`, `/api/snapshot`, `/api/tickets/<id>`, `/api/sessions/<name>`,
`/api/logs?label=<a panel.logs label>`. Everything else is 404.

Binding (T-0165): `panel.bind: loopback` (the default) listens on 127.0.0.1 and needs no token. `panel.bind:
intranet` listens on 0.0.0.0 and requires the browser token on EVERY request from a non-loopback peer, the page
and its static files included: `Authorization: Bearer <token>` or the `zouinify_token` cookie, compared in
constant time through zouinify.panelstate (the panel never sees the hash). A token-less navigation to `/` gets a
401 unlock page whose only script stores the `#t=` fragment as a SameSite=Strict cookie and reloads; the fragment
never reaches the server, so no token ever appears in a log or a URL the server sees. Repeated refusals from one
address are rate-limited (429), every refusal is an AUDIT line in server.log, a foreign Host or Origin is 403.
The process side (start/stop, token rotation, signals) lives in zouinify.panelctl; this module only serves."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from zouinify import panelstate, readonly
from zouinify.panel.sources import Snapshot

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}
ALLOW = "GET, HEAD"
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'none'"
REFUSED = {"error": "method not allowed: the panel is read-only (v1, T-0160); receipts arrive with T-0162", "allow": ALLOW}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
QUERY_RE = re.compile(r"\?[^ \"]*")     # a mistyped /?t=<token> must never reach the log
COOKIE = "zouinify_token"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
RATE_LIMIT = 10          # refusals per window per address ...
RATE_WINDOW_S = 60.0     # ... before 429 for the rest of the window
LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# The unlock page: the 401 answer to a token-less navigation. Its only script reads the #t= fragment (which the
# server never sees), stores it as a SameSite=Strict cookie and reloads; without a fragment it explains.
UNLOCK_SCRIPT = (
    "(function(){var m=location.hash.match(/[#&]t=([A-Za-z0-9_-]{32,256})/);var el=document.getElementById('msg');"
    "if(!m){el.textContent='No valid token in this browser. Open the exact address printed by `zouinify panel token rotate` "
    "in your own terminal (it ends with #t=...).';return;}"
    "document.cookie='" + COOKIE + "='+m[1]+'; Path=/; SameSite=Strict; Max-Age=7776000';"
    "history.replaceState(null,'',location.pathname+location.search);location.reload();})();")
UNLOCK_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-"
              + base64.b64encode(hashlib.sha256(UNLOCK_SCRIPT.encode("utf-8")).digest()).decode("ascii") + "'; form-action 'none'")
UNLOCK_HTML = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
               "<title>zouinify</title><style>body{background:#081321;color:#eef6ff;font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px}"
               "main{max-width:36rem;margin:10vh auto}code{color:#26e0d0}</style></head><body><main><h1>zouin<span style=\"color:#26e0d0\">ify</span></h1>"
               "<p id=\"msg\">Unlocking this browser with the token from the address…</p>"
               "<p>Read-only panel on the lab network: a browser token is required on every request. Nothing here can launch, "
               "kill, type or deploy.</p></main><script>" + UNLOCK_SCRIPT + "</script></body></html>")


class Handler(BaseHTTPRequestHandler):
    server_version = "zouinify-panel/" + panelstate.version()
    sys_version = ""
    protocol_version = "HTTP/1.1"
    auth_label = ""

    # -- plumbing -------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - one line per request into the server log, never a token
        msg = QUERY_RE.sub("?<query-redacted>", CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), fmt % args))
        panelstate.log(self.server.cfg, f"{self.address_string()} {msg}{self.auth_label}")

    def audit(self, status: int, reason: str) -> None:
        ua = CONTROL_RE.sub("", self.headers.get("User-Agent") or "")[:80]
        path = CONTROL_RE.sub("", self.path.split("?", 1)[0])[:120]
        panelstate.log(self.server.cfg, f"AUDIT {status} {reason} ip={self.client_address[0]} {self.command} {path} ua=\"{ua}\"")

    def peer_is_loopback(self) -> bool:
        return self.client_address[0] in LOOPBACK_PEERS

    def host_allowed(self) -> bool:
        """Only the panel's own names may address it: 127.0.0.1 / localhost, plus the LAN names the process side
        computed when bound to the intranet. A page served under another name (DNS rebinding) and any foreign
        Origin are refused."""
        port = self.server.server_address[1]
        names = {"127.0.0.1", "localhost"} | set(self.server.allowed_hosts)
        hosts = {f"{n}:{port}" for n in names}
        if port == 80:
            hosts |= names
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in hosts:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        return not origin or origin in {f"http://{h}" for h in hosts}

    def presented_token(self):
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            tok = auth[7:].strip()
            return tok if TOKEN_RE.match(tok) else None
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and TOKEN_RE.match(v.strip()):
                return v.strip()
        return None

    def authorize(self) -> tuple:
        """(status, reason): 200 when the request may proceed, 401 or 429 otherwise. Loopback peers are never
        asked for the token (same-uid processes can read everything directly); every other peer always is."""
        srv = self.server
        self.auth_label = ""              # per request: a keep-alive connection reuses this handler
        if not srv.token_required or self.peer_is_loopback():
            return 200, "loopback"
        ip = self.client_address[0]
        if srv.rate_limited(ip):
            return 429, "rate-limited"
        tok = self.presented_token()
        if not tok:
            srv.note_failure(ip)
            return 401, "no-token"
        if panelstate.token_matches(srv.cfg, tok):
            self.auth_label = f" token={panelstate.token_id(srv.cfg) or '?'}"
            return 200, "token"
        srv.note_failure(ip)
        return 401, "bad-token"

    def send_bytes(self, status: int, body: bytes, ctype: str, extra: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", (extra or {}).pop("Content-Security-Policy", CSP))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, status: int, obj, head: bool = False, extra: dict | None = None) -> None:
        self.send_bytes(status, json.dumps(obj, indent=1, default=str).encode("utf-8"), "application/json; charset=utf-8", extra, head=head)

    def forbid(self, head: bool = False) -> None:
        self.audit(403, "host-or-origin")
        self.send_json(HTTPStatus.FORBIDDEN, {"error": "host or origin not allowed: the panel answers only to its own names "
                                                       "(127.0.0.1, localhost and the configured LAN host) with no foreign Origin"}, head)

    def deny(self, status: int, reason: str, path: str, head: bool = False) -> None:
        self.audit(status, reason)
        if status == 429:
            return self.send_json(HTTPStatus.TOO_MANY_REQUESTS,
                                  {"error": "too many refused requests from this address: wait a minute (something other than you may be knocking)"},
                                  head, {"Retry-After": str(int(RATE_WINDOW_S))})
        if path in ("/", "/index.html"):
            return self.send_bytes(HTTPStatus.UNAUTHORIZED, UNLOCK_HTML.encode("utf-8"), "text/html; charset=utf-8",
                                   {"Content-Security-Policy": UNLOCK_CSP, "WWW-Authenticate": "Bearer realm=\"zouinify\""}, head=head)
        return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "token missing or wrong: open the address printed by "
                                                                 "`zouinify panel token rotate` in your own terminal"},
                              head, {"WWW-Authenticate": "Bearer realm=\"zouinify\""})

    def refuse(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 1 << 20))
        except ValueError:
            pass
        if not self.host_allowed():
            return self.forbid()
        status, reason = self.authorize()
        if status != 200:
            return self.deny(status, reason, "")
        self.audit(405, "write-attempt")
        self.send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, json.dumps(REFUSED).encode("utf-8"), "application/json; charset=utf-8", {"Allow": ALLOW})

    def do_POST(self):
        self.refuse()

    def do_PUT(self):
        self.refuse()

    def do_DELETE(self):
        self.refuse()

    def do_PATCH(self):
        self.refuse()

    def do_OPTIONS(self):
        self.refuse()

    def do_HEAD(self):
        self.handle_get(head=True)

    def do_GET(self):
        self.handle_get(head=False)

    # -- routing --------------------------------------------------------------------------------------
    def handle_get(self, head: bool) -> None:
        snap: Snapshot = self.server.snap
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        fresh = q.get("fresh", ["0"])[0] in ("1", "true")
        if not self.host_allowed():
            return self.forbid(head)
        status, reason = self.authorize()
        if status != 200:
            return self.deny(status, reason, path, head)
        try:
            if path in ("/", "/index.html"):
                return self.serve_static("index.html", head)
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):], head)
            if path == "/health":
                return self.send_json(HTTPStatus.OK, {"ok": True, "service": "zouinify-panel", "version": panelstate.version(),
                                                      "root": snap.cfg.root, "project": snap.cfg.name, "bind": snap.bind, "port": snap.port,
                                                      "pid": panelstate.pid(), "started_at": snap.started_at, "write_path": False,
                                                      "token_required": self.server.token_required}, head)
            if path == "/api/snapshot":
                return self.send_json(HTTPStatus.OK, snap.build(fresh=fresh), head)
            if path.startswith("/api/tickets/"):
                tid = path[len("/api/tickets/"):]
                d = snap.ticket(tid) if readonly.is_ticket_id(tid) else None
                if d:
                    return self.send_json(HTTPStatus.OK, d, head)
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": f"no ticket {tid[:12]!r}"}, head)
            if path.startswith("/api/sessions/"):
                name = path[len("/api/sessions/"):]
                d = snap.session(name, fresh=fresh) if name and "/" not in name else None
                if d:
                    return self.send_json(HTTPStatus.OK, d, head)
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such session of this project"}, head)
            if path == "/api/logs":
                label = q.get("label", [""])[0]
                logs, _, _ = snap.get("logs", fresh)
                d = (logs or {}).get(label)
                if d:
                    return self.send_json(HTTPStatus.OK, dict(d, label=label), head)
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": f"no log labelled {label!r}; panel.logs labels: {sorted((logs or {}).keys())}"}, head)
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)
        except Exception as e:  # noqa: BLE001 - never take the server down for one request
            self.log_message("error on %s: %r", path, e)
            return self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"[:300]}, head)

    def serve_static(self, rel: str, head: bool) -> None:
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        return self.send_bytes(HTTPStatus.OK, body, ctype, head=head)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg, bind: str, allowed_hosts, token_required: bool):
        super().__init__(address, handler)
        self.cfg = cfg
        self.bind = bind
        self.allowed_hosts = {str(h).strip().lower() for h in (allowed_hosts or ()) if str(h).strip()}
        self.token_required = token_required
        self.started_at = panelstate.now_iso()
        self.snap = Snapshot(cfg, self.server_address[1], bind, self.started_at)
        self.failures: dict = {}
        self.failures_lock = threading.Lock()

    def rate_limited(self, ip: str) -> bool:
        now = time.time()
        with self.failures_lock:
            recent = [t for t in self.failures.get(ip, []) if now - t < RATE_WINDOW_S]
            self.failures[ip] = recent
            return len(recent) >= RATE_LIMIT

    def note_failure(self, ip: str) -> None:
        with self.failures_lock:
            self.failures.setdefault(ip, []).append(time.time())
            if len(self.failures) > 1000:   # never grow without bound
                self.failures = {k: v for k, v in self.failures.items() if v and time.time() - v[-1] < RATE_WINDOW_S}


def bind_address(bind: str) -> str:
    """loopback -> 127.0.0.1; intranet -> every interface."""
    if bind == "intranet":
        return "0.0.0.0"
    if bind == "loopback":
        return "127.0.0.1"
    readonly.fail(f"panel.bind: {bind!r} is not loopback or intranet")
    return "127.0.0.1"


def make_server(cfg, port: int | None = None, bind: str | None = None, allowed_hosts=None, address: str | None = None) -> PanelServer:
    """loopback -> 127.0.0.1, no token; intranet -> 0.0.0.0, token required on every non-loopback request (the
    token must already be set: the process side prints it on a real TTY). `address` narrows the bind for tests,
    which must never expose even a test token on the LAN; production never passes it."""
    bind = bind or cfg.panel.get("bind", "loopback")
    port = cfg.panel["port"] if port is None else port
    if bind == "intranet" and not panelstate.token_configured(cfg):
        readonly.fail("panel.bind: intranet needs the browser token first: run `zouinify panel token rotate` in your own "
                      "terminal (never inside a Claude session), then start the panel")
    return PanelServer((address or bind_address(bind), port), Handler, cfg, bind, allowed_hosts, bind == "intranet")


def run(srv: PanelServer) -> int:
    """Serve until shutdown() is called (zouinify.panelctl installs the signal handlers and keeps the
    user-level projects list); the pid file's runtime fields exist exactly while the server runs."""
    cfg = srv.cfg
    actual = srv.server_address[1]
    panelstate.write_panel_json(cfg, actual, panelstate.pid(), srv.started_at)
    srv.snap.start_refresher()
    panelstate.log(cfg, f"zouinify panel {panelstate.version()} serving {cfg.name} on {srv.server_address[0]}:{actual} "
                        f"(read-only, {srv.bind}{', token required beyond loopback' if srv.token_required else ''}) pid {panelstate.pid()}")
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.snap.stop()
        srv.server_close()
        panelstate.remove_panel_json(cfg, panelstate.pid())
        panelstate.log(cfg, "zouinify panel stopped")
    return 0
