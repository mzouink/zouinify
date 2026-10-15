"""T-0181: the terminal page -- a separate process that types into WORKER panes only, after a passphrase unlock.

In-process server on 127.0.0.1 (ephemeral port) over the fake tmux and fake claude: the unlock is required from every
address, loopback included; wrong passphrases lock the address out; targets are the worker sessions only (a role session,
a shell pane, another project's session are refused and audited); keys are an allowlist; every unlock, open, batch,
close and refusal is one audit line; idle and maximum times close attachments and unlocks; `lock` ends everything;
Host/Origin and methods are checked. Then the process side through the CLI: `term secret rotate` refuses inside a Claude
session or without a TTY and prints the passphrase once on a pty; `term start` needs the secret and the flag;
start/status/stop with a real detached process; `up` starts it when autostart is on. The fake tmux records every
send-keys in calls.log and allows it only with ZOUINIFY_FAKE_TMUX_ALLOW_SEND_KEYS=1, which only TermWorld sets.

The server's idle, maximum and lockout times run on its one clock (TermServer.clock, T-0225): TermWorld gives it a fake
clock frozen at setup, so no request in any test can cross a timer by itself however slow the run, and the timing tests
advance that clock by hand instead of sleeping (the world's 2 s / 3 s timers were real seconds before, and a loaded run made
a 400 into a 410)."""
import http.client
import io
import json
import signal
import os
import pty
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World, ZOUINIFY, mark_runner, release_runner  # noqa: E402

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


