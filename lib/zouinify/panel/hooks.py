"""The hook listener (T-0163): a SECOND HTTP server, bound to 127.0.0.1 only, inside the panel process. It accepts
`POST /api/hook` from the Claude Code hook script (templates/hooks/zouinify-hook.sh, installed by the human in
~/.claude/settings.json) and nothing else: the LAN-facing server keeps answering 405 to every POST, so a forged
event never arrives from the network and the token story is untouched.

Events are DATA. The panel shows a session's last reply, what it waits for and since when; it never acts on an
event. Only allowlisted fields are kept. This project's own events keep the reply or notification text, truncated
and passed through redact(); an event whose cwd lies outside the project keeps NO text at all, not in memory, not
in events.jsonl, not over SSE (the hook fires for every Claude session of the user: desk condition 1, the rule of
the "Waiting for me" page). The only file write is panelstate.append_event().

Who may post: a loopback peer whose socket belongs to this process's uid (T-0169, webauth.same_user_peer via
/proc/net/tcp); another local user of the workstation is refused with 403 and audited. Linux only; when the
lookup fails the listener fails closed. A browser cannot post either (T-0182): any Origin header is refused, the
body must be application/json (so a page would have to preflight, and OPTIONS is 405), Host must be the listener's
own loopback name, and the per-start nonce from panel.json (0600) must come back as X-Zouinify-Hook.

The desk's side of the receipts (T-0162) lives here too, under the same checks (same-uid loopback peer, no Origin, the
listener's own Host, the nonce): `GET /api/poll?since=<id>&timeout_ms=<n>` blocks until a receipt is pending,
`POST /api/ack {ids}` acknowledges what the desk printed (fetching marks nothing: at-least-once delivery), and
`GET /api/receipts/<id>` answers from memory. Never on the LAN-facing port: a LAN device without the token cannot read
receipts, and the desk needs no browser token (it reads the nonce from panel.json, its own user's 0600 file). Another
same-uid process can read that nonce too, so it can fetch or acknowledge receipts (delay or swallow a click), and it
can run a listener of its own and point panel.json at it: a receipt is a claim, full stop, and the desk verifies
every one before acting.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from zouinify import panelstate, webauth
from zouinify.panel import receipts as receipts_mod
from zouinify.panel.redact import redact
from zouinify.panel.sources import project_roots, under

MAX_BODY = 1024 * 1024               # a long reply must still arrive (its Stop clears "waiting"); larger is refused unread
MAX_TEXT = 2000                      # the reply text kept (redacted) for this project's own events
MAX_EVENTS = 500                     # the in-memory ring
MAX_SESSIONS = 1000                  # per-session views kept (least recently updated dropped first)
MAX_STREAM_S = 30 * 60               # an SSE client is sent away after this and reconnects
HEARTBEAT_S = 15.0
EVENTS = ("Stop", "Notification", "SessionStart", "SessionEnd", "UserPromptSubmit")
WAITING_TYPES = ("permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")
CLEARING_EVENTS = ("Stop", "UserPromptSubmit", "SessionEnd", "SessionStart")
SHORT = {"hook_event_name": 40, "session_id": 64, "permission_mode": 24, "notification_type": 40, "source": 24, "reason": 24}
SHORT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")     # the short fields are identifiers, never prose (a foreign event's only channel)
UNDERSCORE_RE = re.compile(r"_")
QUERY_RE = re.compile(r"\?[^ \"]*")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
HOOK_HEADER = "X-Zouinify-Hook"
DESK_PATHS = ("/api/poll", "/api/receipts/", "/api/ack")
DESK_ALLOW = "the hook listener takes POST /api/hook and, for the desk, GET /api/poll, GET /api/receipts/<id> and POST /api/ack"


def clip(value, n: int) -> str:
    """A short identifier field: kept only when it looks like one (letters, digits, _ . : -), else dropped."""
    s = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return s if len(s) <= n and SHORT_RE.match(s) else ""


class EventStore:
    """The ring of accepted events and, per session id, the view the sessions table needs. Thread-safe; notifies
    SSE streams and the Snapshot (which re-reads the sessions source) on every event."""

    def __init__(self, cfg, roots=None):
        self.cfg = cfg
        self.roots = set(roots) if roots is not None else project_roots(cfg)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.events: list = []
        self.by_session: dict = {}
        self.seq = 0
        self.listeners: list = []

    def accept(self, raw) -> dict | None:
        """The allowlisted event built from a hook payload, stored; None when the payload is not an event we keep."""
        if not isinstance(raw, dict):
            return None
        name = clip(raw.get("hook_event_name"), SHORT["hook_event_name"])
        if name not in EVENTS:
            return None
        cwd = raw.get("cwd")
        mine = isinstance(cwd, str) and os.path.isabs(cwd) and under(cwd, self.roots)   # a relative cwd is nobody's
        ev = {"at": panelstate.now_iso(), "event": name, "session_id": clip(raw.get("session_id"), SHORT["session_id"]),
              "project": mine, "permission_mode": clip(raw.get("permission_mode"), SHORT["permission_mode"]),
              "notification_type": clip(raw.get("notification_type"), SHORT["notification_type"]) if name == "Notification" else "",
              "source": clip(raw.get("source"), SHORT["source"]) if name == "SessionStart" else "",
              "reason": clip(raw.get("reason"), SHORT["reason"]) if name == "SessionEnd" else "",
              "text": ""}
        if mine:
            text = raw.get("last_assistant_message") if name == "Stop" else raw.get("message") if name == "Notification" else ""
            if isinstance(text, str) and text.strip():
                ev["text"] = redact(text.strip())[:MAX_TEXT]
        with self.cond:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            del self.events[:-MAX_EVENTS]
            self.update_session(ev)
            self.cond.notify_all()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener never breaks the listener
                pass
        panelstate.append_event(self.cfg, ev)
        return ev

    def update_session(self, ev: dict) -> None:
        sid = ev["session_id"]
        if not sid:
            return
        s = self.by_session.pop(sid, None) or {"known": True, "last_event": "", "last_at": "", "last_reply": "", "last_reply_at": "",
                                                "waiting": False, "waiting_for": "", "since": "", "project": ev["project"]}
        self.by_session[sid] = s                          # re-inserted last: the dict is ordered by last update
        while len(self.by_session) > MAX_SESSIONS:
            del self.by_session[next(iter(self.by_session))]
        s["last_event"] = ev["event"] + (f"({ev['notification_type']})" if ev["notification_type"] else "")
        s["last_at"] = ev["at"]
        if ev["event"] == "Stop":
            s["last_reply"], s["last_reply_at"] = ev["text"], ev["at"]
        if ev["event"] == "Notification" and ev["notification_type"] in WAITING_TYPES:
            if not s["waiting"]:
                s["since"] = ev["at"]
            s["waiting"], s["waiting_for"] = True, ev["text"] or UNDERSCORE_RE.sub(" ", ev["notification_type"])
        elif ev["event"] in CLEARING_EVENTS:
            s["waiting"], s["waiting_for"], s["since"] = False, "", ""

    def state(self, session_id) -> dict | None:
        """The per-session view, or None when no event was seen for that session (the screen heuristic then stands)."""
        if not session_id:
            return None
        with self.lock:
            s = self.by_session.get(str(session_id))
            return dict(s) if s else None

    def recent(self, n: int = 50) -> list:
        with self.lock:
            return [dict(e) for e in self.events[-n:]]

    def wait_after(self, seq: int, timeout: float) -> list:
        """Events with a sequence number above seq, waiting up to timeout seconds for the first one."""
        with self.cond:
            if not any(e["seq"] > seq for e in self.events):
                self.cond.wait(timeout)
            return [dict(e) for e in self.events if e["seq"] > seq]

    def count(self) -> int:
        with self.lock:
            return self.seq


def wire_event(ev: dict) -> dict:
    """The event as the page sees it: the text only for this project's own events (never for a foreign one)."""
    out = {k: ev[k] for k in ("seq", "at", "event", "session_id", "project", "notification_type", "source", "reason")}
    if ev.get("project"):
        out["text"] = ev.get("text", "")
    return out


