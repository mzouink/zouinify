"""T-0195: the "SSO updated" button. The marker is a LOG line (`waits-on: aws-sso -- resume: <step>`, the newest one wins,
never blocked_on: that would reach the human's inbox); the header button is shown when the credentials are not ok or a
ticket waits and hidden otherwise; a click is a receipt of kind sso whose panel_saw is a fresh re-probe (the same read-only
`aws sts get-caller-identity`, the only aws command the panel runs); a repeat click while the panel sees the same state
returns the same id; the panel emits one synthetic `creds` event through the poll on LOGIN -> ok while the desk's poll is
alive; `zouinify poll` lists every waiting ticket with its owner and resume step; `ticket.sh human` ignores the marker.
RED on base f5c385c: no `sso` block, no waits_on, panel_saw "not probed (T-0195)", no creds event.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World  # noqa: E402
from test_receipts import ReceiptWorld, GOOD, PANEL_IMPORT_ERROR  # noqa: E402

try:
    from zouinify import tickets as tk
    from zouinify.panel import receipts as panel_receipts
except Exception as e:  # noqa: BLE001
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e

WAIT_LINE = "- 2026-10-14 00:30 {who}: waits-on: aws-sso -- resume: {step}"


class SsoWorld(ReceiptWorld):
    def setUp(self):
        super().setUp()
        self.creds_cache = os.path.join(self.root, ".ops", "state", "creds.json")

    def set_aws(self, mode: str):
        """The fake aws's answer for the next probe; the 60 s cache is dropped so the next read probes."""
        os.environ["FAKE_AWS_MODE"] = mode
        try:
            os.remove(self.creds_cache)
        except FileNotFoundError:
            pass

    def append_log(self, tid: str, line: str):
        f = [x for x in os.listdir(os.path.join(self.root, "tickets")) if x.startswith(tid + "-")][0]
        with open(os.path.join(self.root, "tickets", f), "a") as fh:
            fh.write(line + "\n")

    def snapshot(self):
        self.srv.fresh_at.clear()          # T-0197: one fresh per 5 s per client; these tests flip the fake aws back to back
        return self.get_json("/api/snapshot?fresh=1")

    def aws_calls(self):
        if not os.path.exists(self.aws_log):
            return []
        with open(self.aws_log) as fh:
            return [json.loads(l) for l in fh if l.strip()]


class TestMarker(SsoWorld):
    def test_newest_log_line_decides_and_terminal_tickets_never_wait(self):
        self.append_log("T-0002", WAIT_LINE.format(who="someone", step="rerun scripts/sync.sh --shard 3"))
        self.append_log("T-0008", WAIT_LINE.format(who="w8", step="aws s3 ls again"))
        self.append_log("T-0008", "- 2026-10-14 00:31 w8: resumed by hand")                 # a later line clears it
        self.append_log("T-0003", WAIT_LINE.format(who="x", step="never: done tickets do not wait"))
        self.append_log("T-0006", "- 2026-10-14 00:32 y: waits-on: aws sso expired -- resume: x")   # not the exact token
        self.append_log("T-0001", "- 2026-10-14 00:33 z: waits-on: aws-sso")                        # no resume text: still waits
        self.set_status("T-0009", "skipped")
        self.append_log("T-0009", WAIT_LINE.format(who="w9", step="skipped tickets are set aside: nobody resumes them"))
        rows = tk.waits_on(self.cfg)
        self.assertEqual([(r["id"], r["owner"], r["status"], r["resume"], r["since"], r["by"]) for r in rows],
                         [("T-0001", "", "ready", "", "2026-10-14 00:33", "z"),
                          ("T-0002", "someone", "in-dev", "rerun scripts/sync.sh --shard 3", "2026-10-14 00:30", "someone")])
        s = self.snapshot()
        self.assertEqual([w["id"] for w in s["sso"]["waiting"]], ["T-0001", "T-0002"])
        self.assertEqual(s["sso"]["marker"], "waits-on: aws-sso -- resume: <exact step>")
        self.assertTrue(s["sso"]["show_button"], "someone waits: the button shows even when creds are ok")

    def test_human_output_of_the_vendored_core_is_unchanged_by_a_waits_on_line(self):
        """The reason the marker is a log line: `ticket.sh human` turns any non-T- blocked_on into a DECIDE item. Run the
        vendored core itself (templates/protocol) on its own throwaway world: the line adds nothing to the human's list."""
        harness = os.path.join(REPO, "templates", "protocol", "scripts", "tests")
        sys.path.insert(0, harness)
        rendered = os.path.join(self.tmp, "rendered")
        self.ok("protocol", "render", "--out", rendered)
        os.environ["TICKET_WORLD_PLUGIN"] = "none"
        os.environ["TICKET_SH_UNDER_TEST"] = os.path.join(REPO, "templates", "protocol", "scripts", "ticket.sh")
        os.environ["TICKET_PLUGINS_UNDER_TEST"] = os.path.join(REPO, "templates", "protocol", "tickets", "plugins")
        os.environ["TICKET_TEMPLATE_UNDER_TEST"] = os.path.join(rendered, "_TEMPLATE.md")
        import importlib
        import ticket_world
        importlib.reload(ticket_world)

        class CoreWorld(ticket_world.TicketShWorld):
            plugin = "none"

            def runTest(self):
                pass
        w = CoreWorld()
        w.setUp()
        try:
            before = json.loads(w.ts("human", "--json", env=w.env_without_who()).stdout)
            p = w.ts("log", "T-0002", "waits-on: aws-sso -- resume: rerun scripts/sync.sh --shard 3")
            self.assertEqual(p.returncode, 0, p.out)
            after = json.loads(w.ts("human", "--json", env=w.env_without_who()).stdout)
            self.assertEqual(after, before, "a waits-on log line never reaches the human's inbox")
            self.assertEqual(w.ts("human", "--count", env=w.env_without_who()).out.strip(), str(len(before)))
            self.assertNotIn("aws-sso", w.ts("human", env=w.env_without_who()).out)
            rows = tk.waits_on_in(os.path.join(w.main, "tickets"))
            self.assertEqual([(r["id"], r["resume"]) for r in rows], [("T-0002", "rerun scripts/sync.sh --shard 3")])
            p = w.ts("log", "T-0002", "resumed: shard 3 synced")
            self.assertEqual(p.returncode, 0, p.out)
            self.assertEqual(tk.waits_on_in(os.path.join(w.main, "tickets")), [], "the next log line clears it")
        finally:
            w.doCleanups()
            sys.modules.pop("ticket_world", None)          # nothing of the harness leaks into later tests of this process
            if harness in sys.path:
                sys.path.remove(harness)


