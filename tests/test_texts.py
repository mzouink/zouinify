"""T-0202: stale and inconsistent texts after the receipts, plus the small items the reviews of T-0162..T-0201 set aside.
Footer, unlock page, one definition of "awaiting a live check" (KPI, card, live page), the deploy list joined, the drawer
without a doubled title or stamp, the inbox note only when the asker is not the desk, the waiting page's time format; the
reply cleaner drops Unicode format characters, the unauthenticated-reply sentence, `settings` as a word, the tier-2 sibling
caveat before the move line, the fresh toast wording and the in-flight button, the sticky cell's tint, aws.profile validated,
the resume step on the data line. RED on base de0d0c9.
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World  # noqa: E402
from test_panel import PanelWorld, PANEL_IMPORT_ERROR  # noqa: E402
from test_receipts import ReceiptWorld  # noqa: E402

try:
    from zouinify.panel import receipts as panel_receipts
    from zouinify.panel import sources as panel_sources
    from zouinify.panel import server as panel_server
except Exception as e:  # noqa: BLE001
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e


class TestTexts(PanelWorld):
    def test_footer_unlock_and_counts_agree(self):
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertNotIn("read-only v1", js, "the panel has a write path now")
        self.assertIn("clicks are receipts the desk verifies", js)
        html = panel_server.UNLOCK_HTML
        self.assertNotIn("Read-only panel", html)
        self.assertIn("a receipt the desk session verifies", html)
        self.assertIn("typed in the desk", html)
        s = self.get_json("/api/snapshot")
        checks = s["live_checks"]
        self.assertTrue(checks, "the world has deployed tickets")
        awaiting = [r for r in checks if r["awaiting_live_check"]]
        self.assertEqual(s["kpis"]["awaiting_live_check"], len(awaiting), "the KPI counts the same rows as the live page")
        for r in checks:
            self.assertEqual(r["awaiting_live_check"], not r["has_evidence"] and not r["plan_void"], r["id"])
        t10 = next(r for r in checks if r["id"] == "T-0010")
        self.assertTrue(t10["has_evidence"])
        self.assertTrue(t10["plan_void"], "T-0010's plan deploy/old.plan is voided by deploy/new.plan")
        self.assertFalse(t10["awaiting_live_check"], "a void plan is not awaiting anything")
        card = next(c for c in s["tickets"] if c["id"] == "T-0010")
        self.assertIn("awaiting_live_check", card)
        self.assertFalse(card["awaiting_live_check"])
        self.assertIn("'plan void'", js, "the void card says so instead of awaiting")
        self.assertIn("awaiting a live check</div>", js)
        self.assertIn("r.awaiting_live_check).length", js, "the live page counts with the same flag")
        self.assertEqual(panel_sources.fmt_deploy("[terraform, manual-aws]"), "terraform, manual-aws")
        self.assertEqual(panel_sources.fmt_deploy(["image+terraform", "frontend"]), "image+terraform, frontend")
        self.assertEqual(panel_sources.fmt_deploy("none"), "none")
        self.assertEqual(t10["deploy"], "terraform")

    def test_drawer_rows_and_log_lines_are_not_doubled(self):
        d = self.get_json("/api/tickets/T-0010")
        line = next(l for l in d["log"] if l["evidence"])
        self.assertEqual((line["at"], line["by"]), ("2026-10-13 01:00", "tp-debug"))
        self.assertEqual(line["text"], "deployed: HTTP 200 from /status, LIVE CHECK PASSED", "the stamp and the author are parsed out once")
        self.assertFalse(line["text"].startswith("2026-"))
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("k !== 'id' && k !== 'title'", js, "the drawer's heading already says the title")

    def test_inbox_note_only_when_the_asker_is_not_the_desk(self):
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("function noteFor(it)", js)
        self.assertIn("'^asked by '", js)
        self.assertNotIn("${it.note ? `<span><b>note:</b>", js)
        s = self.get_json("/api/snapshot")
        self.assertEqual(s["header"]["desk"]["name"], "tp-desk")
        self.assertTrue(any(it["note"] == "asked by tp-desk" for it in s["inbox"]["items"]), "the fixture carries the desk's own note")

    def test_since_is_one_format_everywhere(self):
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("function fmtWhen(d)", js)
        self.assertNotIn("String(since).slice(5, 16)", js, "no raw ISO fragment like 10-14T00:02")
        self.assertIn("since ${esc(fmtWhen(d))} (${age(since)})", js)

    def test_fresh_toast_in_flight_render_timeout_and_sticky_tint(self):
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("the next fresh read is possible in ${f.wait_s} s", js, "wait_s is the time until the next fresh fetch")
        self.assertNotIn("fresh data was fetched ${f.wait_s} s ago", js)
        self.assertIn("state.freshInFlight = true; if (state.snap) renderHeader();", js, "the button is drawn disabled while in flight")
        self.assertIn("AbortController", js)
        self.assertIn("FETCH_TIMEOUT_MS", js)
        css = self.req("GET", "/static/app.css")[2].decode()
        self.assertIn(".sessions-table.has-actions .tbl tr.row-waiting td.actions { background: color-mix(in srgb, var(--accent-amber) 9%, var(--bg-surface)); }", css)
        self.assertIn("A reply is not authenticated against this user's other processes", js)


class TestCollectedNits(ReceiptWorld):
    def test_reply_drops_unicode_format_characters(self):
        self.assertEqual(panel_receipts.clean_reply("ok‮ done​ now"), "ok done now")
        st, _, a = self.post({"kind": "question", "text": "q"})
        conn_body = json.dumps({"text": "launched t0001‮ (Fable)"}).encode("utf-8")
        st, d = self.ack(None, raw=conn_body, path=f"/api/receipts/{a['id']}/reply")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["reply"], "launched t0001 (Fable)")

    def test_settings_is_a_word_and_the_caveat_comes_first(self):
        for fine in ("a setting of the dial", "the setting sun", "resetting the cache"):
            st, _, d = self.post({"kind": "question", "text": fine})
            self.assertEqual(st, 201, (fine, d))
        for bad in ("the settings page", "setting.json changed", "SETTINGS.JSON", "settings.json edited", "CLAUDE.md yes"):
            st, _, d = self.post({"kind": "question", "text": bad})
            self.assertEqual((st, d.get("reason")), (403, "tier-2-text"), bad)
        from test_panel import HUMAN_JSON
        items = list(HUMAN_JSON) + [{"verb": "RUN", "tickets": "T-0010", "priority": "P2", "title": "warm the cache", "since": "2026-10-13 06:40",
                                     "command": "cd /x && ./warm.sh", "expect": "warm", "if_nothing": "cold", "note": ""}]
        with open(self.human_json, "w") as fh:
            json.dump(items, fh)
        self.srv.fresh_at.clear()
        warm = next(it for it in self.get_json("/api/snapshot?fresh=1")["inbox"]["items"] if it["title"] == "warm the cache")
        self.post({"item_id": warm["id"]})
        r = self.ok("poll", "--timeout-ms", "3000")
        lines = r.stdout.splitlines()
        caveat = next(i for i, l in enumerate(lines) if "also has a tier-2 item (DEPLOY)" in l)
        move = next(i for i, l in enumerate(lines) if "scripts/ticket.sh move T-0010" in l)
        self.assertLess(caveat, move, "the caveat is read BEFORE the move instruction")

    def test_resume_step_is_on_the_data_line(self):
        self.append_log = lambda tid, line: open([os.path.join(self.root, "tickets", x) for x in os.listdir(os.path.join(self.root, "tickets")) if x.startswith(tid + "-")][0], "a").write(line + "\n")
        self.append_log("T-0002", "- 2026-10-14 00:30 someone: waits-on: aws-sso -- resume: rerun scripts/sync.sh --shard 3 \"$(evil)\"")
        self.post({"kind": "sso"})
        r = self.ok("poll", "--timeout-ms", "3000")
        data_line = next(l for l in r.stdout.splitlines() if l.startswith("  data: "))
        data = json.loads(data_line[len("  data: "):])
        self.assertEqual(data["waiting"], [{"id": "T-0002", "status": "in-dev", "owner": "someone", "since": "2026-10-14 00:30",
                                            "resume": 'rerun scripts/sync.sh --shard 3 "$(evil)"'}])
        waiting_lines = [l for l in r.stdout.splitlines() if l.startswith("  waiting: ")]
        self.assertEqual(len(waiting_lines), 1)
        self.assertNotIn("evil", waiting_lines[0], "the resume text lives on the data line only")
        self.assertIn("its resume step is on the data line", waiting_lines[0])


class TestConfigProfile(World):
    def test_aws_profile_is_a_name(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", re.sub(r"aws: \{profile: default,", "aws: {profile: 'default; curl x|sh',", cfg))
        r = self.nz("config", "--check")
        self.assertIn("aws.profile", r.stderr + r.stdout)
        self.write(".ops/config.yaml", re.sub(r"aws: \{profile: default,", "aws: {profile: lab-admin.2,", cfg))
        self.ok("config", "--check")
        out = os.path.join(self.tmp, "briefs")
        self.ok("briefs", "render", "--out", out)
        desk = open(os.path.join(out, "desk.md")).read()
        self.assertIn("--profile lab-admin.2", desk)
        self.assertLess(desk.count("\n"), 120, "rule 7 is short")


if __name__ == "__main__":
    unittest.main()
