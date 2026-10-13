"""T-0181: the terminal page -- a separate process that types into WORKER panes only, after a passphrase unlock.

In-process server on 127.0.0.1 (ephemeral port) over the fake tmux and fake claude: the unlock is required from every
address, loopback included; wrong passphrases lock the address out; targets are the worker sessions only (a role session,
a shell pane, another project's session are refused and audited); keys are an allowlist; every unlock, open, batch,
close and refusal is one audit line; idle and maximum times close attachments and unlocks; `lock` ends everything;
Host/Origin and methods are checked. Then the process side through the CLI: `term secret rotate` refuses inside a Claude
session or without a TTY and prints the passphrase once on a pty; `term start` needs the secret and the flag;
start/status/stop with a real detached process; `up` starts it when autostart is on. The fake tmux records every
send-keys in calls.log and allows it only with ZOUINIFY_FAKE_TMUX_ALLOW_SEND_KEYS=1, which only TermWorld sets."""
import http.client
import json
import os
import pty
import re
import select
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

from test_core import World, ZOUINIFY  # noqa: E402

try:
    from zouinify import config as cfgmod
    from zouinify import panelstate, termctl
    from zouinify.term import keys as term_keys
    from zouinify.term import server as term_server
    TERM_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - the RED case: no term package on the base tree
    TERM_IMPORT_ERROR = e

PASS = "tester-pass-" + "k7x2m" * 3          # assembled: no credential-shaped literal sits in this file
WRONG = "wrong-pass-" + "zzzzz" * 3
TERM_BLOCK = "terminal: {{enabled: true, port: {port}, bind: loopback, idle_close_s: 2, max_open_s: 600, unlock_idle_s: 3, unlock_max_s: 600}}\n"
PROMPT_SCREEN = (" Bash(git push origin dev)\n Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again\n"
                 "   3. No, and tell Claude what to do differently (esc)\n aws_secret_access_key = x\n\n   [Fable 5.1] x | dev\n")
PASSPHRASE_RE = re.compile(r"\b[a-z2-9]{5}(?:-[a-z2-9]{5}){3}\b")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def no_claude_env(env):
    e = dict(env)
    for k in list(e):
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"):
            e.pop(k)
    return e