class TestButton(SsoWorld):
    def test_hidden_when_ok_and_nobody_waits_shown_on_login(self):
        self.set_aws("ok")
        s = self.snapshot()
        self.assertEqual(s["sso"]["creds"], "ok")
        self.assertFalse(s["sso"]["show_button"])
        self.set_aws("expired")
        s = self.snapshot()
        self.assertEqual(s["sso"]["creds"], "LOGIN")
        self.assertTrue(s["sso"]["show_button"])
        self.set_aws("denied")
        self.assertTrue(self.snapshot()["sso"]["show_button"], "ERR shows it too: the human may be the one to fix it")
        self.set_aws("hang")
        s = self.snapshot()
        self.assertEqual((s["sso"]["creds"], s["sso"]["show_button"]), ("?", True), "a probe that timed out shows it (per the ticket)")
        self.set_aws("ok")
        self.snapshot()
        aws = self.cfg.aws
        try:
            self.cfg.aws = None                                    # a project without AWS: never a button
            self.set_aws("ok")
            s = self.snapshot()
            self.assertEqual((s["sso"]["creds"], s["sso"]["show_button"], s["sso"]["profile"]), ("n/a", False, ""))
        finally:
            self.cfg.aws = aws
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("if (!x || (!x.show_button && !recentSso)) return ''", js, "hidden, not greyed; a verdict on a recent click stays visible")
        self.assertIn("SSO updated", js)
        self.assertIn("kind: 'sso'", js)
        self.assertIn("still expired", js)
        self.assertIn("r.id === sent.id", js, "the still-expired notice is suppressed by a reply to THIS click only")
        self.assertIn("encodeURIComponent(w.id)", js)

    def test_click_reprobes_records_what_the_panel_saw_and_is_idempotent_per_state(self):
        self.set_aws("expired")
        self.snapshot()
        n = len(self.aws_calls())
        st, _, a = self.post({"kind": "sso"})
        self.assertEqual(st, 201, a)
        self.assertEqual(a["panel_saw"], "LOGIN", "the click re-probed at once and saw the login still expired")
        self.assertEqual(len(self.aws_calls()), n + 1, "exactly one fresh probe, the cache notwithstanding")
        self.assertEqual(self.aws_calls()[-1][:2], ["sts", "get-caller-identity"], "the only aws command the panel runs")
        self.assertEqual(self.get_json("/api/snapshot")["header"]["creds"]["left"], "LOGIN", "the header's creds source was refreshed")
        st, _, a2 = self.post({"kind": "sso"})
        self.assertEqual((st, a2["id"]), (200, a["id"]), "a nervous thumb: same state, same receipt")
        for _ in range(5):
            self.post({"kind": "sso"})
        self.assertEqual(len(self.aws_calls()), n + 1, "repeats and bursts reuse the last probe: no aws process per click")
        self.set_aws("ok")
        with mock.patch.object(panel_receipts, "PROBE_MIN_S", 0.0):      # the human took longer than the probe's reuse window
            st, _, b = self.post({"kind": "sso"})
        self.assertEqual(st, 201)
        self.assertNotEqual(b["id"], a["id"], "after the real login the click is a new receipt")
        self.assertEqual(b["panel_saw"], "ok")
        self.assertEqual(len(self.aws_calls()), n + 2)
        self.assertEqual(self.get_json("/api/snapshot")["header"]["creds"]["left"], "ok")
        st, _, b2 = self.post({"kind": "sso"})
        self.assertEqual((st, b2["id"]), (200, b["id"]))
        got = self.desk_get(f"/api/receipts/{b['id']}")[2]
        self.assertEqual(got["panel_saw"], "ok")
        for rid in (a["id"], b["id"]):
            self.assertNotIn("not probed", self.desk_get(f"/api/receipts/{rid}")[2]["panel_saw"])
        st, _, d = self.post({"kind": "creds"})
        self.assertEqual((st, d["reason"]), (400, "bad-kind"), "the panel's own event kind is not postable from the page")
        with mock.patch.object(panel_receipts.ReceiptStore, "over_budget", lambda self_, t: True):
            calls = len(self.aws_calls())
            st, _, d = self.post({"kind": "sso"})
            self.assertEqual(st, 429, "over the per-token budget: refused BEFORE any probe runs")
            self.assertEqual(len(self.aws_calls()), calls)


