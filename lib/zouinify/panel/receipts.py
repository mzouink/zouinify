"""Receipts (T-0162): the panel's one write. A click on the page becomes a receipt, a CLAIM the desk fetches with its
poll and verifies before acting; nothing here runs, launches, kills, types, moves a ticket or deploys.

- `POST /api/receipts` on the panel port (zouinify.panel.server) carries `{item_id | kind, ticket, text}` and needs the
  browser token on every bind, even from a same-uid loopback peer: every Claude session of this user is such a peer,
  and the token (the HttpOnly cookie set by `GET /api/unlock`, or `Authorization: Bearer`) is what tells the human's
  browser from a session. `build()` turns the body into a receipt or a refusal: tier 2 (an item the classifier puts
  there, a ticket with any tier-2 item in the inbox, a kind that names an approval, a text of ANY kind that names
  CLAUDE.md or settings, Unicode tricks normalised away first) has no endpoint behaviour (403).
- `GET /api/poll`, `GET /api/receipts/<id>` and `POST /api/ack` are served by the loopback-only listener
  (zouinify.panel.hooks) to a same-uid peer that presents the per-start nonce: the desk needs no browser token and a
  LAN device cannot read receipts. A poll blocks until a receipt is PENDING (not acknowledged), heartbeat spaces every
  HEARTBEAT_S, and hands out every pending receipt (or everything after `since=<id>`); fetching marks nothing: the
  desk acknowledges by id after it printed them, and an unacknowledged receipt comes back on the next poll with a
  `redelivered` count. That is at-least-once delivery (a connection that dies mid-write loses nothing), NOT a defence
  against another same-uid process: one can read the nonce from panel.json and fetch or acknowledge receipts too
  (delay or swallow a click), though it cannot create one without the token; this is why the desk verifies every
  receipt and why a click that never arrived is simply clicked again. A repeated click of the same item or kind
  while its receipt is open (not acknowledged, or acknowledged less than IDEMPOTENT_S ago) returns the same id.
- Everything lives in memory (a ring of MAX_RECEIPTS). .ops/state/audit.jsonl (through zouinify.panelstate) is a
  WRITE-ONLY trail of accepted receipts, hand-outs, acknowledgements and refusals: nothing reads it back into the
  channel, so a line any same-user process appends there is never a receipt; a restart drops what was pending and
  the page says so. `get()` answers from memory only.
- Rate limit per token id: RATE_MINUTE accepted receipts a minute and RATE_HOUR an hour; beyond, 429 with a banner
  flag the page shows ("something other than you is knocking"). The per-address budget for refused requests is the
  panel's usual one (zouinify.webauth), applied to the write without the loopback exemption.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from types import MappingProxyType

from zouinify import panelstate, readonly
from zouinify.panel.sources import tier2_text

KINDS = frozenset({"ok", "decision", "question", "new", "launch", "kill", "sso"})
# names a click could carry for the things that are typed by the human, never clicked: refused with 403 by name
TIER2_KINDS = frozenset({"approve", "apply", "deploy", "push", "budget", "permission", "merge", "grant", "settings", "yes", "allow"})
TICKET_KINDS = frozenset({"ok", "decision", "launch"})       # without an inbox item these need `ticket: T-xxxx`
TEXT_KINDS = frozenset({"decision", "question", "new", "kill"})   # these need a text (the answer, the question, the report, the name)
MAX_TEXT = 2000
MAX_BODY = 64 * 1024
MAX_RECEIPTS = 500
MAX_ACK = 200
RATE_MINUTE = 10
RATE_HOUR = 60
IDEMPOTENT_S = 120.0          # a repeat click this long after the desk acknowledged still returns the same id
PENDING_MAX_AGE_S = 3600.0    # an unacknowledged receipt older than this is no longer re-sent (it stays readable by id)
HEARTBEAT_S = 15.0
POLL_DEFAULT_S = 25.0
POLL_MAX_S = 120.0
WINDOW_S = 60.0               # the "last minute" of the snapshot's counters and of the refusal audit cap
REFUSED_AUDIT_PER_WINDOW = 30 # refusal lines written to audit.jsonl per minute; the rest are counted, not written
RECENT = 20
KIND_RE = re.compile(r"^[a-z]{1,16}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")       # a session name (kind kill)
ID_RE = re.compile(r"^[0-9a-f]{6,12}$")
TICKETS_SPLIT_RE = re.compile(r"[,\s]+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # a text keeps its newlines and tabs
WIRE_KEYS = ("id", "at", "kind", "ticket", "text", "item_id", "verb", "title", "token_id", "ip", "user_agent",
             "delivered_at", "acked_at", "redelivered", "source", "panel_saw")
PUBLIC_KEYS = ("id", "at", "kind", "ticket", "text", "item_id", "verb", "title", "delivered_at", "acked_at", "redelivered", "source", "panel_saw")
MESSAGES = MappingProxyType({
    "bad-body": "a receipt is one JSON object: {item_id | kind, ticket, text}",
    "bad-item-id": "item_id: letters, digits, _ and -, at most 40",
    "no-item": "no such inbox item any more (the inbox moved on; the page reloads it)",
    "void-item": "that item is void (a newer plan exists): nothing to acknowledge",
    "ticket-mismatch": "with item_id the ticket is the item's own; leave `ticket` out",
    "tier-2-item": "that item is tier 2 (an approval, a production apply, deploy or push, or a CLAUDE.md / settings decision): "
                   "the human types it in the desk's tmux window; no button exists for it",
    "tier-2-ticket": "that ticket has a tier-2 item in the inbox (an approval, a production apply, deploy or push): its ok is typed "
                     "by the human in the desk's tmux window, never clicked",
    "tier-2-kind": "tier 2 has no endpoint: approvals, production applies, deploys, pushes, budgets, permission prompts and "
                   "CLAUDE.md / settings changes are typed by the human in the desk's tmux window",
    "tier-2-text": "a receipt whose text names CLAUDE.md or settings is tier 2: typed by the human, never clicked",
    "no-kind": "kind or item_id is required (kinds: ok, decision, question, new, launch, kill, sso)",
    "bad-kind": "unknown kind (ok, decision, question, new, launch, kill, sso)",
    "need-ticket": "this kind needs `ticket: T-xxxx`",
    "bad-ticket": "ticket: one or more ids of the form T-xxxx",
    "need-text": "this kind needs a text",
    "bad-name": "kill names exactly one session (letters, digits, . _ -)",
    "text-too-long": f"text: at most {MAX_TEXT} characters",
})


class RateLimited(Exception):
    def __init__(self, retry_after_s: int):
        super().__init__(f"rate limited; retry after {retry_after_s} s")
        self.retry_after_s = retry_after_s


def clean_text(value) -> str:
    """The text of a receipt as stored: a string, control characters (other than newline and tab) removed, trimmed."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else json.dumps(value, sort_keys=True) if isinstance(value, (int, float, bool)) else ""
    return CONTROL_RE.sub("", s).strip()


