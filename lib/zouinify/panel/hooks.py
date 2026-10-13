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
lookup fails the listener fails closed.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from zouinify import panelstate, webauth
from zouinify.panel.redact import redact
from zouinify.panel.sources import project_roots, under

MAX_BODY = 64 * 1024                 # a hook payload is small; anything larger is refused unread
MAX_TEXT = 2000                      # the reply text kept (redacted) for this project's own events
MAX_EVENTS = 500                     # the in-memory ring
MAX_SESSIONS = 1000                  # per-session views kept (least recently updated dropped first)
MAX_STREAM_S = 30 * 60               # an SSE client is sent away after this and reconnects
HEARTBEAT_S = 15.0
EVENTS = ("Stop", "Notification", "SessionStart", "SessionEnd", "UserPromptSubmit")
WAITING_TYPES = ("permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")
CLEARING_EVENTS = ("Stop", "UserPromptSubmit", "SessionEnd", "SessionStart")
SHORT = {"hook_event_name": 40, "session_id": 64, "permission_mode": 24, "notification_type": 40, "source": 24, "reason": 24}
UNDERSCORE_RE = re.compile(r"_")


def clip(value, n: int) -> str:
    return str(value)[:n] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


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
        panelstate.log(self.server.cfg, f"hook {self.address_string()} {fmt % args}")

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
        self.end_headers()
        self.wfile.write(body)

    def refuse(self, status, reason: str) -> None:
        panelstate.log(self.server.cfg, f"AUDIT hook {status} {reason} ip={self.client_address[0]} {self.command} {self.path[:80]}")
        self.close_connection = True
        self.reply(status, {"error": reason}, {"Allow": "POST"} if status == 405 else None)

    def do_POST(self):
        if not self.peer_is_same_user():
            return self.refuse(HTTPStatus.FORBIDDEN, "hooks are accepted from this user's processes on this machine only")
        if self.path.split("?", 1)[0] != "/api/hook":
            return self.refuse(HTTPStatus.NOT_FOUND, "not found")
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

    def do_GET(self):
        self.refuse(HTTPStatus.METHOD_NOT_ALLOWED, "the hook listener takes POST /api/hook only")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET


class HookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg, store: EventStore, port: int):
        super().__init__(("127.0.0.1", port), HookHandler)      # loopback only, whatever panel.bind says
        self.cfg = cfg
        self.store = store


def make_hook_server(cfg, store: EventStore, port: int) -> HookServer:
    """Bind the listener; when the configured port is taken, fall back to an ephemeral one and say so in the log:
    the hook script reads the ACTUAL port from panel.json, so the hooks still arrive."""
    try:
        return HookServer(cfg, store, port)
    except OSError as e:
        if not port:
            raise
        panelstate.log(cfg, f"hook listener: 127.0.0.1:{port} is taken ({e.strerror or e}); using an ephemeral port instead "
                            "(set panel.hook_port to a free one)")
        return HookServer(cfg, store, 0)


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
