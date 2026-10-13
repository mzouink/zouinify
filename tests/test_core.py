"""Tests for the zouinify core CLI (T-0159): config loader, `up`, `launch`, `status`, `down`, `briefs`.

Every test builds a throwaway project in a tmp dir (a `.ops/config.yaml`, briefs, ten tickets, a
fake `scripts/ticket.sh`) and puts `tests/fakes/` first on PATH, so a fake `tmux` and a fake `claude`
record what the CLI runs. No real tmux session, Claude session or ticket is ever touched.

The CLI under test is $ZOUINIFY_UNDER_TEST (default: this checkout's bin/zouinify), so the same suite
gives the RED evidence against the base commit:

    git -C <repo> archive f19a01c | tar -x -C /tmp/base
    ZOUINIFY_UNDER_TEST=/tmp/base/bin/zouinify python3 -m pytest -q tests/test_core.py
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.path.dirname(HERE)
ZOUINIFY = os.environ.get("ZOUINIFY_UNDER_TEST") or os.path.join(REPO, "bin", "zouinify")
FAKES = os.path.join(HERE, "fakes")

TICKET = """---
id: {id}
title: {title}
type: chore
severity: {sev}
status: {status}
repos: [backend]
areas: []
affects: []
deploy: none
owner: {owner}
worktree:
branch:
base:
claimed:
created: 2026-10-13
depends_on: []
siblings: []
blocked_on:
human:
closed_by: []
plan:
resolution:
---

## Problem
x

## Fix
- [ ] x

## Log
- 2026-10-13 00:00 tester: filed.
"""

# id, slug, status, severity, owner
TICKETS = [
    ("T-0001", "first-ready-ticket", "ready", "P2", ""),
    ("T-0002", "claimed-ticket", "in-dev", "P2", "someone"),
    ("T-0003", "finished-ticket", "done", "P3", ""),
    ("T-0004", "dropped-ticket", "dropped", "P3", ""),
    ("T-0005", "second-ready-p0", "ready", "P0", ""),
    ("T-0006", "blocked-one", "blocked", "P1", ""),
    ("T-0007", "ready-but-owned", "ready", "P2", "ghost"),
    ("T-0008", "in-review-one", "in-review", "P2", "w8"),
    ("T-0009", "rtm-one", "ready-to-merge", "P2", "w9"),
    ("T-0010", "deployed-one", "deployed", "P2", ""),
    ("T-0011", "deployed-p0-regression", "deployed", "P0", ""),   # open P0 on the board, nothing for the human to do
]

# the default human inbox of the world (what `ticket.sh human --json` prints): 4 items, exactly one P0
DEFAULT_HUMAN = [
    {"verb": "RUN", "tickets": "T-0005", "priority": "P0", "title": "second ready p0", "since": "2026-10-13 06:00",
     "command": "cd /x && ./fix.sh", "expect": "ok", "if_nothing": "stays broken", "note": "asked by tp-desk"},
    {"verb": "DECIDE", "tickets": "T-0006", "priority": "P1", "title": "blocked one", "since": "2026-10-12 15:00",
     "command": "reply \"T-0006 keep\" or \"T-0006 drop\"", "expect": "T-0006 keep", "if_nothing": "stays blocked", "note": ""},
    {"verb": "CHECK", "tickets": "T-0010", "priority": "P2", "title": "deployed one", "since": "2026-10-13 01:00",
     "command": "open the page", "expect": "it renders", "if_nothing": "unverified", "note": ""},
    {"verb": "SSO", "tickets": "", "priority": "P3", "title": "refresh credentials", "since": "2026-10-13 07:00",
     "command": "aws sso login", "expect": "Successfully logged in", "if_nothing": "checks block", "note": ""},
]

CONFIG = """version: 1
project: {{name: testproj, short: tp}}
repos:
  - {{name: backend, path: ., trunk: dev, prod: main, default: true}}
tickets:
  dir: tickets
  worktrees: .claude/worktrees
  deploy_kinds: [none]
  deploy_plugin: none
  ask_verbs: [RUN, DECIDE, CHECK]
human_only:
  - deploy to production
  - any cloud write against the production account