class TermWorld(World):
    """World + a running terminal server; the only World that may see a send-keys call."""

    ALLOWS_SEND_KEYS = True

    def setUp(self):
        if TERM_IMPORT_ERROR:
            self.fail(f"term package not importable from {REPO}/lib: {TERM_IMPORT_ERROR!r}")
        super().setUp()
        self.term_port = free_port()
        self.write(".ops/config.yaml", open(os.path.join(self.root, ".ops", "config.yaml")).read() + TERM_BLOCK.format(port=self.term_port))
        self.env["ZOUINIFY_FAKE_TMUX_ALLOW_SEND_KEYS"] = "1"
        self.ok("up", "desk")                       # a role session: never reachable from the page
        self.ok("launch", "worker", "T-0001")       # t0001-first-ready-ticket, registered as a worker
        self.other = os.path.join(self.tmp, "other")
        os.makedirs(self.other)
        for name, cwd in (("t0003-shell-worker", self.root), ("t0004-other-project", self.other), ("renamed-window", self.root),
                          ("t0006-wrong-tree", self.root), ("t0007-no-agent", self.root)):
            subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd, "claude", "--model", "fable", "--", name],
                           env=self.env, check=True, capture_output=True)
        with open(os.path.join(self.state, "cmd-t0003-shell-worker.txt"), "w") as fh:
            fh.write("bash\n")                       # a worker-named session whose pane runs a shell
        with open(os.path.join(self.state, "screen-t0001-first-ready-ticket.txt"), "w") as fh:
            fh.write(PROMPT_SCREEN)
        with open(os.path.join(self.state, "sessions.json")) as fh:
            pids = {n: v["pid"] for n, v in json.load(fh).items()}
        # a fake /proc: every fake pane pid is a `claude` process in its own group, foreground on its tty /dev/pts/<pid>
        self.proc = os.path.join(self.tmp, "proc")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for b in ("claude", "tmux", "bash"):
            open(os.path.join(self.bin, b), "w").close()
        for pid in pids.values():
            self.proc_entry(pid, "claude", 1, pid, pid, pid, "claude")
        self.env["ZOUINIFY_PROC"] = self.proc
        agents = [
            # a same-named session of ANOTHER project listed first: agents.find picks the one in this project (L5)
            {"name": "t0001-first-ready-ticket", "cwd": self.other, "status": "idle", "pid": 1, "kind": "interactive"},
            {"name": "tp-desk", "cwd": self.root, "status": "busy", "pid": pids["tp-desk"], "kind": "interactive"},
            {"name": "t0001-first-ready-ticket", "cwd": os.path.join(self.root, ".claude", "worktrees", "t0001-first-ready-ticket"),
             "status": "waiting", "pid": pids["t0001-first-ready-ticket"], "kind": "interactive"},
            {"name": "t0003-shell-worker", "cwd": self.root, "status": "idle", "pid": pids["t0003-shell-worker"], "kind": "interactive"},
            {"name": "t0004-other-project", "cwd": self.other, "status": "waiting", "pid": pids["t0004-other-project"], "kind": "interactive"},
            # a worker named by the pattern whose tmux window carries another name (a session from before the launcher)
            {"name": "t0005-renamed-worker", "cwd": self.root, "status": "waiting", "pid": pids["renamed-window"], "kind": "interactive"},
            # a worker-named pane whose process tree is NOT the agent (the agent pid is unrelated to the pane's)
            {"name": "t0006-wrong-tree", "cwd": self.root, "status": "waiting", "pid": 1, "kind": "interactive"},
        ]
        self.agents = os.path.join(self.tmp, "agents.json")
        with open(self.agents, "w") as fh:
            json.dump(agents, fh)
        self.env["FAKE_CLAUDE_AGENTS"] = self.agents
        self.tmuxdir = os.path.join(self.tmp, "tmuxdir")   # tmux's socket directory ($TMUX_TMPDIR/tmux-<uid>/): empty unless a test plants a server
        os.makedirs(self.tmuxdir)
        self.env["TMUX_TMPDIR"] = self.tmuxdir
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.cfg = cfgmod.load(self.root)
        panelstate.set_term_secret(self.cfg, PASS)
        self.srv = term_server.make_server(self.cfg, port=0, bind="loopback", address="127.0.0.1")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            self.env_patch.stop()
            super().tearDown()

    def proc_entry(self, pid, comm, ppid, pgrp, tty_nr, tpgid, exe_name):
        """One process in the fake /proc: <pid>/stat with the fields the inspection reads, <pid>/exe -> bin/<exe_name>."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as fh:
            fh.write(f"{pid} ({comm}) S {ppid} {pgrp} {pgrp} {tty_nr} {tpgid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        exe = os.path.join(d, "exe")
        if os.path.lexists(exe):
            os.remove(exe)
        os.symlink(os.path.join(self.bin, exe_name), exe)

    def proc_remove(self, pid):
        import shutil
        shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)

    def pane_pid(self, name):
        with open(os.path.join(self.state, "sessions.json")) as fh:
            return json.load(fh)[name]["pid"]

    def req(self, method, path, body=None, cookie=None, headers=None, host=None, raw=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        if cookie:
            conn.putheader("Cookie", f"zouinify_term={cookie}")
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        if data is not None:
            conn.putheader("Content-Type", ctype)
            conn.putheader("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        payload = r.read()
        hdr = dict(r.getheaders())
        conn.close()
        try:
            d = json.loads(payload)
        except ValueError:
            d = payload
        return r.status, hdr, d

    def unlock(self):
        st, hdr, d = self.req("POST", "/term/unlock", {"passphrase": PASS})
        self.assertEqual(st, 200, d)
        sc = hdr["Set-Cookie"]
        return sc.split(";")[0].split("=", 1)[1], sc

    def audit(self):
        p = os.path.join(self.root, ".ops", "state", "term_audit.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def sent(self):
        return [c for c in self.tmux_calls() if c and c[0] == "send-keys"]


class TestTermApi(TermWorld):
    def test_health_and_page_are_public_and_secret_free(self):
        st, _, d = self.req("GET", "/term/health")
        self.assertEqual(st, 200)
        self.assertEqual((d["service"], d["targets"], d["unlock_required"], d["tls"]), ("zouinify-term", "workers", True, False))
        self.assertNotIn("root", d, "the absolute root is never exposed (L4)")
        self.assertRegex(d["root_sha"], r"^[0-9a-f]{16}$")
        self.assertNotIn(self.root, json.dumps(d))
        st, hdr, body = self.req("GET", "/term/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", hdr["Content-Type"])
        self.assertIn(b"Unlock", body)
        self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
        st, _, body = self.req("GET", "/term/static/term.js")
        self.assertEqual(st, 200)
        self.assertIn(b"/term/unlock", body)
        self.assertNotIn(b"send-keys", body)
        st, _, _ = self.req("GET", "/term/static/../server.py")
        self.assertEqual(st, 404)

    def test_the_unlock_is_required_from_loopback_too(self):
        for method, path in (("GET", "/term/me"), ("GET", "/term/targets"), ("POST", "/term/open"), ("POST", "/term/lock"),
                             ("GET", "/term/" + "a" * 64 + "/screen"), ("POST", "/term/" + "a" * 64 + "/keys")):
            st, _, d = self.req(method, path, body={} if method == "POST" else None)
            self.assertEqual(st, 401, (method, path, d))
        st, _, _ = self.req("GET", "/term/me", cookie="b" * 64)
        self.assertEqual(st, 401, "a well-formed but unknown cookie")
        self.assertTrue(any(a["event"] == "refused" and a["reason"] in ("no-token", "bad-cookie") for a in self.audit()))
        self.assertEqual(self.sent(), [])

    def test_unlock_wrong_right_and_lockout(self):
        st, hdr, d = self.req("POST", "/term/unlock", {"passphrase": WRONG})
        self.assertEqual(st, 401)
        self.assertNotIn("Set-Cookie", hdr)
        self.assertEqual(self.audit()[-1]["reason"], "unlock-wrong")
        cookie, sc = self.unlock()
        self.assertEqual(len(cookie), 64)
        for part in ("HttpOnly", "SameSite=Strict", "Path=/term"):
            self.assertIn(part, sc)
        self.assertNotIn("Secure", sc, "no TLS in this test server")
        last = self.audit()[-1]
        self.assertEqual((last["event"], last["unlock8"]), ("unlock", cookie[:8]))
        self.assertNotIn(PASS, json.dumps(self.audit()), "the passphrase is never audited")
        st, _, d = self.req("GET", "/term/me", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual((d["unlocked"], d["targets"]), (True, "workers"))
        # four more wrong ones pass as 401; the fifth within the minute locks the address out
        for _ in range(3):
            st, _, _ = self.req("POST", "/term/unlock", {"passphrase": WRONG})
            self.assertEqual(st, 401)
        st, hdr, _ = self.req("POST", "/term/unlock", {"passphrase": WRONG})
        self.assertEqual(st, 429)
        self.assertIn("Retry-After", hdr)
        self.assertEqual(self.audit()[-1]["reason"], "unlock-wrong-lockout")
        st, _, _ = self.req("POST", "/term/unlock", {"passphrase": PASS})
        self.assertEqual(st, 429, "even the right passphrase waits out the lockout")
        self.assertEqual(self.audit()[-1]["reason"], "unlock-lockout")
        st, _, _ = self.req("GET", "/term/me", cookie=cookie)
        self.assertEqual(st, 200, "an unlock made before the lockout keeps working")

    def test_targets_are_workers_only(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("GET", "/term/targets", cookie=cookie)
        self.assertEqual(st, 200)
        by = {t["name"]: t for t in d["targets"]}
        self.assertEqual(sorted(by), ["t0001-first-ready-ticket", "t0003-shell-worker", "t0005-renamed-worker", "t0006-wrong-tree"],
                         "the desk, another project's session and a worker-named window with no live agent are never listed")
        self.assertFalse(by["t0006-wrong-tree"]["live"])
        self.assertIn("process tree", by["t0006-wrong-tree"]["reason"])
        self.assertEqual((by["t0005-renamed-worker"]["tmux"], by["t0005-renamed-worker"]["live"], by["t0005-renamed-worker"]["ticket"]),
                         ("renamed-window", True, "T-0005"), "a worker's window is found through its pid when the names differ")
        self.assertTrue(by["t0001-first-ready-ticket"]["live"])
        self.assertEqual(by["t0001-first-ready-ticket"]["ticket"], "T-0001")
        self.assertFalse(by["t0003-shell-worker"]["live"])
        self.assertIn("bash", by["t0003-shell-worker"]["reason"])
        self.assertIn("never reachable", d["note"])

    def test_role_shell_and_foreign_panes_are_refused(self):
        cookie, _ = self.unlock()
        cases = (("tp-desk", "role sessions"), ("tp-manager", "role sessions"), ("tp-chrome", "role sessions"),
                 ("t0003-shell-worker", "bash"), ("t0004-other-project", "not a worker session"), ("nope", "not a worker session"),
                 ("t0006-wrong-tree", "process tree"), ("t0007-no-agent", "not a worker session"),
                 ("../x", "not a session name"), ("", "required"))
        for name, why in cases:
            st, _, d = self.req("POST", "/term/open", {"target": name}, cookie=cookie)
            self.assertEqual(st, 404 if name else 400, (name, d))
            self.assertIn(why, (d.get("reason") or d.get("error") or ""), name)
        refused = [a for a in self.audit() if a["event"] == "refused" and str(a.get("reason", "")).startswith("target:")]
        self.assertEqual(len(refused), 9)
        self.assertEqual(self.sent(), [], "nothing was ever typed")

    def test_open_screen_keys_close(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]
        self.assertEqual((len(tid), d["target"], d["tmux"], d["ticket"]), (64, "t0001-first-ready-ticket", "t0001-first-ready-ticket", "T-0001"))
        self.assertRegex(d["pane"], r"^%\d+$", "one concrete pane is pinned at open")
        pane = d["pane"]
        self.assertEqual(self.audit()[-1]["event"], "open")
        st, _, s = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("Do you want to proceed?", s["text"])
        self.assertIn("aws_secret_access_key = <redacted>", s["text"], "the screen goes through the redactor")
        self.assertEqual(s["tid8"], tid[:8])
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "2"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 200, k)
        self.assertEqual(k["sent"], 2)
        calls = self.sent()
        self.assertEqual(calls, [["send-keys", "-t", pane, "-l", "--", "2"], ["send-keys", "-t", pane, "Enter"]],
                         "keys go to the pinned %pane_id, never to a session or window name")
        last = self.audit()[-1]
        self.assertEqual((last["event"], last["tid8"], last["target"], last["keys"], last["n"], last["m"], last["pane"]),
                         ("keys", tid[:8], "t0001-first-ready-ticket", ["Enter"], 2, 2, pane))
        self.assertNotIn("text", last, "typed text is never audited in plain (M1)")
        self.assertEqual(last["text_len"], 1)
        self.assertRegex(last["text_hmac"], r"^[0-9a-f]{16}$")
        st, _, _ = self.req("POST", f"/term/{tid}/close", {}, cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(self.audit()[-1]["reason"], "user")
        st, _, _ = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
        self.assertEqual(st, 410)
        st, _, _ = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410)
        self.assertEqual(len(self.sent()), 2, "nothing more was typed")

    def test_bad_keys_are_refused_and_nothing_is_sent(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        bad = ([{"k": "C-d"}], [{"k": "C-z"}], [{"k": "C-\\"}], [{"k": "C-b"}], [{"k": "Escape"}, {"t": "[A"}], [{"k": "Escape"}, {"t": "OA"}], [{"t": "\x1b[A"}], [{"t": "a​b"}], [{"t": ""}],
               [{"t": "x" * 2001}], [{"t": "a", "k": "Enter"}], [{"x": 1}], [], [{"k": "Enter"}] * 51, "nope")
        for ops in bad:
            st, _, d = self.req("POST", f"/term/{tid}/keys", {"ops": ops}, cookie=cookie)
            self.assertEqual(st, 400, (ops if not isinstance(ops, list) else ops[:2], d))
        st, _, d = self.req("POST", f"/term/{tid}/keys", raw=b"ops=Enter", ctype="application/x-www-form-urlencoded", cookie=cookie)
        self.assertEqual(st, 400, "a form body is not JSON: nothing is parsed from it")
        self.assertEqual(self.sent(), [])
        self.assertTrue(all(a["event"] == "refused" and a["reason"].startswith("keys:") for a in self.audit()[-len(bad) - 1:]))

    def test_idle_close_and_unlock_idle(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        st, _, _ = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 200)
        time.sleep(2.3)                                   # idle_close_s: 2 in this world
        st, _, d = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410, d)
        self.assertTrue(any(a["event"] == "close" and a["reason"] == "idle" and a["tid8"] == tid[:8] for a in self.audit()))
        self.assertEqual(len(self.sent()), 1)
        time.sleep(3.3)                                   # unlock_idle_s: 3
        st, _, _ = self.req("GET", "/term/me", cookie=cookie)
        self.assertEqual(st, 401, "the unlock expired without a keystroke")
        self.assertTrue(any(a["event"] == "lock" and a["reason"] == "idle" and a["unlock8"] == cookie[:8] for a in self.audit()))

    def test_a_pane_that_stops_running_claude_closes_the_attachment(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        with open(os.path.join(self.state, "cmd-t0001-first-ready-ticket.txt"), "w") as fh:
            fh.write("bash\n")                             # the worker dropped to a shell
        st, _, d = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "rm -rf x"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410, d)
        self.assertIn("bash", d["error"])
        self.assertEqual(self.sent(), [], "nothing reaches a pane that is not claude")
        self.assertIn("bash", self.audit()[-1]["reason"])

    def test_a_split_or_second_window_is_refused_and_closes_the_attachment(self):
        """Review B1: a worker could `split-window` a nested `tmux attach` into another session; the check and the send
        must name the same pane, and a session with more than one pane is never typed into."""
        cookie, _ = self.unlock()
        planted = os.path.join(self.state, "panes-t0001-first-ready-ticket.json")
        with open(planted, "w") as fh:
            json.dump([{"id": "%9001", "cmd": "tmux", "active": 1, "pid": 9001}], fh)   # the split holds the active pane
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("2 panes", d["reason"])
        st, _, t = self.req("GET", "/term/targets", cookie=cookie)
        row = next(x for x in t["targets"] if x["name"] == "t0001-first-ready-ticket")
        self.assertFalse(row["live"])
        os.remove(planted)
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]
        with open(planted, "w") as fh:
            json.dump([{"id": "%9001", "cmd": "tmux", "active": 1, "pid": 9001}], fh)   # split after the open
        st, _, s = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
        self.assertEqual(st, 410, s)
        self.assertIn("2 panes", s["error"])
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410)
        self.assertEqual(self.sent(), [], "nothing was typed into a session with a second pane")
        self.assertTrue(any(a["event"] == "close" and "2 panes" in a.get("reason", "") for a in self.audit()))

    def test_a_tmux_client_inside_the_pane_is_refused(self):
        """Review B2: the worker itself runs a tmux client of another session inside its pane, with argv[0] faked to
        `claude`; pane_current_command, the pid descent and the %id all still look right. The real executable gives it away."""
        cookie, _ = self.unlock()
        P = self.pane_pid("t0001-first-ready-ticket")
        self.proc_entry(7777, "claude", P, 7777, P, P, "tmux")            # comm says claude, exe is tmux, holds the pane's tty
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("tmux client runs inside the pane", d["reason"])
        self.proc_remove(7777)
        self.proc_entry(7778, "claude", 1, 7778, P, P, "tmux")            # not under the pane, but on its tty: refused too
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.proc_remove(7778)
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]
        self.proc_entry(7777, "claude", P, 7777, P, P, "tmux")            # the client appears after the open
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410, k)
        self.assertEqual(self.sent(), [], "nothing was typed with a tmux client in the pane")
        self.assertTrue(any("tmux client" in a.get("reason", "") for a in self.audit() if a["event"] == "close"))

    def test_the_foreground_group_must_be_the_agents(self):
        cookie, _ = self.unlock()
        P = self.pane_pid("t0001-first-ready-ticket")
        self.proc_entry(P, "claude", 1, P, P, 4242, "claude")              # some other group holds the tty
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("foreground process group", d["reason"])
        self.assertEqual(self.sent(), [])

    def plant_clients(self, rows, sock=None):
        """What the fake tmux server (`list-clients`) reports: the default server's clients, or those of the server on
        socket <sock> (`-S <dir>/<sock>`)."""
        path = os.path.join(self.state, "clients.json" if sock is None else f"clients-{sock}.json")
        if rows is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w") as fh:
            json.dump(rows, fh)

    def test_a_renamed_copy_of_tmux_inside_the_pane_is_refused(self):
        """T-0183: `cp $(command -v tmux) ~/x; ~/x ...` inside the worker's pane -- argv[0], comm and the exe name all say
        `x`, so the name check of T-0181 passed it. The copy is byte-identical to the tmux zouinify runs, and that is
        what gives it away; a same-sized file with other bytes is not tmux."""
        import shutil
        cookie, _ = self.unlock()
        P = self.pane_pid("t0001-first-ready-ticket")
        real = shutil.which("tmux")
        self.assertTrue(real and os.path.getsize(real) > 0, real)
        shutil.copyfile(real, os.path.join(self.bin, "x"))
        self.proc_entry(7781, "x", P, 7781, P, P, "x")
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("tmux client runs inside the pane", d["reason"])
        self.assertIn("copy of the tmux binary", d["reason"])
        self.proc_remove(7781)
        with open(os.path.join(self.bin, "notmux"), "wb") as fh:
            fh.write(b"\0" * os.path.getsize(real))
        self.proc_entry(7782, "notmux", P, 7782, P, P, "notmux")
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.proc_remove(7782)
        self.assertEqual(self.sent(), [])

    def test_exe_identity_catches_a_hard_link_and_a_copy(self):
        """T-0183 unit: zouinify.term.procs.exe_is_tmux against a planted "real" tmux -- a hard link under another name
        (same inode), a byte-identical copy (same size and sha256), a same-sized stranger (no), the name alone (yes)."""
        import shutil
        from zouinify.term import procs
        real = os.path.join(self.bin, "tmux-real")
        with open(real, "wb") as fh:
            fh.write(os.urandom(4096))
        os.link(real, os.path.join(self.bin, "y"))
        shutil.copyfile(real, os.path.join(self.bin, "z"))
        with open(os.path.join(self.bin, "w"), "wb") as fh:
            fh.write(os.urandom(4096))
        st = os.stat(real)
        ident = {"path": real, "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime": st.st_mtime_ns}
        for pid, name in ((7791, "y"), (7792, "z"), (7793, "w"), (7794, "tmux"), (7795, "claude")):
            self.proc_entry(pid, name, 1, pid, 0, 0, name)
        try:
            with mock.patch.object(procs, "real_tmux", return_value=ident):
                self.assertEqual(procs.exe_is_tmux(7791), procs.HOW_LINKED)
                self.assertEqual(procs.exe_is_tmux(7792), procs.HOW_COPIED)
                self.assertIsNone(procs.exe_is_tmux(7793))
                self.assertEqual(procs.exe_is_tmux(7794), procs.HOW_NAMED)
                self.assertIsNone(procs.exe_is_tmux(7795))
                self.assertIsNone(procs.exe_is_tmux(424242), "no such process")
            with mock.patch.object(procs, "real_tmux", return_value=None):
                self.assertIsNone(procs.exe_is_tmux(7792), "without a tmux on PATH only the name counts")
                self.assertEqual(procs.exe_is_tmux(7794), procs.HOW_NAMED)
            found = procs.real_tmux()
            self.assertEqual(found["path"], os.path.realpath(shutil.which("tmux")), "the identity is the tmux zouinify runs")
        finally:
            for pid in (7791, 7792, 7793, 7794, 7795):
                self.proc_remove(pid)

    def test_a_client_attached_from_inside_the_pane_is_refused(self):
        """T-0183: the tmux server's own list of clients. A client attached from the pane's tty, or by a process under the
        pane's shell, is refused whatever its binary is called (no /proc evidence needed); the human watching the worker
        from their own terminal is not. A client that attaches after the open closes the attachment with nothing sent."""
        cookie, _ = self.unlock()
        P = self.pane_pid("t0001-first-ready-ticket")
        self.plant_clients([{"pid": 7801, "tty": f"/dev/pts/{P}", "session": "tp-desk"}])
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("attached from inside the pane", d["reason"])
        self.assertIn("tp-desk", d["reason"])
        self.plant_clients([{"pid": 7802, "tty": "/dev/pts/555", "session": "tp-desk"}])
        self.proc_entry(7802, "x", P, 7802, 555, 7802, "x")                 # on another tty, but under the pane's shell
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, d)
        self.assertIn("attached from inside the pane", d["reason"])
        self.proc_remove(7802)
        self.plant_clients([{"pid": 1, "tty": "/dev/pts/555", "session": "t0001-first-ready-ticket"}])   # the human, from their terminal
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]
        self.plant_clients([{"pid": 7801, "tty": f"/dev/pts/{P}", "session": "tp-desk"}])
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410, k)
        self.assertEqual(self.sent(), [], "nothing was typed with a client attached from the pane")
        self.assertTrue(any("attached from inside" in a.get("reason", "") for a in self.audit() if a["event"] == "close"))
        self.plant_clients(None)

    def test_every_server_in_the_socket_dir_is_asked(self):
        """T-0183: a client of a second tmux server (`-L other`) attached from inside the pane is found through that server's
        socket in $TMUX_TMPDIR/tmux-<uid>/; a socket nobody listens on and a plain file there are harmless."""
        import socket as socketmod
        cookie, _ = self.unlock()
        P = self.pane_pid("t0001-first-ready-ticket")
        d = os.path.join(self.tmuxdir, f"tmux-{os.getuid()}")
        os.makedirs(d)
        for name in ("other", "stale"):
            s = socketmod.socket(socketmod.AF_UNIX)
            s.bind(os.path.join(d, name))
            self.addCleanup(s.close)
        open(os.path.join(d, "notes.txt"), "w").close()
        self.plant_clients([{"pid": 7811, "tty": f"/dev/pts/{P}", "session": "tp-desk"}], sock="other")
        st, _, r = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 404, r)
        self.assertIn("attached from inside the pane", r["reason"])
        self.assertIn("other server", r["reason"])
        calls = [c for c in self.tmux_calls() if "list-clients" in c]
        self.assertTrue(any(c[:3] == ["-S", os.path.join(d, "other"), "list-clients"] for c in calls), calls)
        self.assertTrue(any(c[:3] == ["-S", os.path.join(d, "stale"), "list-clients"] for c in calls), calls)
        self.assertFalse(any("notes.txt" in " ".join(c) for c in calls), "a plain file is not a server socket")
        self.plant_clients(None, sock="other")
        st, _, r = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, r)

    def test_a_respawned_pane_keeps_its_id_but_is_refused(self):
        """Review L5: respawn-pane keeps the %id and changes the pane pid; the agent no longer descends from it."""
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid, pane = d["tid"], d["pane"]
        with open(os.path.join(self.state, "panepid-t0001-first-ready-ticket.txt"), "w") as fh:
            fh.write("5555\n")
        st, _, s = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
        self.assertEqual(st, 410, s)
        self.assertIn("process tree", s["error"])
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410)
        self.assertEqual(self.sent(), [])

    def test_every_op_is_inspected(self):
        """Review M4: a batch is several tmux calls; the pane is re-inspected before each one, not once per batch."""
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        before = len([c for c in self.tmux_calls() if c and c[0] == "list-panes" and "-s" in c])
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "a"}, {"t": "b"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual((st, k["sent"]), (200, 3), k)
        after = len([c for c in self.tmux_calls() if c and c[0] == "list-panes" and "-s" in c])
        self.assertGreaterEqual(after - before, 3, "one inspection per op")
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}] * 9}, cookie=cookie)
        self.assertEqual(st, 400, "a batch is at most 8 ops")

    def test_a_partial_batch_is_audited(self):
        """Review M3: when tmux fails mid-batch the audit says how many ops went."""
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        with open(os.path.join(self.state, "fail-send-t0001-first-ready-ticket.txt"), "w") as fh:
            fh.write("boom\n")
        st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "ok"}, {"t": "boom"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 502, k)
        self.assertEqual(k["sent"], 1)
        last = self.audit()[-1]
        self.assertEqual((last["event"], last["n"], last["m"], last["partial"]), ("keys", 1, 3, True))
        self.assertNotIn("text", last)
        self.assertEqual(len(self.sent()), 2, "the failing call is the second and last one")

    def test_audit_command_prints_no_typed_text(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        st, _, _ = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "hunter2"}, {"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 200)
        import io
        out = io.StringIO()
        termctl.cmd_audit(self.cfg, since="10m", out=out)
        text = out.getvalue()
        self.assertIn("text_len=7", text)
        self.assertIn("keys=Enter", text)
        self.assertNotIn("hunter2", text)
        with open(os.path.join(self.root, ".ops", "state", "term_audit.jsonl")) as fh:
            self.assertNotIn("hunter2", fh.read())

    def test_concurrent_unlock_checks_are_capped(self):
        """Review L3: PBKDF2 is deliberately slow; only UNLOCK_SLOTS verifications run at once, the rest are told to retry."""
        real = panelstate.term_secret_matches

        def slow(cfg, presented):
            time.sleep(0.8)
            return real(cfg, presented)

        results = []
        with mock.patch.object(term_server.panelstate, "term_secret_matches", slow), mock.patch.object(term_server, "UNLOCK_SLOT_WAIT_S", 0.1):
            def go():
                st, _, _ = self.req("POST", "/term/unlock", {"passphrase": PASS})
                results.append(st)
            threads = [threading.Thread(target=go) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertEqual(sorted(set(results)), [200, 503], results)
        self.assertEqual(results.count(200), term_server.UNLOCK_SLOTS)
        self.assertTrue(any(a["event"] == "refused" and a["reason"] == "unlock-busy" for a in self.audit()))

    def test_lock_ends_everything(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        st, hdr, _ = self.req("POST", "/term/lock", {}, cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("Max-Age=0", hdr["Set-Cookie"])
        for method, path in (("GET", "/term/me"), ("GET", f"/term/{tid}/screen"), ("POST", f"/term/{tid}/keys")):
            st, _, _ = self.req(method, path, body={"ops": [{"k": "Enter"}]} if method == "POST" else None, cookie=cookie)
            self.assertEqual(st, 401, path)
        events = [(a["event"], a.get("reason")) for a in self.audit()]
        self.assertIn(("close", "lock"), events)
        self.assertIn(("lock", "user"), events)
        self.assertEqual(self.sent(), [])

    def test_host_origin_methods_and_content_type(self):
        cookie, _ = self.unlock()
        st, _, _ = self.req("GET", "/term/health", host="evil.example")
        self.assertEqual(st, 403)
        st, _, _ = self.req("GET", "/term/me", cookie=cookie, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403, "a foreign Origin is refused even with the cookie (CSRF)")
        st, _, _ = self.req("GET", "/term/me", cookie=cookie, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        st, hdr, _ = self.req("PUT", "/term/me", cookie=cookie, body={})
        self.assertEqual(st, 405)
        self.assertEqual(hdr["Allow"], "GET, HEAD, POST")
        st, _, _ = self.req("POST", "/term/unlock", raw=json.dumps({"passphrase": PASS}).encode(), ctype="text/plain")
        self.assertEqual(st, 400, "the unlock body must be application/json")
        st, _, _ = self.req("GET", "/nope", cookie=cookie)
        self.assertEqual(st, 404)
        self.assertEqual(self.sent(), [])

    def test_another_local_user_gets_nothing_on_loopback(self):
        """T-0169's second layer: a loopback peer whose socket is not ours is refused before the unlock screen."""
        cookie, _ = self.unlock()
        with mock.patch.object(term_server.webauth, "same_user_peer", lambda conn: False):
            for method, path, body in (("GET", "/term/health", None), ("GET", "/term/", None), ("POST", "/term/unlock", {"passphrase": PASS}),
                                       ("GET", "/term/me", None)):
                st, _, d = self.req(method, path, body=body, cookie=cookie)
                self.assertEqual(st, 403, (path, d))
        self.assertTrue(any(a["event"] == "refused" and a["reason"] == "other-local-user" for a in self.audit()))
        st, _, _ = self.req("GET", "/term/me", cookie=cookie)
        self.assertEqual(st, 200, "our own connections keep working")

    def test_the_panel_sees_the_terminal_only_while_its_process_lives(self):
        info = panelstate.term_info(self.cfg)
        self.assertEqual((info["enabled"], info["running"], info["url"], info["targets"]), (True, False, "", "workers"))
        panelstate.write_term_json(self.cfg, self.port, os.getpid(), "2026-10-13T12:00:00-04:00", "loopback", False, f"http://127.0.0.1:{self.port}/term/")
        info = panelstate.term_info(self.cfg)
        self.assertTrue(info["running"])
        self.assertEqual(info["url"], f"http://127.0.0.1:{self.port}/term/")
        self.assertNotIn("hash", json.dumps(info).lower())
        panelstate.remove_term_json(self.cfg, os.getpid())
        self.assertFalse(panelstate.term_info(self.cfg)["running"])
        self.assertTrue(panelstate.term_secret_configured(self.cfg), "the secret survives the runtime fields")
        with open(os.path.join(REPO, "lib", "zouinify", "panel", "static", "app.js")) as fh:
            js = fh.read()
        self.assertIn("r.role === 'worker'", js, "the panel links only worker rows")
        self.assertIn('rel="noopener"', js)


