"""T-0162: the receipts channel. The panel's ONE write, `POST /api/receipts`, needs the browser token on every bind (a same-uid
loopback peer is what a Claude session is); tier 2 is refused by name, by item and by text; a repeat click returns the same id;
10/min and 60/h per token, 429 with a banner beyond; every accepted receipt and every refusal is audited; the desk's side
(`GET /api/poll`, `GET /api/receipts/<id>`) is served by the loopback-only listener behind the same-uid check and the per-start
nonce, never by the LAN-facing port; a poll returns within a second of the POST, marks the batch delivered, replays undelivered
receipts after a restart; `zouinify poll` prints each receipt with the exact script. RED on base: POST /api/receipts is 405.
"""
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World  # noqa: E402
from test_panel import PanelWorld, PANEL_IMPORT_ERROR  # noqa: E402

try:
    from zouinify import panelstate
    from zouinify.panel import hooks as panel_hooks
    from zouinify.panel import receipts as panel_receipts
    from zouinify.panel import server as panel_server
except Exception as e:  # noqa: BLE001 - the RED case: no receipts module on the base tree
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e

GOOD = "test-token-" + "C" * 40        # assembled: no credential-shaped literal
OTHER = "other-token-" + "D" * 40
HOOK_HEADER = "X-Zouinify-Hook"


