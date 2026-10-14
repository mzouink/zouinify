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
import re
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
    from zouinify.panel import sources as panel_sources
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
    {"verb": "RUN", "tickets": "T-0005", "priority": "P2", "title": "run the fix script", "since": "2026-10-13 06:30",
     "command": "cd /x && ./fix.sh", "expect": "fixed", "if_nothing": "stays broken", "note": ""},
]

PROMPT_SCREEN = """\
 Bash(git push origin dev)
 Do you want to proceed?
 ❯ 1. Yes
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
        # two sessions of the same user waiting ELSEWHERE (T-0175, "Waiting for me"): one with a tmux window in a
        # registered zouinify project, whose screen holds a prompt and a secret; one with no window, in a plain directory
        self.foreign_cwd = os.path.join(self.tmp, "elsewhere", "proj2")
        os.makedirs(self.foreign_cwd)
        subprocess.run(["tmux", "new-session", "-d", "-s", "elsewhere-bf", "-c", self.foreign_cwd, "claude", "--model", "fable", "--", "elsewhere"],
                       env=self.env, check=True, capture_output=True)
        with open(os.path.join(self.state, "sessions.json")) as fh:
            foreign_pid = json.load(fh)["elsewhere-bf"]["pid"]
        with open(os.path.join(self.state, "screen-elsewhere-bf.txt"), "w") as fh:
            fh.write(PROMPT_SCREEN + SECRET_SCREEN + "mysql -pS3cret db\nCookie: sid=abc; zouinify_token=x\n"
                     "mail alice@example.org and keeper@example.org\nsee /groups/lab/user/proj/file.txt on node7.lab.example.org 10.1.2.3\n")
        # a dialog plus a token-shaped string the redactor cannot place -> withheld; a screen with no dialog -> withheld
        for name, text in (("withheld-zz", "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n   2. No\nkey " + "Ab1" * 12 + "\n"),
                           ("nodialog-qq", "just some text\n> \n   [Fable 5.1] x | dev\n")):
            subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", self.foreign_cwd, "claude", "--model", "fable", "--", name],
                           env=self.env, check=True, capture_output=True)
            with open(os.path.join(self.state, f"screen-{name}.txt"), "w") as fh:
                fh.write(text)
        with open(os.path.join(self.state, "sessions.json")) as fh:
            fake_pids = {n: v["pid"] for n, v in json.load(fh).items()}
        os.makedirs(os.path.join(self.home, ".zouinify"), exist_ok=True)
        with open(os.path.join(self.home, ".zouinify", "projects.json"), "w") as fh:
            json.dump([{"name": "proj-two", "short": "p2", "root": self.foreign_cwd, "port": 4701, "pid": 1, "url": "http://127.0.0.1:4701/"}], fh)
        self.agents = os.path.join(self.tmp, "agents.json")
        with open(self.agents, "w") as fh:
            json.dump([
                {"name": "tp-desk", "cwd": self.root, "status": "busy", "pid": os.getpid(), "kind": "interactive", "sessionId": "sid-desk"},
                {"name": "t0001-first-ready-ticket", "cwd": os.path.join(self.root, ".claude", "worktrees", "t0001-first-ready-ticket"), "status": "waiting", "pid": 1, "kind": "interactive", "sessionId": "sid-worker"},
                {"name": "tp-debug", "cwd": self.root, "status": "idle", "pid": 2, "kind": "interactive", "sessionId": "sid-debug"},
                {"name": "other-project", "cwd": self.tmp, "status": "idle", "pid": 3, "kind": "interactive", "sessionId": "sid-other"},
                {"name": "elsewhere-bf", "cwd": self.foreign_cwd, "status": "waiting", "pid": foreign_pid, "kind": "interactive"},
                {"name": "lonely-xy", "cwd": os.path.join(self.tmp, "other3"), "status": "waiting", "pid": 7, "kind": "interactive", "sessionId": "sid-lonely"},
                {"name": "home-session", "cwd": self.home, "status": "waiting", "pid": 8, "kind": "interactive"},
                {"name": "withheld-zz", "cwd": self.foreign_cwd, "status": "waiting", "pid": fake_pids["withheld-zz"], "kind": "interactive"},
                {"name": "nodialog-qq", "cwd": self.foreign_cwd, "status": "waiting", "pid": fake_pids["nodialog-qq"], "kind": "interactive"},
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

    def test_host_and_origin_are_checked(self):
        port = self.srv.server_address[1]

        def raw(host, origin=None, path="/api/snapshot"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            if origin:
                conn.putheader("Origin", origin)
            conn.endheaders()
            r = conn.getresponse()
            body = r.read()
            conn.close()
            return r.status, body
        self.assertEqual(raw(f"127.0.0.1:{port}")[0], 200)
        self.assertEqual(raw(f"localhost:{port}")[0], 200)
        self.assertEqual(raw(f"127.0.0.1:{port}", origin=f"http://localhost:{port}")[0], 200)
        st, body = raw("evil.example")
        self.assertEqual(st, 403, "DNS rebinding: a foreign Host never gets the snapshot")
        self.assertNotIn(b'"inbox"', body)
        self.assertEqual(raw(f"evil.example:{port}")[0], 403)
        self.assertEqual(raw(f"127.0.0.1:{port}", origin="http://evil.example")[0], 403)
        self.assertEqual(raw("evil.example", path="/")[0], 403)
        self.assertEqual(raw("evil.example", path="/health")[0], 403)

    def test_the_one_write_is_the_receipt_and_everything_else_is_405(self):
        for method, path in (("POST", "/api/snapshot"), ("POST", "/"), ("PUT", "/api/tickets/T-0001"), ("PUT", "/api/receipts"),
                             ("DELETE", "/api/sessions/x"), ("PATCH", "/health"), ("POST", "/api/hook")):
            st, hdr, _ = self.req(method, path, body=b"{}")
            self.assertEqual(st, 405, f"{method} {path} -> {st}")
            self.assertEqual(hdr.get("Allow", ""), "POST" if path == "/api/receipts" else "GET, HEAD", f"{method} {path}")
        st, hdr, body = self.req("POST", "/api/receipts", body=b"{}")
        self.assertEqual(st, 401, "the one write exists (T-0162) and needs the browser token even on loopback")
        self.assertIn("token", json.loads(body)["error"])

    def test_snapshot_inbox_is_human_json_in_order(self):
        s = self.get_json("/api/snapshot")
        items = s["inbox"]["items"]
        self.assertEqual([i["verb"] for i in items], ["DEPLOY", "APPROVE", "DECIDE", "RUN"])
        self.assertEqual([i["tickets"] for i in items], [h["tickets"] for h in HUMAN_JSON])
        for i in items:
            self.assertRegex(i["id"], r"^[0-9a-f]{10}$")
        self.assertEqual(items[0]["tier"], 2, "DEPLOY: the human runs it and types the ok; never a button")
        self.assertIn("T-0010 ok", items[0]["hint"])
        self.assertEqual(items[1]["tier"], 2)            # APPROVE is always tier 2
        self.assertIn("type in the", items[1]["hint"])
        self.assertEqual(items[2]["tier"], 1)
        self.assertEqual(items[2]["options"], ["T-0006 delete", "T-0006 keep"])
        self.assertEqual(items[3]["tier"], 1)            # RUN: ok after the human ran it (after T-0162)
        from zouinify.panel.sources import classify_item
        for verb in ("APPLY", "DEPLOY", "PUSH"):
            self.assertEqual(classify_item({"verb": verb, "tickets": "T-0001", "command": "x", "expect": "y"})["tier"], 2, verb)
        for cmd in ("cd /x && terraform -chdir=terraform apply plan.tfplan", "docker push repo/img:tag", "aws ecr put-image --repository-name x",
                    "TICKET=T-0001 scripts/deploy_site.sh", "git push origin main", "scripts/ticket.sh apply x.tfplan --tickets T-0001",
                    "aws lambda update-function-code --function-name f", "scripts/tf.sh prod apply x.tfplan"):
            self.assertEqual(classify_item({"verb": "RUN", "tickets": "T-0001", "command": cmd, "expect": "y"})["tier"], 2, cmd)
        for cmd in ("terraform -chdir=x -auto-approve apply", "TF_CMD=apply; terraform $TF_CMD", "git -C /x push", "git -C /x --no-verify push origin dev",
                    "aws s3 rm s3://b/k", "aws s3 sync . s3://b/", "aws s3 cp f s3://b/f", "gh pr merge 3 --rebase", "make deploy", "make -C infra deploy-prod",
                    "docker --context prod push img", "kubectl -n x apply -f y.yaml", "helm upgrade rel chart", "aws lambda put-function-concurrency --function-name f"):
            self.assertEqual(classify_item({"verb": "RUN", "tickets": "T-0001", "command": cmd, "expect": "y"})["tier"], 2, cmd)
        # aws: every operation mutates unless read-only; global flags anywhere before the service; env prefixes and chains
        for cmd in ("aws --profile prod s3 rm s3://b/k", "aws --region us-east-1 lambda update-function-code --function-name f",
                    "aws dynamodb batch-write-item --request-items file://x", "aws lambda add-permission --function-name f --statement-id s",
                    "aws s3api copy-object --bucket b --key k --copy-source s", "aws sns publish --topic-arn t --message m",
                    "aws ec2 revoke-security-group-ingress --group-id g", "aws secretsmanager rotate-secret --secret-id s",
                    "AWS_PROFILE=prod aws s3 sync . s3://b/", "aws sso login && aws lambda update-function-code --function-name f",
                    "aws --no-cli-pager --output json iam put-role-policy --role-name r", "aws --profile x --region y --debug ecr put-image --repository-name r",
                    "gh workflow run deploy.yml", "gh secret set TOKEN", "aws s3"):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify_item({"verb": "RUN", "tickets": "T-0001", "command": cmd, "expect": "y"})["tier"], 2, cmd)
        for cmd in ("cd /x && ./fix.sh", "aws sso login", "aws lambda get-function --function-name f", "git status", "git -C /x log -3",
                    "terraform -chdir=terraform plan -out=x.tfplan", "aws s3 ls s3://b/", "make test", "kubectl get pods", "gh pr view 3",
                    "aws --profile default sts get-caller-identity", "aws --region us-east-2 lambda get-function --function-name f",
                    "aws --profile x s3 ls s3://b/", "aws sso login --profile default", "aws --profile x --output json stepfunctions describe-execution --execution-arn a",
                    "aws logs filter-log-events --log-group-name g", "aws s3api head-object --bucket b --key k", "aws configure list",
                    "aws --no-cli-pager ecr describe-images --repository-name r", "AWS_PROFILE=x aws --debug iam list-roles", "aws help",
                    "aws sso login && aws sts get-caller-identity"):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify_item({"verb": "RUN", "tickets": "T-0001", "command": cmd, "expect": "y"})["tier"], 1, cmd)
        self.assertEqual(s["kpis"]["inbox"], 4)          # the items of human --json

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

    def test_waiting_for_me_across_projects(self):
        """T-0175: every waiting session of the user, this project first; foreign rows carry a project label and never a path."""
        s = self.get_json("/api/snapshot")
        w = s["waiting_all"]
        names = [r["name"] for r in w["rows"]]
        self.assertEqual(names[0], "t0001-first-ready-ticket", "this project's rows come first")
        self.assertEqual(sorted(names), ["elsewhere-bf", "home-session", "lonely-xy", "nodialog-qq", "t0001-first-ready-ticket", "withheld-zz"])
        by = {r["name"]: r for r in w["rows"]}
        mine = by["t0001-first-ready-ticket"]
        self.assertTrue(mine["in_project"])
        self.assertEqual(mine["project"], self.cfg.name)
        self.assertEqual(mine["tmux"], "t0001-first-ready-ticket")
        self.assertEqual((mine["role"], mine["ticket"]), ("worker", "T-0001"), "this project's rows are the dashboard's own")
        self.assertEqual(mine["waiting_for"], "Do you want to proceed?", "the question above the options, not the footer")
        e = by["elsewhere-bf"]
        self.assertFalse(e["in_project"])
        self.assertEqual((e["project"], e["registered"]), ("proj-two", True), "a registered zouinify project is named")
        self.assertEqual(e["tmux"], "elsewhere-bf", "the window is found through the pane list, no registry entry needed")
        self.assertNotIn("attach", e, "the panel names no tmux command; the page shows the window name")
        self.assertEqual(e["waiting_for"], "Do you want to proceed?")
        lo = by["lonely-xy"]
        self.assertEqual((lo["project"], lo["registered"], lo["tmux"]), ("other3", False, None),
                         "no window: the directory name only, nothing to attach to")
        for r in w["rows"]:
            self.assertTrue(r["since"], r["name"])
            if not r["in_project"]:
                self.assertNotIn("cwd", r)
                self.assertNotIn(self.tmp, json.dumps(r), "a foreign row never carries a path")
        self.assertEqual(w["elsewhere"], 5)
        self.assertEqual(s["kpis"]["waiting_elsewhere"], 5)
        self.assertEqual((by["home-session"]["project"], by["home-session"]["registered"]), ("~", False),
                         "a session started in the home directory is labelled ~, never the login")
        self.assertEqual(by["withheld-zz"]["waiting_for"], "waiting (screen withheld)")
        self.assertIn("identifier-shaped", by["withheld-zz"]["withheld"])
        self.assertEqual(by["nodialog-qq"]["waiting_for"], "waiting (screen withheld)")
        self.assertIn("no prompt or dialog", by["nodialog-qq"]["withheld"])
        self.assertEqual(e["withheld"], "")
        self.assertEqual(s["kpis"]["waiting"], 1, "the project KPI ignores foreign sessions")
        self.assertEqual([r["name"] for r in s["waiting"]], ["t0001-first-ready-ticket"], "the dashboard panel keeps this project's rows")
        self.assertNotIn("elsewhere-bf", {r["name"] for r in s["sessions"]["rows"]}, "the sessions table stays per project")
        d = self.get_json("/api/waiting/elsewhere-bf")
        scr = d["screen"]
        self.assertEqual(d["withheld"], "")
        self.assertEqual(scr, "Bash(git push origin dev)\nDo you want to proceed?\n❯ 1. Yes\n2. Yes, and don't ask again\n"
                              "3. No, and tell Claude what to do differently (esc)",
                         "another project's drawer is an allowlist: the question block and the option lines, nothing else")
        for leak in ("[Fable 5.1]", "x | dev", "aws_", FAKE_SESSION_TOKEN, "Bearer", "<redacted", "mysql", "Cookie", "sid=abc",
                     "alice@", "keeper@", "/groups", "node7", "10.1.2.3", "S3cret", "fine line"):
            self.assertNotIn(leak, scr, f"{leak!r} must not reach another project's drawer, not even redacted")
        self.assertNotIn(self.tmp, json.dumps(d))
        self.assertNotIn("cwd", d)
        wd = self.get_json("/api/waiting/withheld-zz")
        self.assertEqual(wd["screen"], "")
        self.assertIn("identifier-shaped", wd["withheld"])
        nd = self.get_json("/api/waiting/nodialog-qq")
        self.assertEqual((nd["screen"], nd["withheld"]), ("", "no prompt or dialog on the visible screen"))
        md = self.get_json("/api/waiting/t0001-first-ready-ticket")
        self.assertIn("[Fable 5.1]", md["screen"], "this project's own sessions keep showing the whole redacted screen")
        self.assertEqual(md["withheld"], "")
        st, _, _ = self.req("GET", "/api/waiting/tp-desk")
        self.assertEqual(st, 404, "a session that is not waiting has no waiting detail")
        st, _, _ = self.req("GET", "/api/waiting/no-such")
        self.assertEqual(st, 404)
        st, _, _ = self.req("POST", "/api/waiting/elsewhere-bf", body=b"{}")
        self.assertEqual(st, 405, "still no write path")
        _, _, js = self.req("GET", "/static/app.js")
        self.assertIn(b"#/waiting", js)
        self.assertIn(b"Waiting for me", js)
        self.assertIn(b"copy window name", js)
        self.assertNotIn(b"send-keys", js)

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

    def test_head_returns_headers_only(self):
        st, hdr, body = self.req("HEAD", "/api/snapshot")
        self.assertEqual((st, body), (200, b""))
        self.assertGreater(int(hdr["Content-Length"]), 2, "the length of the JSON a GET would send")
        st, _, body = self.req("HEAD", "/api/tickets/T-9999")
        self.assertEqual((st, body), (404, b""))

    def test_non_utf8_capture_does_not_hide_the_sessions(self):
        with open(os.path.join(self.state, "screen-tp-desk.txt"), "wb") as fh:
            fh.write(b"> caf\xe9 \xff\xfe broken bytes\n   [Fable 5.1] x | dev\n")
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertIsNone(s["sessions"]["error"])
        rows = {r["name"]: r for r in s["sessions"]["rows"]}
        self.assertEqual(rows["tp-desk"]["model"], "Fable 5.1")
        self.assertIn("broken bytes", rows["tp-desk"]["last_line"])

    def test_ticket_detail_and_live_checks(self):
        d = self.get_json("/api/tickets/T-0010")
        self.assertEqual(d["front_matter"]["status"], "deployed")
        self.assertEqual(d["log"][0]["text"].split(": ", 1)[1][:9], "deployed:")
        self.assertEqual(d["handoff"], "", "the fixture ticket has no handoff section")
        with open(os.path.join(self.root, "tickets", "T-0010-deployed-one.md"), "a", encoding="utf-8") as fh:
            fh.write("\n## Deploy handoff\nDeploy: none (no infrastructure change). Verify: the page renders.\n")
        d = self.get_json("/api/tickets/T-0010")
        self.assertIn("deploy: none", d["handoff"].lower(), "the handoff block is read from the ticket file")
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
        self.assertNotIn("available after receipts", js)
        self.assertIn("/api/receipts", js)
        self.assertNotIn("fetch(", js.replace("fetch(api", "").replace("fetch(`/api", "").replace("fetch('/api", "").replace('fetch("/api', ""),
                         "the page only ever fetches its own /api")
        for bad in ("/static/../server.py", "/static/..%2F..%2Fcli.py", "/nope"):
            st, _, _ = self.req("GET", bad)
            self.assertEqual(st, 404, bad)

    def test_refresh_button_fetches_fresh_at_most_once_per_5s_per_client(self):
        """T-0197: the button sends ?fresh=1 (a plain load does not); a fresh snapshot re-runs ticket.sh, so a second fresh
        within 5 s from the same client gets the cached answer with its age and runs nothing."""
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertRegex(js, r"data-refresh\]'\)\) \{[^\n]*load\(\{ fresh: true \}\)", "the ↻ click asks for fresh data")
        self.assertIn("fetch('/api/snapshot?fresh=1'", js)
        self.assertNotIn("setInterval(load,", js, "the periodic timer stays on the cached path")
        self.assertIn("board ${esc(boardAge())}", js, "the header shows the board source's age")
        self.get_json("/api/snapshot")                                   # warm the sources
        before = len([c for c in self.ticket_calls() if c.startswith("human --json") or c.startswith("board")])
        a = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual(a["fresh"], {"requested": True, "served": True, "wait_s": 0.0, "min_interval_s": 5.0})
        after_first = len([c for c in self.ticket_calls() if c.startswith("human --json") or c.startswith("board")])
        self.assertGreater(after_first, before, "a served fresh re-ran ticket.sh")
        b = self.get_json("/api/snapshot?fresh=1")
        self.assertFalse(b["fresh"]["served"])
        self.assertGreater(b["fresh"]["wait_s"], 0)
        self.assertLessEqual(b["fresh"]["wait_s"], 5.0)
        self.assertEqual(len([c for c in self.ticket_calls() if c.startswith("human --json") or c.startswith("board")]), after_first,
                         "the second fresh within 5 s ran nothing")
        self.assertGreaterEqual(b["sources"]["board"]["age_s"], 0)
        self.assertNotIn("fresh", self.get_json("/api/snapshot"), "a plain load carries no fresh block")
        self.srv.fresh_at.clear()
        c = self.get_json("/api/snapshot?fresh=1")
        self.assertTrue(c["fresh"]["served"], "after the interval a fresh is served again")

    def test_route_change_scrolls_to_top_and_a_drawer_keeps_the_scroll(self):
        """T-0199: the page's own routing decision, run in node from the function's source (no DOM needed): a new page starts
        at the top; a drawer open/close or a filter change on the same page keeps the list where it was."""
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed here (CI has it)")
        js = self.req("GET", "/static/app.js")[2].decode()
        m = re.search(r"function routeChanged\(prevHash, nextHash\) \{.*?\n  \}\n", js, re.S)
        self.assertTrue(m, "routeChanged() is the page's decision")
        self.assertRegex(js, r"if \(routeChanged\(prev, location\.hash\)\) window\.scrollTo\(0, 0\);", "the hashchange handler scrolls on a page change only")
        cases = [("#/dashboard", "#/sessions", True), ("", "#/tickets", True), ("#/tickets", "#/dashboard", True),
                 ("#/tickets", "#/tickets/T-0001", False), ("#/tickets/T-0001", "#/tickets", False),
                 ("#/tickets?state=ready", "#/tickets?state=done", False), ("#/sessions/x", "#/sessions", False),
                 ("#/waiting/x", "#/sessions/x", True)]
        script = m.group(0) + "const cases = " + json.dumps([[a, b] for a, b, _ in cases]) + ";\nconsole.log(JSON.stringify(cases.map(([a, b]) => routeChanged(a, b))));"
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [c for _, _, c in cases])

    def test_lanes_scroll_inside_and_content_stays_in_its_box(self):
        """T-0199: the board is no longer as tall as its longest lane; a sha list, a receipt badge and the sessions table stay
        inside their boxes."""
        css = self.req("GET", "/static/app.css")[2].decode()
        lane = re.search(r"^\.lane \.cards \{([^}]*)\}", css, re.M).group(1)
        self.assertIn("max-height:", lane)
        self.assertIn("overflow-y: auto", lane)
        self.assertRegex(css, r"\.tcard \.actions \{[^}]*flex-wrap: wrap")
        self.assertRegex(css, r"\.badge\.sent \{[^}]*overflow-wrap: anywhere")
        self.assertRegex(css, r"\.scroll-x \{ overflow-x: auto; \}")
        self.assertRegex(css, r"\.sessions-table\.has-actions \.tbl th\.actions, \.sessions-table\.has-actions \.tbl td\.actions \{[^}]*position: sticky; right: 0")
        js = self.req("GET", "/static/app.js")[2].decode()
        self.assertIn('class="mono closed-by truncate" title="${esc(shas)}"', js, "the full sha list is the title; one line on the card")
        self.assertIn('<div class="body scroll-x"><table class="tbl">', js)
        self.assertIn("<th class=\"actions\">actions</th>", js)

    def test_sources_carry_ages_and_settings(self):
        s = self.get_json("/api/snapshot")
        for name in ("inbox", "board", "sessions", "creds", "logs", "releases"):
            self.assertIn(name, s["sources"], name)
            self.assertIn("age_s", s["sources"][name])
            self.assertIn("ttl_s", s["sources"][name])
        self.assertNotIn("sitting", s)
        self.assertEqual(s["config"]["project"]["name"], "testproj")
        self.assertNotIn("aws_secret", json.dumps(s["config"]))
        self.assertIn("installed", s["hook"])
        self.assertEqual(s["panel"]["bind"], "loopback")
        self.assertEqual(s["header"]["desk"]["name"], "tp-desk")


class TestPinnedState(World):
    """The panel's two files live at <root>/.ops/state, fixed in code, for the real Config only, never through a symlink."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        from zouinify import panelstate
        self.panelstate = panelstate
        self.cfg = cfgmod.load(self.root)

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()

    def test_state_dir_is_not_configurable(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        for bad in ("~/.claude", "/tmp/elsewhere", "../outside", ".ops/other"):
            self.write(".ops/config.yaml", cfg.replace("state_dir: .ops/state", f"state_dir: {bad}"))
            r = self.nz("config", "--check")
            self.assertIn("fixed at .ops/state", r.stderr, bad)
        self.write(".ops/config.yaml", cfg)
        self.assertEqual(self.cfg.state_dir, os.path.join(self.root, ".ops", "state"))
        self.assertEqual(self.panelstate.server_log_path(self.cfg), os.path.join(self.root, ".ops", "state", "server.log"))

    def test_only_the_real_config_is_accepted(self):
        import types
        fake = types.SimpleNamespace(root=self.home, panel={"port": 1}, name="x", state_dir=self.home)
        with self.assertRaises(TypeError):
            self.panelstate.state_dir(fake)
        self.panelstate.log(fake, "x")        # never raises, never writes
        self.assertFalse(os.path.exists(os.path.join(self.home, ".ops")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "server.log")))

    def test_symlinks_are_never_followed(self):
        state = os.path.join(self.root, ".ops", "state")
        os.makedirs(state)
        target = os.path.join(self.tmp, "victim.log")
        with open(target, "w") as fh:
            fh.write("untouched\n")
        os.symlink(target, os.path.join(state, "server.log"))
        self.panelstate.log(self.cfg, "hello")
        with open(target) as fh:
            self.assertEqual(fh.read(), "untouched\n", "O_NOFOLLOW: the planted symlink target is not written")
        os.remove(os.path.join(state, "server.log"))
        os.rmdir(state)
        os.symlink(self.tmp, state)           # the whole state dir as a symlink
        with self.assertRaises(OSError):
            self.panelstate.state_dir(self.cfg)
        self.panelstate.log(self.cfg, "hello")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "server.log")))
        os.remove(state)
        self.panelstate.log(self.cfg, "line\x01with\rcontrol\nchars")
        with open(os.path.join(state, "server.log")) as fh:
            text = fh.read()
        self.assertIn("line\\x01with\\x0dcontrol\\x0achars", text)
        self.assertEqual(text.count("\n"), 1, "a log line can never inject a second line")


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
        self.assertEqual(len(data["items"]), len(HUMAN_JSON))
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
        # the desk's review cases (T-0160)
        self.assertEqual(r('"aws_secret_access_key": "abc"'), '"aws_secret_access_key": <redacted>')
        self.assertEqual(r('"SecretAccessKey": "abc", "SessionToken": "def", "AccessKeyId": "ghi"'),
                         '"SecretAccessKey": <redacted>, "SessionToken": <redacted>, "AccessKeyId": <redacted>')
        self.assertEqual(r("key " + "ASIA" + "QRSTUVWXYZ123456"), "key <redacted>")
        self.assertEqual(r("secret_access_key = x"), "secret_access_key = <redacted>")
        self.assertEqual(r("PASSCODE abc"), "PASSCODE <redacted>")
        self.assertEqual(r("--password hunter2 --rest"), "--password <redacted> --rest")
        self.assertEqual(r("--token=abc"), "--token=<redacted>")
        self.assertEqual(r('password="x y" z'), 'password=<redacted> z')
        secret40 = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCY" + "EXAMPLEKEY12"
        self.assertEqual(len(secret40), 40)
        self.assertEqual(r("sec " + secret40 + " end"), "sec <redacted> end")
        sha = "a" * 20 + "0123456789" + "b" * 10
        self.assertEqual(r("commit " + sha), "commit " + sha, "a 40-hex git sha is evidence, not a secret")
        self.assertEqual(r("blob " + ("Q" * 60 + "q" * 25) + "=="), "blob <redacted>")
        # the desk's third round (T-0160)
        self.assertEqual(r("Authorization: Basic dXNlcjpwYXNz"), "Authorization: Basic <redacted>")
        self.assertEqual(r("Authorization: Digest username=x, response=abc"), "Authorization: Digest <redacted>")
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nABCD\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(r("key:\n" + pem + "\nafter"), "key:\n<redacted private key>\nafter")
        self.assertEqual(r("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\n(cut by the screen)"), "<redacted private key>")
        self.assertEqual(r("pat " + "github_pat_" + "11ABCDEFG0123456789abcdefghij"), "pat <redacted>")
        self.assertEqual(r("slack " + "xoxb-" + "1234567890-abcdefghij"), "slack <redacted>")
        self.assertEqual(r("PGPASSWORD=hunter2 psql"), "PGPASSWORD=<redacted> psql")
        self.assertEqual(r("MY_API_TOKEN: abc"), "MY_API_TOKEN: <redacted>")
        self.assertEqual(r("db postgres://user:pw@host/db"), "db postgres://user:<redacted>@host/db")
        self.assertEqual(r("https://alice:s3cret@example.test/x"), "https://alice:<redacted>@example.test/x")
        self.assertEqual(r("curl -u alice:s3cret https://h/"), "curl -u alice:<redacted> https://h/")
        self.assertEqual(r("curl --user=alice:s3cret https://h/"), "curl --user=alice:<redacted> https://h/")
        self.assertEqual(r("https://example.test/path stays"), "https://example.test/path stays")
        # the desk's fourth round (T-0160)
        self.assertEqual(r("\x1b[32maws_secret_access_key\x1b[0m = \x1b[1mabc\x1b[0m done"), "aws_secret_access_key = <redacted> done")
        self.assertEqual(r("\x1b]0;title\x07plain \x1b[2K\x1b[1;31mred\x1b[0m"), "plain red")
        self.assertEqual(r("tok " + "hf_" + "ABCDEFGHIJKLMNOPQRSTUVWX"), "tok <redacted>")
        self.assertEqual(r("Password for 'https://alice@github.test': hunter2"), "Password for 'https://alice@github.test': <redacted>")
        self.assertEqual(r("Password for 'https://alice@github.test':\nhunter2typed\nnext"), "Password for 'https://alice@github.test': <redacted>\n<redacted>\nnext")
        wrapped = "aws_session_token = FQoGZXIvYXdzEBYaDHNvbWV0aGluZ2hl\ncmUiLGZha2V0b2tlbg==\n$ ls"
        self.assertEqual(r(wrapped), "aws_session_token = <redacted>\n<redacted>\n$ ls")
        self.assertEqual(r("x = <redacted>\nshort\n"), "x = <redacted>\nshort\n", "a short next line is not a continuation")
        # T-0175 merge conditions (manager): glued -p, cookies
        self.assertEqual(r("mysql -pS3cret db"), "mysql -p<redacted> db")
        self.assertEqual(r("mkdir -pv dir && ls -p"), "mkdir -pv dir && ls -p", "combined short flags and a bare -p are not passwords")
        self.assertEqual(r("mysql -P3306 -u root -pS3cret!"), "mysql -P3306 -u root -p<redacted>")
        self.assertEqual(r("mkdir -p/tmp/x"), "mkdir -p/tmp/x", "a glued path is left alone")
        self.assertEqual(r("Cookie: sid=abc; zouinify_token=x"), "Cookie: sid=<redacted>; zouinify_token=<redacted>")
        self.assertEqual(r("Set-Cookie: sid=abc; Path=/; Max-Age=3600; SameSite=Strict; HttpOnly"),
                         "Set-Cookie: sid=<redacted>; Path=/; Max-Age=3600; SameSite=Strict; HttpOnly", "names and attributes stay, values go")
        self.assertEqual(r("curl --cookie 'a=b' h"), "curl --cookie <redacted> h")
        self.assertEqual(r("cookies are fine words"), "cookies are fine words")
        self.assertEqual(r("sshpass -p S3cret ssh h; mkdir -p dir; mysql -p hunter2"), "sshpass -p <redacted> ssh h; mkdir -p dir; mysql -p <redacted>",
                         "a space-separated -p is a password for tools whose -p means one, a directory for mkdir")
        self.assertEqual(r("psql -p 5432 -h db"), "psql -p 5432 -h db", "an all-digit -p value is a port, not a password")
        self.assertEqual(r("mysql -P3306 -p5432 -pS3cret"), "mysql -P3306 -p5432 -p<redacted>", "a glued all-digit value is a port too")
        self.assertEqual(r("docker run -e PW=hunter2 -e PIN=1234 img"), "docker run -e PW=<redacted> -e PIN=<redacted> img", "T-0179: PW and PIN env values")
        self.assertEqual(r("pass=x pwd=y PASS=z tests pass: 3 credentials: abc"), "pass=<redacted> pwd=<redacted> PASS=<redacted> tests pass: 3 credentials: <redacted>")