class FakeClock:
    """A clock the tests move: frozen until advance() is called. It starts at the real time so a stamp taken from it sits where a
    wall-clock one would (the lockout's `get(ip, 0)` sentinel stays in the past; nothing compares it with a wall stamp)."""

    def __init__(self):
        self.t = time.time()

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


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
        self.assertIs(self.srv.clock, time.time, "a server made for real runs on the wall clock")
        self.clock = FakeClock()
        self.srv.clock = self.clock.now             # frozen: no timer expires unless a test advances it
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
        """Sixteen refusals in a row on one attachment: with the frozen clock the attachment cannot go idle between them, however
        slow the run (before T-0225 this test read 410 instead of 400 under load)."""
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
        st, _, _ = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
        self.assertEqual(st, 200, "the attachment (and so the unlock) is still there: no second passed for the server")
        self.assertEqual(self.sent(), [])
        self.assertTrue(all(a["event"] == "refused" and a["reason"].startswith("keys:") for a in self.audit()[-len(bad) - 1:]))

    def test_idle_close_and_unlock_idle(self):
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        tid = d["tid"]
        st, _, _ = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 200)
        self.clock.advance(1.9)                           # idle_close_s: 2 in this world: not yet
        st, _, _ = self.req("GET", f"/term/{tid}/screen", cookie=cookie)   # goes through the attachment: 410 once idle-closed
        self.assertEqual(st, 200)
        self.clock.advance(0.4)                           # 2.3 s since the keystroke: the attachment is idle
        st, _, d = self.req("POST", f"/term/{tid}/keys", {"ops": [{"k": "Enter"}]}, cookie=cookie)
        self.assertEqual(st, 410, d)
        self.assertTrue(any(a["event"] == "close" and a["reason"] == "idle" and a["tid8"] == tid[:8] for a in self.audit()))
        self.assertEqual(len(self.sent()), 1)
        self.clock.advance(3.3)                           # unlock_idle_s: 3 since the last request
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
            h1, st0 = procs.sha256_of(real), os.stat(real)
            with open(real, "wb") as fh:
                fh.write(os.urandom(4096))                        # same size; mtime put back: only ctime moves
            os.utime(real, ns=(st0.st_atime_ns, st0.st_mtime_ns))
            self.assertNotEqual(procs.sha256_of(real), h1, "the hash cache must miss on a rewritten file with its mtime restored")
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

    def test_a_hung_tmux_server_does_not_stall_the_page(self):
        """T-0183 review: a planted socket in tmux-<uid>/ that accepts and never answers must not hold every inspection
        for the 20 s tmux default (times 16 sockets): the request fails closed within the short list-clients timeout with
        503 + Retry-After and nothing sent, the attachment stays, and the listing still answers."""
        import socket as socketmod
        from zouinify import tmux as tmuxmod
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]
        dd = os.path.join(self.tmuxdir, f"tmux-{os.getuid()}")
        os.makedirs(dd)
        s = socketmod.socket(socketmod.AF_UNIX)
        s.bind(os.path.join(dd, "hung"))
        self.addCleanup(s.close)
        marker = os.path.join(self.state, "hang-hung")
        open(marker, "w").close()
        # the clock is frozen (TermWorld), so the 2 s attachment idle cannot expire during the checks below
        with mock.patch.object(tmuxmod, "LIST_CLIENTS_TIMEOUT_S", 0.5), mock.patch.object(tmuxmod, "CLIENTS_BUDGET_S", 1.0):
            t0 = time.monotonic()
            st, hdr, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
            self.assertEqual(st, 503, k)
            self.assertLess(time.monotonic() - t0, 5.0, "the hung server bounded by the short timeout")
            self.assertIn("did not answer", k["error"])
            self.assertEqual(hdr.get("Retry-After"), "2")
            self.assertEqual(self.sent(), [], "nothing typed while a server does not answer")
            st, _, sc = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
            self.assertEqual(st, 503, sc)
            st, _, o = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
            self.assertEqual(st, 503, o)
            st, _, l = self.req("GET", "/term/targets", cookie=cookie)
            self.assertEqual(st, 200, l)
            row = next(r for r in l["targets"] if r["name"] == "t0001-first-ready-ticket")
            self.assertFalse(row["live"])
            self.assertIn("did not answer", row["reason"])
            os.remove(marker)
            st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
            self.assertEqual(st, 200, k)
            self.assertEqual(len(self.sent()), 2, "the attachment survived the unanswered inspection")
        self.assertTrue(any(a["event"] == "refused" and "no answer" in a.get("reason", "") for a in self.audit()))
        self.assertFalse(any(a["event"] == "close" for a in self.audit()), "a server that does not answer closes nothing")

    def test_a_hung_default_tmux_server_fails_the_pane_calls_closed_within_the_short_timeout(self):
        """T-0194: has-session, list-panes, capture-pane and send-keys in the pane inspection get the same short per-call
        timeout as list-clients (they used _run's 20 s default): a default server that accepts and never answers fails the
        request closed on the 503 path with nothing sent and the attachment kept; a send-keys that times out mid-batch is
        audited partial like a refusal and says the op's fate is unknown. The clock is frozen (TermWorld), so no timer
        expires meanwhile; the timeouts themselves are real seconds, made short."""
        from zouinify import tmux as tmuxmod
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        tid = d["tid"]

        def hang(cmd):
            marker = os.path.join(self.state, f"hang-{cmd}")
            open(marker, "w").close()
            return marker

        # 1.5 s, not less: the patched timeout also binds the calls that must answer, and the fake tmux (a Python script)
        # spawns with a heavy tail under load (p99 ~0.9 s beside another suite); the wall bounds follow from it
        t = 1.5
        one = 2 * t + 2.0                                              # one hung call, with slack
        with mock.patch.object(tmuxmod, "PANE_TIMEOUT_S", t, create=True):         # create: on the base the stall itself shows
            # list-panes hangs: the inspection cannot finish -> 503 wherever it is needed, nothing sent, nothing closed
            marker = hang("list-panes")
            t0 = time.monotonic()
            st, hdr, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
            self.assertEqual(st, 503, k)
            self.assertLess(time.monotonic() - t0, one, "bounded by the short timeout, not tmux's 20 s default")
            self.assertIn("did not answer", k["error"])
            self.assertIn(f"list-panes timed out after {t:g} s", k["error"])
            self.assertEqual(hdr.get("Retry-After"), "2")
            self.assertEqual(self.sent(), [], "nothing typed while the server does not answer")
            st, _, sc = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
            self.assertEqual(st, 503, sc)
            st, _, o = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
            self.assertEqual(st, 503, o)
            t0 = time.monotonic()                                     # the listing enumerates with list-panes -a: 503 too, bounded
            st, hdr, l = self.req("GET", "/term/targets", cookie=cookie)
            self.assertEqual(st, 503, l)
            self.assertLess(time.monotonic() - t0, one)
            self.assertEqual(hdr.get("Retry-After"), "2")
            os.remove(marker)
            # has-session hangs: the same path; the listing answers, each row "not checked" within the bound
            marker = hang("has-session")
            t0 = time.monotonic()
            st, hdr, sc = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
            self.assertEqual(st, 503, sc)
            self.assertLess(time.monotonic() - t0, one)
            self.assertIn("has-session timed out", sc["error"])
            self.assertEqual(hdr.get("Retry-After"), "2")
            t0 = time.monotonic()
            st, _, l = self.req("GET", "/term/targets", cookie=cookie)
            self.assertEqual(st, 200, l)
            self.assertLess(time.monotonic() - t0, len(l["targets"]) * t + 3.0, "every row bounded by the short timeout")
            row = next(r for r in l["targets"] if r["name"] == "t0001-first-ready-ticket")
            self.assertFalse(row["live"])
            self.assertIn("has-session timed out", row["reason"])
            os.remove(marker)
            # capture-pane hangs: the inspection passes, the read does not -> 503, the attachment kept
            marker = hang("capture-pane")
            t0 = time.monotonic()
            st, hdr, sc = self.req("GET", f"/term/{tid}/screen", cookie=cookie)
            self.assertEqual(st, 503, sc)
            self.assertLess(time.monotonic() - t0, one)
            self.assertIn("capture-pane timed out", sc["error"])
            self.assertEqual(hdr.get("Retry-After"), "2")
            os.remove(marker)
            # send-keys hangs: the first op times out -> 502 like a refusal mid-batch, audited partial; the op's fate is unknown
            marker = hang("send-keys")
            t0 = time.monotonic()
            st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
            self.assertEqual(st, 502, k)
            self.assertLess(time.monotonic() - t0, one)
            self.assertEqual(k["sent"], 0)
            self.assertIn(f"did not answer within {t:g} s on op 1 of 2", k["error"])
            self.assertIn("whether op 1 arrived is unknown", k["error"])
            last = self.audit()[-1]
            self.assertEqual((last["event"], last["n"], last["m"], last["partial"]), ("keys", 0, 2, True))
            self.assertIn("send-keys timed out", last["reason"])
            self.assertNotIn("text", last)
            os.remove(marker)
            # everything answers again: the attachment survived all four
            st, _, k = self.req("POST", f"/term/{tid}/keys", {"ops": [{"t": "y"}, {"k": "Enter"}]}, cookie=cookie)
            self.assertEqual(st, 200, k)
            self.assertEqual(k["sent"], 2)
        self.assertFalse(any(a["event"] == "close" for a in self.audit()), "a server that does not answer closes nothing")
        self.assertTrue(any(a["event"] == "refused" and "no answer" in a.get("reason", "") for a in self.audit()))

    def test_the_pane_calls_take_a_timeout_and_the_launcher_keeps_the_default(self):
        """T-0194: has_session, panes_of, capture_pane_id and send_keys accept a timeout; without one, _run's 20 s default
        (the launcher's calls) stays; a call that times out raises TmuxError marked timed_out, send_keys's with .sent."""
        from zouinify import tmux as tmuxmod
        self.assertEqual(tmuxmod.DEFAULT_TIMEOUT_S, 20.0)
        self.assertEqual(tmuxmod.PANE_TIMEOUT_S, 2.0)
        cookie, _ = self.unlock()
        st, _, d = self.req("POST", "/term/open", {"target": "t0001-first-ready-ticket"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        pane = d["pane"]
        seen = []
        real_run = tmuxmod.subprocess.run

        def spy(argv, **kw):
            seen.append((argv[1], kw.get("timeout")))
            return real_run(argv, **kw)

        with mock.patch.object(tmuxmod.subprocess, "run", spy):
            self.assertTrue(tmuxmod.has_session("tp-desk"))
            self.assertEqual(len(tmuxmod.panes_of("tp-desk")), 1)
            self.assertTrue(tmuxmod.has_session("tp-desk", timeout=7.0))            # generous: these must answer under load
            self.assertEqual(len(tmuxmod.panes_of("tp-desk", timeout=7.0)), 1)
            self.assertIn("proceed", tmuxmod.capture_pane_id(pane, timeout=7.0))      # the screen the world plants for t0001
            self.assertEqual(len(tmuxmod.list_panes(timeout=7.0)), len(tmuxmod.list_panes()))
            self.assertEqual(tmuxmod.send_keys(pane, [("t", "a")], timeout=7.0), 1)
        self.assertEqual(seen, [("has-session", 20.0), ("list-panes", 20.0), ("has-session", 7.0), ("list-panes", 7.0),
                                ("capture-pane", 7.0), ("list-panes", 7.0), ("list-panes", 20.0), ("send-keys", 7.0)])
        marker = os.path.join(self.state, "hang-has-session")
        open(marker, "w").close()
        t0 = time.monotonic()
        with self.assertRaises(tmuxmod.TmuxError) as cm:
            tmuxmod.has_session("tp-desk", timeout=0.3)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertTrue(getattr(cm.exception, "timed_out", False))
        self.assertEqual(str(cm.exception), "tmux has-session timed out after 0.3 s")
        os.remove(marker)
        marker = os.path.join(self.state, "hang-send-keys")
        open(marker, "w").close()
        with self.assertRaises(tmuxmod.TmuxError) as cm:
            tmuxmod.send_keys(pane, [("t", "a"), ("k", "Enter")], timeout=0.3)
        self.assertEqual((cm.exception.sent, cm.exception.timed_out), (0, True))
        os.remove(marker)

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
    """The process side: `term start` runs a real detached server; tearDown stops whatever is still recorded in term.json, so
    a failing test leaves no server behind (a killed run is tests/conftest.py's job)."""

    def tearDown(self):
        p = os.path.join(self.root, ".ops", "state", "term.json")
        try:
            with open(p, encoding="utf-8") as fh:
                pid = json.load(fh).get("pid")
            if pid:
                os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError, TypeError):
            pass
        super().tearDown()

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

    def test_a_leftover_server_of_a_dead_run_is_reaped_and_a_live_runs_is_not(self):
        """T-0225: a killed pytest leaves its detached server running for hours; tests/conftest.py stops, at the next session
        start, every test server whose world's `.test-runner` is unlocked and names a dead pytest, and leaves a live run's
        alone (the lock its runner holds is the proof of life)."""
        import conftest
        self.set_secret()
        self.ok("term", "start")
        with open(os.path.join(self.root, ".ops", "state", "term.json")) as fh:
            pid = int(json.load(fh)["pid"])
        self.addCleanup(conftest._signal, pid, self.root, signal.SIGTERM)   # on any failure below, the server does not outlive the test
        self.assertTrue(conftest._alive(pid))
        self.assertTrue(conftest.same_server(pid, self.root), "the kill target is re-read from /proc: this user's server for this path")
        self.assertFalse(conftest.same_server(pid, self.root + "x"), "another path: not the same server")
        self.assertFalse(conftest.same_server(os.getpid(), self.root), "another process: not a server")
        self.assertTrue(conftest._signal(pid, self.root, 0), "signal 0 through the gate (a pidfd where there is one): delivered")
        self.assertFalse(conftest._signal(pid, self.root + "x", 0), "the gate refuses another path")
        self.assertFalse(conftest._signal(os.getpid(), self.root, 0), "and a process that is no server")
        sent, opened = [], []                                         # the pidfd branch, with the platform calls faked (this box has none)
        def fake_open(p):
            if p != pid:
                raise ProcessLookupError(p)
            opened.append(os.open(os.devnull, os.O_RDONLY))
            return opened[-1]
        def fake_send(fd, sig, *a):
            sent.append((fd, sig))
        with mock.patch.object(os, "pidfd_open", fake_open, create=True), mock.patch.object(signal, "pidfd_send_signal", fake_send, create=True):
            self.assertTrue(conftest._signal(pid, self.root, 0))
            self.assertEqual(sent, [(opened[0], 0)], "sent through the pidfd opened before the /proc re-read")
            self.assertFalse(conftest._signal(pid, self.root + "x", 0), "another path: nothing sent")
            self.assertFalse(conftest._signal(os.getpid(), self.root, 0), "no pidfd for a process that is no server")
        self.assertEqual(len(sent), 1)
        for fd in opened:
            with self.assertRaises(OSError, msg="every pidfd closed"):
                os.fstat(fd)
        marker = os.path.join(self.tmp, ".test-runner")
        self.assertEqual(open(marker).read().strip(), str(os.getpid()), "the world names the pytest that owns it")
        mine = [row for row in conftest.test_servers() if row[0] == pid]
        self.assertEqual([(v, w) for _, _, w, v, _ in mine], [("keep", os.path.realpath(self.tmp))], "a live runner's server is kept")
        self.assertIn("holds the marker lock", mine[0][4], "this pytest holds the lock: that is the proof, before any pid check")
        reaped = f"reaped a leftover test server from a dead run: pid {pid}"     # the checks name this pid: other sessions' servers may be reaped alongside
        buf = io.StringIO()
        conftest.reap_leftovers(buf)
        self.assertNotIn(reaped, buf.getvalue())
        self.assertTrue(conftest._alive(pid), "left alone")
        # the runner's lock is the proof of life: a marker naming a pid that is dead HERE (a sandboxed runner writes a
        # namespace-local pid) is still a live runner's while the lock is held
        gone = subprocess.Popen(["true"])
        gone.wait()
        with open(marker, "w") as fh:
            fh.write(f"{gone.pid}\n")
        self.assertEqual(conftest.runner_of(self.tmp), ("locked", gone.pid))
        buf = io.StringIO()
        conftest.reap_leftovers(buf)
        self.assertIn(f"kept a zouinify test server: pid {pid}", buf.getvalue())
        self.assertIn("holds the marker lock", buf.getvalue())
        self.assertTrue(conftest._alive(pid), "kept")
        release_runner(self.tmp)                                      # the runner died: its lock went with it
        self.assertEqual(conftest.runner_of(self.tmp), ("dead", gone.pid), "now provably dead: lock free and no such pid")
        mark_runner(self.tmp)                                         # back to a live runner for the cases below
        self.assertEqual(conftest.runner_of(self.tmp), ("locked", os.getpid()))
        # no marker at all (test code older than the marker, maybe another session's run): kept, and said so -- never reaped
        os.unlink(marker)
        release_runner(self.tmp)
        mine = [row for row in conftest.test_servers() if row[0] == pid]
        self.assertEqual([v for _, _, _, v, _ in mine], ["keep"])
        self.assertIn("no readable runner marker", mine[0][4])
        buf = io.StringIO()
        conftest.reap_leftovers(buf)
        self.assertIn(f"kept a zouinify test server: pid {pid}", buf.getvalue())
        self.assertNotIn(reaped, buf.getvalue())
        self.assertTrue(conftest._alive(pid))
        self.assertTrue(os.path.isdir(self.root), "the world is untouched")
        for garbled in ("not a pid\n", "-1\n", "0\n", "100000000000000000000\n"):   # unreadable or impossible: kept too, no error
            with open(marker, "w") as fh:
                fh.write(garbled)
            self.assertEqual(conftest.runner_of(self.tmp), ("none", None), garbled)
            buf = io.StringIO()
            conftest.reap_leftovers(buf)
            self.assertIn(f"kept a zouinify test server: pid {pid}", buf.getvalue(), garbled)
            self.assertTrue(conftest._alive(pid), garbled)
        with open(marker, "w") as fh:                                 # a dead pid and no lock: the runner died
            fh.write(f"{gone.pid}\n")
        self.assertEqual([p for p, _, _ in conftest.leftover_test_servers() if p == pid], [pid], "its runner is provably dead: a leftover")
        buf = io.StringIO()
        self.assertGreaterEqual(conftest.reap_leftovers(buf), 1)
        self.assertIn(reaped, buf.getvalue())
        self.assertFalse(conftest._alive(pid), "stopped")
        self.assertFalse(os.path.exists(self.tmp), "and the world directory is gone")
        os.makedirs(self.root, exist_ok=True)                         # tearDown wants a tree to remove

    def test_the_reaper_refuses_any_project_path_that_is_not_a_plain_test_world(self):
        """T-0225 review: the path that gates an rm -rf is refused on the given string (`..`, a symlinked world) and acted on only
        through its real path, which must be exactly one zouinify-(test|init)-* directory directly under the temp dir; anything else
        is kept, never resolved into something deletable."""
        import conftest
        tmp = conftest.TMP
        world = os.path.realpath(self.tmp)
        self.assertEqual(conftest.world_of(self.root), (world, ""), "this test's own world: real, one level under the temp dir")
        self.assertEqual(conftest.world_of(os.path.join(self.tmp, "other"))[0], world, "any project name inside the world")
        real_lstat = os.lstat
        def foreign(path, *a, **kw):                                  # the world and the marker as another user's
            st = real_lstat(path, *a, **kw)
            if os.fspath(path) in (world, self.tmp, os.path.join(world, ".test-runner")):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.getuid() + 1, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return st
        with mock.patch.object(os, "lstat", foreign):
            self.assertEqual(conftest.world_of(self.root), (None, "the world belongs to another user"), "a world planted by another user in a shared temp dir")
        def foreign_fstat(fd):
            st = os.stat(fd)                                          # the real fstat, with the uid swapped
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.getuid() + 1, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        with mock.patch.object(os, "fstat", foreign_fstat):
            self.assertEqual(conftest.runner_of(self.tmp), ("foreign", None), "another user's marker: not read, not a reason to reap")
        self.assertEqual(conftest.world_of(self.root), (world, ""), "and this user's again without the patch")
        init = tempfile.mkdtemp(prefix="zouinify-init-", dir=tmp)
        self.addCleanup(os.rmdir, init)
        self.assertEqual(conftest.world_of(f"{init}/proj"), (init, ""), "an init world too")
        other = os.path.join(os.path.dirname(tmp.rstrip("/")) or "/", "another-tmp")   # a sibling of the temp dir
        for bad, reason in ((f"{tmp}/zouinify-test-x/../../home/someone/proj", "`..`"),
                            (f"{tmp}/zouinify-test-x/../{os.path.basename(self.tmp)}/proj", "`..`"),      # resolves to a real world: still refused
                            (f"{tmp}/zouinify-test-X", f"exactly one directory under {tmp}"),             # the bare world: no child to be the project
                            (f"{tmp}/zouinify-test-X/deeper/proj", f"exactly one directory under {tmp}"),
                            (f"{other}/zouinify-test-x/proj", f"exactly one directory under {tmp}"),      # under another temp dir
                            (f"{tmp}/other-dir/proj", "not a zouinify test world name"),
                            (f"{tmp}//zouinify-test-x/proj", "redundant separators"),
                            ("zouinify-test-x/proj", "not an absolute path"), ("", "not an absolute path")):
            world, why = conftest.world_of(bad)
            self.assertIsNone(world, bad)
            self.assertIn(reason, why, bad)
        link = os.path.join(tmp, "zouinify-test-link" + os.path.basename(self.tmp)[-6:])
        os.symlink(self.tmp, link)
        self.addCleanup(os.unlink, link)
        world, why = conftest.world_of(f"{link}/proj")
        self.assertIsNone(world, "a symlinked world is refused even though it resolves to a real one")
        self.assertIn("symlink", why)
        self.assertEqual(conftest.world_of(f"{tmp}/zouinify-test-nosuchworld-{os.getpid()}/proj"), (None, "the world is not a plain directory"))
        self.assertTrue(os.path.isdir(self.root), "nothing was deleted along the way")

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
