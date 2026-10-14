"""The throwaway world every scripts/ticket.sh test runs in (T-0092; its own module since T-0186, so zouinify can vendor
the harness with the core and run the generic tests against tickets/plugins/none). No network, no real repo touched.

T-0092: ticket.sh hid `git push` stderr (`2>/dev/null` in `new`), so a GitHub outage (every push
answered with `remote: Internal Server Error` for ~7 min on 2026-10-07) looked like an id race:
`new` renumbered 5 times and died with "could not allocate an id after 5 attempts", and
`move`/`log` silently left commits local. The fix: print every failed push attempt's stderr, retry
commit_push commands with backoff (TICKET_PUSH_BACKOFF), leave the commit local with a loud
"main is N ahead of origin" note and push it first on the next call, stop `new` renumbering on a
non-non-fast-forward error, and make `board` warn when main is ahead or two files share one id.

ticket.sh computes MAIN from the git repo containing the script itself (readlink -f $0 ->
git-common-dir), so it must NEVER be run against the real repo here. Every test builds a
throwaway world in a tmp dir:

  origin.git   bare, trunk dev, with a pre-receive hook that declines
               every push (printing `Internal Server Error`) while <tmp>/outage exists, and only
               the next push while <tmp>/fail_once exists
  main/        clone with tickets/_TEMPLATE.md, T-0001 (ready), T-0002 (in-dev) and
               scripts/ticket.sh copied from the script under test, committed and pushed
  other/       second clone, used to win push races

The script under test is $TICKET_SH_UNDER_TEST (default: this worktree's scripts/ticket.sh), so
the same suite gives RED evidence against the base version:

    git show HEAD:scripts/ticket.sh > /tmp/base_ticket.sh
    TICKET_SH_UNDER_TEST=/tmp/base_ticket.sh python3 -m pytest -q scripts/tests/test_ticket_sh.py

The world's main gets tickets/ticket.env (TICKET_ENV below: two repos named backend and frontend, like the real project)
and a copy of tickets/plugins/ ($TICKET_PLUGINS_UNDER_TEST, default: this worktree's); the template is
$TICKET_TEMPLATE_UNDER_TEST (default: this worktree's); $TICKET_WORLD_AWK runs the script with that awk binary (T-0220); `plugin` (or $TICKET_WORLD_PLUGIN) picks the plugin the world
runs with (default aws-terraform; the aws-only classes are not written for none).
"""

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
SCRIPT = os.environ.get("TICKET_SH_UNDER_TEST") or os.path.join(_REPO, "scripts", "ticket.sh")
PLUGINS = os.environ.get("TICKET_PLUGINS_UNDER_TEST") or os.path.join(_REPO, "tickets", "plugins")
TEMPLATE = os.environ.get("TICKET_TEMPLATE_UNDER_TEST") or os.path.join(_REPO, "tickets", "_TEMPLATE.md")
TRUNK = "dev"
TICKET_ENV = """# generated-by: ticket_world.py (the test world); bare KEY=value lines (T-0186)
PROJECT=qa-world
TRUNK=dev
TICKETS_DIR=tickets
WORKTREES=.claude/worktrees
REPOS=backend frontend
REPO_BACKEND_PATH=.
REPO_BACKEND_TRUNK=dev
REPO_BACKEND_PROD=main
REPO_FRONTEND_PATH=../no-frontend
REPO_FRONTEND_TRUNK=dev
REPO_FRONTEND_PROD=main
PROTECTED_BRANCHES_EXTRA=feat/legacy-trunk-a feat/legacy-trunk-b
ASK_VERBS=APPLY DEPLOY PUSH RUN DECIDE CHECK APPROVE SSO
TICKET_TYPES=bug feature process chore
DEPLOY_PLUGIN={plugin}
DEPLOY_KINDS=none terraform image+terraform frontend manual-aws
"""
REAL_GIT = shutil.which("git")

PRE_RECEIVE = """#!/usr/bin/env bash
# simulated GitHub outage: a 5xx-style decline, NOT a non-fast-forward
if [ -e "{tmp}/fail_once" ]; then rm -f "{tmp}/fail_once"; echo "Internal Server Error" >&2; exit 1; fi
if [ -e "{tmp}/outage" ]; then echo "Internal Server Error" >&2; exit 1; fi
cat >/dev/null
exit 0
"""

REAL_SLEEP = shutil.which("sleep")

