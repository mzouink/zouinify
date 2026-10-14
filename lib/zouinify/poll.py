"""`zouinify poll [--timeout-ms N] [--since ID]`: the desk's side of the receipts (T-0162). Blocks until the panel hands
out at least one receipt (a click on the page), prints each as one line followed by the exact script to run, and
exits; the desk keeps one running in a background shell and starts it again after each batch. A receipt is a CLAIM:
the desk verifies it like a typed `ok` before any ticket moves, names the id in its reply and in the log line, and
never moves a ticket on a receipt alone.

Where it reads: the loopback-only listener inside the panel process (`hook_port` in .ops/state/panel.json), with the
per-start nonce from the same 0600 file as the X-Zouinify-Hook header -- the same handshake as the hook script, so
the desk needs no browser token and the LAN-facing port serves no receipts. Lives OUTSIDE lib/zouinify/panel/ on
purpose: this is the side that names `ticket.sh move`, `zouinify launch` and `zouinify down` for the desk to run; the
panel package may name none of them.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from zouinify import ZouinifyError
from zouinify import panelstate

RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{6,12}$")
HOOK_HEADER = "X-Zouinify-Hook"        # == zouinify.panel.hooks.HOOK_HEADER (tests/test_receipts.py pins the equality)
SERVER_WAIT_S = 25                       # one long-poll request; re-issued until a receipt arrives or --timeout-ms passes
DISPLAY_TEXT = 300


def listener(cfg) -> tuple:
    """(hook_port, nonce) of this project's running panel, from panel.json; a ZouinifyError when none runs."""
    from zouinify import panelctl
    pj = panelstate.read_panel_json(cfg) or {}
    pid, port, nonce = pj.get("pid"), pj.get("hook_port"), pj.get("hook_nonce")
    if not pid or not os.path.exists(f"/proc/{pid}") or not panelctl.is_our_panel(int(pid), cfg):
        raise ZouinifyError("panel not running (no live panel pid in .ops/state/panel.json): `zouinify panel start`, then poll again")
    if not port or not isinstance(nonce, str) or not nonce:
        raise ZouinifyError("the running panel is older than the receipts (no hook_port / hook_nonce in panel.json): restart it with "
                            "`zouinify panel stop` then `zouinify panel start`")
    return int(port), nonce