class TestForeignDialog(unittest.TestCase):
    """T-0175 re-review: another project's dialog is an ALLOWLIST. Every probe of the security review is withheld or clean."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")

    def test_clean_dialog_shows_question_and_options_only(self):
        text, why = panel_sources.foreign_dialog(PROMPT_SCREEN + SECRET_SCREEN)
        self.assertEqual(why, "")
        self.assertEqual(text, "Bash(git push origin dev)\nDo you want to proceed?\n❯ 1. Yes\n2. Yes, and don't ask again\n3. No, and tell Claude what to do differently (esc)")
        ask = "│ T-0140 is live. Want me to fix deploy.sh and push the file now?\n❯ 1. Yes, fix and push now\n     Add tertiary_spans to push_data()'s list, upload the file, invalidate the path.\n  2. Just log it, not now\n  3. Type something.\n──────────\n  4. Chat about this\nEnter to select · ↑/↓ to navigate · Esc to cancel\n"
        text, why = panel_sources.foreign_dialog(ask)
        self.assertEqual(why, "")
        self.assertTrue(text.startswith("T-0140 is live. Want me to fix deploy.sh and push the file now?\n❯ 1. Yes, fix and push now\n"), text)
        self.assertNotIn("Chat about this", text, "lines after a separator are not part of the option block")
        for ok in ("edit deploy.sh and config.py?", "on 2026-10-13 at 09:30:15?", "close T-0140 and t0119?", "a misconfiguration internationalization?",
                   "serve on port -p5432?", "Bash(git push origin dev)", "Bash(git commit -a -m fix)", "Bash(sort -u names)",
                   "pin the version and send a pw later?"):
            self.assertEqual(panel_sources.foreign_dialog(ok + "\n ❯ 1. Yes\n   2. No\n")[1], "", ok)

    def test_every_probe_is_withheld(self):
        fd = panel_sources.foreign_dialog
        probes = [
            ("Do you want to proceed?\n ❯ 1. AKIAIOSF\n   2. No\nODNN7EXAMPLE\n", "identifier-shaped", "a secret split across lines"),
            ("key " + "0123456789abcdef" * 4 + "?\n ❯ 1. Yes\n   2. No\n", "identifier-shaped", "a bare 64-hex key"),
            ("copy host:/home/a/b now?\n ❯ 1. Yes\n   2. No\n", "path", "a path after a colon"),
            ("open file:/etc/shadow?\n ❯ 1. Yes\n   2. No\n", "path", "a file: path"),
            ("read home/alice/.aws/credentials?\n ❯ 1. Yes\n   2. No\n", "path", "a relative path"),
            ("read ./alice/.ssh/id_rsa?\n ❯ 1. Yes\n   2. No\n", "path", "a ./ path"),
            ("read /tmp/my file/x?\n ❯ 1. Yes\n   2. No\n", "path", "a path with a space"),
            ("read /donn\u00e9es/\u00e9t\u00e9?\n ❯ 1. Yes\n   2. No\n", "path", "a non-ASCII path"),
            ("read C:\\Users\\a?\n ❯ 1. Yes\n   2. No\n", "path", "a backslash path"),
            ("read ~ now?\n ❯ 1. Yes\n   2. No\n", "path", "the home marker"),
            ("run sshpass -p S3cret ssh h?\n ❯ 1. Yes\n   2. No\n", "credential", "a space-separated password"),
            ("run mysql -p hunter2?\n ❯ 1. Yes\n   2. No\n", "credential", "mysql -p value"),
            ("set pass=x?\n ❯ 1. Yes\n   2. No\n", "credential", "pass="),
            ("set pwd=x?\n ❯ 1. Yes\n   2. No\n", "credential", "pwd="),
            ("set PASS=x?\n ❯ 1. Yes\n   2. No\n", "credential", "PASS="),
            ("credentials: abc\n ❯ 1. Yes\n   2. No\n", "credential", "credentials:"),
            ("permission denied: /etc/shadow?\n ❯ 1. Yes\n   2. No\n", "path", "a question with a path above real options"),
            ("mail alice\uff20example.org?\n ❯ 1. Yes\n   2. No\n", "path, address", "a fullwidth @"),
            ("pass\u200bword=x?\n ❯ 1. Yes\n   2. No\n", "credential", "a zero-width character inside a label"),
            ("Cookie: abc123\n ❯ 1. Yes\n   2. No\n", "cookie", "a cookie value without ="),
            ("Set-Cookie: Path=x\n ❯ 1. Yes\n   2. No\n", "cookie", "a cookie under an attribute name"),
            ("send the cookie?\n ❯ 1. Yes\n   2. No\n", "cookie", "the word cookie"),
            ("deploy to lab.example.org?\n ❯ 1. Yes\n   2. No\n", "host", "a two-label host"),
            ("account 123456789012?\n ❯ 1. Yes\n   2. No\n", "long number", "a 12-digit account id"),
            ("ask t0070-extract-shared-cognito-auth-edge-g-c8?\n ❯ 1. Yes\n   2. No\n", "token-shaped", "a long dashed name"),
            ("x" * 401 + "\n ❯ 1. Yes\n   2. No\n", "longer than 400", "an over-long line"),
            ("just text\n> \n   [Fable 5.1] x | dev\n", "no prompt or dialog", "no dialog at all"),
            # the final review's denylist: short passwords next to flags or words the redactor does not know
            ("Bash(docker login -u bob -p hunter2)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "docker login -p"),
            ("Bash(echo 'S3cr3t!' | sudo -S apt update)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "sudo -S"),
            ("Bash(gpg --passphrase hunter2 f)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "gpg --passphrase"),
            ("passphrase: hunter2\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "passphrase:"),
            ("Bash(export DB_PASS=hunter2)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "DB_PASS="),
            ("Bash(zip -P hunter2 a.zip f)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "zip -P"),
            ("Bash(unzip -P hunter2 a.zip)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "unzip -P"),
            ("Bash(openssl enc -k hunter2 -in f)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "openssl -k"),
            ("Bash(htpasswd -b f bob hunter2)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "htpasswd -b"),
            ("the password is hunter2\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "password is"),
            ("token swordfish\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "token word"),
            ("secret swordfish\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "secret word"),
            ("rotate the api key?\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "the word key"),
            ("auth header?\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "auth"),
            ("use mysql -W now?\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "-W flag"),
            ("ping 10.1.2.3?\n ❯ 1. Yes\n   2. No\n", "network address", "an IPv4 address"),
            ("ping fe80::1?\n ❯ 1. Yes\n   2. No\n", "network address", "an IPv6 address"),
            ("ping ::1?\n ❯ 1. Yes\n   2. No\n", "network address", "the IPv6 loopback"),
            # T-0179: the remaining short-password forms
            ("Bash(ssh-keygen -N hunter2 -f k)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "ssh-keygen -N"),
            ("Bash(redis-cli -a hunter2 ping)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "redis-cli -a"),
            ("Bash(lftp -u bob,hunter2 host)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "lftp -u user,pass"),
            ("Bash(smbclient -U bob%hunter2 share)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "smbclient -U user%pass"),
            ("Bash(curl -u bob:hunter2 h)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "curl -u user:pass"),
            ("PIN 1234\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "PIN"),
            ("Bash(docker run -e PW=hunter2 img)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "-e PW="),
            ("Bash(sudo --stdin apt update)\n ❯ 1. Yes\n   2. No\n", "credential, login or flag word", "sudo --stdin"),
        ]
        for screen, reason, label in probes:
            text, why = fd(screen)
            self.assertEqual(text, "", label)
            self.assertIn(reason, why, label)

    def test_fake_prompt_lines_do_not_widen_the_dialog(self):
        fd = panel_sources.foreign_dialog
        text, why = fd("Do you want to proceed?\nsecret=abc\n\n\nreal question?\n ❯ 1. Yes\n   2. No\n")
        self.assertEqual((text, why), ("real question?\n❯ 1. Yes\n2. No", ""), "only the block adjacent to the options is shown")
        self.assertEqual(fd("ls: permission denied\n"), ("", "no prompt or dialog on the visible screen"),
                         "T-0177: a bare prompt-like line is not a prompt; without a real dialog nothing is shown")
        self.assertEqual(fd("Do you want to proceed?\nsecret=abc\n"), ("", "no prompt or dialog on the visible screen"))
        self.assertEqual(fd("│ Do you want to proceed? │\nsecret=abc\n")[0], "Do you want to proceed?", "a boxed one is, and shows alone")
        long = "is this the question you wanted answered " * 5          # 205 clean characters, as on a wide pane
        text, why = fd(long.strip() + "?\n ❯ 1. Yes\n   2. No\n")
        self.assertEqual(why, "")
        self.assertEqual(text.split("\n")[0], long[:159] + "\u2026", "the content rules see the whole line; the drawer shows 160 characters")


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

    def test_intranet_bind_needs_the_token(self):
        self.set_panel(f"port: {self.port}, autostart: false, bind: intranet")
        r = self.nz("panel", "start")
        self.assertIn("panel token rotate", r.stderr, "T-0165: intranet starts only with a browser token")
        self.assertNotIn("#t=", r.stderr)
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
            r = self.nz("panel", "start")
            self.assertIn("another project", r.stderr)
            self.assertIn(other, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")))
            r = self.nz("panel", "stop")
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
            r = self.nz("panel", "start")
            self.assertIn("foreign", r.stderr.lower())
        finally:
            s.close()

    def test_stop_never_kills_a_reused_pid(self):
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            os.makedirs(os.path.join(self.root, ".ops", "state"), exist_ok=True)
            with open(os.path.join(self.root, ".ops", "state", "panel.json"), "w") as fh:
                json.dump({"pid": sleeper.pid, "port": self.port, "root": self.root}, fh)
            r = self.ok("panel", "stop")
            self.assertIn("another process", r.stdout)
            self.assertIsNone(sleeper.poll(), "the unrelated process must survive")
            self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")))
        finally:
            sleeper.kill()
            sleeper.wait()

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