# Wrappers put first on PATH by tests that need them (wrap_env); each behavior is armed by a flag
# file in <tmp> so it fires exactly when the test wants:
#   git  `-C <main> push` (ticket.sh's g() shape):
#        <tmp>/push500 -> answer like a real HTTPS 500 (RPC failed / remote end hung up), exit 1
#        <tmp>/race    -> first let the other clone push its prepared commit, so main's push sees a
#                         fresh ref advertisement and gets a GENUINE non-fast-forward rejection
#        <tmp>/lease_race (content: a branch) -> origin's copy of that branch is moved first, so a
#                         --force-with-lease delete must be refused as stale
#        <tmp>/leave_behind (content: paths relative to the worktree, one per line; "+path" = a file that
#                         appeared meanwhile with content the repo never held) -> `worktree remove` does what
#                         it did on NFS (T-0196): the registration goes, the directory stays with those files,
#                         stderr "failed to delete '<path>': Directory not empty", exit 1
#        <tmp>/refuse_remove -> `worktree remove` refuses up front (nothing changes), exit 128
#   sleep: every call logged to <tmp>/sleep.log; <tmp>/sleep_race -> the other clone pushes its
#        prepared commit during the backoff
GIT_WRAPPER = """#!/usr/bin/env bash
if [ "${{3:-}}" = fetch ] && [ -e "{tmp}/fail_fetch_once" ]; then
  rm -f "{tmp}/fail_fetch_once"
  echo "fatal: unable to access 'https://github.com/x/y/': The requested URL returned error: 502" >&2
  exit 128
fi
if [ "${{3:-}}" = worktree ] && [ "${{4:-}}" = remove ]; then
  if [ -e "{tmp}/refuse_remove" ]; then echo "fatal: validation failed, cannot remove working tree: '$5' is locked (test)" >&2; exit 128; fi
  if [ -e "{tmp}/leave_behind" ]; then
    wt=$5; keep="{tmp}/leave_behind.keep"; rm -rf "$keep"; mkdir -p "$keep"
    while IFS= read -r f; do
      case $f in
        "") continue ;;
        +*) f=${{f#+}}; mkdir -p "$keep/$(dirname "$f")"; echo "stray $RANDOM $$ $(date +%s%N)" >"$keep/$f" ;;
        *) mkdir -p "$keep/$(dirname "$f")"; cp -p "$wt/$f" "$keep/$f" ;;
      esac
    done <"{tmp}/leave_behind"
    "{git}" "$@" >/dev/null 2>&1 || true   # the real remove: clean tree, so the registration and the files go
    mkdir -p "$wt"; cp -a "$keep/." "$wt/"   # ... and the skipped files are back, in a directory git no longer knows
    echo "error: failed to delete '$wt': Directory not empty" >&2
    exit 1
  fi
fi
if [ "${{3:-}}" = commit ] && {{ [ -e "{tmp}/fail_commit" ] || [ -e "{tmp}/fail_commit_once" ]; }}; then   # T-0218: a concurrent git holds the index (once, or for good)
  rm -f "{tmp}/fail_commit_once"
  echo "fatal: Unable to create '{tmp}/main/.git/index.lock': File exists." >&2
  echo "Another git process seems to be running in this repository... remove the file manually to continue." >&2
  exit 128
fi
if [ "${{3:-}}" = push ]; then
  if [ -e "{tmp}/push500" ]; then
    echo "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500" >&2
    echo "fatal: the remote end hung up unexpectedly" >&2
    exit 1
  fi
  if [ -e "{tmp}/race" ]; then
    rm -f "{tmp}/race"
    "{git}" -C "{tmp}/other" push -q origin "{trunk}" >"{tmp}/race.out" 2>&1 || echo "race push failed" >>"{tmp}/race.out"
  fi
  if [ -e "{tmp}/lease_race" ]; then  # origin's copy of that branch moves right before the (leased) delete
    br=$(cat "{tmp}/lease_race"); rm -f "{tmp}/lease_race"
    "{git}" -C "{tmp}/other" push -q -f origin "{trunk}:refs/heads/$br" >"{tmp}/race.out" 2>&1 || echo "lease race push failed" >>"{tmp}/race.out"
  fi
fi
exec "{git}" "$@"
"""

SLEEP_WRAPPER = """#!/usr/bin/env bash
echo "sleep $*" >>"{tmp}/sleep.log"
if [ -e "{tmp}/sleep_race" ]; then
  rm -f "{tmp}/sleep_race"
  "{git}" -C "{tmp}/other" push -q origin "{trunk}" >"{tmp}/race.out" 2>&1 || echo "race push failed" >>"{tmp}/race.out"
fi
exec "{sleep}" "$@"
"""


def _put_plain(lines, text):
    """fill the `## In plain words` section (T-0217) in place: the template's section (its <placeholder> line replaced)
    when the heading is there, else a new section right after the front matter"""
    heads = [i for i, l in enumerate(lines) if l.rstrip() == "## In plain words"]
    if heads:
        i = heads[0] + 1; j = i
        while j < len(lines) and not lines[j].startswith("## "):
            j += 1
        lines[i:j] = text.splitlines() + [""]
    else:
        close = [i for i, l in enumerate(lines) if l == "---"][1]
        lines[close + 1:close + 1] = ["", "## In plain words"] + text.splitlines()