def ticket_ids(value: str) -> list:
    return [t for t in TICKETS_SPLIT_RE.split(value or "") if t]


def tier2_items_for(ticket_list: list, inbox_items) -> list:
    """The inbox items of tier 2 that name any of these tickets."""
    want = set(ticket_list)
    return [it for it in (inbox_items or []) if int(it.get("tier") or 1) == 2 and want & set(ticket_ids(str(it.get("tickets") or "")))]


def build(body, inbox_items) -> tuple:
    """(0, fields) for an acceptable receipt, else (http status, reason). `inbox_items` are the rows of the inbox source
    (classified, with their ids): an `item_id` must name one of them and its tier decides; an `ok`/`decision` for a
    ticket that has any tier-2 item in the inbox is refused too (the item's tier cannot be dodged by naming the ticket)."""
    if not isinstance(body, dict):
        return 400, "bad-body"
    item_id = body.get("item_id")
    item_id = item_id.strip() if isinstance(item_id, str) else ""
    kind = body.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    ticket = body.get("ticket")
    ticket = ticket.strip() if isinstance(ticket, str) else ""
    text = clean_text(body.get("text"))
    verb, title = "", ""
    if item_id:
        if not ITEM_ID_RE.match(item_id):
            return 400, "bad-item-id"
        item = next((it for it in (inbox_items or []) if str(it.get("id") or "") == item_id), None)
        if item is None:
            return 404, "no-item"
        if int(item.get("tier") or 1) == 2:
            return 403, "tier-2-item"
        if item.get("void"):
            return 409, "void-item"
        own = str(item.get("tickets") or "")
        if ticket and ticket_ids(ticket) != ticket_ids(own):
            return 400, "ticket-mismatch"
        verb = str(item.get("verb") or "").upper()
        title = str(item.get("title") or "")[:200]
        kind = "decision" if verb == "DECIDE" else "ok"
        ticket = own
    else:
        if not kind:
            return 400, "no-kind"
        if kind in TIER2_KINDS:
            return 403, "tier-2-kind"
        if not KIND_RE.match(kind) or kind not in KINDS:
            return 400, "bad-kind"
    if tier2_text(text):
        return 403, "tier-2-text"
    if len(text) > MAX_TEXT:
        return 400, "text-too-long"
    if kind in TICKET_KINDS and not item_id and not ticket:
        return 400, "need-ticket"
    ids = ticket_ids(ticket)
    if ticket and not all(readonly.is_ticket_id(t) for t in ids):
        return 400, "bad-ticket"
    if kind in ("ok", "decision") and not item_id and tier2_items_for(ids, inbox_items):
        return 403, "tier-2-ticket"
    if kind in TEXT_KINDS and not text:
        return 400, "need-text"
    if kind == "kill" and not NAME_RE.match(text):
        return 400, "bad-name"
    return 0, {"kind": kind, "ticket": ticket, "text": text, "item_id": item_id, "verb": verb, "title": title}


