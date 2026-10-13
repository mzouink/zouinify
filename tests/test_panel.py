"""Tests for the read-only control panel (T-0160): the HTTP API, the sources, redaction, the loopback
binding, the start/stop control and the `up` integration.

The world is tests/test_core.py's throwaway project (fake tmux, fake claude, fake ticket.sh); the server
runs in-process on an ephemeral port for the API tests, and as a real detached process (on a free port)
for the start/stop tests. The repo under test is $ZOUINIFY_REPO_UNDER_TEST (default: this checkout), so
a base checkout without lib/zouinify/panel/ gives the RED evidence (ImportError / unknown command).
"""
import http.client
import json
import os
import signal
import socket
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

from test_core import World, TICKETS  # noqa: E402

try:
    from zouinify import config as cfgmod
    from zouinify.panel import server as panel_server
    from zouinify.panel import redact as panel_redact
    PANEL_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - the RED case: no panel package on the base tree
    PANEL_IMPORT_ERROR = e


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


HUMAN_JSON = [
    {"verb": "DEPLOY", "tickets": "T-0010", "priority": "P2", "title": "deploy the thing", "since": "2026-10-13 06:00",
     "command": "cd /x && scripts/deploy_site.sh", "expect": "LIVE CHECK PASSED", "if_nothing": "stays unreleased", "note": "asked by tp-desk"},
    {"verb": "APPROVE", "tickets": "T-0008", "priority": "P1", "title": "the CLAUDE.md rule wording", "since": "2026-10-13 05:00",
     "command": "reply \"T-0008 yes CLAUDE.md\" or \"T-0008 no\"", "expect": "T-0008 yes CLAUDE.md", "if_nothing": "cannot merge", "note": ""},
    {"verb": "DECIDE", "tickets": "T-0006", "priority": "P1", "title": "keep or delete", "since": "2026-10-12 15:00",
     "command": "reply \"T-0006 delete\" or \"T-0006 keep\"", "expect": "T-0006 delete", "if_nothing": "stays blocked", "note": ""},
]

PROMPT_SCREEN = """\
 Bash(git push origin dev)
 Do you want to proceed?
 > 1. Yes
   2. Yes, and don't ask again
   3. No, and tell Claude what to do differently (esc)

   [Fable 5.1] x | dev
"""

# the fake session token is assembled from fragments: no credential-shaped literal may sit in this file
FAKE_SESSION_TOKEN = "FQoGZXIvYXdzEBYa" + "DHNvbWV0aGluZ2hlcmUi" + "LGZha2V0b2tlbg=="
SECRET_SCREEN = f"""\
> export AWS creds
 aws_secret_access_key = x
 aws_session_token = {FAKE_SESSION_TOKEN}
 Bearer abc.def.ghi
 fine line here
   [Opus 5.5] x | dev
"""