def _ticket(template, tid, slug, status, title, fields=None, plain=None):
    """a ticket from the template; plain= writes a `## In plain words` section right after the front matter (T-0217)"""
    fields = dict(fields or {})
    fields.update({"id": tid, "title": title, "status": status, "created": "2026-10-08 10:00"})
    out, in_fm = [], False
    for n, line in enumerate(template.splitlines()):
        if line == "---":
            in_fm = n == 0
        elif in_fm and ":" in line and line.split(":", 1)[0] in fields:  # front matter only; drops the template's comment
            key = line.split(":", 1)[0]
            line = "%s: %s" % (key, fields[key])
        out.append(line)
    if plain is not None:
        _put_plain(out, plain)
    return tid + "-" + slug + ".md", "\n".join(out) + "\n"


class TicketShWorld(unittest.TestCase):
    plugin = os.environ.get("TICKET_WORLD_PLUGIN") or "aws-terraform"   # tickets/ticket.env DEPLOY_PLUGIN of the world; a subclass (or TICKET_WORLD_PLUGIN=none) picks the generic core

    def write_ticket_env(self, text=None, root=None):
        """tickets/ticket.env of the world's main (not committed by this helper)"""
        path = os.path.join(root or self.main, "tickets", "ticket.env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(TICKET_ENV.format(plugin=self.plugin) if text is None else text)
        return path

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ticket_sh_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = dict(os.environ)
        awk = os.environ.get("TICKET_WORLD_AWK")   # T-0220: run the script with this awk (gawk, busybox awk, nawk ...) instead of PATH's
        if awk:
            shim = os.path.join(self.tmp, "awkbin"); os.makedirs(shim, exist_ok=True)
            os.symlink(awk, os.path.join(shim, "awk"))
            self.env["PATH"] = shim + os.pathsep + self.env["PATH"]
        self.env.update({
            "HOME": self.home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "qa", "GIT_AUTHOR_EMAIL": "qa@example.invalid",
            "GIT_COMMITTER_NAME": "qa", "GIT_COMMITTER_EMAIL": "qa@example.invalid",
            "TICKET_AS": "qa", "TICKET_PUSH_BACKOFF": "0 0 0",
            "TICKET_FE_REPO": os.path.join(self.tmp, "no-frontend"),
        })
        for k in ("TICKET_FORCE", "TICKET_TRUNK", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(k, None)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.main = os.path.join(self.tmp, "main")
        self.other = os.path.join(self.tmp, "other")
        self.git("init", "-q", "--bare", "-b", TRUNK, self.origin)
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write(PRE_RECEIVE.format(tmp=self.tmp))
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.git("clone", "-q", self.origin, self.main)
        self.git("-C", self.main, "checkout", "-q", "-b", TRUNK)
        os.makedirs(os.path.join(self.main, "tickets"))
        os.makedirs(os.path.join(self.main, "scripts"))
        with open(os.path.join(self.main, ".gitignore"), "w") as fh:
            fh.write(".claude/\n")  # as in the real repo: `claim` puts the worktree under main/.claude/worktrees
        shutil.copy(TEMPLATE, os.path.join(self.main, "tickets", "_TEMPLATE.md"))
        shutil.copy(SCRIPT, os.path.join(self.main, "scripts", "ticket.sh"))
        shutil.copytree(PLUGINS, os.path.join(self.main, "tickets", "plugins"))   # T-0186: the core needs its plugin and ticket.env
        self.write_ticket_env()
        with open(TEMPLATE, encoding="utf-8") as fh:
            self.template = fh.read()
        self.write_ticket("T-0001", "alpha-ticket", "ready", "alpha ticket")
        self.write_ticket("T-0002", "beta-ticket", "in-dev", "beta ticket")
        self.git("-C", self.main, "add", "-A")
        self.git("-C", self.main, "commit", "-q", "-m", "init")
        self.git("-C", self.main, "push", "-q", "-u", "origin", TRUNK)
        self.git("clone", "-q", "-b", TRUNK, self.origin, self.other)

    # ---- helpers -------------------------------------------------------------------------
    def git(self, *args, check=True):
        p = subprocess.run([REAL_GIT, *args], env=self.env, capture_output=True, text=True, timeout=30)
        if check and p.returncode != 0:
            raise AssertionError("git %s failed: %s%s" % (" ".join(args), p.stdout, p.stderr))
        return p.stdout.strip()

    def write_ticket(self, tid, slug, status, title, repo=None, fields=None, plain=None):
        name, text = _ticket(self.template, tid, slug, status, title, fields, plain)
        path = os.path.join(repo or self.main, "tickets", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def ts(self, *args, env=None):
        p = subprocess.run(["bash", os.path.join(self.main, "scripts", "ticket.sh"), *args],
                           cwd=self.tmp, env=env or self.env, capture_output=True, text=True, timeout=60)
        p.out = p.stdout + p.stderr
        return p

    def outage(self, on=True):
        flag = os.path.join(self.tmp, "outage")
        if on:
            open(flag, "w").close()
        elif os.path.exists(flag):
            os.remove(flag)

    def origin_head(self):
        return self.git("--git-dir", self.origin, "rev-parse", TRUNK)

    def origin_log(self):
        return self.git("--git-dir", self.origin, "log", "--format=%s", TRUNK)

    def main_head(self):
        return self.git("-C", self.main, "rev-parse", "HEAD")

    def main_log(self):
        return self.git("-C", self.main, "log", "--format=%s", "HEAD")

    def assert_main_clean(self):
        self.assertEqual(self.git("-C", self.main, "status", "--porcelain"), "", "main worktree not clean")

    def ahead(self):
        self.git("-C", self.main, "fetch", "-q", "origin")
        return int(self.git("-C", self.main, "rev-list", "--count", "origin/%s..HEAD" % TRUNK))

    def flag(self, name):
        open(os.path.join(self.tmp, name), "w").close()

    def flag_gone(self, name):
        return not os.path.exists(os.path.join(self.tmp, name))

    def wrap_env(self):
        wrapdir = os.path.join(self.tmp, "wrapbin")
        if not os.path.isdir(wrapdir):
            os.makedirs(wrapdir)
            fmt = dict(tmp=self.tmp, git=REAL_GIT, trunk=TRUNK, sleep=REAL_SLEEP)
            for name, body in (("git", GIT_WRAPPER), ("sleep", SLEEP_WRAPPER)):
                path = os.path.join(wrapdir, name)
                with open(path, "w") as fh:
                    fh.write(body.format(**fmt))
                os.chmod(path, 0o755)
        return dict(self.env, PATH=wrapdir + os.pathsep + self.env["PATH"])

    def sleep_calls(self):
        path = os.path.join(self.tmp, "sleep.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def race_out(self):
        path = os.path.join(self.tmp, "race.out")
        return open(path).read() if os.path.exists(path) else ""

    def other_commit_ticket(self, tid, slug, title, msg):
        self.write_ticket(tid, slug, "backlog", title, repo=self.other)
        self.git("-C", self.other, "add", "-A")
        self.git("-C", self.other, "commit", "-q", "-m", msg)

    def local_pending_commit(self, line="- local pending line"):
        """a ticket commit on main's trunk that never reached origin (an earlier failed push)"""
        path = os.path.join(self.main, "tickets", "T-0001-alpha-ticket.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.git("-C", self.main, "commit", "-q", "-am", "tickets: T-0001 pending local")

    def rebase_in_progress(self):
        gd = os.path.join(self.main, ".git")
        return [d for d in ("rebase-merge", "rebase-apply") if os.path.exists(os.path.join(gd, d))]

    def assert_main_sane(self):
        self.assertEqual(self.rebase_in_progress(), [], "main left mid-rebase")
        self.assertEqual(self.git("-C", self.main, "rev-parse", "--abbrev-ref", "HEAD"), TRUNK,
                         "main not on trunk")
        self.assert_main_clean()

    def ticket_files(self, root):
        return sorted(f for f in os.listdir(os.path.join(root, "tickets")) if f.startswith("T-"))

    def env_without_who(self, **extra):
        env = dict(self.env, **extra)
        env.pop("TICKET_AS", None)
        return env

    def front(self, name, key):
        with open(os.path.join(self.main, "tickets", name), encoding="utf-8") as fh:
            for line in fh.read().split("\n---", 1)[0].splitlines():
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].split("#")[0].strip()
        return None

    def set_plain(self, name, text):
        """write the `## In plain words` section (T-0217) of tickets/<name> in main (not committed)"""
        path = os.path.join(self.main, "tickets", name)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        _put_plain(lines, text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path

    def log_of(self, name):
        with open(os.path.join(self.main, "tickets", name), encoding="utf-8") as fh:
            return fh.read().split("## Log", 1)[1]

    def commit_main(self, msg):
        self.git("-C", self.main, "add", "-A")
        self.git("-C", self.main, "commit", "-q", "-m", msg)
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)

    def write_freeze(self, text):
        with open(os.path.join(self.main, "tickets", "FREEZE"), "w", encoding="utf-8") as fh:
            fh.write(text)
        self.commit_main("tickets: FREEZE")

    def ticket_text(self, name):
        with open(os.path.join(self.main, "tickets", name), encoding="utf-8") as fh:
            return fh.read()
