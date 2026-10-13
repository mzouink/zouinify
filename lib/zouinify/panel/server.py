"""The HTTP server of the panel: stdlib only, loopback only (v1), GET/HEAD only -- any other method is
405. Routes: `/` and `/static/*` (the page), `/health`, `/api/snapshot`, `/api/tickets/<id>`,
`/api/sessions/<name>`, `/api/logs?label=<a panel.logs label>`. Everything else is 404. The process
side (start/stop, signals) lives in zouinify.panelctl; this module only serves."""
from __future__ import annotations

import json
import os
import re
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


class Handler(BaseHTTPRequestHandler):
    server_version = "zouinify-panel/" + panelstate.version()
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - one line per request into the server log
        msg = CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), fmt % args)
        panelstate.log(self.server.cfg, f"{self.address_string()} {msg}")

    def host_allowed(self) -> bool:
        """Only the panel's own origin may talk to it: a page served to a browser under another name (DNS
        rebinding: `Host: evil.example` resolving to 127.0.0.1) is refused, and so is a foreign Origin."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in hosts:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        return not origin or origin in {f"http://{h}" for h in hosts}

    def forbid(self, head: bool = False) -> None:
        self.send_json(HTTPStatus.FORBIDDEN, {"error": "host or origin not allowed: the panel answers only to 127.0.0.1:<port> "
                                                       "and localhost:<port> with no foreign Origin"}, head)

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

    def send_json(self, status: int, obj, head: bool = False) -> None:
        self.send_bytes(status, json.dumps(obj, indent=1, default=str).encode("utf-8"), "application/json; charset=utf-8", head=head)

    def refuse(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 1 << 20))
        except ValueError:
            pass
        if not self.host_allowed():
            return self.forbid()
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
        try:
            if path in ("/", "/index.html"):
                return self.serve_static("index.html", head)
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):], head)
            if path == "/health":
                return self.send_json(HTTPStatus.OK, {"ok": True, "service": "zouinify-panel", "version": panelstate.version(),
                                                      "root": snap.cfg.root, "project": snap.cfg.name, "bind": snap.bind, "port": snap.port,
                                                      "pid": panelstate.pid(), "started_at": snap.started_at, "write_path": False}, head)
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
            self.log_message("error on %s: %r", self.path, e)
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

    def __init__(self, address, handler, cfg, bind: str):
        super().__init__(address, handler)
        self.cfg = cfg
        self.started_at = panelstate.now_iso()
        self.snap = Snapshot(cfg, self.server_address[1], bind, self.started_at)


def make_server(cfg, port: int | None = None, bind: str | None = None) -> PanelServer:
    """A server bound to 127.0.0.1. `panel.bind: intranet` is refused until the token exists (T-0162)."""
    bind = bind or cfg.panel.get("bind", "loopback")
    if bind != "loopback":
        readonly.fail("panel.bind: intranet needs the browser token and the hardening of T-0162; "
                      "v1 binds to loopback only (set panel.bind: loopback, or ssh -L to reach it)")
    port = cfg.panel["port"] if port is None else port
    return PanelServer(("127.0.0.1", port), Handler, cfg, bind)


def run(srv: PanelServer) -> int:
    """Serve until shutdown() is called (zouinify.panelctl installs the signal handlers and keeps the
    user-level projects list); the pid file exists exactly while the server runs."""
    cfg = srv.cfg
    actual = srv.server_address[1]
    panelstate.write_panel_json(cfg, actual, panelstate.pid(), srv.started_at)
    srv.snap.start_refresher()
    panelstate.log(cfg, f"zouinify panel {panelstate.version()} serving {cfg.name} on http://127.0.0.1:{actual}/ "
                        f"(read-only, loopback) pid {panelstate.pid()}")
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.snap.stop()
        srv.server_close()
        panelstate.remove_panel_json(cfg, panelstate.pid())
        panelstate.log(cfg, "zouinify panel stopped")
    return 0