class HookHandler(BaseHTTPRequestHandler):
    server_version = "zouinify-hook/" + panelstate.version()
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10

    def log_message(self, fmt, *args):  # noqa: D401 - one line per request into the server log, never a payload
        msg = QUERY_RE.sub("?<query-redacted>", CONTROL_RE.sub("", fmt % args))[:200]
        panelstate.log(self.server.cfg, f"hook {self.address_string()} {msg}")

    def host_ok(self) -> bool:
        """The listener answers to its own loopback names only (a page under another name cannot address it)."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def peer_is_loopback(self) -> bool:
        return self.client_address[0] in webauth.LOOPBACK_PEERS

    def peer_is_same_user(self) -> bool:
        """The hook comes from one of THIS user's Claude sessions: a loopback peer whose socket is ours (T-0169)."""
        return self.peer_is_loopback() and webauth.same_user_peer(self.connection)

    def reply(self, status, obj: dict, extra: dict | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):   # the peer left: nothing to report, no traceback in the log
            self.close_connection = True

    def refuse(self, status, reason: str) -> None:
        path = CONTROL_RE.sub("", self.path.split("?", 1)[0])[:80]
        panelstate.log(self.server.cfg, f"AUDIT hook {status} {reason} ip={self.client_address[0]} {self.command} {path}")
        self.close_connection = True
        allow = None
        if status == 405:
            allow = {"Allow": "GET" if path == DESK_PATHS[0] or path.startswith(DESK_PATHS[1]) else "POST"}
        self.reply(status, {"error": reason}, allow)

    def serve_ack(self) -> None:
        """POST /api/ack {ids: [...]}: the desk acknowledges the receipts it printed; they stop being pending."""
        why = self.desk_refusal()
        if why:
            return self.refuse(HTTPStatus.FORBIDDEN, why)
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self.refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "an ack is application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.refuse(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length <= 0 or length > 64 * 1024:
            return self.refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "an ack is 1..65536 bytes")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            return self.refuse(HTTPStatus.BAD_REQUEST, "not JSON")
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list):
            return self.refuse(HTTPStatus.BAD_REQUEST, "an ack is {ids: [<receipt id>, ...]}")
        done = self.server.receipts.ack(ids)
        panelstate.log(self.server.cfg, f"ACK {self.address_string()} {len(done)} of {len(ids)} receipt(s): {' '.join(done) or '-'}")
        self.reply(HTTPStatus.OK, {"acked": done, "at": panelstate.now_iso()})

    def do_POST(self):
        if not self.peer_is_same_user():
            return self.refuse(HTTPStatus.FORBIDDEN, "hooks are accepted from this user's processes on this machine only")
        path = self.path.split("?", 1)[0]
        if path == DESK_PATHS[2]:
            if self.server.receipts is None:
                return self.refuse(HTTPStatus.NOT_FOUND, "no receipts on this listener")
            return self.serve_ack()
        if path != "/api/hook":
            return self.refuse(HTTPStatus.NOT_FOUND, "not found")
        if self.headers.get("Origin") is not None:
            return self.refuse(HTTPStatus.FORBIDDEN, "a browser page may not post hooks (Origin present)")
        if not self.host_ok():
            return self.refuse(HTTPStatus.FORBIDDEN, "Host is not the listener's own loopback name")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self.refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "hooks are application/json (a browser would have to preflight)")
        if not panelstate.hook_nonce_matches(self.server.cfg, self.headers.get(HOOK_HEADER)):
            return self.refuse(HTTPStatus.FORBIDDEN, "missing or wrong X-Zouinify-Hook (the per-start nonce from panel.json)")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.refuse(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length <= 0 or length > MAX_BODY:
            return self.refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"a hook payload is 1..{MAX_BODY} bytes")
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            return self.refuse(HTTPStatus.BAD_REQUEST, "not JSON (or nested too deep)")
        try:
            ev = self.server.store.accept(raw)
        except (ValueError, TypeError, RecursionError):      # a NUL byte in a path, an absurd shape: a bad request, not a crash
            return self.refuse(HTTPStatus.BAD_REQUEST, "malformed hook payload")
        if ev is None:
            return self.refuse(HTTPStatus.BAD_REQUEST, "not a hook event the panel keeps (Stop, Notification, SessionStart, SessionEnd, UserPromptSubmit)")
        panelstate.log(self.server.cfg, f"HOOK {ev['event']}{'(' + ev['notification_type'] + ')' if ev['notification_type'] else ''} "
                                        f"session={ev['session_id'][:8]} project={'yes' if ev['project'] else 'no'} seq={ev['seq']}")
        self.reply(HTTPStatus.OK, {"ok": True, "seq": ev["seq"], "kept_text": bool(ev["text"])})

    def desk_refusal(self):
        """The reason a request for the desk's side is refused, or None: the same four checks as a hook post."""
        if not self.peer_is_same_user():
            return "the desk's side is served to this user's processes on this machine only"
        if self.headers.get("Origin") is not None:
            return "a browser page may not read receipts here (Origin present)"
        if not self.host_ok():
            return "Host is not the listener's own loopback name"
        if not panelstate.hook_nonce_matches(self.server.cfg, self.headers.get(HOOK_HEADER)):
            return "missing or wrong X-Zouinify-Hook (the per-start nonce from panel.json)"
        return None

    def serve_poll(self, query: str) -> None:
        """GET /api/poll?since=<id>&timeout_ms=<n>: block until a receipt is pending (a space every HEARTBEAT_S keeps the
        connection alive, like lavish), answer `{receipts: [...], since, presence, at}` and close. A timeout answers an
        empty list; what was handed out is marked delivered."""
        q = urllib.parse.parse_qs(query)
        since = q.get("since", [""])[0]
        since = since if receipts_mod.ID_RE.match(since) else ""
        try:
            timeout_s = int(q.get("timeout_ms", [str(int(receipts_mod.POLL_DEFAULT_S * 1000))])[0]) / 1000.0
        except ValueError:
            timeout_s = receipts_mod.POLL_DEFAULT_S
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def beat():
            self.wfile.write(b" ")
            self.wfile.flush()
        batch: list = []
        try:
            batch, known = self.server.receipts.poll(since, timeout_s, beat)
            body = {"receipts": [receipts_mod.wire(r) for r in batch], "since": batch[-1]["id"] if batch else since,
                    "since_known": known if since else None, "at": panelstate.now_iso()}
            self.wfile.write(json.dumps(body).encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        panelstate.log(self.server.cfg, f"POLL {self.address_string()} since={since or '-'} -> {len(batch)} receipt(s)")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = urllib.parse.unquote(path)
        if path == DESK_PATHS[0] or path.startswith(DESK_PATHS[1]):
            if self.server.receipts is None:
                return self.refuse(HTTPStatus.NOT_FOUND, "no receipts on this listener")
            why = self.desk_refusal()
            if why:
                return self.refuse(HTTPStatus.FORBIDDEN, why)
            if path == DESK_PATHS[0]:
                return self.serve_poll(query)
            rec = self.server.receipts.get(path[len(DESK_PATHS[1]):])
            if rec is None:
                return self.reply(HTTPStatus.NOT_FOUND, {"error": "no such receipt in memory (the audit file is not consulted)"})
            return self.reply(HTTPStatus.OK, rec)
        self.refuse(HTTPStatus.METHOD_NOT_ALLOWED, DESK_ALLOW)

    def not_allowed(self):
        self.refuse(HTTPStatus.METHOD_NOT_ALLOWED, DESK_ALLOW)

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_allowed


class HookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64              # closing several sessions at once fires many hooks together: no dropped bursts

    def __init__(self, cfg, store: EventStore, port: int, receipts=None):
        super().__init__(("127.0.0.1", port), HookHandler)      # loopback only, whatever panel.bind says
        self.cfg = cfg
        self.store = store
        self.receipts = receipts                                 # the ReceiptStore the panel port writes (T-0162)


def make_hook_server(cfg, store: EventStore, port: int, receipts=None) -> HookServer:
    """Bind the listener; when the configured port is taken, fall back to an ephemeral one and say so in the log:
    the hook script reads the ACTUAL port from panel.json, so the hooks still arrive."""
    try:
        return HookServer(cfg, store, port, receipts)
    except OSError as e:
        if not port:
            raise
        panelstate.log(cfg, f"hook listener: 127.0.0.1:{port} is taken ({e.strerror or e}); using an ephemeral port instead "
                            "(set panel.hook_port to a free one)")
        return HookServer(cfg, store, 0, receipts)


def stream(handler, store: EventStore, stop_event, write) -> None:
    """The body of GET /api/events on the main server: an SSE feed of wire_event() lines with a heartbeat, until the
    client leaves, the server stops or MAX_STREAM_S pass. `write(bytes)` is the handler's wfile.write + flush."""
    seq = store.count()
    write(f": zouinify events from seq {seq}\n\n".encode("utf-8"))
    started = time.time()
    while not stop_event.is_set() and time.time() - started < MAX_STREAM_S:
        events = store.wait_after(seq, HEARTBEAT_S)
        if not events:
            write(b": ping\n\n")
            continue
        for ev in events:
            seq = max(seq, ev["seq"])
            write(f"id: {ev['seq']}\nevent: hook\ndata: {json.dumps(wire_event(ev))}\n\n".encode("utf-8"))