class TestTermKeys(unittest.TestCase):
    def setUp(self):
        if TERM_IMPORT_ERROR:
            self.fail(f"term package not importable: {TERM_IMPORT_ERROR!r}")

    def test_parse_ops(self):
        p = term_keys.parse_ops
        self.assertEqual(p({"ops": [{"t": "2"}, {"k": "Enter"}]}), [("t", "2"), ("k", "Enter")])
        self.assertEqual(p({"ops": [{"t": "-rf is text"}]}), [("t", "-rf is text")])
        for bad in ({"ops": [{"k": "C-d"}]}, {"ops": [{"k": "Enter", "t": "x"}]}, {"ops": [{"t": "\x07"}]}, {"ops": [{"t": "é​"}]},
                    {"ops": []}, {"ops": "Enter"}, {}, [], {"ops": [{"t": 3}]}, {"ops": [{"k": "enter"}]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                p(bad)
        self.assertNotIn("C-d", term_keys.NAMED_KEYS)
        self.assertNotIn("C-z", term_keys.NAMED_KEYS)


class TestTermControl(World):
    """The process side through the CLI: the passphrase on a real TTY only, start/status/stop of a detached process, `up`."""

    def setUp(self):
        if TERM_IMPORT_ERROR:
            self.fail(f"term package not importable: {TERM_IMPORT_ERROR!r}")
        super().setUp()
        self.term_port = free_port()
        self.write(".ops/config.yaml", open(os.path.join(self.root, ".ops", "config.yaml")).read() + TERM_BLOCK.format(port=self.term_port))

    def set_secret(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            cfg = cfgmod.load(self.root)
            panelstate.set_term_secret(cfg, PASS)
            return cfg

    def test_secret_rotate_refuses_in_a_claude_session_and_without_a_tty(self):
        r = self.nz("term", "secret", "rotate", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("never inside a Claude session", r.stderr + r.stdout)
        r = self.nz("term", "secret", "rotate", env=no_claude_env(self.env))
        self.assertIn("interactively", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "term.json")), "nothing recorded")

    def test_secret_rotate_prints_once_on_a_pty_and_keeps_only_the_hash(self):
        env = no_claude_env(self.env)
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(self.root)
            os.execve(ZOUINIFY, [ZOUINIFY, "term", "secret", "rotate"], env)
        out = b""
        deadline = time.time() + 60
        pressed = False
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if b"Press Enter" in out and not pressed:
                    os.write(fd, b"\n")
                    pressed = True
            else:
                _, status = os.waitpid(pid, os.WNOHANG)
                if status or _ == pid:
                    break
        os.waitpid(pid, 0) if not pressed else None
        text = out.decode("utf-8", "replace")
        self.assertIn("shown ONCE", text)
        m = PASSPHRASE_RE.search(text)
        self.assertTrue(m, text)
        passphrase = m.group(0)
        self.assertIn("\x1b[2J", text, "the screen is cleared after Enter")
        with open(os.path.join(self.root, ".ops", "state", "term.json")) as fh:
            tj = json.load(fh)
        self.assertNotIn(passphrase, json.dumps(tj), "the plaintext is never on disk")
        for k in ("secret_hash", "secret_salt", "secret_rounds", "secret_id"):
            self.assertIn(k, tj)
        with mock.patch.dict(os.environ, self.env, clear=True):
            cfg = cfgmod.load(self.root)
            self.assertTrue(panelstate.term_secret_matches(cfg, passphrase))
            self.assertFalse(panelstate.term_secret_matches(cfg, passphrase + "x"))

    def test_start_needs_the_flag_and_the_secret(self):
        r = self.nz("term", "start")
        self.assertIn("no passphrase is set", r.stderr + r.stdout)
        self.set_secret()
        self.write(".ops/config.yaml", open(os.path.join(self.root, ".ops", "config.yaml")).read().replace("enabled: true", "enabled: false"))
        r = self.nz("term", "start")
        self.assertIn("terminal.enabled is false", r.stderr + r.stdout)
        r = self.z("term", "status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not running", r.stdout)

    def test_start_status_stop_and_up(self):
        self.set_secret()
        r = self.ok("term", "start")
        self.assertIn("terminal page started", r.stdout)
        self.assertIn("workers only", r.stdout)
        try:
            r = self.ok("term", "status")
            self.assertIn("running", r.stdout)
            self.assertIn("targets workers", r.stdout)
            r = self.ok("term", "url")
            self.assertIn(f"http://127.0.0.1:{self.term_port}/term/", r.stdout)
            self.assertNotIn(PASS, r.stdout)
            r = self.ok("term", "start")
            self.assertIn("already running", r.stdout)
            r = self.ok("up", "desk", "--dry-run", env=dict(self.env))
            self.assertNotIn("terminal", r.stdout, "autostart is off in this world: `up` leaves the terminal page alone")
            self.write(".ops/config.yaml", open(os.path.join(self.root, ".ops", "config.yaml")).read().replace("enabled: true", "enabled: true, autostart: true"))
            r = self.ok("up", "desk", "--dry-run")
            self.assertIn("terminal      running", r.stdout)
            r = self.ok("term", "audit")
            self.assertIn("audit line(s)", r.stdout)
        finally:
            r = self.ok("term", "stop")
        self.assertIn("stopped", r.stdout)
        with open(os.path.join(self.root, ".ops", "state", "term.json")) as fh:
            tj = json.load(fh)
        self.assertIn("secret_hash", tj)
        self.assertNotIn("pid", tj, "the runtime fields are gone, the secret stays")
        r = self.z("term", "status")
        self.assertNotEqual(r.returncode, 0)

    def test_config_rejects_other_targets_and_bad_values(self):
        base = open(os.path.join(self.root, ".ops", "config.yaml")).read().replace(TERM_BLOCK.format(port=self.term_port), "")
        for block, msg in (("terminal: {enabled: true, targets: all}\n", "only `workers`"),
                           ("terminal: {enabled: true, port: 4799}\n", "must differ from panel.port"),
                           ("terminal: {enabled: true, tls: maybe}\n", "terminal.tls"),
                           ("terminal: {enabled: true, idle_close_s: 0}\n", "idle_close_s")):
            self.write(".ops/config.yaml", base + block)
            r = self.nz("config", "--check")
            self.assertIn(msg, r.stderr + r.stdout, block)
        self.write(".ops/config.yaml", base)
        r = self.ok("config", "--json")
        t = json.loads(r.stdout)["terminal"]
        self.assertEqual((t["enabled"], t["port"], t["bind"], t["tls"], t["targets"]), (False, 4800, "loopback", "auto", "workers"))

    def test_the_fake_tmux_still_refuses_send_keys_by_default(self):
        self.ok("up", "desk")
        r = subprocess.run(["tmux", "send-keys", "-t", "=tp-desk:", "x"], env=no_claude_env(self.env), capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("FORBIDDEN", r.stderr)
        # the World's tearDown would fail on that recorded call: scrub it here, the point of this test is the refusal itself
        p = os.path.join(self.state, "calls.log")
        with open(p) as fh:
            lines = [l for l in fh if not l.startswith('["send-keys"')]
        with open(p, "w") as fh:
            fh.writelines(lines)