def wire(rec: dict) -> dict:
    """A receipt as the desk's poll, the audit and GET /api/receipts/<id> see it (never the sequence number)."""
    return {k: rec.get(k, "") for k in WIRE_KEYS}


def wire_public(rec: dict) -> dict:
    """A receipt as the page sees it in the snapshot: no address, no user agent, no token id."""
    return {k: rec.get(k, "") for k in PUBLIC_KEYS}


def hhmm(iso: str) -> str:
    return iso[11:16] if isinstance(iso, str) and len(iso) >= 16 else iso or ""


def age_s(iso: str) -> float:
    """Seconds since an ISO time of ours; a huge number when it does not parse (so it never counts as fresh)."""
    try:
        t = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return 1e12
    if t.tzinfo is None:
        t = t.astimezone()
    return (dt.datetime.now().astimezone() - t).total_seconds()


class ReceiptStore:
    """The receipts in memory, their hand-out to and acknowledgement by the desk's poll, the per-token rate limit,
    the presence counters."""

    def __init__(self, cfg, started_at: str):
        self.cfg = cfg
        self.started_at = started_at
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.receipts: list = []
        self.seq = 0
        self.accepted: list = []          # (monotonic time, token id) of accepted receipts, for the rate limit and the counters
        self.refused: list = []           # monotonic times of refused writes
        self.refused_written: list = []   # monotonic times of refusal lines written to the audit, for the cap
        self.pollers = 0
        self.last_poll_at = ""
        self.last_poll_end = ""

    # -- helpers --------------------------------------------------------------------------------------
    def new_id(self, at: str, kind: str) -> str:
        seed = f"{self.started_at}|{self.seq}|{at}|{kind}"
        rid = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]
        known = {r["id"] for r in self.receipts}
        n = 0
        while rid in known:
            n += 1
            rid = hashlib.sha1(f"{seed}|{n}".encode("utf-8")).hexdigest()[:6]
        return rid

    def recent_count(self, times: list, window_s: float, now=None) -> int:
        now = time.monotonic() if now is None else now
        return sum(1 for t in times if now - (t[0] if isinstance(t, tuple) else t) < window_s)

    def prune(self, now: float) -> None:
        self.accepted = [t for t in self.accepted if now - t[0] < 3600.0]
        self.refused = [t for t in self.refused if now - t < WINDOW_S]
        self.refused_written = [t for t in self.refused_written if now - t < WINDOW_S]

    def is_open(self, rec: dict) -> bool:
        """A receipt a repeat click collapses onto: not acknowledged yet, or acknowledged less than IDEMPOTENT_S ago."""
        if not rec.get("acked_at"):
            return True
        return age_s(rec["acked_at"]) < IDEMPOTENT_S

    def is_pending(self, rec: dict) -> bool:
        """Handed out by every poll until the desk acknowledges it, for at most PENDING_MAX_AGE_S."""
        return not rec.get("acked_at") and age_s(rec["at"]) < PENDING_MAX_AGE_S

    def audit(self, record: dict, what: str) -> None:
        if not panelstate.append_audit(self.cfg, record):
            panelstate.log(self.cfg, f"AUDIT-FAILED {what}: audit.jsonl could not be appended (full, unwritable or over its cap)")

    # -- the write -----------------------------------------------------------------------------------
    def add(self, fields: dict, token_id: str, ip: str, user_agent: str, panel_saw: str = "", source: str = "browser") -> tuple:
        """(receipt, repeat): the open receipt with the same kind, item, ticket and text when there is one (repeat=True),
        else a new one, rate-limited per token id (RateLimited beyond the budget) and appended to the audit."""
        with self.cond:
            now = time.monotonic()
            self.prune(now)
            key = (fields["kind"], fields.get("item_id", ""), fields.get("ticket", ""), fields.get("text", ""))
            for rec in reversed(self.receipts):
                if (rec["kind"], rec.get("item_id", ""), rec.get("ticket", ""), rec.get("text", "")) == key and self.is_open(rec):
                    return dict(rec), True
            mine = [t[0] for t in self.accepted if t[1] == token_id]
            minute = [t for t in mine if now - t < WINDOW_S]
            if len(minute) >= RATE_MINUTE:
                raise RateLimited(int(WINDOW_S - (now - min(minute))) + 1)
            if len(mine) >= RATE_HOUR:
                raise RateLimited(int(3600.0 - (now - min(mine))) + 1)
            self.seq += 1
            at = panelstate.now_iso()
            rec = {"seq": self.seq, "id": self.new_id(at, fields["kind"]), "at": at, "kind": fields["kind"], "ticket": fields.get("ticket", ""),
                   "text": fields.get("text", ""), "item_id": fields.get("item_id", ""), "verb": fields.get("verb", ""),
                   "title": fields.get("title", ""), "token_id": token_id, "ip": ip, "user_agent": user_agent[:120],
                   "delivered_at": "", "acked_at": "", "redelivered": 0, "source": source, "panel_saw": panel_saw}
            self.receipts.append(rec)
            del self.receipts[:-MAX_RECEIPTS]
            self.accepted.append((now, token_id))
            self.cond.notify_all()
            out = dict(rec)
        self.audit(dict(wire(out), event="receipt"), f"receipt {out['id']}")
        return out, False

    def note_refused(self, status: int, reason: str, ip: str, user_agent: str, credential: str, kind: str = "") -> None:
        """Count a refused write (the page's banner) and audit it, at most REFUSED_AUDIT_PER_WINDOW lines a minute.
        `credential` says what was presented (header, cookie, none), never which token."""
        with self.lock:
            now = time.monotonic()
            self.prune(now)
            self.refused.append(now)
            write = len(self.refused_written) < REFUSED_AUDIT_PER_WINDOW
            if write:
                self.refused_written.append(now)
        if write:
            self.audit({"event": "refused", "at": panelstate.now_iso(), "status": int(status), "reason": reason, "kind": kind[:16],
                        "ip": ip, "user_agent": user_agent[:120], "credential": credential}, f"refused {reason}")

    # -- the read side (the desk) ----------------------------------------------------------------------
    def get(self, rid: str):
        """One receipt from memory, or None (a line appended to audit.jsonl by hand is not here)."""
        if not isinstance(rid, str) or not ID_RE.match(rid):
            return None
        with self.lock:
            rec = next((r for r in self.receipts if r["id"] == rid), None)
            return wire(rec) if rec else None

    def pending(self, since: str) -> tuple:
        """Locked by the caller: (rows, since_known). With a known `since`: every receipt after it, acknowledged or not;
        else every pending (unacknowledged, not expired) receipt."""
        anchor = next((r for r in self.receipts if r["id"] == since), None) if since else None
        if anchor is not None:
            return [r for r in self.receipts if r["seq"] > anchor["seq"]], True
        return [r for r in self.receipts if self.is_pending(r)], False

    def poll(self, since: str, timeout_s: float, heartbeat=None) -> tuple:
        """Block up to timeout_s for pending receipts, calling heartbeat() every HEARTBEAT_S while waiting. Returns
        (batch, since_known). A hand-out stamps `delivered_at` the first time and counts later ones in `redelivered`;
        nothing is marked done: the desk acknowledges by id. An empty batch on timeout."""
        timeout_s = max(0.0, min(float(timeout_s), POLL_MAX_S))
        now_iso = panelstate.now_iso()
        with self.cond:
            self.pollers += 1
            self.last_poll_at = now_iso
        batch: list = []
        known = False
        try:
            deadline = time.monotonic() + timeout_s
            last_beat = time.monotonic()
            while True:
                with self.cond:
                    batch, known = self.pending(since)
                    if batch:
                        at = panelstate.now_iso()
                        for r in batch:
                            if r.get("delivered_at"):
                                r["redelivered"] = int(r.get("redelivered") or 0) + 1
                            else:
                                r["delivered_at"] = at
                        batch = [dict(r) for r in batch]
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.cond.wait(min(remaining, max(0.1, HEARTBEAT_S - (time.monotonic() - last_beat))))
                if heartbeat is not None and time.monotonic() - last_beat >= HEARTBEAT_S:
                    heartbeat()
                    last_beat = time.monotonic()
        finally:
            with self.cond:
                self.pollers = max(0, self.pollers - 1)
                self.last_poll_end = panelstate.now_iso()
        if batch:
            self.audit({"event": "fetched", "at": panelstate.now_iso(), "ids": [r["id"] for r in batch], "since": since or ""}, "fetched")
        return batch, known

    def ack(self, ids) -> list:
        """The desk acknowledges receipts it printed: they stop being pending. Unknown ids are ignored; returns the ids
        acknowledged now (an already acknowledged one is not repeated)."""
        done = []
        at = panelstate.now_iso()
        with self.cond:
            want = [str(i) for i in (ids or []) if isinstance(i, str) and ID_RE.match(i)][:MAX_ACK]
            for r in self.receipts:
                if r["id"] in want and not r.get("acked_at"):
                    r["acked_at"] = at
                    done.append(r["id"])
        if done:
            self.audit({"event": "acked", "at": at, "ids": done}, "acked")
        return done

    # -- what the page sees ------------------------------------------------------------------------------
    def presence(self) -> str:
        with self.lock:
            return self.presence_locked()

    def presence_locked(self) -> str:
        if self.pollers > 0:
            return "polling"
        if self.last_poll_end:
            return f"not polling since {hhmm(self.last_poll_end)}"
        return "no poll since the panel started"

    def meta(self) -> dict:
        with self.lock:
            now = time.monotonic()
            self.prune(now)
            return {"write_path": True, "token_configured": panelstate.token_configured(self.cfg), "pollers": self.pollers,
                    "polling": self.pollers > 0, "last_poll_at": self.last_poll_at, "last_poll_end": self.last_poll_end,
                    "presence": self.presence_locked(), "accepted_1m": self.recent_count(self.accepted, WINDOW_S, now),
                    "refused_1m": len(self.refused), "pending": sum(1 for r in self.receipts if self.is_pending(r)),
                    "total": len(self.receipts), "recent": [wire_public(r) for r in self.receipts[-RECENT:]][::-1],
                    "started_at": self.started_at, "rate": {"per_minute": RATE_MINUTE, "per_hour": RATE_HOUR}}