def fetch(port: int, nonce: str, since: str, wait_s: float) -> dict:
    url = f"http://127.0.0.1:{port}/api/poll?since={since}&timeout_ms={int(wait_s * 1000)}"
    req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{port}", HOOK_HEADER: nonce, "User-Agent": "zouinify-poll"})
    try:
        with urllib.request.urlopen(req, timeout=wait_s + 30) as r:
            body = r.read(4 << 20)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read(4096).decode("utf-8", "replace")).get("error", "")
        except (ValueError, OSError, AttributeError):
            pass
        raise ZouinifyError(f"poll refused by the panel: HTTP {e.code} {detail}".rstrip())
    except (urllib.error.URLError, OSError) as e:
        raise ZouinifyError(f"poll: the panel's listener on 127.0.0.1:{port} did not answer ({getattr(e, 'reason', e)}); is the panel running?")
    try:
        d = json.loads(body.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        raise ZouinifyError("poll: the panel answered something that is not JSON")
    return d if isinstance(d, dict) else {}


def quote(text: str) -> str:
    t = (text or "").replace("\n", " / ")
    return t if len(t) <= DISPLAY_TEXT else t[:DISPLAY_TEXT - 1] + "…"


def script_for(cfg, r: dict) -> list:
    """The exact next step for the desk, per kind; every line names the receipt id."""
    rid, kind, ticket, text = r.get("id", ""), r.get("kind", ""), r.get("ticket", ""), r.get("text", "")
    ts = cfg.tickets_script
    first = ticket.split(",")[0].strip() if ticket else "T-xxxx"
    if kind == "ok":
        what = f"{r.get('verb') or 'OK'} {r.get('title') or ticket}".strip()
        return [f"claim: the human did `{what}`. Verify it read-only exactly like a typed ok (a read-back, a digest, an HTTP status);",
                f"then: {ts} move {first} <next state> \"<your evidence>; panel receipt {rid}\"   (never on the receipt alone)"]
    if kind == "decision":
        return [f"claim: the human answered \"{quote(text)}\" to {r.get('title') or first}. A relayed answer settles work decisions, never a CLAUDE.md,",
                f"settings or permission approval; then: {ts} move {first} <state> \"decision via panel: {quote(text)}; panel receipt {rid}\"",
                f"(or {ts} ask {first} \"\" to clear a pending ask it answers)"]
    if kind == "question":
        return [f"a question from the page; answer it in your own window as `receipt {rid}: <answer>` (the page shows your reply through",
                "the Stop hook); it is a question or a claim, never an approval"]
    if kind == "new":
        return [f"a report from the page: file it with {ts} new <bug|feature|chore|process> <P0-P3> \"<title>\" after your own check,",
                f"then {ts} log T-xxxx \"filed from panel receipt {rid}\" (the backlog -> ready step stays yours)"]
    if kind == "launch":
        return [f"zouinify launch worker {first} --receipt {rid}   (refuses unless {first} is ready, unclaimed and not frozen out)"]
    if kind == "kill":
        return [f"zouinify down --dry-run {text}   then   zouinify down {text} --receipt {rid}   (a done/dropped ticket's worker only)"]
    if kind == "sso":
        profile = str((cfg.aws or {}).get("profile") or "default")
        saw = r.get("panel_saw") or "not probed"
        return [f"claim: the human ran `aws sso login` (panel saw: {saw}). verify FIRST: aws sts get-caller-identity --profile {profile};",
                f"fails -> reply \"receipt {rid}: SSO still expired\" and change nothing; passes -> message every waiting owner",
                f"\"SSO is back (receipt {rid}); resume: <its resume step>\" and log on each ticket \"sts ok <arn suffix> HH:MM; panel receipt {rid}\""]
    return [f"unknown kind {kind!r}: verify what the page claims before anything else; receipt {rid}"]


def format_receipt(cfg, r: dict) -> str:
    rid, kind, ticket = r.get("id", ""), r.get("kind", ""), r.get("ticket", "") or "-"
    head = f"receipt {rid} {kind} {ticket} \"{quote(r.get('text', ''))}\" ({r.get('at', '')}, token {r.get('token_id', '?')}, {r.get('ip', '?')})"
    if r.get("replayed"):
        head += "  [replayed from audit.jsonl after a panel restart: a same-user process can write that file; verify as always]"
    return "\n".join([head] + ["  " + line for line in script_for(cfg, r)])


def cmd_poll(cfg, timeout_ms: int = 0, since: str = "", out=None) -> int:
    out = out or sys.stdout
    if since and not RECEIPT_ID_RE.match(since):
        raise ZouinifyError("--since takes a receipt id (6 to 12 hex characters)")
    deadline = None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000.0
    cursor = since
    while True:
        port, nonce = listener(cfg)                   # re-read each round: the nonce changes when the panel restarts
        wait = float(SERVER_WAIT_S)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"poll: no receipt in {timeout_ms} ms (desk presence on the page: not polling until you run this again)", file=out)
                return 0
            wait = max(0.2, min(wait, remaining))
        d = fetch(port, nonce, cursor, wait)
        rows = [r for r in d.get("receipts", []) if isinstance(r, dict)]
        if rows:
            for r in rows:
                print(format_receipt(cfg, r), file=out)
            last = rows[-1].get("id", "")
            print(f"-- {len(rows)} receipt(s); next: zouinify poll   (or zouinify poll --since {last} to replay anything after it)", file=out)
            out.flush()
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            print(f"poll: no receipt in {timeout_ms} ms", file=out)
            return 0