class TestPollAndEvent(SsoWorld):
    def test_poll_line_names_every_waiting_ticket_and_its_owner(self):
        self.append_log("T-0002", WAIT_LINE.format(who="someone", step="rerun scripts/sync.sh --shard 3"))
        self.append_log("T-0008", WAIT_LINE.format(who="w8", step="aws s3 ls again"))
        self.append_log("T-0008", "- 2026-10-14 00:31 w8: resumed by hand")
        self.set_aws("expired")
        self.post({"kind": "sso"})
        r = self.ok("poll", "--timeout-ms", "3000")
        out = r.stdout
        self.assertIn("claim: the human ran `aws sso login` (panel saw: LOGIN). verify FIRST: aws sts get-caller-identity --profile default;", out)
        self.assertIn('waiting: T-0002 (status "in-dev", owner "someone", since 2026-10-14 00:30) -- its resume step is on the data line', out)
        self.assertIn('"resume": "rerun scripts/sync.sh --shard 3"', out, "the resume step is JSON on the data line (T-0202)")
        self.assertNotIn("T-0008", out, "a later log line cleared it")
        self.assertIn('zouinify reply ', out)
        self.assertIn('"sso ok: resumed T-xxxx, T-yyyy"', out)
        self.assertIn('"still expired: run aws sso login" and change nothing', out)
        self.assertNotIn("SSO still expired", out, "one wording, the one the page colours amber")
        self.append_log("T-0002", "- 2026-10-14 00:40 someone: resumed")
        self.set_aws("ok")
        with mock.patch.object(panel_receipts, "PROBE_MIN_S", 0.0):      # the human took longer than the probe's reuse window
            self.post({"kind": "sso"})
        r = self.ok("poll", "--timeout-ms", "3000")
        self.assertIn("(panel saw: ok)", r.stdout)
        self.assertIn("waiting: none (no open ticket's newest log line is `waits-on: aws-sso`)", r.stdout)

    def test_login_to_ok_transition_emits_one_creds_event(self):
        self.set_aws("expired")
        self.srv.snap.refresh("creds")
        self.assertEqual(self.get_json("/api/snapshot")["header"]["creds"]["left"], "LOGIN")
        self.set_aws("ok")
        self.srv.snap.refresh("creds")
        st, _, d = self.poll(timeout_ms=300)
        self.assertEqual([(r["kind"], r["source"], r["panel_saw"]) for r in d["receipts"]], [("creds", "panel", "ok")],
                         "the event is pending like a receipt until the desk's poll fetches and acknowledges it")
        ev = d["receipts"][0]
        self.assertIn("LOGIN -> ok", ev["text"])
        self.srv.snap.refresh("creds")                                    # ok -> ok: nothing more
        self.srv.snap.refresh("creds")
        self.ack([ev["id"]])
        self.assertEqual(self.poll(timeout_ms=200)[2]["receipts"], [], "one event per transition")
        self.assertEqual([x for x in self.audit() if x["event"] == "event"][-1]["kind"], "creds", "audited as an event, not a receipt")
        # LOGIN -> ? -> ok: the first probe after `aws sso login` often times out; the definite states still say LOGIN -> ok
        self.set_aws("expired")
        self.srv.snap.refresh("creds")
        self.set_aws("hang")
        self.srv.snap.refresh("creds")
        self.assertEqual(self.get_json("/api/snapshot")["header"]["creds"]["left"], "?")
        self.set_aws("ok")
        self.srv.snap.refresh("creds")
        st, _, d = self.poll(timeout_ms=300)
        self.assertEqual(len(d["receipts"]), 1, "a second LOGIN -> ok (through a ?), a second event")
        self.ack([r["id"] for r in d["receipts"]])
        # the human clicked after logging in: the click is the receipt, no creds event on top of it (the review's blocker)
        self.set_aws("expired")
        self.srv.snap.refresh("creds")
        self.set_aws("ok")
        with mock.patch.object(panel_receipts, "PROBE_MIN_S", 0.0):
            st, _, clicked = self.post({"kind": "sso"})
        self.assertEqual((st, clicked["panel_saw"]), (201, "ok"))
        self.assertEqual(self.get_json("/api/snapshot")["header"]["creds"]["left"], "ok", "the click's probe updated the header")
        self.srv.snap.refresh("creds")                                    # the refresher runs after the click: ok -> ok, nothing
        st, _, d = self.poll(timeout_ms=300)
        self.assertEqual([r["kind"] for r in d["receipts"]], ["sso"], "one receipt, no duplicate event: the desk acts once")
        self.ack([r["id"] for r in d["receipts"]])
        # and the other order, minutes later (the earlier click is no longer open): the probe saw the login come back, then
        # the human clicks too: both arrive, and the brief says act once, reply to both
        with mock.patch.object(panel_receipts, "IDEMPOTENT_S", 0.0), mock.patch.object(panel_receipts, "PROBE_MIN_S", 0.0):
            self.set_aws("expired")
            self.srv.snap.refresh("creds")
            self.set_aws("ok")
            self.srv.snap.refresh("creds")
            self.post({"kind": "sso"})
        st, _, d = self.poll(timeout_ms=300)
        self.assertEqual([r["kind"] for r in d["receipts"]], ["creds", "sso"], "both arrive; the brief says: act once, reply to both")
        r = self.ok("poll", "--timeout-ms", "3000", "--no-ack")
        self.assertIn(f"event {d['receipts'][0]['id']} creds (", r.stdout)
        self.assertIn("the panel's credentials probe saw LOGIN -> ok (the human logged in without clicking). verify FIRST:", r.stdout)
        self.assertIn("A creds event and an sso click for the same login may both arrive: act once, reply to both.", r.stdout)
        self.assertIn("waiting: none", r.stdout)

    def test_the_panel_package_spawns_nothing_new(self):
        r = subprocess.run(["grep", "-rn", "subprocess\\|Popen\\|os.system", os.path.join(REPO, "lib", "zouinify", "panel")], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "", r.stdout)
        src = open(os.path.join(REPO, "lib", "zouinify", "status.py")).read()
        self.assertEqual(src.count('["aws", "sts", "get-caller-identity"'), 1, "the one aws command, in status.sts_probe")


class TestBriefs(World):
    def test_desk_brief_renders_the_sso_rule_with_the_profile(self):
        out = os.path.join(self.tmp, "briefs")
        self.ok("briefs", "render", "--out", out)
        desk = open(os.path.join(out, "desk.md")).read()
        self.assertIn("7. **SSO updated.**", desk)
        self.assertIn("aws sts get-caller-identity --profile default", desk)
        self.assertIn('waits-on: aws-sso -- resume: <exact step>', desk)
        self.assertIn('zouinify reply <id> "still expired: run aws sso login"', desk)
        rendered = os.path.join(self.tmp, "protocol")
        self.ok("protocol", "render", "--out", rendered)
        self.assertIn("waits-on: aws-sso -- resume: <the exact step to rerun>", open(os.path.join(rendered, "WORKER_BRIEF.md")).read())
        self.assertIn("waits-on: aws-sso -- resume: <exact step>", open(os.path.join(rendered, "README.md")).read())


if __name__ == "__main__":
    unittest.main()
