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

    def tearDown(self):
        self.peer_patch.stop()
        self.srv.close_hooks()
        super().tearDown()

    def post_hook(self, body, raw: bytes | None = None, length: int | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=30)
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        conn.putrequest("POST", "/api/hook")
        conn.putheader("Content-Type", "application/json")
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
        conn = http.client.HTTPConnection("127.0.0.1", self.hook_port, timeout=30)
        conn.request("POST", "/api/other", body=b"{}", headers={"Content-Length": "2"})
        r = conn.getresponse(); r.read(); conn.close()
        self.assertEqual(r.status, 404)
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

    def test_hook_script_posts_for_a_project_session_and_is_silent_elsewhere(self):
        self.assertTrue(os.access(HOOK_SCRIPT, os.X_OK), HOOK_SCRIPT)
        panelstate.write_panel_json(self.cfg, self.srv.server_address[1], os.getpid(), self.srv.started_at, hook_port=self.hook_port)
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
        pj = json.load(open(os.path.join(self.root, ".ops", "state", "panel.json"))) if os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")) else {}
        self.assertNotIn("hook_port", pj, "write_panel_json is the server's job in run(); the in-process test did not call it yet")


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
        self.write(".ops/config.yaml", cfg_text.replace("panel: {", f"panel: {{hook_port: {d['panel']['port']}, ", 1))
        r = self.nz("config", "--check")
        self.assertIn("panel.hook_port", r.stderr + r.stdout, "the hook port may not equal the panel port")


if __name__ == "__main__":
    unittest.main()
