"""T-0163: the hook listener. A second loopback-only server inside the panel process takes `POST /api/hook` from the
Claude Code hook script; events are data the sessions rows show (last reply, waiting since exactly when). Desk
conditions: an event whose cwd is outside the project keeps no text anywhere; the script exits 0 fast; forged,
oversized and junk payloads are refused; the only file write is events.jsonl through panelstate."""
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
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
    from zouinify.panel import server as panel_server
except Exception as e:  # noqa: BLE001
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e

HOOK_SCRIPT = os.path.join(REPO, "templates", "hooks", "zouinify-hook.sh")


def payload(event: str, cwd: str, session_id: str, **extra) -> dict:
    d = {"hook_event_name": event, "session_id": session_id, "cwd": cwd, "transcript_path": "/x/y.jsonl", "permission_mode": "auto"}
    d.update(extra)
    return d


class HookWorld(PanelWorld):
    def setUp(self):
        super().setUp()
        self.hook_port = self.srv.hooks.server_address[1]
        self.srv.serve_hooks()
        self.fake_remote = False
        test = self
        self.peer_patch = mock.patch.object(panel_hooks.HookHandler, "peer_is_loopback", lambda h: not test.fake_remote)
        self.peer_patch.start()
        # what run() does for a real server: the pid file with the listener's port and its per-start nonce (T-0182)
        self.nonce = panelstate.new_hook_nonce()
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], os.getpid(), self.srv.started_at,
                                    hook_port=self.hook_port, hook_nonce=self.nonce)

    def tearDown(self):
        self.peer_patch.stop()
        self.srv.close_hooks()
        super().tearDown()

    def post_hook(self, body, raw: bytes | None = None, length: int | None = None, headers: dict | None = None,
                  ctype: str = "application/json", nonce=True, host=None, path: str = "/api/hook"):
        """A POST the way the hook script sends it (Host, JSON content type, the nonce header); each piece can be
        overridden to probe the listener's checks."""
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=30)
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.hook_port}")
        if ctype:
            conn.putheader("Content-Type", ctype)
        if nonce is True:
            conn.putheader(panel_hooks.HOOK_HEADER, self.nonce)
        elif nonce:
            conn.putheader(panel_hooks.HOOK_HEADER, str(nonce))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(data) if length is None else length))
        conn.endheaders()
        conn.send(data)
        r = conn.getresponse()
        out = (r.status, r.read())
        conn.close()
        return out

    def events_file(self) -> list:
        p = os.path.join(self.root, ".ops", "state", "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def server_log(self) -> str:
        p = os.path.join(self.root, ".ops", "state", "server.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


class TestHookListener(HookWorld):
    def test_stop_event_becomes_the_last_reply_redacted(self):
        st, body = self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="Deployed T-0145: serial 3 -> 4; password=hunter2 was in the log",
                                          stop_hook_active=False, background_tasks=[{"command": "tail -f secret.log"}]))
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["kept_text"])
        s = self.get_json("/api/snapshot?fresh=1")
        row = {r["name"]: r for r in s["sessions"]["rows"]}["tp-desk"]
        self.assertEqual(row["last_event"], "Stop")
        self.assertIn("Deployed T-0145", row["last_reply"])
        self.assertIn("password=<redacted>", row["last_reply"])
        self.assertNotIn("hunter2", json.dumps(s))
        self.assertNotIn("secret.log", json.dumps(s), "background_tasks and other fields are dropped at the door")
        self.assertEqual(row["state"], "working", "the desk is busy per claude agents: an event never promotes a busy session")
        ev = self.events_file()[-1]
        self.assertEqual((ev["event"], ev["session_id"], ev["project"]), ("Stop", "sid-desk", True))
        self.assertIn("password=<redacted>", ev["text"])
        self.assertNotIn("transcript_path", ev)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, ".ops", "state", "events.jsonl")).st_mode), 0o600)
        self.assertIn("HOOK Stop session=sid-desk project=yes", self.server_log())
        self.assertNotIn("Deployed", self.server_log(), "the log never carries the text")

    def test_permission_prompt_marks_waiting_exactly_and_stop_clears_it(self):
        self.post_hook(payload("Notification", self.root, "sid-debug", notification_type="permission_prompt",
                               message="Claude needs your permission to use Bash", title="Permission needed"))
        s = self.get_json("/api/snapshot?fresh=1")
        row = {r["name"]: r for r in s["sessions"]["rows"]}["tp-debug"]
        self.assertEqual(row["state"], "waiting", "idle per claude agents, but the hook says a prompt is up")
        self.assertEqual(row["waiting_for"], "Claude needs your permission to use Bash")
        self.assertEqual(row["last_event"], "Notification(permission_prompt)")
        since = row["since"]
        ev_at = self.events_file()[-1]["at"]
        self.assertEqual(since, ev_at, "since is the event's time, not the first refresh that noticed")
        self.assertIn("tp-debug", [w["name"] for w in s["waiting"]])
        self.assertIn("tp-debug", [w["name"] for w in s["waiting_all"]["rows"]])
        time.sleep(0.05)
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual({r["name"]: r for r in s["sessions"]["rows"]}["tp-debug"]["since"], since, "stable across refreshes")
        self.post_hook(payload("Stop", self.root, "sid-debug", last_assistant_message="Done."))
        s = self.get_json("/api/snapshot?fresh=1")
        row = {r["name"]: r for r in s["sessions"]["rows"]}["tp-debug"]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["waiting_for"], "")
        self.assertEqual(row["last_reply"], "Done.")
        self.post_hook(payload("Notification", self.root, "sid-debug", notification_type="idle_prompt", message="Claude is waiting for your input"))
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual({r["name"]: r for r in s["sessions"]["rows"]}["tp-debug"]["state"], "waiting")
        self.post_hook(payload("UserPromptSubmit", self.root, "sid-debug", prompt="go on"))
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual({r["name"]: r for r in s["sessions"]["rows"]}["tp-debug"]["state"], "idle")
        self.assertNotIn("go on", json.dumps(s), "a prompt's text is never kept")

    def test_agents_waiting_status_still_wins_and_busy_is_never_promoted(self):
        self.post_hook(payload("Stop", self.root, "sid-worker", last_assistant_message="finished"))
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual({r["name"]: r for r in s["sessions"]["rows"]}["t0001-first-ready-ticket"]["state"], "waiting",
                         "claude agents says waiting: the hook's Stop does not override it")
        self.post_hook(payload("Notification", self.root, "sid-desk", notification_type="permission_prompt", message="x?"))
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertEqual({r["name"]: r for r in s["sessions"]["rows"]}["tp-desk"]["state"], "working")

    def test_foreign_events_keep_no_text_anywhere(self):
        text = "the secret plan of another project"
        st, body = self.post_hook(payload("Stop", self.tmp, "sid-other", last_assistant_message=text))
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(body)["kept_text"])
        self.post_hook(payload("Notification", os.path.join(self.tmp, "other3"), "sid-lonely", notification_type="permission_prompt", message=text))
        for ev in self.events_file():
            self.assertFalse(ev["project"])
            self.assertEqual(ev["text"], "", "a foreign event carries no text in events.jsonl")
        self.assertNotIn(text, self.server_log())
        self.assertNotIn(text, json.dumps(self.srv.events.recent()))
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertNotIn(text, json.dumps(s))
        lonely = next(r for r in s["waiting_all"]["rows"] if r["name"] == "lonely-xy")
        self.assertEqual(lonely["since"], self.events_file()[-1]["at"], "a foreign prompt event gives the exact since, nothing else")
        self.assertNotIn(text, json.dumps(lonely))
        for ev in self.srv.events.recent():
            self.assertNotIn("text", panel_hooks.wire_event(ev), "the SSE wire form of a foreign event has no text field at all")

    def test_forged_oversized_and_junk_payloads_are_refused(self):
        self.fake_remote = True
        st, body = self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="forged"))
        self.assertEqual(st, 403)
        self.fake_remote = False
        self.assertIn("AUDIT hook 403", self.server_log())
        st, _ = self.post_hook(None, raw=b"{" + b" " * (panel_hooks.MAX_BODY + 10) + b"}")
        self.assertEqual(st, 413)
        st, _ = self.post_hook(None, raw=b"not json at all")
        self.assertEqual(st, 400)
        st, _ = self.post_hook(payload("PreToolUse", self.root, "sid-desk", tool_name="Bash"))
        self.assertEqual(st, 400, "only the five kept events")
        st, _ = self.post_hook(["a", "list"])
        self.assertEqual(st, 400)
        st, _ = self.post_hook(None, raw=b"")
        self.assertEqual(st, 413, "an empty body is refused too")
        self.assertEqual(self.events_file(), [], "nothing refused was stored")
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=30)
        conn.request("GET", "/api/hook")
        r = conn.getresponse(); r.read(); conn.close()
        self.assertEqual(r.status, 405)
        st, _ = self.post_hook(None, raw=b"{}", path="/api/other")
        self.assertEqual(st, 404)
        st, _, _ = self.req("POST", "/api/hook", body=json.dumps(payload("Stop", self.root, "sid-desk", last_assistant_message="x")).encode())
        self.assertEqual(st, 405, "the main (LAN-facing) server still refuses every POST: hooks come in on the loopback port only")
        self.assertEqual(self.events_file(), [])

    def test_review_lows_relative_cwd_bounds_and_malformed(self):
        """T-0163 review: a relative cwd is nobody's (it would resolve against the panel's own cwd); the per-session
        table and the day's file are bounded; deep nesting and a NUL byte are 400s, not crashes."""
        text = "text that a relative cwd must not keep"
        for cwd in (".", "tickets", "../proj", ""):
            st, body = self.post_hook(payload("Stop", cwd, "sid-desk", last_assistant_message=text))
            self.assertEqual(st, 200, cwd)
            self.assertFalse(json.loads(body)["kept_text"], repr(cwd))
        self.assertNotIn(text, json.dumps(self.events_file()) + json.dumps(self.srv.events.recent()))
        st, _ = self.post_hook(None, raw=("[" * 30000 + "]" * 30000).encode())        # 60 KB: under the size cap, over the recursion limit
        self.assertEqual(st, 400, "deep nesting is a bad request, not a crash")
        st, _ = self.post_hook(payload("Stop", self.root + "/x\u0000y", "sid-desk", last_assistant_message="nul"))
        self.assertEqual(st, 400, "a NUL byte in cwd is a bad request")
        for i in range(panel_hooks.MAX_SESSIONS + 50):
            self.srv.events.accept(payload("Stop", self.tmp, f"flood-{i}"))
        self.assertLessEqual(len(self.srv.events.by_session), panel_hooks.MAX_SESSIONS)
        self.assertIsNone(self.srv.events.state("flood-0"), "the least recently updated sessions are dropped first")
        self.assertIsNotNone(self.srv.events.state(f"flood-{panel_hooks.MAX_SESSIONS + 49}"))
        p = os.path.join(self.root, ".ops", "state", "events.jsonl")
        before = os.path.getsize(p)
        with mock.patch.object(panelstate, "EVENTS_MAX_BYTES", before + 10):
            self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="over the day's cap"))
        self.assertEqual(os.path.getsize(p), before, "the day's file stops growing at the cap")
        self.assertIn("over the day's cap", self.srv.events.state("sid-desk")["last_reply"], "the event is still shown live")

    def test_events_jsonl_rotates_daily_and_never_follows_a_symlink(self):
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="one"))
        p = os.path.join(self.root, ".ops", "state", "events.jsonl")
        self.assertEqual(len(self.events_file()), 1)
        yesterday = time.time() - 36 * 3600
        os.utime(p, (yesterday, yesterday))
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="two"))
        rotated = [f for f in os.listdir(os.path.join(self.root, ".ops", "state")) if f.startswith("events.20") and f.endswith(".jsonl")]
        self.assertEqual(len(rotated), 1, rotated)
        self.assertEqual([e["text"] for e in self.events_file()], ["two"], "the new day's file holds the new event only")
        old = os.path.join(self.root, ".ops", "state", "events.2020-01-01.jsonl")
        with open(old, "w") as fh:
            fh.write("{}\n")
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="three"))
        self.assertFalse(os.path.exists(old), "files older than the keep window are pruned")
        os.remove(p)
        target = os.path.join(self.tmp, "elsewhere.jsonl")
        os.symlink(target, p)
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="four"))
        self.assertFalse(os.path.islink(p), "a planted symlink is removed, never written through")
        self.assertFalse(os.path.exists(target))
        self.assertEqual([e["text"] for e in self.events_file()], ["four"])

    def test_sse_feed_delivers_events_within_a_second(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.request("GET", "/api/events")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type", ""))
        first = r.fp.readline()
        self.assertTrue(first.startswith(b": zouinify events from seq"), first)
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="streamed reply"))
        self.post_hook(payload("Stop", self.tmp, "sid-other", last_assistant_message="foreign text"))
        got, deadline = [], time.time() + 5
        while time.time() < deadline and len([l for l in got if l.startswith(b"data:")]) < 2:
            line = r.fp.readline()
            if not line:
                break
            got.append(line)
        conn.close()
        data = [json.loads(l[5:]) for l in got if l.startswith(b"data:")]
        self.assertEqual(len(data), 2, got)
        self.assertEqual((data[0]["event"], data[0]["project"], data[0]["text"]), ("Stop", True, "streamed reply"))
        self.assertEqual((data[1]["event"], data[1]["project"]), ("Stop", False))
        self.assertNotIn("text", data[1], "no text field for a foreign event over the wire")
        self.assertIn(b"event: hook\n", got)
        st, hdr, body = self.req("HEAD", "/api/events")
        self.assertEqual((st, body), (200, b""))

    def script_env(self) -> dict:
        env = {k: v for k, v in self.env.items() if k != "ZOUINIFY_PROJECT"}
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        return env

    def run_script(self, p: dict, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([HOOK_SCRIPT], input=json.dumps(p), capture_output=True, text=True, env=env or self.script_env(), timeout=10)

    def wait_events(self, n: int, timeout: float = 3.0) -> list:
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.events_file()) < n:
            time.sleep(0.05)
        return self.events_file()

    def test_hook_script_posts_for_a_project_session_and_is_silent_elsewhere(self):
        self.assertTrue(os.access(HOOK_SCRIPT, os.X_OK), HOOK_SCRIPT)
        env = {k: v for k, v in self.env.items() if k != "ZOUINIFY_PROJECT"}
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        p = payload("Stop", os.path.join(self.root, ".claude", "worktrees", "t0001-first-ready-ticket"), "sid-worker", last_assistant_message="from the script")
        r = subprocess.run([HOOK_SCRIPT], input=json.dumps(p), capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        deadline = time.time() + 3
        while time.time() < deadline and not self.events_file():
            time.sleep(0.05)
        self.assertEqual([e["text"] for e in self.events_file()], ["from the script"], "the cwd walked up to the project root")
        t0 = time.time()
        for i in range(60):
            r = subprocess.run([HOOK_SCRIPT], input=json.dumps(payload("Stop", os.path.join(self.tmp, "nowhere", str(i)), "sid-x", last_assistant_message="x")),
                               capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 6, "60 non-project calls: milliseconds each, never a network call")
        self.assertEqual(len(self.events_file()), 1, "nothing was posted for a cwd outside any project")
        r = subprocess.run([HOOK_SCRIPT], input="", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, "an empty stdin exits 0")
        env2 = dict(env, ZOUINIFY_PROJECT=self.root)
        r = subprocess.run([HOOK_SCRIPT], input=json.dumps(payload("SessionEnd", "/somewhere/else", "sid-desk", reason="other")), capture_output=True, text=True, env=env2, timeout=10)
        self.assertEqual(r.returncode, 0)
        deadline = time.time() + 3
        while time.time() < deadline and len(self.events_file()) < 2:
            time.sleep(0.05)
        self.assertEqual(self.events_file()[-1]["event"], "SessionEnd", "$ZOUINIFY_PROJECT names the project explicitly")
        self.assertFalse(self.events_file()[-1]["project"], "but the event's cwd is outside it: no text, as with any foreign event")

    def test_another_local_user_is_refused_everywhere(self):
        """T-0169: a loopback peer whose socket is not ours (another account, an ssh -L as another user) is a stranger:
        the hook listener says 403, and so does the loopback-bound panel, both audited; nothing is stored."""
        from zouinify import webauth
        with mock.patch.object(webauth, "same_user_peer", return_value=False):
            st, body = self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="forged by another user"))
            self.assertEqual(st, 403)
            self.assertIn("this user", json.loads(body)["error"])
            st, _, body = self.req("GET", "/api/snapshot")
            self.assertEqual(st, 403, "loopback bind, another uid: no token exists to offer, so refused outright")
            self.assertIn("another local user", json.loads(body)["error"])
            st, _, _ = self.req("GET", "/")
            self.assertEqual(st, 403)
        self.assertEqual(self.events_file(), [])
        self.assertIn("AUDIT hook 403", self.server_log())
        self.assertIn("AUDIT 403 other-local-user ip=127.0.0.1 GET /api/snapshot", self.server_log())
        self.assertNotIn("forged by another user", self.server_log())
        st, _, _ = self.req("GET", "/api/snapshot")
        self.assertEqual(st, 200, "the real peer (this process) is the panel's user")

    def test_snapshot_reports_the_listener(self):
        s = self.get_json("/api/snapshot")
        self.assertEqual(s["hook"]["hook_port"], self.hook_port)
        self.assertFalse(s["hook"]["installed"], "the test home has no settings.json")
        self.assertIn("zouinify-hook.sh", s["hook"]["snippet"])
        self.assertIn("permission_prompt|idle_prompt", s["hook"]["snippet"])
        self.assertIn("add the snippet yourself", s["hook"]["note"])
        pj = json.load(open(os.path.join(self.root, ".ops", "state", "panel.json")))
        self.assertEqual(pj["hook_port"], self.hook_port)
        self.assertEqual(pj["hook_nonce"], self.nonce, "the per-start nonce lives in the 0600 pid file, nowhere else")
        self.assertNotIn(self.nonce, json.dumps(s), "never in the snapshot")
        self.assertNotIn(self.nonce, self.server_log(), "never in the log")

    def test_t0182_browser_shaped_requests_are_refused(self):
        """M1: a web page cannot post hooks: any Origin is refused, the body must be application/json (a page would
        have to preflight; OPTIONS is 405), Host must be the listener's own name, and the nonce must match."""
        ev = payload("Stop", self.root, "sid-desk", last_assistant_message="CSRF planted reply")
        st, body = self.post_hook(ev, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertIn("Origin", json.loads(body)["error"])
        st, body = self.post_hook(ev, headers={"Origin": "null"})
        self.assertEqual(st, 403)
        st, body = self.post_hook(ev, ctype="text/plain")
        self.assertEqual(st, 415, "a CORS simple request shape")
        st, body = self.post_hook(ev, ctype="")
        self.assertEqual(st, 415)
        st, body = self.post_hook(ev, host="evil.example")
        self.assertEqual(st, 403)
        st, body = self.post_hook(ev, host=f"evil.example:{self.hook_port}")
        self.assertEqual(st, 403)
        st, body = self.post_hook(ev, nonce=False)
        self.assertEqual(st, 403, "no nonce")
        self.assertIn("X-Zouinify-Hook", json.loads(body)["error"])
        st, body = self.post_hook(ev, nonce=self.nonce[:-1] + ("A" if self.nonce[-1] != "A" else "B"))
        self.assertEqual(st, 403, "wrong nonce")
        self.assertEqual(self.events_file(), [], "nothing refused was stored")
        s = self.get_json("/api/snapshot?fresh=1")
        self.assertNotIn("CSRF planted reply", json.dumps(s))
        self.assertIn("AUDIT hook 403", self.server_log())
        self.assertIn("AUDIT hook 415", self.server_log())
        self.assertNotIn(self.nonce, self.server_log())
        st, body = self.post_hook(ev, ctype="application/json; charset=utf-8", host=f"localhost:{self.hook_port}")
        self.assertEqual(st, 200, "the script's shape, with a charset and the other loopback name")

    def test_t0182_script_sends_only_to_an_owned_live_panel(self):
        """M3 and M2: a planted ancestor .ops that this user does not own is ignored; a pid file that is a symlink, not
        ours, or names a dead or foreign process ends the script silently."""
        root_owned = "/etc/hostname" if os.path.exists("/etc/hostname") else "/etc/passwd"
        if os.stat(root_owned).st_uid == os.getuid():
            self.skipTest("no root-owned file to stand in for another user's plant")
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, ".ops", "state"))
        os.makedirs(os.path.join(shared, "work", "deeper"))
        os.symlink(root_owned, os.path.join(shared, ".ops", "config.yaml"))                 # planted by "another user"
        with open(os.path.join(shared, ".ops", "state", "panel.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "hook_port": self.hook_port, "hook_nonce": self.nonce}, fh)
        r = self.run_script(payload("Stop", os.path.join(shared, "work", "deeper"), "sid-x", last_assistant_message="VICTIM-REPLY"))
        self.assertEqual(r.returncode, 0)
        time.sleep(0.3)
        self.assertEqual(self.events_file(), [], "a config.yaml this user does not own never names a target")
        # a config.yaml of ours but a pid file that is a symlink to someone else's file
        os.remove(os.path.join(shared, ".ops", "config.yaml"))
        with open(os.path.join(shared, ".ops", "config.yaml"), "w") as fh:
            fh.write("version: 1\n")
        os.remove(os.path.join(shared, ".ops", "state", "panel.json"))
        os.symlink(root_owned, os.path.join(shared, ".ops", "state", "panel.json"))
        r = self.run_script(payload("Stop", os.path.join(shared, "work"), "sid-x", last_assistant_message="VICTIM-REPLY"))
        self.assertEqual(r.returncode, 0)
        time.sleep(0.3)
        self.assertEqual(self.events_file(), [])
        # M2: our own project, but the pid file names a dead process, then a process of another user
        for pid in (2147483000, 1):
            panelstate.write_panel_json(self.cfg, self.srv.server_address[1], pid, self.srv.started_at, hook_port=self.hook_port, hook_nonce=self.nonce)
            r = self.run_script(payload("Stop", self.root, "sid-desk", last_assistant_message="STALE-PID-REPLY"))
            self.assertEqual(r.returncode, 0, pid)
            time.sleep(0.3)
            self.assertEqual(self.events_file(), [], f"pid {pid}: not a live process of ours, nothing sent")
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], os.getpid(), self.srv.started_at, hook_port=self.hook_port, hook_nonce=self.nonce)
        r = self.run_script(payload("Stop", self.root, "sid-desk", last_assistant_message="live again"))
        self.assertEqual([e["text"] for e in self.wait_events(1)], ["live again"])
        # a stale nonce (the file was rewritten by a new start) is refused by the listener
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], os.getpid(), self.srv.started_at, hook_port=self.hook_port, hook_nonce="stale-" + "x" * 20)
        st, _ = self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="old nonce"))
        self.assertEqual(st, 403)

    def test_t0182_lows_fields_size_routing_backlog_hygiene_housekeeping(self):
        # short fields are identifiers: prose or symbols in them are dropped, even for own events
        st, _ = self.post_hook(payload("Notification", self.tmp, "sid-other", notification_type="NT SECRET tango!", source="SRC SECRET", reason="RSN;SECRET"))
        self.assertEqual(st, 200)
        ev = self.events_file()[-1]
        self.assertEqual((ev["notification_type"], ev["source"], ev["reason"]), ("", "", ""))
        self.assertNotIn("SECRET", json.dumps(self.srv.events.recent()) + self.server_log())
        st, _ = self.post_hook(payload("Notification", self.root, "sid-debug", notification_type="permission_prompt", message="ok?"))
        self.assertEqual(self.events_file()[-1]["notification_type"], "permission_prompt")
        self.assertEqual(self.srv.events.state("sid-x-nope"), None)
        st, _ = self.post_hook(payload("Stop", self.root, "sid x!", last_assistant_message="bad id"))
        self.assertEqual(self.events_file()[-1]["session_id"], "", "an odd session id is dropped, the event kept")
        # a long reply (over the old 64 KiB) still arrives through the script and its Stop clears the prompt
        self.post_hook(payload("Notification", self.root, "sid-worker", notification_type="permission_prompt", message="allow?"))
        r = self.run_script(payload("Stop", self.root, "sid-worker", last_assistant_message="a long reply line. " * 4000))   # 76 KB of prose
        self.assertEqual(r.returncode, 0)
        evs = self.wait_events(5)
        self.assertEqual(evs[-1]["event"], "Stop")
        self.assertEqual(len(evs[-1]["text"]), 2000)
        self.assertFalse(self.srv.events.state("sid-worker")["waiting"], "the Stop cleared it")
        # the FIRST cwd wins: a nested one later in the payload does not route the event elsewhere
        r = self.run_script(payload("Stop", self.root, "sid-desk", last_assistant_message="routed by the top-level cwd", background_tasks=[{"cwd": "/nowhere/else"}]))
        self.assertEqual(self.wait_events(6)[-1]["text"], "routed by the top-level cwd")
        # a burst: many concurrent posts, none reset by a short listen backlog
        import threading
        results = []
        def one(i):
            try:
                results.append(self.post_hook(payload("Stop", self.tmp, f"burst-{i}"))[0])
            except OSError as e:
                results.append(repr(e))
        ts = [threading.Thread(target=one, args=(i,)) for i in range(40)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(sorted(set(results)), [200], results[:5])
        # log hygiene: a huge query string is not copied into the log
        before = len(self.server_log())
        st, _ = self.post_hook(payload("Stop", self.tmp, "sid-q"), path="/api/hook?" + "q" * 60000)
        self.assertEqual(st, 200)
        self.assertLess(len(self.server_log()) - before, 1000, "the request line is capped and its query redacted")
        self.assertIn("?<query-redacted>", self.server_log())
        # housekeeping: a planted DIRECTORY with a rotated-file name does not stop the file log
        os.makedirs(os.path.join(self.root, ".ops", "state", "events.2020-01-01.jsonl"))
        n = len(self.events_file())
        self.post_hook(payload("Stop", self.root, "sid-desk", last_assistant_message="still logged"))
        self.assertEqual(len(self.events_file()), n + 1)
        # a peer that leaves before the reply: no exception escapes reply()
        class Gone:
            def write(self, b):
                raise BrokenPipeError()
        h = panel_hooks.HookHandler.__new__(panel_hooks.HookHandler)
        h.wfile = Gone(); h.request_version = "HTTP/1.1"; h.requestline = "POST /api/hook HTTP/1.1"; h.command = "POST"
        h.server = self.srv.hooks; h.client_address = ("127.0.0.1", 1); h.close_connection = False
        h.send_response = lambda *a, **k: None; h.send_header = lambda *a, **k: None
        h.end_headers = lambda: (_ for _ in ()).throw(BrokenPipeError())
        h.reply(200, {"ok": True})
        self.assertTrue(h.close_connection)


class TestUpPrintsTheSnippet(World):
    def test_up_prints_the_snippet_and_never_edits_settings(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        r = self.ok("up", "desk", "--no-panel")
        self.assertNotIn("zouinify-hook", r.stdout, "without the panel there is nothing to hook into")
        r = self.ok("up", "desk", "--dry-run")
        self.assertIn("add this yourself", r.stdout)
        self.assertIn("a session must not edit your settings", r.stdout)
        self.assertIn("zouinify-hook.sh", r.stdout)
        self.assertIn("permission_prompt|idle_prompt", r.stdout)
        self.assertFalse(os.path.exists(settings), "printed, never written")
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        with open(settings, "w") as fh:
            fh.write('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.claude/skills/zouinify/templates/hooks/zouinify-hook.sh"}]}]}}')
        r = self.ok("up", "desk", "--dry-run")
        self.assertNotIn("add this yourself", r.stdout, "already installed: nothing to print")

    def test_config_hook_port_defaults_to_port_plus_two(self):
        r = self.ok("config", "--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["panel"]["hook_port"], d["panel"]["port"] + 2, "port + 1 is the terminal page's")
        cfg_text = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        for bad in (d["panel"]["port"], d["panel"]["port"] + 1):
            self.write(".ops/config.yaml", cfg_text.replace("panel: {", f"panel: {{hook_port: {bad}, ", 1))
            r = self.nz("config", "--check")
            self.assertIn("panel.hook_port", r.stderr + r.stdout, f"{bad}: the hook port may not be the panel port or the terminal port")
        self.write(".ops/config.yaml", cfg_text.replace(f"port: {d['panel']['port']},", "port: 65534,", 1))
        r = self.nz("config", "--check")
        self.assertIn("set it explicitly", r.stderr + r.stdout, "no silent default when port + 2 is out of range")


if __name__ == "__main__":
    unittest.main()