class PanelWorld(World):
    """World + a fake agents listing + planted screens; the in-process server for API tests."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable from {REPO}/lib: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        self.port = free_port()
        self.write(".ops/config.yaml", open(os.path.join(self.root, ".ops", "config.yaml")).read()
                   .replace("port: 4799", f"port: {self.port}")
                   .replace("panel: {", "panel: {autostart: false, logs: [{label: apply, glob: 'deploy/*.apply.log'}], plans_glob: 'deploy/*.plan', "))
        os.makedirs(os.path.join(self.root, "deploy"))
        self.write("deploy/old.plan", "old")
        time.sleep(0.02)
        self.write("deploy/new.plan", "new")
        self.write("deploy/new.plan.apply.log", "line1\naws_secret_access_key = x\nApply complete! serial 3 -> 4\n")
        self.write("tickets/T-0010-deployed-one.md", open(os.path.join(self.root, "tickets", "T-0010-deployed-one.md")).read()
                   .replace("plan:\n", "plan: deploy/old.plan\n").replace("deploy: none", "deploy: terraform")
                   .replace("- 2026-10-13 00:00 tester: filed.", "- 2026-10-13 00:00 tester: filed.\n- 2026-10-13 01:00 tp-debug: deployed: HTTP 200 from /status, LIVE CHECK PASSED"))
        self.write("tickets/T-0009-rtm-one.md", open(os.path.join(self.root, "tickets", "T-0009-rtm-one.md")).read()
                   .replace("plan:\n", "plan: deploy/new.plan\n").replace("status: ready-to-merge", "status: ready-to-merge"))
        os.makedirs(os.path.join(self.root, "tickets", "releases"))
        self.write("tickets/releases/R-2026-10-12-1.md", "---\nid: R-2026-10-12-1\nstage: prod\nstatus: live\ntag: prod/2026-10-12-1\n---\n\nrelease\n")
        self.human_json = os.path.join(self.tmp, "human.json")
        with open(self.human_json, "w") as fh:
            json.dump(HUMAN_JSON, fh)
        self.env["FAKE_HUMAN_JSON"] = self.human_json
        # two "running" sessions in the fake tmux: the desk and a worker at a permission prompt; a foreign one elsewhere
        self.ok("up", "desk")
        self.ok("launch", "worker", "T-0001")
        with open(os.path.join(self.state, "screen-t0001-first-ready-ticket.txt"), "w") as fh:
            fh.write(PROMPT_SCREEN)
        self.agents = os.path.join(self.tmp, "agents.json")
        with open(self.agents, "w") as fh:
            json.dump([
                {"name": "tp-desk", "cwd": self.root, "status": "busy", "pid": os.getpid(), "kind": "interactive"},
                {"name": "t0001-first-ready-ticket", "cwd": os.path.join(self.root, ".claude", "worktrees", "t0001-first-ready-ticket"), "status": "waiting", "pid": 1, "kind": "interactive"},
                {"name": "tp-debug", "cwd": self.root, "status": "idle", "pid": 2, "kind": "interactive"},
                {"name": "other-project", "cwd": self.tmp, "status": "idle", "pid": 3, "kind": "interactive"},
            ], fh)
        self.env["FAKE_CLAUDE_AGENTS"] = self.agents
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.cfg = cfgmod.load(self.root)
        self.srv = panel_server.make_server(self.cfg, port=0)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            self.env_patch.stop()
            super().tearDown()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=60)
        conn.request(method, path, body=body)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), data

    def get_json(self, path):
        st, hdr, data = self.req("GET", path)
        self.assertEqual(st, 200, f"{path}: {st} {data[:300]}")
        return json.loads(data)


class TestApi(PanelWorld):
    def test_health_and_binding(self):
        d = self.get_json("/health")
        self.assertTrue(d["ok"])
        self.assertEqual(d["root"], self.root)
        self.assertEqual(d["bind"], "loopback")
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_no_write_path(self):
        for method, path in (("POST", "/api/receipts"), ("POST", "/api/snapshot"), ("POST", "/"), ("PUT", "/api/tickets/T-0001"),
                             ("DELETE", "/api/sessions/x"), ("PATCH", "/health")):
            st, hdr, _ = self.req(method, path, body=b"{}")
            self.assertEqual(st, 405, f"{method} {path} -> {st}")
            self.assertIn("GET", hdr.get("Allow", ""))

    def test_snapshot_inbox_is_human_json_in_order(self):
        s = self.get_json("/api/snapshot")
        items = s["inbox"]["items"]
        self.assertEqual([i["verb"] for i in items], ["DEPLOY", "APPROVE", "DECIDE"])
        self.assertEqual([i["tickets"] for i in items], [h["tickets"] for h in HUMAN_JSON])
        for i in items:
            self.assertRegex(i["id"], r"^[0-9a-f]{10}$")
        self.assertEqual(items[0]["tier"], 1)
        self.assertEqual(items[1]["tier"], 2)            # APPROVE is always tier 2
        self.assertIn("type in the", items[1]["hint"])
        self.assertEqual(items[2]["tier"], 1)
        self.assertEqual(items[2]["options"], ["T-0006 delete", "T-0006 keep"])
        self.assertEqual(s["kpis"]["inbox"], 3)          # the items of human --json

    def test_snapshot_board_lanes_and_chips(self):
        s = self.get_json("/api/snapshot")
        b = s["board"]
        lanes = {k: [t["id"] for t in v] for k, v in b["lanes"].items()}
        self.assertEqual(sorted(lanes["ready"]), ["T-0001", "T-0005", "T-0007"])
        self.assertEqual(lanes["in-dev"], ["T-0002"])
        self.assertEqual(sorted(lanes["review"]), ["T-0008", "T-0009"])   # in-review + ready-to-merge
        self.assertEqual(lanes["deployed"], ["T-0011", "T-0010"])        # severity first
        total = sum(len(v) for v in lanes.values()) + sum(b["chips"].values()) + sum(b.get("other", {}).values())
        self.assertEqual(total, len(TICKETS))
        self.assertEqual(b["chips"]["blocked"], 1)
        self.assertEqual(b["chips"]["done"], 1)
        self.assertEqual(b["chips"]["dropped"], 1)
        self.assertEqual(s["kpis"]["p0"], 0, "the KPI counts P0 items in the inbox (none in HUMAN_JSON)")
        self.assertEqual(s["kpis"]["p0_open_tickets"], 2)
        self.assertEqual([t["id"] for t in s["p0"]], ["T-0005", "T-0011"], "the banner lists every open P0 ticket, with its state")
        # newest plan: deploy/new.plan; T-0010's plan is old -> VOID
        self.assertEqual(b["newest_plan"], "new.plan")
        dep = [t for t in b["lanes"]["deployed"] if t["id"] == "T-0010"][0]
        self.assertTrue(dep["plan_void"])
        self.assertEqual(s["kpis"]["to_deploy"], 0)
        self.assertEqual(s["kpis"]["deployed"], 2)

    def test_sessions_waiting_and_mapping(self):
        s = self.get_json("/api/snapshot")
        rows = {r["name"]: r for r in s["sessions"]["rows"]}
        self.assertNotIn("other-project", rows, "sessions of other projects are not listed")
        self.assertEqual(rows["tp-desk"]["role"], "desk")
        self.assertEqual(rows["tp-desk"]["tmux"], "tp-desk")
        self.assertEqual(rows["tp-desk"]["state"], "working")
        self.assertEqual(rows["tp-desk"]["model"], "Fable 5.1")
        self.assertFalse(rows["tp-desk"]["model_mismatch"])
        self.assertEqual(s["sessions"]["rows"][0]["name"], "tp-desk", "the desk row is pinned first")
        w = rows["t0001-first-ready-ticket"]
        self.assertEqual(w["state"], "waiting")
        self.assertEqual(w["role"], "worker")
        self.assertEqual(w["ticket"], "T-0001")
        self.assertEqual(w["tmux"], "t0001-first-ready-ticket")
        self.assertIn("Do you want to proceed", w["waiting_for"])
        self.assertEqual([r["name"] for r in s["waiting"]], ["t0001-first-ready-ticket"])
        self.assertTrue(s["waiting"][0]["since"])
        self.assertEqual(rows["tp-debug"]["state"], "idle")
        self.assertEqual(rows["tp-debug"]["model"], "?", "no tmux window: no status bar to read")
        gone = [r for r in s["sessions"]["rows"] if r["state"] == "gone"]
        self.assertIn("tp-manager", [r["name"] for r in gone], "a configured role that is not running shows as gone")
        self.assertEqual(s["kpis"]["waiting"], 1)
        self.assertEqual(s["kpis"]["working"], 1)

    def test_redaction_everywhere(self):
        with open(os.path.join(self.state, "screen-tp-desk.txt"), "w") as fh:
            fh.write(SECRET_SCREEN)
        d = self.get_json("/api/sessions/tp-desk?fresh=1")
        screen = d["screen"]
        self.assertIn("aws_secret_access_key = <redacted>", screen)
        self.assertNotIn("= x\n", screen)
        self.assertNotIn(FAKE_SESSION_TOKEN[:12], screen)
        self.assertIn("Bearer <redacted>", screen)
        self.assertIn("fine line here", screen)
        self.assertEqual(d["model"], "Opus 5.5")
        self.assertTrue(d["model_mismatch"], "Opus on a session configured for fable: the red badge fires")
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertNotIn("= x", json.dumps(s["sessions"]))
        logs = self.get_json("/api/logs?label=apply")
        self.assertIn("aws_secret_access_key = <redacted>", "\n".join(logs["tail"]))
        self.assertIn("Apply complete! serial 3 -> 4", "\n".join(logs["tail"]))
        self.assertEqual(logs["path"], "deploy/new.plan.apply.log")
        st, _, _ = self.req("GET", "/api/logs?label=nope")
        self.assertEqual(st, 404)

    def test_ticket_detail_and_live_checks(self):
        d = self.get_json("/api/tickets/T-0010")
        self.assertEqual(d["front_matter"]["status"], "deployed")
        self.assertEqual(d["log"][0]["text"].split(": ", 1)[1][:9], "deployed:")
        self.assertIn("none", d["handoff"].lower())
        for bad in ("/api/tickets/T-9999", "/api/tickets/../x", "/api/tickets/..%2F..%2Fetc", "/api/sessions/no-such"):
            st, _, _ = self.req("GET", bad)
            self.assertEqual(st, 404, bad)
        s = self.get_json("/api/snapshot")
        live = {t["id"]: t for t in s["live_checks"]}
        self.assertIn("LIVE CHECK PASSED", live["T-0010"]["evidence"])
        self.assertTrue(live["T-0010"]["has_evidence"])
        self.assertEqual(s["releases"][0]["id"], "R-2026-10-12-1")
        self.assertEqual(s["header"]["freeze"]["on"], False)
        self.write("tickets/FREEZE", "T-0005\n")
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual(s["header"]["freeze"], {"on": True, "ids": ["T-0005"]})

    def test_static_page_has_no_external_requests(self):
        st, hdr, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode()
        self.assertIn("text/html", hdr.get("Content-Type", ""))
        self.assertNotRegex(html, r'(src|href)="https?://', "no CDN, no fonts, nothing leaves the host")
        self.assertIn("zouin", html)
        for path in ("/static/app.js", "/static/app.css"):
            st, hdr, body = self.req("GET", path)
            self.assertEqual(st, 200, path)
            self.assertNotRegex(body.decode(), r"https?://(?!127\.0\.0\.1|localhost)", f"{path} references an external host")
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn("available after receipts (T-0162)", js)
        self.assertNotIn("fetch(", js.replace("fetch(api", "").replace("fetch(`/api", "").replace("fetch('/api", "").replace('fetch("/api', ""),
                         "the page only ever fetches its own /api")
        for bad in ("/static/../server.py", "/static/..%2F..%2Fcli.py", "/nope"):
            st, _, _ = self.req("GET", bad)
            self.assertEqual(st, 404, bad)

    def test_sources_carry_ages_and_settings(self):
        s = self.get_json("/api/snapshot")
        for name in ("inbox", "board", "sessions", "creds", "logs", "releases"):
            self.assertIn(name, s["sources"], name)
            self.assertIn("age_s", s["sources"][name])
            self.assertIn("ttl_s", s["sources"][name])
        self.assertEqual(s["sitting"]["at"], "09:30")
        self.assertEqual(s["config"]["project"]["name"], "testproj")
        self.assertNotIn("aws_secret", json.dumps(s["config"]))
        self.assertIn("installed", s["hook"])
        self.assertEqual(s["panel"]["bind"], "loopback")
        self.assertEqual(s["header"]["desk"]["name"], "tp-desk")


class TestLoading(PanelWorld):
    def test_requests_never_block_while_the_refresher_warms_up(self):
        snap = self.srv.snap
        self.assertEqual(snap.loading(), sorted(snap.src.keys(), key=list(snap.src.keys()).index), "nothing loaded yet")
        slow = threading.Thread(target=time.sleep, args=(3,), daemon=True)
        slow.start()
        snap.thread = slow                                  # a refresher is "running": a cold source must not block
        t0 = time.time()
        self.assertEqual(snap.get("inbox"), (None, None, None))
        self.assertLess(time.time() - t0, 0.5)
        s = self.get_json("/api/snapshot")
        self.assertIn("inbox", s["loading"])
        self.assertEqual(s["inbox"]["items"], [])
        self.assertTrue(s["sources"]["inbox"]["loading"])
        snap.thread = None                                  # no refresher: the request loads it synchronously
        data, age, err = snap.get("inbox")
        self.assertEqual(len(data["items"]), 3)
        self.assertIsNone(err)
        snap.start_refresher()                              # the real refresher warms every source
        deadline = time.time() + 30
        while time.time() < deadline and snap.loading():
            time.sleep(0.2)
        self.assertEqual(snap.loading(), [])
        snap.stop()


class TestRedact(unittest.TestCase):
    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")

    def test_patterns(self):
        r = panel_redact.redact
        self.assertEqual(r("aws_secret_access_key = x"), "aws_secret_access_key = <redacted>")
        self.assertEqual(r("WEB_TOKEN=abc123"), "WEB_TOKEN=<redacted>")
        self.assertEqual(r("passcode: 4421"), "passcode: <redacted>")
        self.assertEqual(r("password = hunter2"), "password = <redacted>")
        self.assertEqual(r("key " + "sk-" + "ant-api03-abcdef"), "key <redacted>")   # assembled: no key-shaped literal in this file
        self.assertEqual(r("Authorization: Bearer eyJhbGciOi"), "Authorization: Bearer <redacted>")
        self.assertEqual(r("token_id = a1b2 stays"), "token_id = a1b2 stays")
        self.assertEqual(r("ordinary line with /status 200"), "ordinary line with /status 200")
        self.assertEqual(r(""), "")


class TestControl(World):
    """Real detached server processes on free ports: start, health, status, url, stop; the intranet refusal;
    the foreign-port refusal; `up` autostart."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        self.port = free_port()
        self.set_panel(f"port: {self.port}, autostart: false, bind: loopback")

    def set_panel(self, inner):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        import re
        cfg = re.sub(r"panel: \{.*\}", "panel: {" + inner + ", state_dir: .ops/state, refresh_s: 30}", cfg)
        self.write(".ops/config.yaml", cfg)

    def tearDown(self):
        p = os.path.join(self.root, ".ops", "state", "panel.json")
        if os.path.exists(p):
            try:
                pid = json.load(open(p)).get("pid")
                if pid:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
        super().tearDown()

    def health(self, port):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health")
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            return None, None

    def test_start_status_url_stop(self):
        r = self.ok("panel", "start")
        self.assertIn(f"http://127.0.0.1:{self.port}/", r.stdout)
        st, d = self.health(self.port)
        self.assertEqual(st, 200)
        self.assertEqual(d["root"], self.root)
        pj = json.load(open(os.path.join(self.root, ".ops", "state", "panel.json")))
        self.assertEqual(pj["port"], self.port)
        self.assertTrue(os.path.exists(f"/proc/{pj['pid']}"))
        reg = json.load(open(os.path.join(self.home, ".zouinify", "projects.json")))
        self.assertEqual([p["root"] for p in reg], [self.root])
        r = self.ok("panel", "status")
        self.assertIn("running", r.stdout)
        r = self.ok("panel", "url")
        self.assertEqual(r.stdout.strip(), f"http://127.0.0.1:{self.port}/")
        r = self.ok("panel", "start")                       # idempotent
        self.assertIn("already running", r.stdout)
        r = self.ok("panel", "stop")
        self.assertIn("stopped", r.stdout)
        deadline = time.time() + 5
        while time.time() < deadline and self.health(self.port)[0]:
            time.sleep(0.1)
        self.assertIsNone(self.health(self.port)[0])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")))
        reg = json.load(open(os.path.join(self.home, ".zouinify", "projects.json")))
        self.assertEqual(reg, [])
        r = self.ok("panel", "stop")
        self.assertIn("not running", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".ops", "state", "server.log")))

    def test_intranet_bind_refused_in_v1(self):
        self.set_panel(f"port: {self.port}, autostart: false, bind: intranet")
        r = self.fail("panel", "start")
        self.assertIn("T-0162", r.stderr)
        self.assertIsNone(self.health(self.port)[0])

    def test_foreign_port_refused(self):
        # project B's server answers /health on our port -> start refuses, nothing is reused
        other = os.path.join(self.tmp, "other")
        os.makedirs(os.path.join(other, ".ops"))
        with open(os.path.join(self.root, ".ops", "config.yaml")) as fh:
            cfg = fh.read().replace("name: testproj", "name: otherproj")
        with open(os.path.join(other, ".ops", "config.yaml"), "w") as fh:
            fh.write(cfg)
        r = self.ok("panel", "start", cwd=other, env=dict(self.env, ZOUINIFY_PROJECT=other))
        self.assertIn("http://", r.stdout)
        try:
            r = self.fail("panel", "start")
            self.assertIn("another project", r.stderr)
            self.assertIn(other, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")))
            r = self.fail("panel", "stop")
            self.assertIn("another project", r.stderr)
            self.assertEqual(self.health(self.port)[0], 200, "the other project's server is left alone")
        finally:
            self.ok("panel", "stop", cwd=other, env=dict(self.env, ZOUINIFY_PROJECT=other))
        # a non-panel process on the port: refuse too
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # the stopped server's connections may sit in TIME_WAIT
        s.bind(("127.0.0.1", self.port))
        s.listen(1)
        try:
            r = self.fail("panel", "start")
            self.assertIn("foreign", r.stderr.lower())
        finally:
            s.close()

    def test_up_starts_the_panel_when_autostart(self):
        self.set_panel(f"port: {self.port}, autostart: true, bind: loopback")
        r = self.ok("up", "desk", "--dry-run")
        self.assertIn("would start", r.stdout)
        self.assertIsNone(self.health(self.port)[0])
        r = self.ok("up", "desk", "--no-panel")
        self.assertIsNone(self.health(self.port)[0])
        r = self.ok("up", "desk")
        self.assertIn(f"http://127.0.0.1:{self.port}/", r.stdout)
        self.assertEqual(self.health(self.port)[0], 200)
        r = self.ok("up", "desk")
        self.assertIn("running", r.stdout)
        self.ok("panel", "stop")


if __name__ == "__main__":
    unittest.main()