class ReceiptWorld(PanelWorld):
    """PanelWorld + the token, the listener served, the per-start nonce in panel.json, a fake 'our panel' process."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"receipts not importable from {REPO}/lib: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        panelstate.set_token(self.cfg, GOOD)
        self.srv.serve_hooks()
        self.hook_port = self.srv.hooks.server_address[1]
        self.fake_remote = False
        test = self
        self.patches = [mock.patch.object(panel_server.Handler, "peer_is_loopback", lambda h: not test.fake_remote),
                        mock.patch.object(panel_hooks.HookHandler, "peer_is_loopback", lambda h: not test.fake_remote)]
        for p in self.patches:
            p.start()
        self.panel_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "lib/zouinify/cli.py", "--project", self.root,
                                            "panel", "start", "--foreground"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.nonce = panelstate.new_hook_nonce()
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], self.panel_proc.pid, self.srv.started_at,
                                    hook_port=self.hook_port, hook_nonce=self.nonce)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.srv.close_hooks()
        try:
            self.panel_proc.kill()
            self.panel_proc.wait(5)
        except OSError:
            pass
        super().tearDown()

    # -- the write, on the panel port ---------------------------------------------------------------------
    def post(self, body, token=GOOD, cookie=False, headers=None, ctype="application/json", raw=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=60)
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        conn.putrequest("POST", "/api/receipts", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.srv.server_address[1]}")
        if token and cookie:
            conn.putheader("Cookie", f"zouinify_token={token}")
        elif token:
            conn.putheader("Authorization", "Bearer " + token)
        if ctype:
            conn.putheader("Content-Type", ctype)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        conn.send(data)
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), json.loads(r.read() or b"{}"))
        conn.close()
        return out

    # -- the desk's side, on the listener -----------------------------------------------------------------
    def desk_get(self, path, nonce=True, host=None, headers=None, timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=timeout)
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.hook_port}")
        if nonce is True:
            conn.putheader(HOOK_HEADER, self.nonce)
        elif nonce:
            conn.putheader(HOOK_HEADER, str(nonce))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, raw, json.loads(raw.decode("utf-8").strip() or "{}")
        except ValueError:
            return r.status, raw, None

    def poll(self, since="", timeout_ms=300, **kw):
        return self.desk_get(f"/api/poll?since={since}&timeout_ms={timeout_ms}", **kw)

    def inbox_items(self):
        return self.get_json("/api/snapshot?fresh=1")["inbox"]["items"]

    def item(self, verb):
        return next(it for it in self.inbox_items() if it["verb"] == verb)

    def server_log(self):
        p = os.path.join(self.root, ".ops", "state", "server.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def audit(self):
        p = os.path.join(self.root, ".ops", "state", "audit.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]


class TestWrite(ReceiptWorld):
    def test_the_token_is_required_even_from_a_same_uid_loopback_peer(self):
        st, hdr, d = self.post({"kind": "question", "text": "is T-0001 safe?"}, token=None)
        self.assertEqual(st, 401, d)
        self.assertIn("Bearer", hdr.get("WWW-Authenticate", ""))
        self.assertIn("AUDIT 401 no-token ip=127.0.0.1 POST /api/receipts", self.server_log())
        self.assertEqual([a["reason"] for a in self.audit() if a["event"] == "refused"], ["no-token"])
        st, _, d = self.post({"kind": "question", "text": "is T-0001 safe?"}, token=OTHER)
        self.assertEqual(st, 401, d)
        self.assertIn("AUDIT 401 bad-token", self.server_log())
        self.assertEqual(self.get_json("/api/snapshot")["panel"]["receipts"]["total"], 0, "nothing was stored")
        # the cookie the unlock set, or the bearer: both are the token
        st, _, d = self.post({"kind": "question", "text": "is T-0001 safe?"}, cookie=True)
        self.assertEqual(st, 201, d)
        self.assertRegex(d["id"], r"^[0-9a-f]{6}$")
        self.assertEqual(d["kind"], "question")
        self.assertIn("RECEIPT " + d["id"] + " question - new ip=127.0.0.1 token=" + GOOD_ID(self), self.server_log())
        # reading stays token-free on loopback for the panel's own user, as before
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)

    def test_without_a_configured_token_the_write_is_refused_and_the_page_says_why(self):
        pj = json.load(open(panelstate.panel_json_path(self.cfg)))
        for k in panelstate.TOKEN_FIELDS:
            pj.pop(k, None)
        with open(panelstate.panel_json_path(self.cfg), "w") as fh:
            json.dump(pj, fh)
        st, _, d = self.post({"kind": "question", "text": "hello"}, token=GOOD)
        self.assertEqual(st, 401)
        self.assertIn("panel token rotate", d["error"])
        self.assertEqual(d["reason"], "no-token-configured")
        s = self.get_json("/api/snapshot")
        self.assertFalse(s["panel"]["receipts"]["token_configured"])
        self.assertTrue(s["panel"]["write_path"])
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("no browser token yet", js, "the page disables its controls with the reason, never hides the write path")

    def test_foreign_origin_other_uid_and_other_methods(self):
        st, _, d = self.post({"kind": "question", "text": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertIn("AUDIT 403 host-or-origin", self.server_log())
        st, _, _ = self.post({"kind": "question", "text": "x"}, host="evil.example:1")
        self.assertEqual(st, 403)
        with mock.patch.object(panel_server.Handler, "peer_is_same_user", lambda h: False):
            st, _, d = self.post({"kind": "question", "text": "x"})
            self.assertEqual(st, 403, "another local uid on a loopback bind: refused even with the token")
            self.assertEqual(d["reason"], "other-local-user")
        for method, path in (("POST", "/api/snapshot"), ("POST", "/"), ("PUT", "/api/receipts"), ("DELETE", "/api/receipts"), ("PATCH", "/api/receipts")):
            st, hdr, _ = self.req(method, path, body=b"{}")
            self.assertEqual(st, 405, f"{method} {path}: every other write stays 405")
        st, hdr, body = self.req("GET", "/api/receipts")
        self.assertEqual(st, 404, "the LAN-facing port serves no receipts list")
        st, _, body = self.req("GET", "/api/poll")
        self.assertEqual(st, 404, "the LAN-facing port has no /api/poll")
        h = self.get_json("/health")
        self.assertTrue(h["write_path"])
        self.assertEqual(h["writes"], ["POST /api/receipts"])

    def test_tier_two_has_no_endpoint_behaviour(self):
        approve, deploy, decide = self.item("APPROVE"), self.item("DEPLOY"), self.item("DECIDE")
        cases = [({"kind": "approve"}, "tier-2-kind"), ({"kind": "apply", "ticket": "T-0001"}, "tier-2-kind"), ({"kind": "deploy"}, "tier-2-kind"),
                 ({"kind": "push"}, "tier-2-kind"), ({"kind": "budget"}, "tier-2-kind"), ({"kind": "permission"}, "tier-2-kind"),
                 ({"item_id": approve["id"]}, "tier-2-item"), ({"item_id": deploy["id"]}, "tier-2-item"),
                 ({"item_id": decide["id"], "text": "yes, change CLAUDE.md"}, "tier-2-text"),
                 ({"kind": "ok", "ticket": "T-0001", "text": "settings edited, go"}, "tier-2-text"),
                 ({"kind": "decision", "ticket": "T-0001", "text": "allow it in settings.json"}, "tier-2-text")]
        for body, reason in cases:
            st, _, d = self.post(body)
            self.assertEqual(st, 403, (body, d))
            self.assertEqual(d["reason"], reason, body)
            self.assertTrue(d["tier2"], body)
        self.assertEqual(self.get_json("/api/snapshot")["panel"]["receipts"]["total"], 0)
        self.assertEqual(sum(1 for a in self.audit() if a["event"] == "refused" and a["status"] == 403), len(cases))
        self.assertEqual(self.server_log().count("AUDIT 403 tier-2"), len(cases))
        self.assertEqual(panel_receipts.TIER2_KINDS & panel_receipts.KINDS, frozenset())

    def test_item_receipts_ok_and_decision(self):
        run, decide, check = self.item("RUN"), self.item("DECIDE"), None
        st, _, d = self.post({"item_id": run["id"]})
        self.assertEqual(st, 201, d)
        self.assertEqual((d["kind"], d["ticket"]), ("ok", "T-0005"))
        st, _, d2 = self.post({"item_id": decide["id"]})
        self.assertEqual(st, 400, "an answer is needed for a decision")
        self.assertEqual(d2["reason"], "need-text")
        st, _, d2 = self.post({"item_id": decide["id"], "text": "T-0006 delete"})
        self.assertEqual(st, 201, d2)
        self.assertEqual(d2["kind"], "decision")
        st, _, d3 = self.post({"item_id": "0000000000"})
        self.assertEqual(st, 404)
        self.assertEqual(d3["reason"], "no-item")
        for body, status, reason in (({"kind": "ok"}, 400, "need-ticket"), ({"kind": "launch", "ticket": "nope"}, 400, "bad-ticket"),
                                     ({"kind": "kill", "text": "a b"}, 400, "bad-name"), ({"kind": "question"}, 400, "need-text"),
                                     ({"kind": "zap"}, 400, "bad-kind"), ({}, 400, "no-kind"), ({"kind": "new", "text": "x" * 2001}, 400, "text-too-long"),
                                     ({"item_id": "../x"}, 400, "bad-item-id")):
            st, _, dd = self.post(body)
            self.assertEqual((st, dd.get("reason")), (status, reason), body)
        st, _, _ = self.post(None, raw=b"[1,2]")
        self.assertEqual(st, 400)
        st, _, _ = self.post(None, raw=b"{not json")
        self.assertEqual(st, 400)
        st, _, _ = self.post({"kind": "question", "text": "x"}, ctype="text/plain")
        self.assertEqual(st, 415)
        st, _, _ = self.post(None, raw=b"{" + b" " * (70 * 1024) + b"}")
        self.assertEqual(st, 413)
        recs = [a for a in self.audit() if a["event"] == "receipt"]
        self.assertEqual([r["kind"] for r in recs], ["ok", "decision"])
        self.assertLessEqual({"id", "at", "kind", "ticket", "text", "token_id", "ip", "user_agent", "item_id"}, set(recs[0]))
        self.assertEqual(recs[0]["item_id"], run["id"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, ".ops", "state", "audit.jsonl")).st_mode), 0o600)
        self.assertNotIn(GOOD, open(os.path.join(self.root, ".ops", "state", "audit.jsonl")).read(), "never the token itself")

    def test_a_repeat_click_returns_the_same_id_while_the_receipt_is_open(self):
        run = self.item("RUN")
        st, _, a = self.post({"item_id": run["id"]})
        st2, _, b = self.post({"item_id": run["id"]})
        self.assertEqual((st, st2), (201, 200))
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["repeat"])
        st, _, c = self.post({"kind": "sso"})
        st, _, c2 = self.post({"kind": "sso"})
        self.assertEqual(c["id"], c2["id"], "the same kind without an item collapses too")
        self.assertEqual(self.get_json("/api/snapshot")["panel"]["receipts"]["total"], 2)
        st, _, d = self.poll(timeout_ms=200)
        self.assertEqual([r["id"] for r in d["receipts"]], [a["id"], c["id"]])
        st, _, b2 = self.post({"item_id": run["id"]})
        self.assertEqual(b2["id"], a["id"], "fetched by the desk less than two minutes ago: still the same receipt")
        with mock.patch.object(panel_receipts, "IDEMPOTENT_S", 0.0):
            time.sleep(0.05)
            st, _, e = self.post({"item_id": run["id"]})
            self.assertEqual(st, 201)
            self.assertNotEqual(e["id"], a["id"], "after the window a new click is a new receipt")

    def test_eleventh_receipt_in_a_minute_is_429_with_the_banner(self):
        ids = []
        for i in range(10):
            st, _, d = self.post({"kind": "question", "text": f"question {i}"})
            self.assertEqual(st, 201, d)
            ids.append(d["id"])
        self.assertEqual(len(set(ids)), 10)
        st, hdr, d = self.post({"kind": "question", "text": "question 10"})
        self.assertEqual(st, 429, d)
        self.assertTrue(d["banner"])
        self.assertIn("Retry-After", hdr)
        self.assertIn("AUDIT 429 receipt-rate", self.server_log())
        self.assertEqual([a["reason"] for a in self.audit() if a["event"] == "refused"], ["receipt-rate"])
        st, _, same = self.post({"kind": "question", "text": "question 3"})
        self.assertEqual(st, 200, "a repeat of an open receipt is not a new one and is not rate-limited")
        self.assertEqual(same["id"], ids[3])
        s = self.get_json("/api/snapshot")
        self.assertEqual(s["panel"]["receipts"]["refused_1m"], 1)
        self.assertEqual(s["panel"]["receipts"]["accepted_1m"], 10)
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("something other than you is knocking", js)


def GOOD_ID(test):
    return panelstate.token_id(test.cfg)


class TestDeskSide(ReceiptWorld):
    def test_get_receipt_answers_from_memory_not_from_the_file(self):
        st, _, a = self.post({"kind": "question", "text": "from the page"})
        st, raw, d = self.desk_get(f"/api/receipts/{a['id']}")
        self.assertEqual(st, 200, raw)
        self.assertEqual((d["id"], d["kind"], d["text"], d["token_id"]), (a["id"], "question", "from the page", GOOD_ID(self)))
        self.assertNotIn("seq", d)
        with open(os.path.join(self.root, ".ops", "state", "audit.jsonl"), "a") as fh:
            fh.write(json.dumps({"event": "receipt", "id": "feedfa", "at": panelstate.now_iso(), "kind": "launch", "ticket": "T-0001",
                                 "text": "", "token_id": GOOD_ID(self), "ip": "127.0.0.1", "user_agent": "hand"}) + "\n")
        st, raw, d = self.desk_get("/api/receipts/feedfa")
        self.assertEqual(st, 404, "a line appended by hand is not a receipt")
        st, _, d = self.poll(timeout_ms=200)
        self.assertEqual([r["id"] for r in d["receipts"]], [a["id"]], "and the poll does not hand it out either")
        self.assertEqual(self.desk_get("/api/receipts/zz")[0], 404)

    def test_poll_returns_within_a_second_marks_delivered_and_replays_after_since(self):
        box = {}

        def do_poll():
            box["t0"] = time.monotonic()
            box["res"] = self.poll(timeout_ms=10000)
            box["dt"] = time.monotonic() - box["t0"]
        th = threading.Thread(target=do_poll)
        th.start()
        time.sleep(0.4)
        self.assertEqual(self.get_json("/api/snapshot")["header"]["desk"]["presence"], "polling")
        st, _, a = self.post({"kind": "launch", "ticket": "T-0001"})
        th.join(5)
        self.assertFalse(th.is_alive(), "the poll must return once a receipt is pending")
        self.assertLess(box["dt"] - 0.4, 1.0, f"a receipt reaches the poll within a second ({box['dt']:.2f} s)")
        st, raw, d = box["res"]
        self.assertEqual(st, 200)
        self.assertEqual([(r["id"], r["kind"], r["ticket"]) for r in d["receipts"]], [(a["id"], "launch", "T-0001")])
        self.assertTrue(d["receipts"][0]["delivered_at"])
        self.assertEqual(d["since"], a["id"])
        st, _, empty = self.poll(timeout_ms=200)
        self.assertEqual(empty["receipts"], [], "delivered once: not handed out again without a cursor")
        st, _, b = self.post({"kind": "kill", "text": "t0003-finished-ticket"})
        st, _, after = self.poll(since=a["id"], timeout_ms=200)
        self.assertEqual([r["id"] for r in after["receipts"]], [b["id"]], "since=<id>: everything after it, delivered or not")
        st, _, again = self.poll(since=a["id"], timeout_ms=200)
        self.assertEqual([r["id"] for r in again["receipts"]], [b["id"]], "and the same cursor replays it: a lost batch is not lost")
        delivered = [x for x in self.audit() if x["event"] == "delivered"]
        self.assertEqual([x["ids"] for x in delivered], [[a["id"]], [b["id"]], [b["id"]]])
        presence = self.get_json("/api/snapshot")["header"]["desk"]["presence"]
        self.assertRegex(presence, r"^not polling since \d\d:\d\d$")
        self.assertEqual(self.get_json("/api/snapshot")["panel"]["receipts"]["pending"], 0)

    def test_poll_heartbeats_spaces_and_a_timeout_is_empty(self):
        with mock.patch.object(panel_receipts, "HEARTBEAT_S", 0.2):
            st, raw, d = self.poll(timeout_ms=700)
        self.assertEqual(st, 200)
        self.assertTrue(raw.startswith(b"  "), raw[:20])
        self.assertEqual(d["receipts"], [])
        self.assertRegex(self.server_log(), r"POLL 127\.0\.0\.1 since=- -> 0 receipt\(s\)")

    def test_undelivered_receipts_replay_after_a_restart_flagged(self):
        st, _, a = self.post({"kind": "question", "text": "fetched before the restart"})
        self.poll(timeout_ms=200)
        st, _, b = self.post({"kind": "launch", "ticket": "T-0001"})
        self.srv.shutdown()
        self.srv.server_close()
        self.srv.close_hooks()
        self.srv = panel_server.make_server(self.cfg, port=0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.srv.serve_hooks()
        self.hook_port = self.srv.hooks.server_address[1]
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], self.panel_proc.pid, self.srv.started_at,
                                    hook_port=self.hook_port, hook_nonce=self.nonce)
        s = self.get_json("/api/snapshot")
        self.assertEqual(s["panel"]["receipts"]["pending"], 1)
        st, _, d = self.poll(timeout_ms=200)
        self.assertEqual([(r["id"], r["replayed"]) for r in d["receipts"]], [(b["id"], True)], "only the undelivered one, flagged")
        self.assertIsNone(self.desk_get(f"/api/receipts/{a['id']}")[2].get("kind"), "the delivered one is not back")
        st, _, d = self.poll(timeout_ms=200)
        self.assertEqual(d["receipts"], [], "replayed once, delivered, done")

    def test_the_desk_side_is_refused_from_lan_other_uid_no_nonce_bad_host_or_origin(self):
        self.post({"kind": "question", "text": "x"})
        self.fake_remote = True
        self.assertEqual(self.poll(timeout_ms=100)[0], 403, "a non-loopback address")
        self.fake_remote = False
        with mock.patch.object(panel_hooks.HookHandler, "peer_is_same_user", lambda h: False):
            self.assertEqual(self.poll(timeout_ms=100)[0], 403, "another uid")
        self.assertEqual(self.poll(timeout_ms=100, nonce=False)[0], 403, "no nonce")
        self.assertEqual(self.poll(timeout_ms=100, nonce="wrong-nonce-" + "x" * 10)[0], 403, "wrong nonce")
        self.assertEqual(self.poll(timeout_ms=100, host="evil.example:1")[0], 403, "foreign Host")
        self.assertEqual(self.poll(timeout_ms=100, headers={"Origin": f"http://127.0.0.1:{self.hook_port}"})[0], 403, "a page may not poll")
        self.assertEqual(self.server_log().count("AUDIT hook 403"), 6)
        self.assertEqual(self.get_json("/api/snapshot")["panel"]["receipts"]["pending"], 1, "nothing was handed out")
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=10)
        conn.request("GET", "/api/hook")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()


class TestPollCli(ReceiptWorld):
    def test_poll_prints_each_receipt_with_the_script_and_exits(self):
        run = self.item("RUN")
        th = threading.Thread(target=lambda: (time.sleep(0.5), self.post({"item_id": run["id"]})))
        th.start()
        r = self.ok("poll", "--timeout-ms", "15000")          # blocks until the click half a second later, then exits
        th.join()
        first = re.findall(r"^receipt ([0-9a-f]{6}) ", r.stdout, re.M)
        self.assertEqual(len(first), 1, r.stdout)
        self.assertRegex(r.stdout, rf"(?m)^receipt {first[0]} ok T-0005 \"\" \(\d{{4}}-\d\d-\d\dT[^,]+, token [0-9a-f]{{4}}, 127\.0\.0\.1\)$")
        self.assertIn(f"scripts/ticket.sh move T-0005 <next state> \"<your evidence>; panel receipt {first[0]}\"", r.stdout)
        self.post({"kind": "launch", "ticket": "T-0001"})
        self.post({"kind": "kill", "text": "t0003-finished-ticket"})
        self.post({"kind": "question", "text": "is the plan the newest?"})
        self.post({"kind": "sso"})
        r = self.ok("poll", "--timeout-ms", "15000")          # the four waiting receipts come out in one batch
        out = r.stdout
        ids = first + re.findall(r"^receipt ([0-9a-f]{6}) ", out, re.M)
        self.assertEqual(len(ids), 5, out)
        self.assertIn(f"zouinify launch worker T-0001 --receipt {ids[1]}", out)
        self.assertIn(f"zouinify down t0003-finished-ticket --receipt {ids[2]}", out)
        self.assertIn(f"receipt {ids[3]}: <answer>", out)
        self.assertIn("aws sts get-caller-identity --profile default", out)
        self.assertIn(f"-- 4 receipt(s); next: zouinify poll   (or zouinify poll --since {ids[4]}", out)
        self.assertNotIn("replayed", out)
        self.assertEqual(self.ticket_calls(), [c for c in self.ticket_calls() if c.startswith("human") or c.startswith("board")],
                         "poll runs no mutating ticket.sh command")
        r = self.ok("poll", "--timeout-ms", "300")
        self.assertIn("no receipt in 300 ms", r.stdout)
        r = self.ok("poll", "--since", ids[3], "--timeout-ms", "300")
        self.assertEqual(re.findall(r"^receipt ([0-9a-f]{6}) ", r.stdout, re.M), [ids[4]], "--since replays what came after that id")
        r = self.nz("poll", "--since", "not-an-id")
        self.assertIn("receipt id", r.stderr)

    def test_poll_without_a_running_panel_says_so(self):
        os.remove(panelstate.panel_json_path(self.cfg))
        r = self.nz("poll", "--timeout-ms", "300")
        self.assertIn("panel not running", r.stderr)

    def test_poll_and_hook_share_the_header_name(self):
        from zouinify import poll as poll_mod
        self.assertEqual(poll_mod.HOOK_HEADER, panel_hooks.HOOK_HEADER)
        self.assertEqual(HOOK_HEADER, panel_hooks.HOOK_HEADER)


class TestPage(ReceiptWorld):
    def test_the_page_posts_receipts_and_keeps_tier_two_typed(self):
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertNotIn("available after receipts", js)
        self.assertIn("fetch('/api/receipts', { method: 'POST'", js)
        self.assertIn("tier 2: never clickable", js)
        self.assertIn("data-rc", js)
        self.assertNotRegex(js, r"localStorage|sessionStorage|document\.cookie", "page script never holds the token")
        for forbidden in ("new-session", "kill-session", "send-keys"):
            self.assertNotIn(forbidden, js)
        s = self.get_json("/api/snapshot")
        self.assertTrue(s["panel"]["write_path"])
        self.assertEqual(s["header"]["desk"]["presence"], "no poll since the panel started")
        self.assertFalse(s["header"]["desk"]["polling"])
        for k in ("token_configured", "pollers", "accepted_1m", "refused_1m", "pending", "recent", "rate", "presence"):
            self.assertIn(k, s["panel"]["receipts"], k)
        hints = {it["verb"]: it["hint"] for it in s["inbox"]["items"]}
        self.assertIn("a receipt the desk verifies", hints["RUN"])
        self.assertIn("type", hints["APPROVE"])


class TestDownReceipt(World):
    def test_down_receipt_logs_on_the_ticket_signed_by_the_caller(self):
        self.ok("launch", "worker", "T-0001")
        self.set_status("T-0001", "done")
        r = self.ok("down", "t0001-first-ready-ticket", "--receipt", "abc123")
        self.assertIn("killed", r.stdout)
        logs = [c for c in self.ticket_calls() if c.startswith("log T-0001 ")]
        self.assertEqual(logs, ["log T-0001 killed t0001-first-ready-ticket (panel receipt abc123)"])
        with open(os.path.join(self.root, "tickets", "T-0001-first-ready-ticket.md")) as fh:
            self.assertIn("tester-session: killed t0001-first-ready-ticket (panel receipt abc123)", fh.read())
        env = dict(self.env)
        env.pop("TICKET_AS")
        r = self.nz("down", "t0001-first-ready-ticket", "--receipt", "zz", env=env)
        self.assertIn("TICKET_AS", r.stderr)


if __name__ == "__main__":
    unittest.main()