sittings: {{tz: America/New_York, times: ["09:30", "16:00"], freeze_before_min: 10, push_before_min: 10}}
budget: {{paid_usd_per_day: 5, approval_text: "T-{{id}} budget ok"}}
model: fable
sessions:
  desk:         {{name: tp-desk,    tmux: tp-desk,    model: fable, {desk_pm}brief: .ops/briefs/desk.md,         always: true}}
  manager:      {{name: tp-manager, tmux: tp-manager, model: fable, permission_mode: auto, brief: .ops/briefs/manager.md,      always: true}}
  debug:        {{name: tp-debug,   tmux: tp-debug,   model: fable, permission_mode: auto, brief: .ops/briefs/debug.md,        always: true}}
  chrome_debug: {{name: tp-chrome,  tmux: tp-chrome,  model: fable, permission_mode: auto, brief: .ops/briefs/chrome_debug.md, flags: [--chrome], optional: true}}
  worker:       {{name: "t{{num}}-{{slug}}", tmux: "t{{num}}-{{slug}}", model: fable, {worker_pm}
                 prompt: "Read tickets/WORKER_BRIEF.md, then claim {{id}}."}}
panel: {{port: 4799, bind: loopback, state_dir: .ops/state, refresh_s: 30, autostart: false}}
aws: {{profile: default, sso_cache: {sso}, assumed_token_hours: 2}}
contact: {{human: tester}}
"""


def _chmod_x(path):
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class World(unittest.TestCase):
    """A throwaway project with fakes on PATH; `self.z(...)` runs the CLI under test in it."""

    desk_pm = "permission_mode: auto, "
    worker_pm = "permission_mode: auto,"

    def setUp(self):
        self.assertTrue(os.path.isfile(ZOUINIFY) and os.access(ZOUINIFY, os.X_OK),
                        f"no executable CLI at {ZOUINIFY}")
        self.tmp = tempfile.mkdtemp(prefix="zouinify-test-")
        self.root = os.path.join(self.tmp, "proj")
        self.sso = os.path.join(self.tmp, "sso")
        self.home = os.path.join(self.tmp, "home")
        for d in (".ops/briefs", "tickets", "scripts"):
            os.makedirs(os.path.join(self.root, d))
        os.makedirs(self.sso)
        os.makedirs(os.path.join(self.home, ".aws"))
        self.write(".ops/config.yaml", CONFIG.format(desk_pm=self.desk_pm, worker_pm=self.worker_pm, sso=self.sso))
        for role in ("desk", "manager", "debug", "chrome_debug"):
            self.write(f".ops/briefs/{role}.md", f"# {role} brief for testproj\nYou are the {role}. Never deploy.\n")
        self.write("tickets/WORKER_BRIEF.md", "# Worker brief\nclaim your ticket.\n")
        for tid, slug, status, sev, owner in TICKETS:
            self.write(f"tickets/{tid}-{slug}.md", TICKET.format(id=tid, title=slug.replace("-", " "), sev=sev, status=status, owner=owner))
        shutil.copy(os.path.join(FAKES, "ticket.sh"), os.path.join(self.root, "scripts", "ticket.sh"))
        _chmod_x(os.path.join(self.root, "scripts", "ticket.sh"))
        for f in ("tmux", "claude"):
            _chmod_x(os.path.join(FAKES, f))
        self.state = os.path.join(self.tmp, "tmux-state")
        os.makedirs(self.state)
        self.ticket_log = os.path.join(self.tmp, "ticket-calls.log")
        self.human_json = os.path.join(self.tmp, "human.json")
        with open(self.human_json, "w") as fh:
            json.dump(DEFAULT_HUMAN, fh)
        env = dict(os.environ)
        env.pop("ZOUINIFY_PROJECT", None)
        env.update(PATH=FAKES + os.pathsep + env.get("PATH", ""), FAKE_TMUX_STATE=self.state,
                   FAKE_TICKET_LOG=self.ticket_log, FAKE_HUMAN_COUNT="4", FAKE_HUMAN_JSON=self.human_json, TICKET_AS="tester-session",
                   HOME=self.home, ZOUINIFY_NOW="2026-10-13T07:00:00")
        self.env = env

    def tearDown(self):
        calls = self.tmux_calls()
        self.assertFalse([c for c in calls if c and c[0] == "send-keys"], "the CLI must never run tmux send-keys")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------------------------
    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)

    def z(self, *args, env=None, cwd=None):
        return subprocess.run([ZOUINIFY, *args], cwd=cwd or self.root, env=env or self.env,
                              capture_output=True, text=True, timeout=120)

    def ok(self, *args, **kw):
        r = self.z(*args, **kw)
        self.assertEqual(r.returncode, 0, f"{args}: rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def nz(self, *args, **kw):
        """Run the CLI and require a non-zero exit. (Never name a helper `fail`: it shadows TestCase.fail, which
        assertIn/assertNotIn/assertIsNone/assertLess/assertMultiLineEqual and friends call on failure, so their
        failures became a CLI run that "failed as expected" -- the suite was green that way from T-0159 to T-0170.)"""
        r = self.z(*args, **kw)
        self.assertNotEqual(r.returncode, 0, f"{args} should exit non-zero\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def tmux_calls(self):
        p = os.path.join(self.state, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def new_sessions(self):
        return [c for c in self.tmux_calls() if c[0] == "new-session"]

    def kills(self):
        return [c for c in self.tmux_calls() if c[0] == "kill-session"]

    def sessions(self):
        p = os.path.join(self.state, "sessions.json")
        if not os.path.exists(p):
            return {}
        with open(p) as fh:
            return json.load(fh)

    def registry(self):
        p = os.path.join(self.root, ".ops", "state", "sessions.json")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return json.load(fh)

    def ticket_calls(self):
        if not os.path.exists(self.ticket_log):
            return []
        with open(self.ticket_log) as fh:
            return [l.rstrip("\n") for l in fh if l.strip()]

    def set_status(self, tid, status):
        f = [x for x in os.listdir(os.path.join(self.root, "tickets")) if x.startswith(tid + "-")][0]
        p = os.path.join(self.root, "tickets", f)
        with open(p) as fh:
            text = fh.read()
        text = re.sub(r"^status: .*$", f"status: {status}", text, count=1, flags=re.M)
        with open(p, "w") as fh:
            fh.write(text)

    @staticmethod
    def argv_of(call):
        """The command part of a recorded new-session call (after the tmux options)."""
        i = 1
        while i < len(call):
            if call[i] == "-d":
                i += 1
            elif call[i] in ("-s", "-c", "-e"):
                i += 2
            else:
                break
        return call[i:]

    @staticmethod
    def opts_of(call):
        out = {"-e": []}
        i = 1
        while i < len(call):
            if call[i] == "-d":
                i += 1
            elif call[i] == "-e":
                out["-e"].append(call[i + 1])
                i += 2
            elif call[i] in ("-s", "-c"):
                out[call[i]] = call[i + 1]
                i += 2
            else:
                break
        return out


class TestEntryAndConfig(World):
    def test_version_and_help(self):
        r = self.ok("version")
        self.assertRegex(r.stdout.strip(), r"^\d+\.\d+\.\d+")
        r = self.z()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("up", r.stdout + r.stderr)

    def test_config_check_ok(self):
        r = self.ok("config", "--check")
        self.assertIn("ok", r.stdout)
        self.assertIn("testproj", r.stdout)

    def test_config_check_from_subdir_and_env(self):
        sub = os.path.join(self.root, "tickets")
        self.ok("config", "--check", cwd=sub)
        env = dict(self.env, ZOUINIFY_PROJECT=self.root)
        self.ok("config", "--check", cwd=self.tmp, env=env)
        r = self.nz("config", "--check", cwd=self.tmp)
        self.assertIn("config.yaml", r.stderr)

    def test_config_json_is_normalized(self):
        r = self.ok("config", "--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["project"]["name"], "testproj")
        self.assertEqual(d["sessions"]["chrome_debug"]["flags"], ["--chrome"])
        self.assertEqual(d["sessions"]["desk"]["permission_mode"], "auto")
        self.assertEqual(d["panel"]["bind"], "loopback")

    def test_config_check_rejects_broken_files(self):
        self.write(".ops/config.yaml", "version: 2\nproject: {name: x, short: x}\nsessions: {}\n")
        r = self.nz("config", "--check")
        self.assertIn("version", r.stderr)
        self.assertIn("sessions", r.stderr)
        self.write(".ops/config.yaml", CONFIG.format(desk_pm="", worker_pm="", sso=self.sso).replace("desk:", "dsk:", 1))
        r = self.nz("config", "--check")
        self.assertIn("desk", r.stderr)
        self.write(".ops/config.yaml", CONFIG.format(desk_pm="", worker_pm="", sso=self.sso).replace("tmux: tp-manager", "tmux: tp-desk"))
        r = self.nz("config", "--check")
        self.assertIn("tp-desk", r.stderr)


class TestUp(World):
    def test_up_twice_launches_once(self):
        r = self.ok("up")
        self.assertEqual(sorted(s for s in self.sessions()), ["tp-debug", "tp-desk", "tp-manager"])
        self.assertEqual(len(self.new_sessions()), 3)
        self.assertIn("launched", r.stdout)
        r = self.ok("up")
        self.assertEqual(len(self.new_sessions()), 3, "second `up` must launch nothing")
        self.assertEqual(r.stdout.count("attached"), 3)
        self.assertNotIn("launched", r.stdout)

    def test_up_launch_line_flags(self):
        self.ok("up")
        for call in self.new_sessions():
            o, a = self.opts_of(call), self.argv_of(call)
            name = o["-s"]
            self.assertEqual(o["-c"], self.root)
            self.assertIn(f"TICKET_AS={name}", o["-e"])
            self.assertIn(f"ZOUINIFY_PROJECT={self.root}", o["-e"])
            self.assertEqual(a[0], "claude")
            self.assertEqual(a[1:3], ["--model", "fable"])
            self.assertEqual(a[a.index("-n") + 1], name)
            self.assertEqual(a[a.index("--permission-mode") + 1], "auto")
            role = {"tp-desk": "desk", "tp-manager": "manager", "tp-debug": "debug"}[name]
            self.assertTrue(a[-1].startswith(f"# {role} brief for testproj"), a[-1][:60])
            self.assertEqual(a[-2], "--", "the prompt follows `--` so a brief starting with '-' is not an option")
        # the fake status bar shows the model derived from --model: `up` saw it
        screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", "tp-desk"], env=self.env, capture_output=True, text=True).stdout
        self.assertIn("[Fable 5.1]", screen)

    def test_up_reports_status_from_claude_agents(self):
        agents = os.path.join(self.tmp, "agents.json")
        with open(agents, "w") as fh:
            json.dump([{"name": "tp-desk", "cwd": self.root, "status": "busy", "pid": 1, "kind": "interactive"}], fh)
        self.ok("up")
        r = self.ok("up", env=dict(self.env, FAKE_CLAUDE_AGENTS=agents))
        line = [l for l in r.stdout.splitlines() if "tp-desk" in l][0]
        self.assertIn("attached", line)
        self.assertIn("busy", line)
        self.assertIn("Fable 5.1", line)

    def test_up_dry_run_launches_nothing_and_prints_exact_lines(self):
        r = self.ok("up", "--dry-run")
        self.assertEqual(self.new_sessions(), [])
        self.assertEqual(self.registry(), [])
        self.assertIn("tmux new-session -d -s tp-desk -c " + self.root, r.stdout)
        self.assertIn("-e TICKET_AS=tp-desk", r.stdout)
        self.assertIn("claude --model fable -n tp-desk --permission-mode auto", r.stdout)
        self.assertIn(os.path.join(self.root, ".ops/briefs/desk.md"), r.stdout)

    def test_up_registry(self):
        self.ok("up")
        reg = self.registry()
        self.assertEqual(sorted(e["name"] for e in reg), ["tp-debug", "tp-desk", "tp-manager"])
        desk = [e for e in reg if e["name"] == "tp-desk"][0]
        self.assertEqual(desk["role"], "desk")
        self.assertEqual(desk["tmux"], "tp-desk")
        self.assertEqual(desk["by"], "tester-session")
        self.assertIsNone(desk["ticket"])
        self.assertRegex(desk["launched_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_up_optional_role_only_when_named(self):
        self.ok("up")
        self.assertNotIn("tp-chrome", self.sessions())
        self.ok("up", "chrome_debug")
        self.assertIn("tp-chrome", self.sessions())
        call = [c for c in self.new_sessions() if self.opts_of(c)["-s"] == "tp-chrome"][0]
        a = self.argv_of(call)
        self.assertIn("--chrome", a)
        self.assertTrue(a[-1].startswith("# chrome_debug brief"))

    def test_up_refuses_unknown_role_and_worker(self):
        r = self.nz("up", "nosuchrole")
        self.assertIn("nosuchrole", r.stderr)
        r = self.nz("up", "worker")
        self.assertIn("launch worker", r.stderr)
        self.assertEqual(self.new_sessions(), [])

    def test_up_fails_loudly_on_wrong_model(self):
        self.ok("up", "desk")
        with open(os.path.join(self.state, "screen-tp-manager.txt"), "w") as fh:
            fh.write("> hi\n   [Sonnet 5.5] x | dev\n")
        r = self.nz("up", "manager", "--wait", "1")
        self.assertIn("Sonnet 5.5", r.stdout + r.stderr)
        self.assertIn("fable", (r.stdout + r.stderr).lower())

    def test_up_without_tmux_env_flag_support(self):
        # the fake accepts -e; this only pins the order: options first, then `claude ...`
        self.ok("up", "desk")
        call = self.new_sessions()[0]
        self.assertEqual(call[:5], ["new-session", "-d", "-s", "tp-desk", "-c"])


class TestPermissionModeIsConfig(World):
    desk_pm = ""        # no permission_mode on the desk ...
    worker_pm = ""      # ... nor on the worker: T-0150 is the human's open decision

    def test_no_permission_mode_flag_when_unset(self):
        self.ok("up", "desk")
        a = self.argv_of(self.new_sessions()[0])
        self.assertNotIn("--permission-mode", a)
        self.assertNotIn("auto", a)
        self.ok("launch", "worker", "T-0001")
        a = self.argv_of(self.new_sessions()[1])
        self.assertNotIn("--permission-mode", a)
        self.assertEqual(a[1:3], ["--model", "fable"])


class TestLaunchWorker(World):
    def test_launch_worker_ok(self):
        r = self.ok("launch", "worker", "T-0001")
        self.assertIn("t0001-first-ready-ticket", self.sessions())
        call = self.new_sessions()[0]
        o, a = self.opts_of(call), self.argv_of(call)
        self.assertEqual(o["-s"], "t0001-first-ready-ticket")
        self.assertEqual(o["-c"], self.root)
        self.assertIn("TICKET_AS=t0001-first-ready-ticket", o["-e"])
        self.assertEqual(a[:3], ["claude", "--model", "fable"])
        self.assertEqual(a[a.index("-n") + 1], "t0001-first-ready-ticket")
        self.assertEqual(a[a.index("--permission-mode") + 1], "auto")
        self.assertEqual(a[-1], "Read tickets/WORKER_BRIEF.md, then claim T-0001.")
        self.assertIn("Fable 5.1", r.stdout)
        reg = self.registry()
        self.assertEqual(reg[0]["ticket"], "T-0001")
        self.assertEqual(reg[0]["role"], "worker")
        # the worker claims the ticket itself: the launcher never ran a mutating ticket.sh command
        self.assertFalse([c for c in self.ticket_calls() if c.split()[0] in ("claim", "move", "set", "new", "ask", "put")])

    def test_launch_worker_refusals(self):
        cases = {
            "T-0006": "blocked",             # not ready
            "T-0002": "in-dev",              # claimed
            "T-0007": "ghost",               # ready but owner set
            "T-0999": "no ticket",           # unknown
            "T-01": "T-xxxx",                # malformed id
        }
        for tid, needle in cases.items():
            r = self.nz("launch", "worker", tid)
            self.assertIn(needle, r.stderr, f"{tid}: {r.stderr}")
        self.assertEqual(self.new_sessions(), [])
        self.assertEqual(self.registry(), [])

    def test_launch_worker_freeze(self):
        self.write("tickets/FREEZE", "# hotfix only\nT-0005\n")
        r = self.nz("launch", "worker", "T-0001")
        self.assertIn("FREEZE", r.stderr)
        self.assertIn("T-0005", r.stderr)
        self.assertEqual(self.new_sessions(), [])
        self.ok("launch", "worker", "T-0005")
        self.assertIn("t0005-second-ready-p0", self.sessions())

    def test_launch_worker_duplicate_name(self):
        self.ok("launch", "worker", "T-0001")
        r = self.nz("launch", "worker", "T-0001")
        self.assertIn("already", r.stderr)
        self.assertEqual(len(self.new_sessions()), 1)

    def test_launch_worker_dry_run(self):
        r = self.ok("launch", "worker", "T-0001", "--dry-run")
        self.assertEqual(self.new_sessions(), [])
        self.assertIn("tmux new-session -d -s t0001-first-ready-ticket", r.stdout)
        self.assertIn("'Read tickets/WORKER_BRIEF.md, then claim T-0001.'", r.stdout)

    def test_launch_worker_receipt_logs_on_ticket(self):
        r = self.ok("launch", "worker", "T-0001", "--receipt", "a1b2c3")
        logs = [c for c in self.ticket_calls() if c.startswith("log T-0001 ")]
        self.assertEqual(len(logs), 1, self.ticket_calls())
        self.assertIn("launched t0001-first-ready-ticket (panel receipt a1b2c3)", logs[0])
        with open(os.path.join(self.root, "tickets", "T-0001-first-ready-ticket.md")) as fh:
            self.assertIn("tester-session: launched t0001-first-ready-ticket (panel receipt a1b2c3)", fh.read())
        self.assertIn("a1b2c3", r.stdout)

    def test_launch_worker_receipt_needs_ticket_as(self):
        env = dict(self.env)
        env.pop("TICKET_AS")
        r = self.nz("launch", "worker", "T-0001", "--receipt", "zz", env=env)
        self.assertIn("TICKET_AS", r.stderr)
        self.assertEqual(self.new_sessions(), [], "refuse before launching")

    def test_launch_role_sessions(self):
        self.ok("launch", "chrome_debug")
        a = self.argv_of(self.new_sessions()[0])
        self.assertIn("--chrome", a)
        self.assertEqual(a[a.index("-n") + 1], "tp-chrome")
        r = self.ok("launch", "chrome_debug")
        self.assertIn("attached", r.stdout)
        self.assertEqual(len(self.new_sessions()), 1)
        r = self.nz("launch", "worker")
        self.assertIn("T-xxxx", r.stderr)


class TestDown(World):
    def setUp(self):
        super().setUp()
        self.ok("up")
        self.ok("launch", "worker", "T-0001")
        self.ok("launch", "worker", "T-0005")

    def test_down_dry_run_kills_nothing(self):
        self.set_status("T-0001", "done")
        r = self.ok("down", "--dry-run")
        self.assertEqual(self.kills(), [])
        self.assertIn("t0001-first-ready-ticket", r.stdout)
        self.assertIn("would kill", r.stdout)
        self.assertIn("t0001-first-ready-ticket", self.sessions())

    def test_down_kills_only_done_or_dropped_workers(self):
        self.set_status("T-0001", "done")
        self.set_status("T-0005", "dropped")
        r = self.ok("down")
        killed = sorted(c[2].lstrip("=") for c in self.kills())
        self.assertEqual(killed, ["t0001-first-ready-ticket", "t0005-second-ready-p0"])
        self.assertEqual(sorted(self.sessions()), ["tp-debug", "tp-desk", "tp-manager"])
        self.assertEqual(sorted(e["name"] for e in self.registry()), ["tp-debug", "tp-desk", "tp-manager"])
        self.assertIn("killed", r.stdout)

    def test_down_refuses_open_ticket_role_and_unregistered(self):
        r = self.nz("down", "t0001-first-ready-ticket")       # T-0001 is still ready
        self.assertIn("ready", r.stdout + r.stderr)
        r = self.nz("down", "tp-desk")
        self.assertIn("role", r.stdout + r.stderr)
        r = self.nz("down", "some-foreign-session")
        self.assertIn("not launcher-registered", r.stdout + r.stderr)
        self.assertEqual(self.kills(), [])
        self.assertEqual(len(self.sessions()), 5)

    def test_down_without_names_reports_refusals_and_exits_zero(self):
        r = self.ok("down")
        self.assertEqual(self.kills(), [])
        self.assertIn("refused", r.stdout)
        self.assertIn("nothing to kill", r.stdout)


class TestHarness(World):
    """T-0170: nothing the World adds may shadow a unittest.TestCase attribute, and assertions must really fail."""

    def test_no_helper_shadows_testcase(self):
        inherited = set(dir(unittest.TestCase)) - {"setUp", "tearDown", "setUpClass", "tearDownClass", "__doc__", "__module__"}
        classes, todo = [], [World]
        while todo:                                   # every descendant, not only the direct subclasses
            cls = todo.pop()
            classes.append(cls)
            todo.extend(cls.__subclasses__())
        self.assertGreater(len(classes), 1)
        for cls in classes:
            shadowed = sorted(set(vars(cls)) & inherited)
            self.assertFalse(shadowed, f"{cls.__name__} redefines unittest.TestCase attribute(s) {shadowed}")
        self.assertIs(type(self).fail, unittest.TestCase.fail)

    def test_assertions_really_fail(self):
        for check in (lambda: self.assertIn("a", "b"), lambda: self.assertNotIn("a", "a"), lambda: self.assertEqual("x", "y"),
                      lambda: self.assertIsNone(1), lambda: self.assertLess(2, 1), lambda: self.assertDictEqual({}, {"k": 1})):
            with self.assertRaises(AssertionError):
                check()


class TestStatus(World):
    LINE_RE = re.compile(r"^inbox (\d+) \| P0 (\d+) \| creds ([^|]+?) \| sitting (\d\d:\d\d) in ([^|]+?) \| (\d+) dev (\d+) rev (\d+) rtd (\d+) dep$")

    def test_status_line(self):
        r = self.ok("status", "--line")
        line = r.stdout.strip().splitlines()[-1]
        m = self.LINE_RE.match(line)
        self.assertTrue(m, line)
        self.assertEqual(m.group(1), "4")            # the 4 items of human --json
        self.assertEqual(m.group(2), "1")            # one P0 item in the inbox (T-0011, a deployed P0, asks nothing: not counted)
        self.assertEqual(m.group(3), "none")         # no sso cache, no credentials file
        self.assertEqual(m.group(4), "09:30")        # ZOUINIFY_NOW is 07:00 ET
        self.assertEqual(m.group(5), "2h30")
        self.assertEqual(m.group(6), "1")            # T-0002 in-dev
        self.assertEqual(m.group(7), "1")            # T-0008 in-review
        self.assertEqual(m.group(8), "1")            # T-0009 ready-to-merge
        self.assertEqual(m.group(9), "2")            # T-0010 and T-0011 deployed

    def test_status_creds_sources(self):
        with open(os.path.join(self.sso, "abc.json"), "w") as fh:
            json.dump({"expiresAt": "2026-10-13T12:30:00Z", "accessToken": "SECRET-must-not-print"}, fh)
        r = self.ok("status", "--line", "--no-cache")
        m = self.LINE_RE.match(r.stdout.strip().splitlines()[-1])
        self.assertEqual(m.group(3), "1h30")         # 11:00Z now (07:00 ET) -> 12:30Z
        self.assertNotIn("SECRET", r.stdout)
        os.remove(os.path.join(self.sso, "abc.json"))
        cred = os.path.join(self.home, ".aws", "credentials")
        with open(cred, "w") as fh:
            fh.write("[default]\naws_access_key_id = AKIAFAKE\naws_secret_access_key = topsecret\n")
        t = time.time() - 45 * 60
        os.utime(cred, (t, t))
        env = dict(self.env)
        env.pop("ZOUINIFY_NOW")
        r = self.ok("status", "--line", "--no-cache", env=env)
        m = self.LINE_RE.match(r.stdout.strip().splitlines()[-1])
        self.assertRegex(m.group(3), r"^1h1[45] est$")
        self.assertNotIn("topsecret", r.stdout)

    def test_status_cache_30s(self):
        self.ok("status", "--line")
        n = len([c for c in self.ticket_calls() if c == "human --json"])
        self.assertEqual(n, 1)
        self.ok("status", "--line")
        self.assertEqual(len([c for c in self.ticket_calls() if c == "human --json"]), 1, "cached")
        self.ok("status", "--line", "--no-cache")
        self.assertEqual(len([c for c in self.ticket_calls() if c == "human --json"]), 2)

    def test_status_json_and_long(self):
        r = self.ok("status", "--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["inbox"], 4)
        self.assertEqual(d["p0"], 1)                 # inbox P0 items
        self.assertEqual(d["p0_open_tickets"], 2)    # T-0005 (ready) + T-0011 (deployed) on the board
        self.assertEqual(d["counts"]["in-dev"], 1)
        self.assertEqual(d["sitting"]["at"], "09:30")
        r = self.ok("status")
        self.assertIn("inbox", r.stdout)
        self.assertIn("09:30", r.stdout)


class TestBriefs(World):
    def test_briefs_render(self):
        out = os.path.join(self.tmp, "briefs-out")
        r = self.ok("briefs", "render", "--out", out)
        files = sorted(os.listdir(out))
        self.assertEqual(files, ["chrome_debug.md", "debug.md", "desk.md", "manager.md", "worker.md"])
        with open(os.path.join(out, "desk.md")) as fh:
            desk = fh.read()
        self.assertIn("tp-desk", desk)
        self.assertIn("deploy to production", desk)
        self.assertIn("09:30", desk)
        self.assertNotIn("{{", desk)
        for f in files:
            with open(os.path.join(out, f)) as fh:
                text = fh.read()
            self.assertNotIn("{{", text, f)
            self.assertIn("tp-desk", text, f)
        # refuses to overwrite a changed file without --force
        with open(os.path.join(out, "desk.md"), "a") as fh:
            fh.write("\nlocal edit\n")
        r = self.nz("briefs", "render", "--out", out)
        self.assertIn("desk.md", r.stderr)
        self.ok("briefs", "render", "--out", out, "--force")

    def test_briefs_list(self):
        r = self.ok("briefs", "list")
        self.assertIn("desk", r.stdout)
        self.assertIn("worker", r.stdout)


class TestPassthrough(World):
    def test_board_and_human_passthrough(self):
        r = self.ok("board")
        self.assertIn("fake board", r.stdout)
        r = self.ok("human", "--count")
        self.assertEqual(r.stdout.strip(), "4")


class TestReviewFixes(World):
    """The items the T-0159 reviews asked for (desk + independent sub-agent)."""

    def test_duplicate_session_race_is_attached(self):
        self.ok("up", "desk")
        # has-session lies "missing" for tp-desk while the session exists: new-session answers "duplicate session"
        r = self.ok("up", env=dict(self.env, FAKE_TMUX_RACE="tp-desk"))
        line = [l for l in r.stdout.splitlines() if l.startswith("desk")][0]
        self.assertIn("attached", line)
        self.assertEqual(len([c for c in self.new_sessions() if self.opts_of(c)["-s"] == "tp-desk"]), 2, "one real, one refused duplicate")
        self.assertEqual(len(self.sessions()), 3)

    def test_exact_name_not_prefix(self):
        subprocess.run(["tmux", "new-session", "-d", "-s", "tp-desk-old", "-c", self.root, "claude", "--model", "fable", "x"],
                       env=self.env, check=True)
        r = self.ok("up", "desk")
        self.assertIn("launched", r.stdout)
        self.assertIn("tp-desk", self.sessions())

    def test_no_model_seen_is_a_warning(self):
        with open(os.path.join(self.state, "screen-tp-desk.txt"), "w") as fh:
            fh.write("> starting\n(no status bar yet)\n")
        r = self.ok("up", "desk", "--wait", "0.3")
        self.assertIn("WARNING", r.stdout)
        self.assertIn("launched", r.stdout)
        self.assertIn("tp-desk", [e["name"] for e in self.registry()], "registered before the wait")

    def test_model_re_is_a_config_key(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("model: fable\n", "model: fable\nmodel_re: 'model=(\\w+)'\n"))
        with open(os.path.join(self.state, "screen-tp-desk.txt"), "w") as fh:
            fh.write("custom bar model=Fable ok\n")
        r = self.ok("up", "desk")
        self.assertIn("model Fable", r.stdout)
        self.write(".ops/config.yaml", cfg.replace("model: fable\n", "model: fable\nmodel_re: '(['\n"))
        r = self.nz("config", "--check")
        self.assertIn("model_re", r.stderr)

    def test_down_refuses_tampered_registry(self):
        self.ok("up")
        self.set_status("T-0003", "done")
        reg_path = os.path.join(self.root, ".ops", "state", "sessions.json")
        reg = json.load(open(reg_path))
        reg.append({"name": "t0003-finished-ticket", "tmux": "tp-desk", "role": "worker", "ticket": "T-0003"})
        json.dump(reg, open(reg_path, "w"))
        r = self.nz("down", "t0003-finished-ticket")
        self.assertIn("desk", r.stdout + r.stderr)
        self.assertEqual(self.kills(), [])
        self.assertIn("tp-desk", self.sessions())
        r = self.ok("down")
        self.assertEqual(self.kills(), [])

    def test_brief_outside_project_is_refused(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        for bad in ("~/.aws/credentials", "/etc/passwd", "../outside.md"):
            self.write(".ops/config.yaml", cfg.replace("brief: .ops/briefs/desk.md", f"brief: {bad}"))
            r = self.nz("config", "--check")
            self.assertIn("inside the project", r.stderr, bad)
            r = self.nz("up", "desk")
            self.assertEqual(self.new_sessions(), [], bad)

    def test_empty_permission_mode_is_unset(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("permission_mode: auto, brief: .ops/briefs/desk.md", 'permission_mode: "", brief: .ops/briefs/desk.md'))
        self.ok("config", "--check")
        self.ok("up", "desk")
        self.assertNotIn("--permission-mode", self.argv_of(self.new_sessions()[0]))

    def test_concurrent_launches_keep_both_registry_entries(self):
        import threading
        results = {}

        def go(tid):
            results[tid] = self.z("launch", "worker", tid)
        ts = [threading.Thread(target=go, args=(t,)) for t in ("T-0001", "T-0005")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for tid, r in results.items():
            self.assertEqual(r.returncode, 0, f"{tid}: {r.stderr}")
        self.assertEqual(sorted(e["ticket"] for e in self.registry()), ["T-0001", "T-0005"])


if __name__ == "__main__":
    unittest.main()
