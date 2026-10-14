"""The generic tests of scripts/ticket.sh (T-0189): everything that holds for any project running the core, on any deploy
plugin -- push handling (T-0092), the lock and the shared FETCH_HEAD, claim's worktree rules, the freeze, merge and prune,
the human inbox's asks and questions, skipped, the board and its JSON. Fixtures are neutral (no resource of any one project:
the second repo is "frontend", another repo is "sidecar", the old trunks are feat/legacy-trunk-a/b), so zouinify vendors this
file with ticket_world.py and runs it against the vendored core with TICKET_WORLD_PLUGIN=none. The tests that need the
aws-terraform plugin (the evidence gates, the *.tfplan rules, apply) or this project's historical log lines are in
test_ticket_sh.py.

    python3 -m pytest -q scripts/tests/test_ticket_core.py
    TICKET_WORLD_PLUGIN=none python3 -m pytest -q scripts/tests/test_ticket_core.py
    TICKET_SH_UNDER_TEST=<base copy> python3 -m pytest -q scripts/tests/test_ticket_core.py   # RED evidence against a base script
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

from ticket_world import TicketShWorld, _REPO, TRUNK, REAL_GIT


class TestExistingBehavior(TicketShWorld):
    def test_normal_move_pushes(self):
        p = self.ts("move", "T-0001", "in-dev", "qa: normal move")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushed:", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        self.assertIn("T-0001 ready -> in-dev", self.origin_log())
        self.assert_main_clean()

    def test_skipped_gate_refused(self):
        before = self.main_head()
        p = self.ts("move", "T-0001", "merged", "qa: skip gates")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("skips a gate", p.out)
        self.assertEqual(self.main_head(), before)
        self.assert_main_clean()


class TestPushOutage(TicketShWorld):
    def test_move_during_outage_shows_stderr_keeps_commit_then_next_call_pushes(self):
        self.outage(True)
        p = self.ts("move", "T-0001", "in-dev", "qa: during outage")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("Internal Server Error", p.stderr)
        self.assertIn("push attempt 4 failed", p.stderr)
        self.assertIn("main is 1 ahead of origin -- push will be retried by the next ticket.sh call", p.stderr)
        self.assert_main_clean()
        self.assertIn("T-0001 ready -> in-dev", self.main_log())
        self.assertNotIn("T-0001 ready -> in-dev", self.origin_log())

        self.outage(False)
        p = self.ts("log", "T-0002", "qa: after outage")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushing first", p.out)
        self.assertIn("pushed pending", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        olog = self.origin_log()
        self.assertIn("T-0001 ready -> in-dev", olog)
        self.assertIn("T-0002 log", olog)
        self.assert_main_clean()

    def test_log_during_outage_reports_every_attempt(self):
        self.outage(True)
        p = self.ts("log", "T-0002", "qa: log during outage")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("Internal Server Error", p.stderr)
        for k in (1, 2, 3, 4):
            self.assertIn("push attempt %d failed:" % k, p.stderr)
        self.assertNotIn("push attempt 5 failed", p.stderr)  # "0 0 0" = 1 attempt + 3 retries
        self.assertIn("main is 1 ahead of origin", p.stderr)
        self.assertEqual(self.ahead(), 1)
        self.assert_main_clean()

    def test_transient_failure_retried_and_succeeds(self):
        open(os.path.join(self.tmp, "fail_once"), "w").close()
        p = self.ts("move", "T-0001", "in-dev", "qa: one transient 5xx")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushed:", p.out)
        self.assertIn("push attempt 1 failed:", p.stderr)
        self.assertIn("Internal Server Error", p.stderr)
        self.assertEqual(self.origin_head(), self.main_head())
        self.assertIn("T-0001 ready -> in-dev", self.origin_log())

    def test_new_during_outage_does_not_renumber(self):
        before = self.main_head()
        self.outage(True)
        p = self.ts("new", "bug", "P3", "outage new ticket")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("Internal Server Error", p.stderr)
        self.assertIn("committed locally but NOT pushed", p.stderr)
        self.assertNotIn("could not allocate an id", p.out)
        self.assertEqual(self.ticket_files(self.main),
                         ["T-0001-alpha-ticket.md", "T-0002-beta-ticket.md", "T-0003-outage-new-ticket.md"])
        new_commits = self.git("-C", self.main, "log", "--format=%s", "%s..HEAD" % before).splitlines()
        self.assertEqual(new_commits, ["tickets: T-0003 new (bug P3) outage new ticket"])
        self.assertIn("T-0003", p.out)
        self.assertNotIn("T-0004", p.out)
        self.assertNotIn("T-0003", self.origin_log())
        self.assert_main_clean()

    def test_new_racing_non_ff_renumbers(self):
        # the other clone prepares T-0003 and pushes it right before main's first push
        self.other_commit_ticket("T-0003", "other-session-ticket", "other session ticket",
                                 "tickets: T-0003 new (bug P3) other session ticket")
        self.flag("race")
        p = self.ts("new", "bug", "P3", "racing new ticket", env=self.wrap_env())
        self.assertTrue(self.flag_gone("race"), "race was never triggered")
        self.assertNotIn("race push failed", self.race_out())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushed: T-0004", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        self.assertEqual(self.ticket_files(self.other),
                         ["T-0001-alpha-ticket.md", "T-0002-beta-ticket.md",
                          "T-0003-other-session-ticket.md", "T-0004-racing-new-ticket.md"])
        self.assert_main_clean()

    # ---- review of 4a090c9 ---------------------------------------------------------------
    def test_A_pending_commit_conflicting_with_origin_leaves_main_clean(self):
        self.outage(True)
        p = self.ts("move", "T-0001", "in-dev", "qa: during outage")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("T-0001 ready -> in-dev", self.main_log())
        self.outage(False)
        # another session changes the SAME status line of T-0001 and wins the push
        path = os.path.join(self.other, "tickets", "T-0001-alpha-ticket.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("status: ready", "status: blocked", 1))
        self.git("-C", self.other, "commit", "-q", "-am", "tickets: T-0001 ready -> blocked (other)")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)

        p = self.ts("log", "T-0002", "x")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assert_main_sane()
        self.assertIn("conflict with origin", p.stderr)

    def test_I_rebase_already_in_progress_is_not_aborted(self):
        # a human resolving a rebase in main by hand must not lose it to a ticket.sh run's EXIT trap
        self.outage(True)
        self.ts("move", "T-0001", "in-dev", "qa: during outage")
        self.outage(False)
        path = os.path.join(self.other, "tickets", "T-0001-alpha-ticket.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("status: ready", "status: blocked", 1))
        self.git("-C", self.other, "commit", "-q", "-am", "tickets: T-0001 ready -> blocked (other)")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        self.git("-C", self.main, "pull", "-q", "--rebase", "origin", TRUNK, check=False)
        self.assertTrue(self.rebase_in_progress(), "setup: expected a hand rebase in progress")

        p = self.ts("log", "T-0002", "x")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertTrue(self.rebase_in_progress(), "ticket.sh aborted a rebase it did not start")
        self.assertNotIn("aborted an interrupted rebase", p.stderr)

    def test_B_new_transient_5xx_keeps_same_id(self):
        self.flag("fail_once")
        p = self.ts("new", "bug", "P3", "transient new ticket")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertTrue(self.flag_gone("fail_once"), "first push never hit the hook")
        self.assertIn("pushed: T-0003", p.out)
        self.assertNotIn("T-0004", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        self.assertEqual(self.ticket_files(self.main),
                         ["T-0001-alpha-ticket.md", "T-0002-beta-ticket.md", "T-0003-transient-new-ticket.md"])
        self.assert_main_sane()

    def _new_5xx_then_other_takes_id(self, other_slug):
        self.other_commit_ticket("T-0003", other_slug, "other title",
                                 "tickets: T-0003 new (bug P3) other title")
        self.flag("fail_once")   # our first push: server error (not non-ff)
        self.flag("sleep_race")  # during the backoff the other clone pushes its T-0003
        p = self.ts("new", "bug", "P3", "backoff new ticket", env=self.wrap_env())
        self.assertTrue(self.flag_gone("fail_once"), "first push never hit the hook")
        self.assertTrue(self.flag_gone("sleep_race"), "no backoff sleep happened: " + p.out)
        self.assertNotIn("race push failed", self.race_out())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushed: T-0004", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        files = self.ticket_files(self.main)
        ids = [f[:6] for f in files]
        self.assertEqual(len(ids), len(set(ids)), "duplicate id on origin/main: %s" % files)
        self.assertIn("T-0003-%s.md" % other_slug, files)
        self.assertIn("T-0004-backoff-new-ticket.md", files)
        self.assert_main_sane()
        return files

    def test_C_new_5xx_then_other_pushes_same_id_different_slug_renumbers(self):
        self._new_5xx_then_other_takes_id("other-different-slug")

    def test_D_new_5xx_then_other_pushes_same_filename_renumbers(self):
        files = self._new_5xx_then_other_takes_id("backoff-new-ticket")  # add/add conflict
        with open(os.path.join(self.main, "tickets", "T-0003-backoff-new-ticket.md"), encoding="utf-8") as fh:
            self.assertIn("title: other title", fh.read(), "T-0003 must be the other session's ticket")
        self.assertEqual(len(files), 4, files)

    def test_E_new_https_500_does_not_renumber(self):
        before = self.main_head()
        self.flag("push500")
        p = self.ts("new", "bug", "P3", "https five hundred ticket", env=self.wrap_env())
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("committed locally but NOT pushed", p.stderr)
        self.assertIn("do NOT re-run new", p.stderr)
        self.assertIn("HTTP 500", p.stderr)
        new_commits = self.git("-C", self.main, "log", "--format=%s", "%s..HEAD" % before).splitlines()
        self.assertEqual(new_commits, ["tickets: T-0003 new (bug P3) https five hundred ticket"])
        self.assertEqual(self.ticket_files(self.main),
                         ["T-0001-alpha-ticket.md", "T-0002-beta-ticket.md", "T-0003-https-five-hundred-ticket.md"])
        self.assertNotIn("T-0003", self.origin_log())
        self.assert_main_sane()

    def test_F_sync_pushes_pending_first(self):
        self.local_pending_commit()
        path = os.path.join(self.main, "tickets", "T-0002-beta-ticket.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## Problem\n", text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("## Problem\n", "## Problem\nqa hand edit\n", 1))
        p = self.ts("sync", "T-0002", "x")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushing first", p.out)
        self.assertEqual(self.origin_head(), self.main_head())
        olog = self.origin_log()
        self.assertIn("tickets: T-0001 pending local", olog)
        self.assertIn("T-0002 edit (x)", olog)
        self.assert_main_sane()

    def test_G_pending_push_failure_is_labelled(self):
        self.local_pending_commit()
        self.outage(True)
        p = self.ts("log", "T-0002", "x")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("pending push failed:", p.stderr)
        self.assertIn("push attempt 1 failed:", p.stderr)
        self.assertIn("Internal Server Error", p.stderr)
        self.assertEqual(self.ahead(), 2)
        self.assert_main_sane()

    def test_H_commit_push_plain_non_ff_race_does_not_sleep(self):
        path = os.path.join(self.other, "tickets", "T-0002-beta-ticket.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("- other session line\n")
        self.git("-C", self.other, "commit", "-q", "-am", "tickets: T-0002 log (other)")
        self.flag("race")
        p = self.ts("move", "T-0001", "in-dev", "qa: non-ff race", env=self.wrap_env())
        self.assertTrue(self.flag_gone("race"), "race was never triggered")
        self.assertNotIn("race push failed", self.race_out())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("pushed:", p.out)
        self.assertEqual(self.sleep_calls(), [], "a non-ff race must retry without backoff")
        self.assertEqual(self.origin_head(), self.main_head())
        olog = self.origin_log()
        self.assertIn("T-0002 log (other)", olog)
        self.assertIn("T-0001 ready -> in-dev", olog)
        self.assert_main_sane()


class TestBoardWarnings(TicketShWorld):
    def test_board_warns_main_ahead(self):
        path = os.path.join(self.main, "tickets", "T-0001-alpha-ticket.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("- local only line\n")
        self.git("-C", self.main, "commit", "-q", "-am", "tickets: T-0001 local only")
        p = self.ts("board")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("MAIN IS 1 AHEAD OF origin/", p.out)
        self.assertEqual(self.ahead(), 1, "board must stay read-only (no push)")

    def test_board_no_ahead_warning_when_in_sync(self):
        p = self.ts("board")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertNotIn("AHEAD OF origin/", p.out)
        self.assertNotIn("DUPLICATE TICKET ID", p.out)

    def test_board_warns_duplicate_id(self):
        self.write_ticket("T-0003", "first-dup", "backlog", "first dup")
        self.write_ticket("T-0003", "second-dup", "backlog", "second dup")
        self.git("-C", self.main, "add", "-A")
        self.git("-C", self.main, "commit", "-q", "-m", "tickets: duplicate ids")
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        p = self.ts("board")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("DUPLICATE TICKET ID", p.out)


class TestClaimWorktreeArg(TicketShWorld):
    """T-0176: `claim T-xxxx <owner> [branch] [worktree]` joined an absolute worktree onto the repo path
    (`git worktree add "$MAIN/$wt"`), so a session's claim of T-0174 with a not-yet-existing absolute path created a
    BACKEND worktree inside main, which dirtied main and blocked every session. Now an absolute worktree is used as given
    and must already be a worktree of the ticket's repo; a relative one goes under .claude/worktrees/ only; a ticket of
    another repo (sidecar, auth) always names an existing worktree and ticket.sh creates nothing. RED on 4cdc607: the
    absolute claim succeeds, `work in: <main>//abs/path`, and main is dirty."""

    def test_absolute_worktree_that_does_not_exist_is_refused_and_main_stays_clean(self):
        gone = os.path.join(self.tmp, "sidecar", ".worktrees", "t0001-x")
        p = self.ts("claim", "T-0001", "qa", "t0001-x", gone)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("worktree %s does not exist" % gone, p.out)
        self.assertFalse(os.path.exists(self.main + gone), "no worktree joined onto main: " + self.main + gone)
        self.assertFalse(os.path.exists(gone)); self.assert_main_clean()
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
        plain = os.path.join(self.tmp, "plain"); os.makedirs(plain)   # exists, but is nobody's worktree
        p = self.ts("claim", "T-0001", "qa", "t0001-x", plain)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is not a worktree of %s" % self.main, p.out); self.assert_main_clean()
        p = self.ts("claim", "T-0001", "qa", "t0001-x", "work/t0001")   # relative but outside .claude/worktrees/: would dirty main
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("a relative worktree is .claude/worktrees/<name>", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "work"))); self.assert_main_clean()
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
        p = self.ts("claim", "T-0001", "qa")   # the default still works: created under .claude/worktrees/
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("work in: %s   (commit trailer" % os.path.join(self.main, ".claude", "worktrees", "t0001-alpha-ticket"), p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "worktree"), ".claude/worktrees/t0001-alpha-ticket")
        self.assert_main_sane()

    def test_absolute_existing_worktree_of_the_repo_is_used_as_given(self):
        wt = os.path.join(self.tmp, "elsewhere", "t0001")
        self.git("-C", self.main, "worktree", "add", "-q", wt, "-b", "pre-made", TRUNK)
        p = self.ts("claim", "T-0001", "qa", "wrong-name", wt)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is on branch pre-made, not wrong-name", p.out); self.assert_main_clean()
        trunk_before = self.main_head()
        p = self.ts("claim", "T-0001", "qa", "pre-made", wt)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("work in: %s   (commit trailer" % wt, p.out)
        self.assertEqual((self.front("T-0001-alpha-ticket.md", "worktree"), self.front("T-0001-alpha-ticket.md", "branch"), self.front("T-0001-alpha-ticket.md", "base")),
                         (wt, "pre-made", trunk_before[:7]))
        self.assertFalse(os.path.isdir(os.path.join(self.main, ".claude", "worktrees", "t0001-alpha-ticket")), "nothing created")
        out = self.ts("board", env=self.env_without_who()).out
        self.assertIn("T-0001  P2  alpha ticket", out); self.assertIn("behind 1  ahead 0  dirty 0", out)   # the board resolves the absolute path
        self.assert_main_sane()

    def test_other_repo_ticket_needs_an_existing_worktree_and_creates_nothing(self):
        self.write_ticket("T-0003", "sidecar-thing", "ready", "a sidecar thing", fields={"repos": "[sidecar]"})
        self.commit_main("tickets: sidecar ticket")
        p = self.ts("claim", "T-0003", "qa")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("T-0003 is a sidecar ticket", p.out); self.assertIn("EXISTING worktree", p.out)
        p = self.ts("claim", "T-0003", "qa", "t0003-x", ".worktrees/t0003-x")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("absolute path of an existing worktree", p.out)
        p = self.ts("claim", "T-0003", "qa", "t0003-x", os.path.join(self.tmp, "sidecar", ".worktrees", "t0003-x"))
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("does not exist", p.out)
        self.assertFalse(os.path.isdir(os.path.join(self.main, ".claude")), "the backend got no worktree"); self.assert_main_clean()
        z = os.path.join(self.tmp, "sidecar"); os.makedirs(z)   # a real worktree of another repo
        self.git("init", "-q", "-b", "main", z)
        with open(os.path.join(z, "f"), "w") as fh:
            fh.write("x\n")
        self.git("-C", z, "add", "-A"); self.git("-C", z, "commit", "-q", "-m", "init")
        zwt = os.path.join(z, ".worktrees", "t0003-x")
        self.git("-C", z, "worktree", "add", "-q", zwt, "-b", "t0003-x", "main")
        p = self.ts("claim", "T-0003", "qa", "other-branch", zwt)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is on branch t0003-x, not other-branch", p.out)
        fake = os.path.join(self.tmp, "sidecar-copy"); shutil.copytree(zwt, fake, symlinks=True)   # looks like a linked worktree, is registered nowhere (T-0151)
        p = self.ts("claim", "T-0003", "qa", "t0003-x", fake)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is not a registered worktree of %s" % os.path.realpath(z), p.out)
        self.assertEqual(self.front("T-0003-sidecar-thing.md", "status"), "ready"); self.assert_main_clean()
        p = self.ts("claim", "T-0003", "qa", "t0003-x", zwt)
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("work in: %s   (commit trailer" % zwt, p.out)
        f = "T-0003-sidecar-thing.md"
        self.assertEqual((self.front(f, "status"), self.front(f, "worktree"), self.front(f, "branch"), self.front(f, "base")),
                         ("in-dev", zwt, "t0003-x", self.git("-C", z, "rev-parse", "--short", "HEAD")))
        self.assertFalse(os.path.isdir(os.path.join(self.main, ".claude")), "the backend got no worktree")
        self.assertIn("T-0003  P2  a sidecar thing", self.ts("board", env=self.env_without_who()).out)
        self.assert_main_sane()
    # ---- review of 7338a2e: four edge claims that broke the promise ("main stays clean, a claim is a worktree") -------
    def test_relative_worktree_cannot_escape_dot_claude_worktrees(self):
        """1. `..` passed the .claude/worktrees/?* glob: .claude/worktrees/../../escape created main/escape (main dirty again)
        and ../../../outside landed outside the repo. RED on 7338a2e: exit 0 and main/escape exists."""
        for bad in (".claude/worktrees/../../escape", ".claude/worktrees/../../../outside", ".claude/worktrees/a/b", ".claude/worktrees/..", ".claude/worktrees/."):
            p = self.ts("claim", "T-0001", "qa", "x1", bad)
            self.assertEqual(p.returncode, 1, bad + ": " + p.out); self.assertIn("one level, no '..'", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "escape"))); self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside")))
        self.assert_main_clean(); self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
        self.assertFalse(self.git("-C", self.main, "branch", "--list", "x1"), "no branch created by a refused claim")
        p = self.ts("claim", "T-0001", "qa", "x1", ".claude/worktrees/t0001-x/")   # a trailing slash is just dropped
        self.assertEqual(p.returncode, 0, p.out); self.assertEqual(self.front("T-0001-alpha-ticket.md", "worktree"), ".claude/worktrees/t0001-x")
        self.assert_main_sane()

    def test_main_checkout_is_never_a_claimable_worktree(self):
        """2. $MAIN, $MAIN/ and a symlink to main were accepted (worktree=$MAIN, branch=trunk): development in main on trunk.
        RED on 7338a2e: exit 0 with branch=dev."""
        link = os.path.join(self.tmp, "link-to-main"); os.symlink(self.main, link)
        for path in (self.main, self.main + "/", link):
            p = self.ts("claim", "T-0001", "qa", "", path)
            self.assertEqual(p.returncode, 1, path + ": " + p.out); self.assertIn("is the main checkout of %s, not a worktree" % self.main, p.out)
        self.assert_main_clean(); self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "branch"), "")
        z = os.path.join(self.tmp, "sidecar"); os.makedirs(z)   # the same for another repo's main checkout
        self.git("init", "-q", "-b", "main", z)
        with open(os.path.join(z, "f"), "w") as fh:
            fh.write("x\n")
        self.git("-C", z, "add", "-A"); self.git("-C", z, "commit", "-q", "-m", "init")
        self.write_ticket("T-0003", "sidecar-thing", "ready", "a sidecar thing", fields={"repos": "[sidecar]"})
        self.commit_main("tickets: sidecar ticket")
        p = self.ts("claim", "T-0003", "qa", "", z)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is the main checkout of the sidecar repo, not a worktree", p.out)
        self.assertEqual(self.front("T-0003-sidecar-thing.md", "status"), "ready"); self.assert_main_sane()

    def test_plain_directory_under_dot_claude_worktrees_is_refused(self):
        """3. a plain directory under .claude/worktrees/ made `git -C` climb to main and recorded branch=trunk.
        RED on 7338a2e: exit 0 with branch=dev."""
        plain = os.path.join(self.main, ".claude", "worktrees", "plain"); os.makedirs(plain)
        p = self.ts("claim", "T-0001", "qa", "", ".claude/worktrees/plain")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("exists but is not a worktree of %s" % self.main, p.out)
        self.assertEqual((self.front("T-0001-alpha-ticket.md", "status"), self.front("T-0001-alpha-ticket.md", "branch")), ("ready", ""))
        self.assert_main_clean()
        with open(os.path.join(plain, "note.txt"), "w") as fh:   # an absolute path to the same plain dir: same answer
            fh.write("x\n")
        p = self.ts("claim", "T-0001", "qa", "", plain)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("is not a worktree of", p.out); self.assert_main_sane()

    def test_absolute_worktree_is_stored_resolved(self):
        """4. a symlink to a registered worktree (and a trailing slash) is accepted, and the ticket stores the resolved path.
        RED on 7338a2e: the symlink path was stored as given."""
        wt = os.path.join(self.tmp, "elsewhere", "t0001")
        self.git("-C", self.main, "worktree", "add", "-q", wt, "-b", "pre-made", TRUNK)
        link = os.path.join(self.tmp, "link-to-wt"); os.symlink(wt, link)
        p = self.ts("claim", "T-0001", "qa", "", link + "/")
        self.assertEqual(p.returncode, 0, p.out)
        real = os.path.realpath(wt)
        self.assertEqual((self.front("T-0001-alpha-ticket.md", "worktree"), self.front("T-0001-alpha-ticket.md", "branch")), (real, "pre-made"))
        self.assertIn("work in: %s   (commit trailer" % real, p.out)
        self.assert_main_sane()


class TestSharedFetchHead(TicketShWorld):
    """T-0113: `git pull` rebased onto whatever the shared .git/FETCH_HEAD said; an unlocked `git fetch` in
    main rewrote it mid-run -> "fatal: Cannot rebase onto multiple branches". ticket.sh must not read it."""

    def hammer_fetch_head(self, stop):
        fh = os.path.join(self.main, ".git", "FETCH_HEAD")
        head = self.git("-C", self.main, "rev-parse", "HEAD")
        bad = ("%s\t\tbranch '%s' of %s\n%s\t\tbranch 'other' of %s\n" % (head, TRUNK, self.origin, head, self.origin))
        while not stop.is_set():
            try:
                with open(fh + ".tmp", "w") as f:
                    f.write(bad)
                os.replace(fh + ".tmp", fh)
            except OSError:
                pass

    def test_concurrent_fetch_head_rewrites_do_not_break_calls(self):
        import threading
        stop = threading.Event()
        t = threading.Thread(target=self.hammer_fetch_head, args=(stop,), daemon=True)
        t.start()
        try:
            for k in range(6):
                # someone else moved origin, so every call really has to rebase
                self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
                self.other_commit_ticket("T-00%02d" % (10 + k), "o%d" % k, "other %d" % k, "tickets: other %d" % k)
                self.git("-C", self.other, "push", "-q", "origin", TRUNK)
                p = self.ts("log", "T-0001", "qa: line %d" % k)
                self.assertEqual(p.returncode, 0, p.out)
                self.assertNotIn("multiple branches", p.out)
        finally:
            stop.set()
            t.join()
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()

    def test_static_two_for_merge_fetch_head(self):
        self.other_commit_ticket("T-0010", "o", "other", "tickets: other")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        head = self.git("-C", self.main, "rev-parse", "HEAD")
        with open(os.path.join(self.main, ".git", "FETCH_HEAD"), "w") as f:
            f.write("%s\t\tbranch 'a' of x\n%s\t\tbranch 'b' of x\n" % (head, head))
        p = self.ts("move", "T-0001", "in-dev", "qa: after a foreign fetch")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("tickets: other", self.main_log())
        self.assert_main_sane()

    def test_fetch_failure_is_retried_once(self):
        env = self.wrap_env()
        self.flag("fail_fetch_once")
        p = self.ts("move", "T-0001", "in-dev", "qa: flaky fetch", env=dict(env, TICKET_FETCH_RETRY_SLEEP="0"))
        self.assertEqual(p.returncode, 0, p.out)
        self.assertTrue(self.flag_gone("fail_fetch_once"), "the wrapper never saw a fetch")
        self.assert_main_sane()

    def test_commit_force_removed_from_origin_stays_removed(self):
        # a human rewrote trunk (e.g. to drop a leaked secret): like `git pull --rebase` (fork-point),
        # ticket.sh must not resurrect the dropped commit and push it back
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        self.other_commit_ticket("T-0010", "secret", "leaked", "tickets: BAD leaked secret")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        p = self.ts("log", "T-0001", "qa: sees BAD")
        self.assertEqual(p.returncode, 0, p.out)
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        self.git("-C", self.other, "reset", "-q", "--hard", "HEAD~2")  # drop BAD and our log line
        self.other_commit_ticket("T-0011", "new", "new", "tickets: NEW after the rewrite")
        self.git("-C", self.other, "push", "-q", "-f", "origin", TRUNK)
        p = self.ts("move", "T-0001", "in-dev", "qa: after the rewrite")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertNotIn("BAD leaked secret", self.origin_log())
        self.assertNotIn("BAD leaked secret", self.main_log())
        self.assertIn("tickets: NEW after the rewrite", self.main_log())
        self.assert_main_sane()

    def test_ticket_sh_does_not_write_fetch_head(self):
        fh = os.path.join(self.main, ".git", "FETCH_HEAD")
        with open(fh, "w") as f:
            f.write("sentinel\n")
        self.other_commit_ticket("T-0010", "o", "other", "tickets: other")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        p = self.ts("move", "T-0001", "in-dev", "qa: fetch head untouched")
        self.assertEqual(p.returncode, 0, p.out)
        with open(fh) as f:
            self.assertEqual(f.read(), "sentinel\n")


class ProtocolV2Helpers(TicketShWorld):
    """the helpers of TestProtocolV2, shared by its generic tests here and its plugin-dependent tests in test_ticket_sh.py (T-0189)"""
    def deployable(self, tid, slug, status="ready-to-deploy", deploy="terraform"):
        self.write_ticket(tid, slug, status, slug, fields={"deploy": deploy})


class TestProtocolV2(ProtocolV2Helpers):
    """T-0128 (process review 4.2 / 5.4): TICKET_AS is required for every mutating command (the `$USER`
    fallback signed 44 log lines with the shell user), `claim` honours tickets/FREEZE, and `move` refuses
    a `deployed` line that names nothing an apply produces or a frontend `done` without the hard-reload
    check. RED on the base script: every command below succeeds there."""

    # ---- TICKET_AS ---------------------------------------------------------------------
    def test_mutating_commands_refuse_without_ticket_as(self):
        env = self.env_without_who(USER="shelluser")
        before = self.main_head()
        for args in (("log", "T-0002", "x"), ("move", "T-0001", "in-dev", "qa: no who"),
                     ("set", "T-0002", "plan", "p.tfplan"), ("new", "bug", "P3", "no who ticket"),
                     ("claim", "T-0001", "qa")):
            p = self.ts(*args, env=env)
            self.assertNotEqual(p.returncode, 0, "%s succeeded without TICKET_AS: %s" % (args, p.out))
            self.assertIn("export TICKET_AS=<your ListAgents name>", p.out, args)
        self.assertEqual(self.main_head(), before, "a command without TICKET_AS committed something")
        self.assertEqual(self.ticket_files(self.main), ["T-0001-alpha-ticket.md", "T-0002-beta-ticket.md"])
        self.assertNotIn("shelluser", self.ticket_text("T-0002-beta-ticket.md"))  # no $USER fallback signature
        self.assert_main_sane()

    def test_sync_and_put_refuse_without_ticket_as(self):
        env = self.env_without_who()
        copy = os.path.join(self.tmp, "T-0002-copy.md")
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write(self.ticket_text("T-0002-beta-ticket.md"))
        p = self.ts("put", "T-0002", copy, "x", env=env)
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("export TICKET_AS=<your ListAgents name>", p.out)
        p = self.ts("sync", "T-0002", "x", env=env)
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("export TICKET_AS=<your ListAgents name>", p.out)
        self.assert_main_sane()

    def test_board_works_without_ticket_as_and_log_lines_are_signed(self):
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertNotIn("export TICKET_AS=<your ListAgents name>", p.out)
        self.assertIn("== IN-DEV", p.out)
        p = self.ts("log", "T-0002", "signed line")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn(" qa: signed line", self.ticket_text("T-0002-beta-ticket.md"))

    # ---- FREEZE ------------------------------------------------------------------------
    def test_claim_refused_under_freeze_unless_listed(self):
        self.write_freeze("# manager's list for the cutover\nT-0009\n")
        before = self.main_head()
        p = self.ts("claim", "T-0001", "qa")
        self.assertNotEqual(p.returncode, 0, "claim succeeded under FREEZE: " + p.out)
        self.assertIn("tickets/FREEZE", p.out)
        self.assertIn("T-0009", p.out)  # the message says what may be claimed
        self.assertEqual(self.main_head(), before)
        self.assertIn("status: ready", self.ticket_text("T-0001-alpha-ticket.md"))
        self.assertFalse(os.path.isdir(os.path.join(self.main, ".claude", "worktrees", "t0001-alpha-ticket")),
                         "refused claim must not create the worktree")
        self.assert_main_sane()
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("FREEZE is on", p.out)
        self.assertIn("T-0009", p.out)

        self.write_freeze("T-0009\nT-0001   # hotfix, added by the manager\n")
        p = self.ts("claim", "T-0001", "qa")
        self.assertEqual(p.returncode, 0, p.out)
        text = self.ticket_text("T-0001-alpha-ticket.md")
        self.assertIn("status: in-dev", text)
        self.assertIn(" qa: claimed -> in-dev.", text)
        self.assertNotIn("FORCED", text)
        self.assertTrue(os.path.isdir(os.path.join(self.main, ".claude", "worktrees", "t0001-alpha-ticket")))
        self.assert_main_sane()

    def test_claim_under_freeze_forced_is_stamped(self):
        self.write_freeze("T-0009\n")
        p = self.ts("claim", "T-0001", "qa", env=dict(self.env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        text = self.ticket_text("T-0001-alpha-ticket.md")
        self.assertIn("status: in-dev", text)
        self.assertIn(" qa: FORCED past tickets/FREEZE (not listed): claimed -> in-dev.", text)
        self.assert_main_sane()

    def test_freeze_match_is_whole_id(self):
        self.write_freeze("T-00011\n")  # must not satisfy T-0001
        p = self.ts("claim", "T-0001", "qa")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("tickets/FREEZE", p.out)

    def test_no_freeze_file_claim_as_before(self):
        p = self.ts("board")
        self.assertNotIn("FREEZE is on", p.out)
        p = self.ts("claim", "T-0001", "qa")
        self.assertEqual(p.returncode, 0, p.out)
        text = self.ticket_text("T-0001-alpha-ticket.md")
        self.assertIn("status: in-dev", text)
        self.assertIn(" qa: claimed -> in-dev.", text)
        self.assertNotIn("FORCED", text)
        self.assert_main_sane()

    # ---- evidence ----------------------------------------------------------------------


class MergeAndPruneHelpers(TicketShWorld):
    """the helpers of TestMergeAndPrune, shared by its generic tests here and its plugin-dependent tests in test_ticket_sh.py (T-0189)"""
    def claim(self, tid, env=None):
        p = self.ts("claim", tid, "qa", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        return p.out.split("work in: ", 1)[1].split()[0]  # the worktree path it printed

    def branch_commit(self, wt, name, msg, trailer="Ticket: T-0001"):
        with open(os.path.join(wt, name), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        self.git("-C", wt, "add", name)
        self.git("-C", wt, "commit", "-q", "-m", msg + ("\n\n" + trailer if trailer else ""))
        return self.git("-C", wt, "rev-parse", "--short=7", "HEAD")

    def push_branch(self, wt):
        br = self.git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD")
        self.git("-C", wt, "push", "-q", "-u", "origin", br)
        return br

    def to_ready_to_merge(self, tid, env=None):
        for st in ("in-review", "ready-to-merge"):
            p = self.ts("move", tid, st, "qa: -> %s" % st, env=env)
            self.assertEqual(p.returncode, 0, p.out)

    def rebase_and_push(self, wt):
        """what the owner does right before merge: the claim and both moves put tickets/ commits on trunk"""
        self.git("-C", wt, "fetch", "-q", "origin", TRUNK)
        self.git("-C", wt, "rebase", "-q", "origin/" + TRUNK)
        br = self.git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD")
        self.git("-C", wt, "push", "-q", "--force-with-lease", "origin", br)
        return [l.split()[0] for l in self.git("-C", wt, "log", "--format=%h", "origin/%s..HEAD" % TRUNK).splitlines()][::-1]

    def ready_branch(self, tid="T-0001", n=1, trailer="Ticket: T-0001"):
        wt = self.claim(tid)
        for i in range(n):
            self.branch_commit(wt, "f%d.txt" % i, "change %d" % i, trailer)
        br = self.push_branch(wt)
        self.to_ready_to_merge(tid)
        shas = self.rebase_and_push(wt)  # shas after the rebase: the ones closed_by must record
        self.assertEqual(len(shas), n)
        return wt, br, shas

    def origin_has_branch(self, br, origin=None):
        p = subprocess.run([REAL_GIT, "--git-dir", origin or self.origin, "show-ref", "--verify", "-q", "refs/heads/" + br], env=self.env)
        return p.returncode == 0

    def local_has_branch(self, br, repo=None):
        p = subprocess.run([REAL_GIT, "-C", repo or self.main, "show-ref", "--verify", "-q", "refs/heads/" + br], env=self.env)
        return p.returncode == 0

    # ---- merge -------------------------------------------------------------------------
    def merged_and_done(self):
        wt, br, shas = self.ready_branch()
        p = self.ts("merge", "T-0001"); self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("move", "T-0001", "done", "deploy: none, merged as %s" % shas[0]); self.assertEqual(p.returncode, 0, p.out)
        return wt, br

    def commit_tickets(self, msg="tickets: test setup"):
        self.git("-C", self.main, "add", "-A"); self.git("-C", self.main, "commit", "-q", "-m", msg)
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)

    def set_front(self, name, key, value):
        f = os.path.join(self.main, "tickets", name)
        with open(f, encoding="utf-8") as fh:
            fm, body = fh.read().split("\n---", 1)
        fm = "\n".join(("%s: %s" % (key, value)) if line.startswith(key + ":") else line for line in fm.split("\n"))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(fm + "\n---" + body)

    def fe_world(self):
        """a frontend origin + main clone, like the real second repo (TICKET_FE_REPO)"""
        fe_origin = os.path.join(self.tmp, "fe_origin.git"); fe_main = os.path.join(self.tmp, "fe_main")
        self.git("init", "-q", "--bare", "-b", TRUNK, fe_origin)
        self.git("clone", "-q", fe_origin, fe_main); self.git("-C", fe_main, "checkout", "-q", "-b", TRUNK)
        with open(os.path.join(fe_main, ".gitignore"), "w") as fh:
            fh.write(".claude/\n")
        with open(os.path.join(fe_main, "app.js"), "w") as fh:
            fh.write("v1\n")
        self.git("-C", fe_main, "add", "-A"); self.git("-C", fe_main, "commit", "-q", "-m", "fe init")
        self.git("-C", fe_main, "push", "-q", "-u", "origin", TRUNK)
        return fe_origin, fe_main, dict(self.env, TICKET_FE_REPO=fe_main)

    def prune_in_the_window(self, change, lock_wait="30"):
        """hold the lock, start `prune T-0001`, wait for pass 1's verdict line, apply `change`, release the lock; return the output"""
        lock = os.path.join(self.main, ".git", "ticket.sh.lock")
        holder = subprocess.Popen(["flock", "-o", lock, "sleep", "120"])
        self.addCleanup(holder.kill)
        time.sleep(0.3)
        proc = subprocess.Popen(["bash", os.path.join(self.main, "scripts", "ticket.sh"), "prune", "T-0001"], cwd=self.tmp,
                                env=dict(self.env, TICKET_LOCK_WAIT=lock_wait), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        seen = []
        for line in proc.stdout:
            seen.append(line)
            if "would remove" in line:
                break
        self.assertTrue(seen and "would remove" in seen[-1], "".join(seen))
        change()
        holder.kill(); holder.wait()
        rest = proc.communicate(timeout=90)[0]
        return proc.returncode, "".join(seen) + rest

    RACE_GIT = """#!/usr/bin/env bash
# a commit lands in the worktree right before the SECOND `ls-files -oi` (pass 2's worktree_verdict, after the sha check)
case " $* " in *" ls-files -oi "*)
  n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}
  if [ "$n" = 2 ]; then echo racy > {wt}/racy.txt; {git} -C {wt} add racy.txt; {git} -C {wt} commit -q -m "racy work"; {git} -C {wt} rev-parse HEAD > {sha}; fi;;
esac
exec {git} "$@"
"""

    def race_env(self, wt):
        d = os.path.join(self.tmp, "racebin"); os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "git")
        with open(path, "w") as fh:
            fh.write(self.RACE_GIT.format(cnt=os.path.join(self.tmp, "cnt"), wt=wt, git=REAL_GIT, sha=os.path.join(self.tmp, "racysha")))
        os.chmod(path, 0o755)
        return dict(self.env, PATH=d + os.pathsep + self.env["PATH"])


class TestMergeAndPrune(MergeAndPruneHelpers):
    """T-0129: `ticket.sh merge` is the only merge path (ff-only under the lock, every commit trailered,
    trunk pushed at once or the local merge undone, closed_by captured AFTER the merge) and
    `ticket.sh prune` removes a done/dropped ticket's worktree + branch (local and origin), refusing
    anything with commits outside trunk. RED on the base script: both commands are unknown there."""

    def test_claim_and_merge_clear_an_ask_and_a_second_ask_warns(self):
        """S3: every status change clears the ask (move did; claim and merge did not), and an overwrite is said out loud"""
        p = self.ts("ask", "T-0001", "DECIDE which bucket | reply T-0001 a or b | your answer | waits"); self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("ask", "T-0001", "DECIDE which bucket, really | reply T-0001 a or b | your answer | waits")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("note: this replaces the pending ask: DECIDE which bucket |", p.out)
        self.assertIn("(replaced: DECIDE which bucket | reply T-0001 a or b | your answer | waits)", self.log_of("T-0001-alpha-ticket.md"))
        wt = self.claim("T-0001")
        self.assertRegex(self.ticket_text("T-0001-alpha-ticket.md"), r"(?m)^human:\s*$")
        self.assertIn("the pending ask for the human was cleared by the claim.", self.log_of("T-0001-alpha-ticket.md"))
        self.branch_commit(wt, "f.txt", "change"); br = self.push_branch(wt); self.to_ready_to_merge("T-0001")
        self.rebase_and_push(wt)
        p = self.ts("ask", "T-0001", "APPROVE the merge | reply T-0001 ok | merged | waits"); self.assertEqual(p.returncode, 0, p.out)
        self.rebase_and_push(wt)   # the ask commit put trunk ahead
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertRegex(self.ticket_text("T-0001-alpha-ticket.md"), r"(?m)^human:\s*$")
        self.assertIn("(the pending ask for the human was cleared by the merge)", self.log_of("T-0001-alpha-ticket.md"))
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "0")

    def test_merge_ff_only_and_closed_by_after_merge(self):
        wt, br, shas = self.ready_branch(n=2)
        pre = self.main_head()
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        new = self.git("-C", self.main, "rev-parse", "HEAD~1")  # the ticket's own merged-commit sits on top
        self.assertNotEqual(new, pre)
        self.assertEqual(self.git("-C", self.main, "rev-parse", br), new, "trunk must be the branch tip (ff)")
        self.assertEqual(self.origin_head(), self.main_head(), "merged trunk + ticket commit pushed")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s, %s]" % tuple(shas))
        self.assertNotIn(pre[:7], self.front("T-0001-alpha-ticket.md", "closed_by"), "closed_by must never be the pre-merge sha (T-0011, T-0043)")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertIn("ready-to-merge -> merged. ticket.sh merge: ff-merged %s..%s, pushed" % (pre[:7], new[:7]), self.log_of("T-0001-alpha-ticket.md"))
        self.assertIn("next: scripts/ticket.sh move T-0001 done", p.out)  # deploy: none
        self.assertTrue(os.path.isdir(wt), "merge must not remove the worktree (prune does, at done)")
        self.assertTrue(os.path.exists(os.path.join(self.main, "f1.txt")))
        self.assert_main_sane()

    def test_merge_refuses_behind_trunk(self):
        wt, br, shas = self.ready_branch()
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        self.other_commit_ticket("T-0010", "o", "other", "tickets: other moved trunk")
        self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        p = self.ts("merge", "T-0001")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("1 commit(s) behind %s (0 of them touch files outside tickets/)" % TRUNK, p.out)
        self.assertIn("rebase first", p.out)
        self.assertIn("push --force-with-lease origin " + br, p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")), "nothing merged")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge")
        self.assert_main_sane()

    def test_merge_refuses_missing_trailer(self):
        wt, br, shas = self.ready_branch(trailer="")
        p = self.ts("merge", "T-0001")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("no 'Ticket: T-0001' trailer", p.out)
        self.assertIn(shas[0], p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")))
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge")
        self.assert_main_sane()

    def test_merge_foreign_trailer_needs_allow_and_is_logged_in_both(self):
        wt, br, shas = self.ready_branch(trailer="Ticket: T-0002")
        p = self.ts("merge", "T-0001")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("--allow-trailers T-0002", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")))
        p = self.ts("merge", "T-0001", "--allow-trailers", "T-0002")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertIn("trailers also allowed: T-0002", self.log_of("T-0001-alpha-ticket.md"))
        self.assertIn("T-0001 merged the shared branch", self.log_of("T-0002-beta-ticket.md"))
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()

    def test_merge_refuses_unpushed_branch(self):
        wt, br, shas = self.ready_branch()
        self.branch_commit(wt, "late.txt", "forgot to push")
        p = self.ts("merge", "T-0001")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("push the branch", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")))
        self.assert_main_sane()

    def test_merge_refuses_wrong_state(self):
        wt = self.claim("T-0001")
        self.branch_commit(wt, "f0.txt", "change")
        self.push_branch(wt)
        p = self.ts("move", "T-0001", "in-review", "qa: -> in-review")
        self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("merge", "T-0001")
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("not 'ready-to-merge'", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")))

    def test_merge_push_race_undoes_the_local_merge(self):
        wt, br, shas = self.ready_branch()
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        self.other_commit_ticket("T-0010", "o", "other", "tickets: other moved trunk during the merge")
        self.flag("race")  # the other clone pushes right before our trunk push -> genuine non-ff
        p = self.ts("merge", "T-0001", env=self.wrap_env())
        self.assertTrue(self.flag_gone("race"), "race never triggered")
        self.assertNotIn("race push failed", self.race_out())
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("local merge was undone", p.out)
        self.assertFalse(os.path.exists(os.path.join(self.main, "f0.txt")), "main must be back at the pre-merge tree")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge")
        self.assertEqual(self.sleep_calls(), [], "a non-ff must not back off")
        self.assert_main_sane()
        self.assertEqual(self.ahead(), 0, "nothing of ours left unpushed")

    def test_merge_frontend_only_ticket_lands_in_the_frontend_repo(self):
        fe_origin = os.path.join(self.tmp, "fe_origin.git"); fe_main = os.path.join(self.tmp, "fe_main")
        self.git("init", "-q", "--bare", "-b", TRUNK, fe_origin)
        self.git("clone", "-q", fe_origin, fe_main)
        self.git("-C", fe_main, "checkout", "-q", "-b", TRUNK)
        with open(os.path.join(fe_main, ".gitignore"), "w") as fh:
            fh.write(".claude/\n")
        with open(os.path.join(fe_main, "app.js"), "w") as fh:
            fh.write("v1\n")
        self.git("-C", fe_main, "add", "-A"); self.git("-C", fe_main, "commit", "-q", "-m", "fe init")
        self.git("-C", fe_main, "push", "-q", "-u", "origin", TRUNK)
        env = dict(self.env, TICKET_FE_REPO=fe_main)
        self.write_ticket("T-0003", "fe-ticket", "ready", "fe ticket", fields={"repos": "[frontend]"})
        self.git("-C", self.main, "add", "-A"); self.git("-C", self.main, "commit", "-q", "-m", "tickets: T-0003")
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        wt = self.claim("T-0003", env=env)
        self.assertTrue(wt.startswith(fe_main), wt)
        sha = self.branch_commit(wt, "app.js", "fe change", trailer="Ticket: T-0003")
        br = self.push_branch(wt)
        self.to_ready_to_merge("T-0003", env=env)
        be_pre = self.main_head(); fe_pre = self.git("-C", fe_main, "rev-parse", "HEAD")
        p = self.ts("merge", "T-0003", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.git("-C", fe_main, "rev-parse", "HEAD"), self.git("-C", fe_main, "rev-parse", br))
        self.assertEqual(self.git("--git-dir", fe_origin, "rev-parse", TRUNK), self.git("-C", fe_main, "rev-parse", "HEAD"), "frontend trunk pushed")
        self.assertEqual(self.front("T-0003-fe-ticket.md", "closed_by"), "[frontend %s]" % sha)
        self.assertIn("frontend %s..%s" % (fe_pre[:7], self.git("-C", fe_main, "rev-parse", "--short=7", "HEAD")), self.log_of("T-0003-fe-ticket.md"))
        self.assertEqual(self.git("-C", self.main, "rev-parse", "HEAD~1"), be_pre, "backend trunk only gained the ticket commit")
        self.assert_main_sane()
        # and prune in the frontend repo
        p = self.ts("move", "T-0003", "done", "deploy: none, merged", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("prune", "T-0003", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.isdir(wt))
        self.assertFalse(self.local_has_branch(br, fe_main))
        self.assertFalse(self.origin_has_branch(br, fe_origin))

    def test_merge_records_a_branch_already_on_trunk_instead_of_stranding_the_ticket(self):
        # the other half of a two-repo ticket landed before a failed push, or someone ff-merged by hand
        wt, br, shas = self.ready_branch(n=2)
        self.git("-C", self.main, "merge", "-q", "--ff-only", br)
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("was already on %s" % TRUNK, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s, %s]" % tuple(shas))  # from the ticket's base: field
        self.assertIn("already on %s" % TRUNK, self.log_of("T-0001-alpha-ticket.md"))
        self.assert_main_sane()

    # ---- prune -------------------------------------------------------------------------
    def test_prune_dry_run_then_real_removes_worktree_branch_and_remote(self):
        wt, br = self.merged_and_done()
        head = self.main_head()
        p = self.ts("prune", "T-0001", "--dry-run", env=self.env_without_who())  # no TICKET_AS needed for a listing
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("would remove worktree", p.out); self.assertIn("branch " + br, p.out); self.assertIn("origin/" + br, p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.local_has_branch(br)); self.assertTrue(self.origin_has_branch(br))
        self.assertEqual(self.main_head(), head, "dry-run commits nothing")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.isdir(wt)); self.assertFalse(self.local_has_branch(br)); self.assertFalse(self.origin_has_branch(br))
        self.assertIn("worktree removed (ticket.sh prune): T-0001 (backend): removed worktree .claude/worktrees/t0001-alpha-ticket, branch %s, origin/%s" % (br, br),
                      self.log_of("T-0001-alpha-ticket.md"))
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()
        p = self.ts("prune", "T-0001")  # idempotent
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("nothing left", p.out)

    def test_prune_refuses_unique_commits_and_dirty_worktree(self):
        wt, br = self.merged_and_done()
        extra = self.branch_commit(wt, "late.txt", "after the merge")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("1 commit(s) not in %s -- left alone" % TRUNK, p.out); self.assertIn(extra, p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))
        self.git("-C", wt, "reset", "-q", "--hard", "HEAD~1")
        with open(os.path.join(wt, "scratch.txt"), "w") as fh:
            fh.write("uncommitted\n")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("is dirty -- left alone", p.out)
        self.assertTrue(os.path.isdir(wt))
        os.remove(os.path.join(wt, "scratch.txt"))
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.isdir(wt))

    def test_prune_treats_a_rebased_copy_as_merged_but_not_an_edited_one(self):
        """the first real dry-run's only holdouts (T-0089, T-0092) were branches left at their pre-rebase tip:
        every commit had a patch-equivalent copy in trunk. Prune judges by `git cherry`, not ancestry."""
        wt, br = self.merged_and_done()
        a = self.branch_commit(wt, "late.txt", "late fix")
        b = self.branch_commit(wt, "other.txt", "other fix")
        self.git("-C", wt, "push", "-q", "--force", "origin", br)
        self.git("-C", self.main, "cherry-pick", a)                       # same patch, new sha: a rebased copy
        self.git("-C", self.main, "cherry-pick", b)
        with open(os.path.join(self.main, "other.txt"), "w", encoding="utf-8") as fh:
            fh.write("edited while resolving a conflict\n")               # same subject, different patch
        self.git("-C", self.main, "commit", "-q", "-a", "--amend", "--no-edit")
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        self.assertEqual(self.git("-C", self.main, "rev-list", "--count", "%s..%s" % (TRUNK, br)), "2", "neither is an ancestor")
        p = self.ts("prune", "T-0001", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("1 commit(s) not in %s -- left alone (no patch-equivalent in %s either)" % (TRUNK, TRUNK), p.out)
        self.assertIn(b, p.out); self.assertNotIn(a, p.out, "the rebased copy is not listed as unique")
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))
        self.git("-C", wt, "reset", "-q", "--hard", "HEAD~1")              # drop the edited one; only the copy remains
        self.git("-C", wt, "push", "-q", "--force", "origin", br)
        p = self.ts("prune", "T-0001", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("would remove worktree", p.out); self.assertIn("1 commit(s) already in %s as rebased copies" % TRUNK, p.out)
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.isdir(wt)); self.assertFalse(self.local_has_branch(br)); self.assertFalse(self.origin_has_branch(br))
        self.assertIn("as rebased copies", self.log_of("T-0001-alpha-ticket.md"))
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()

    def test_prune_refuses_open_tickets_and_done_iterates_in_one_commit(self):
        wt, br = self.merged_and_done()
        self.write_ticket("T-0003", "no-branch-left", "done", "gone already", fields={"branch": "t0003-no-branch-left", "worktree": ".claude/worktrees/t0003-no-branch-left"})
        self.git("-C", self.main, "add", "-A"); self.git("-C", self.main, "commit", "-q", "-m", "tickets: T-0003"); self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        p = self.ts("prune", "T-0002")  # in-dev
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("only done/dropped tickets are pruned", p.out)
        p = self.ts("prune", "--done", "--dry-run")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("would remove worktree", p.out); self.assertIn("T-0003 (backend): nothing left", p.out)
        before = self.main_head()
        p = self.ts("prune", "--done")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.isdir(wt))
        new_commits = self.git("-C", self.main, "log", "--format=%s", "%s..HEAD" % before).splitlines()
        self.assertEqual(new_commits, ["tickets: prune (worktrees removed): T-0001"])
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()

    def test_prune_treats_skipped_as_open(self):
        """T-0157: a skipped ticket is parked, not finished -- prune never removes it and it holds any branch it names.
        (GREEN on the base script too: skipped is 'open' by construction; this pins the property the README promises.)"""
        wt, br = self.merged_and_done()
        self.write_ticket("T-0003", "parked-on-same-branch", "skipped", "parked work", fields={"branch": br, "worktree": ".claude/worktrees/t0001-alpha-ticket"})
        self.commit_tickets("tickets: T-0003 skipped")
        p = self.ts("prune", "T-0003")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("T-0003 is 'skipped' -- only done/dropped tickets are pruned", p.out)
        head = self.main_head()
        p = self.ts("prune", "--done")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("T-0001 (backend): branch or worktree also named by open ticket(s) T-0003", p.out); self.assertIn("left alone", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.local_has_branch(br)); self.assertTrue(self.origin_has_branch(br))
        self.assertEqual(self.main_head(), head, "nothing pruned, nothing committed")
        self.assert_main_sane()


    # ---- review of f05cc3a (2026-10-12): blockers B1-B3, should-fixes 4-11, nits -------------------
    def test_prune_refuses_trunk_protected_and_odd_branch_names(self):
        """B1: a done ticket naming main, trunk, an old trunk, archive/* or an odd name must never reach the delete"""
        for name in ("main", "archive/old-thing", "feat/legacy-trunk-a"):
            self.git("-C", self.main, "push", "-q", "origin", "HEAD:refs/heads/" + name)
        cases = {"T-0005": "main", "T-0006": TRUNK, "T-0007": "archive/old-thing", "T-0008": "feat/legacy-trunk-a", "T-0009": "-rf"}
        for tid, br in cases.items():
            self.write_ticket(tid, "named-" + tid[-1], "done", "names " + br, fields={"branch": br})
        self.commit_tickets()
        before = self.main_head()
        for args in (list(cases) + ["--dry-run"], list(cases)):
            p = self.ts("prune", *args)
            self.assertEqual(p.returncode, 0, p.out)
            self.assertNotIn("would remove", p.out); self.assertNotIn("removed ", p.out)
            self.assertIn("branch 'main' -- refused: protected name", p.out)
            self.assertIn("branch '%s' -- refused: it is trunk" % TRUNK, p.out)
            self.assertIn("branch 'archive/old-thing' -- refused: protected name", p.out)
            self.assertIn("branch 'feat/legacy-trunk-a' -- refused: protected name", p.out)
            self.assertIn("branch '-rf' -- refused", p.out)
        for name in ("main", "archive/old-thing", "feat/legacy-trunk-a", TRUNK):
            self.assertTrue(self.origin_has_branch(name), name)
        self.assertEqual(self.main_head(), before, "nothing to log, no commit")

    def test_merge_refuses_a_trunk_or_protected_branch_name(self):
        """B1/B3: branch: dev used to exit 1 with no message at all (set -e in a pipefail'd substitution)"""
        self.write_ticket("T-0005", "on-trunk", "ready-to-merge", "branch is trunk", fields={"branch": TRUNK})
        self.write_ticket("T-0006", "on-main", "ready-to-merge", "branch is main", fields={"branch": "main"})
        self.commit_tickets()
        for name, why in (("T-0005-on-trunk.md", "it is trunk (%s)" % TRUNK), ("T-0006-on-main.md", "protected name")):
            p = self.ts("merge", name[:6])
            self.assertEqual(p.returncode, 1, p.out)
            self.assertIn("refusing to merge from", p.out); self.assertIn(why, p.out)
            self.assertEqual(self.front(name, "status"), "ready-to-merge")

    def test_merge_refuses_a_multi_repo_ticket_without_a_branch_for_every_repo(self):
        """B2: repos: [backend, frontend] with one plain branch used to merge the backend half silently"""
        self.write_ticket("T-0007", "two-repos", "ready", "two repos", fields={"repos": "[backend, frontend]"})
        self.commit_tickets()
        wt = self.claim("T-0007")
        self.branch_commit(wt, "x.txt", "x", "Ticket: T-0007")
        self.push_branch(wt); self.to_ready_to_merge("T-0007"); self.rebase_and_push(wt)
        before = self.origin_head()
        p = self.ts("merge", "T-0007")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("cannot merge", p.out); self.assertIn("is one plain name", p.out)
        self.assertEqual(self.origin_head(), before, "nothing merged")
        self.assertEqual(self.front("T-0007-two-repos.md", "status"), "ready-to-merge")
        self.write_ticket("T-0008", "auth-too", "ready-to-merge", "auth too",
                          fields={"repos": "[backend, auth]", "branch": "{backend: t0008-x, auth: t0008-x}"})
        self.commit_tickets()
        p = self.ts("merge", "T-0008")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("cannot merge", p.out); self.assertIn("'auth'", p.out)

    def test_merge_records_an_already_merged_branch_whose_tip_has_no_trailer(self):
        """B3 (probe P10): a hand ff-merge that included an untrailered fixup is recorded, closed_by = the trailered sha"""
        wt, br, shas = self.ready_branch(n=1)
        self.branch_commit(wt, "fixup.txt", "fixup without trailer", trailer="")
        self.git("-C", wt, "push", "-q", "origin", br)
        self.git("-C", self.main, "merge", "-q", "--ff-only", br); self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("was already on %s" % TRUNK, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s]" % shas[0])
        self.assert_main_sane()

    def test_move_to_merged_by_hand_is_refused_without_force(self):
        """should-fix 4: merge is the only path to `merged`"""
        wt, br, shas = self.ready_branch()
        before = self.main_head()
        p = self.ts("move", "T-0001", "merged", "ff-merged by hand")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("scripts/ticket.sh merge T-0001", p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge"); self.assertEqual(self.main_head(), before)
        p = self.ts("move", "T-0001", "merged", "merged by hand before T-0129, shas %s" % shas[0], env=dict(self.env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("FORCED (merged by hand, not ticket.sh merge)", self.log_of("T-0001-alpha-ticket.md"))

    def test_prune_leaves_a_detached_or_rebranched_worktree_alone(self):
        """should-fix 5 (probes P2, P2b): commits on a detached HEAD or another branch in the worktree were lost"""
        wt, br = self.merged_and_done()
        self.git("-C", wt, "checkout", "-q", "--detach")
        self.branch_commit(wt, "lost.txt", "detached work")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("detached HEAD", p.out); self.assertIn("left alone", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))
        self.git("-C", wt, "checkout", "-q", "-b", "side")
        self.branch_commit(wt, "side.txt", "side work")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("is on side, not on %s" % br, p.out)
        self.assertTrue(os.path.isdir(wt))
        self.git("-C", wt, "checkout", "-q", br)
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out); self.assertFalse(os.path.isdir(wt)); self.assertTrue(self.local_has_branch("side"))

    def test_prune_remote_delete_is_leased_on_the_sha_the_check_saw(self):
        """should-fix 6: origin moved between the check and the delete -> the delete is refused and git's words are shown"""
        wt, br = self.merged_and_done()
        env = self.wrap_env()
        with open(os.path.join(self.tmp, "lease_race"), "w") as fh:
            fh.write(br)
        p = self.ts("prune", "T-0001", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertTrue(self.flag_gone("lease_race"), "the wrapper fired")
        self.assertIn("delete of origin/%s refused" % br, p.out); self.assertIn("stale info", p.out)
        self.assertTrue(self.origin_has_branch(br)); self.assertFalse(os.path.isdir(wt)); self.assertFalse(self.local_has_branch(br))
        logged = self.log_of("T-0001-alpha-ticket.md").split("worktree removed (ticket.sh prune):", 1)[1]
        self.assertIn("removed worktree .claude/worktrees/t0001-alpha-ticket, branch %s" % br, logged)
        self.assertNotIn("origin/" + br, logged)

    def test_prune_leaves_a_branch_another_open_ticket_names(self):
        """should-fix 8 (probe P5): a worktree shared with an in-dev ticket"""
        wt, br = self.merged_and_done()
        self.set_front("T-0002-beta-ticket.md", "branch", br)
        self.commit_tickets("tickets: T-0002 shares the branch")
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("also named by open ticket(s) T-0002", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))

    def test_merge_logs_a_partial_two_repo_merge_and_finishes_on_rerun(self):
        """should-fix 9 (probe P9): the backend half lands, the frontend push fails -> logged, status kept, re-run completes"""
        fe_origin, fe_main, env = self.fe_world()
        self.git("-C", fe_main, "checkout", "-q", "-b", "t0001-fe")
        with open(os.path.join(fe_main, "b.js"), "w") as fh:
            fh.write("1\n")
        self.git("-C", fe_main, "add", "-A"); self.git("-C", fe_main, "commit", "-q", "-m", "fe change\n\nTicket: T-0001")
        fe_sha = self.git("-C", fe_main, "rev-parse", "--short=7", "HEAD")
        self.git("-C", fe_main, "push", "-q", "origin", "t0001-fe"); self.git("-C", fe_main, "checkout", "-q", TRUNK)
        wt = self.claim("T-0001", env=env)
        self.branch_commit(wt, "x.txt", "x"); br = self.push_branch(wt); self.to_ready_to_merge("T-0001", env=env)
        self.set_front("T-0001-alpha-ticket.md", "branch", "{backend: %s, frontend: t0001-fe}" % br)
        self.set_front("T-0001-alpha-ticket.md", "repos", "[backend, frontend]")
        self.commit_tickets("tickets: T-0001 two-repo map")
        shas = self.rebase_and_push(wt)
        hook = os.path.join(fe_origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\necho 'Internal Server Error' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        p = self.ts("merge", "T-0001", env=env)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("x.txt", self.git("--git-dir", self.origin, "ls-tree", "--name-only", TRUNK), "the backend half landed")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge")
        log = self.log_of("T-0001-alpha-ticket.md")
        self.assertIn("PARTIAL merge (ticket.sh merge): backend ff-merged", log); self.assertIn("frontend NOT merged", log)
        self.assertEqual(self.git("--git-dir", fe_origin, "rev-parse", TRUNK), self.git("-C", fe_main, "rev-parse", TRUNK), "frontend local merge undone")
        os.remove(hook)
        p = self.ts("merge", "T-0001", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("was already on %s" % TRUNK, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s, frontend %s]" % (shas[0], fe_sha))
        self.assert_main_sane()

    def test_prune_compares_with_origin_trunk_too(self):
        """should-fix 10: work merged into the LOCAL trunk only is not on origin yet -> left alone until pushed"""
        self.write_ticket("T-0003", "local-only", "done", "merged locally", fields={"branch": "t0003-local-only"})
        self.commit_tickets()
        self.git("-C", self.other, "checkout", "-q", "-b", "t0003-local-only")
        with open(os.path.join(self.other, "l.txt"), "w") as fh:
            fh.write("1\n")
        self.git("-C", self.other, "add", "-A"); self.git("-C", self.other, "commit", "-q", "-m", "local only\n\nTicket: T-0003")
        sha = self.git("-C", self.other, "rev-parse", "HEAD")
        self.git("-C", self.other, "push", "-q", "origin", "t0003-local-only")
        self.git("-C", self.main, "fetch", "-q", "origin", "t0003-local-only")
        self.git("-C", self.main, "cherry-pick", sha)  # a copy on the local trunk, not pushed
        p = self.ts("prune", "T-0003", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("not in refs/remotes/origin/%s -- left alone" % TRUNK, p.out); self.assertNotIn("would remove", p.out)
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        p = self.ts("prune", "T-0003", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("would remove origin/t0003-local-only", p.out)

    def test_prune_checks_before_taking_the_lock(self):
        """should-fix 11: the slow pass (fetches, cherry) runs without the lock; only the removals wait for it"""
        wt, br = self.merged_and_done()
        holder = subprocess.Popen(["flock", os.path.join(self.main, ".git", "ticket.sh.lock"), "sleep", "30"])
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        p = self.ts("prune", "T-0001", env=dict(self.env, TICKET_LOCK_WAIT="1"))
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("would remove worktree", p.out, "the checks ran first, without the lock")
        self.assertIn("held the lock for 1 s", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))
        holder.kill(); holder.wait()
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out); self.assertFalse(os.path.isdir(wt))

    def test_merge_refuses_a_multi_id_trailer_unless_allowed(self):
        """nit (probe P7): "Ticket: T-0001, T-0002" on one line names two tickets"""
        wt, br, shas = self.ready_branch(trailer="Ticket: T-0001, T-0002")
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("trailer T-0002 is another ticket", p.out)
        p = self.ts("merge", "T-0001", "--allow-trailers", "T-0002")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("shared branch, trailers also allowed: T-0002", self.log_of("T-0001-alpha-ticket.md"))
        self.assertIn("T-0001 merged the shared branch", self.log_of("T-0002-beta-ticket.md"))

    def test_merge_allow_trailers_needs_real_ids(self):
        """nit: --allow-trailers without a value used to crash on an unset $2"""
        for args, msg in ((("T-0001", "--allow-trailers"), "needs the ids"), (("T-0001", "--allow-trailers", "bogus"), "not a T-xxxx id"),
                          (("T-0001", "--allow-trailers", "T-0001"), "is this ticket"), (("T-0001", "--allow-trailers", "T-0999"), "no ticket T-0999")):
            p = self.ts("merge", *args)
            self.assertEqual(p.returncode, 1, p.out); self.assertIn(msg, p.out)

    def test_prune_leaves_an_unregistered_worktree_path_alone(self):
        """nit: the worktree: field points at a plain directory"""
        self.write_ticket("T-0003", "plain-dir", "done", "plain dir", fields={"branch": "t0003-plain-dir", "worktree": ".claude/worktrees/t0003-plain-dir"})
        self.commit_tickets()
        self.git("-C", self.main, "branch", "t0003-plain-dir")
        d = os.path.join(self.main, ".claude", "worktrees", "t0003-plain-dir"); os.makedirs(d)
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("not a worktree\n")
        p = self.ts("prune", "T-0003")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("is not a registered worktree", p.out)
        self.assertTrue(os.path.isfile(os.path.join(d, "notes.txt"))); self.assertTrue(self.local_has_branch("t0003-plain-dir"))

    # ---- prune after a failed `git worktree remove`, and prune --orphans (T-0196) ----------------------
    def test_prune_finishes_a_worktree_remove_that_left_copies_of_repo_objects(self):
        """T-0196: on NFS `git worktree remove` dropped the registration and left tickets/T-0017..T-0034 behind
        ("Directory not empty"); prune said "worktree remove failed, left alone" over a dir that was no worktree any more
        and kept the branch. The wrapper replays it with two tracked files and an ignored file the verdict had approved:
        every one has a copy in the repo, so prune finishes the removal and the log says what git left."""
        wt, br = self.merged_and_done()
        os.makedirs(os.path.join(wt, ".claude"))
        with open(os.path.join(wt, ".claude", "cache.txt"), "w") as fh:
            fh.write("ignored scratch\n")   # .claude/ is ignored: the verdict lets it go, git may leave it too
        with open(os.path.join(self.tmp, "leave_behind"), "w") as fh:
            fh.write("tickets/T-0001-alpha-ticket.md\ntickets/_TEMPLATE.md\n.claude/cache.txt\n")
        p = self.ts("prune", "T-0001", env=self.wrap_env())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertNotIn("left alone", p.out)
        self.assertIn("prune: T-0001 (backend): git worktree remove dropped the registration but left 3 file(s) in %s "
                      "(error: failed to delete '%s': Directory not empty); every one is a copy of a repo object or an ignored file "
                      "already approved, so prune removed the directory" % (wt, wt), p.out)
        self.assertFalse(os.path.exists(wt)); self.assertFalse(self.local_has_branch(br)); self.assertFalse(self.origin_has_branch(br))
        self.assertNotIn("t0001-alpha-ticket", self.git("-C", self.main, "worktree", "list"))
        log = self.log_of("T-0001-alpha-ticket.md")
        self.assertIn("removed worktree .claude/worktrees/t0001-alpha-ticket, branch %s, origin/%s" % (br, br), log)
        self.assertIn("(git left 3 file(s) behind -- 'error: failed to delete", log); self.assertIn("-- prune removed the directory)", log)
        self.assert_main_sane()

    def test_prune_keeps_a_left_behind_dir_holding_a_file_the_repo_never_had_and_says_so(self):
        """the same failure, but one leftover has content no repo object holds: the dir stays, named, the message says git
        had already dropped the registration (no "left alone" over a non-worktree), the merged branch still goes, and the
        orphans sweep names the dir too and keeps it"""
        wt, br = self.merged_and_done()
        with open(os.path.join(self.tmp, "leave_behind"), "w") as fh:
            fh.write("tickets/_TEMPLATE.md\n+notes/stray.txt\n")
        p = self.ts("prune", "T-0001", env=self.wrap_env())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: T-0001 (backend): git worktree remove dropped the registration but left %s "
                      "(error: failed to delete '%s': Directory not empty); 1 of 2 file(s) in it have no copy in the repo's objects "
                      "-- directory KEPT" % (wt, wt), p.out)
        self.assertIn("\n    notes/stray.txt\n", p.out); self.assertIn("scripts/ticket.sh prune --orphans", p.out)
        self.assertNotIn("worktree remove failed, left alone", p.out)
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes", "stray.txt")))
        self.assertNotIn("t0001-alpha-ticket", self.git("-C", self.main, "worktree", "list"))
        self.assertFalse(self.local_has_branch(br)); self.assertFalse(self.origin_has_branch(br))
        self.assertIn("removed branch %s, origin/%s (worktree dir KEPT: git left 2 file(s), 1 with no copy in the repo's objects "
                      "-- 'error: failed to delete" % (br, br), self.log_of("T-0001-alpha-ticket.md"))
        self.assert_main_sane()
        p = self.ts("prune", "--orphans", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0001-alpha-ticket (backend): not a registered worktree; 1 of 2 file(s) "
                      "have no copy in the repo's objects -- left alone (look at these, then delete it by hand):\n    notes/stray.txt\n", p.out)
        self.assertIn("nothing to remove", p.out)
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes", "stray.txt")))

    def test_prune_leaves_a_worktree_alone_when_git_refuses_to_remove_it(self):
        """git refused up front (locked, or dirty in the window): still registered, nothing changed -- the old message, made exact"""
        wt, br = self.merged_and_done()
        self.flag("refuse_remove")
        p = self.ts("prune", "T-0001", env=self.wrap_env())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: T-0001 (backend): worktree remove refused, worktree left alone (still registered): "
                      "fatal: validation failed, cannot remove working tree:", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.local_has_branch(br)); self.assertTrue(self.origin_has_branch(br))
        self.assertIn("t0001-alpha-ticket", self.git("-C", self.main, "worktree", "list"))
        self.assertNotIn("worktree removed (ticket.sh prune)", self.log_of("T-0001-alpha-ticket.md"))

    def test_prune_orphans_removes_unregistered_dirs_that_hold_only_repo_objects(self):
        """--orphans: a dir under .claude/worktrees/ that no worktree registers goes when every file in it is a copy of a repo
        object (git's leftover .git pointer file aside); a tNNNN-* name is logged on its ticket, a registered worktree is never touched"""
        wt, br = self.merged_and_done()   # a registered worktree: never an orphan
        self.write_ticket("T-0003", "gamma", "done", "gamma"); self.commit_tickets()
        wtd = os.path.join(self.main, ".claude", "worktrees")
        d = os.path.join(wtd, "t0003-gamma"); os.makedirs(os.path.join(d, "tickets")); os.makedirs(os.path.join(d, "empty"))
        shutil.copy(os.path.join(self.main, "tickets", "_TEMPLATE.md"), os.path.join(d, "tickets", "_TEMPLATE.md"))
        shutil.copy(os.path.join(self.main, "scripts", "ticket.sh"), os.path.join(d, "ticket.sh"))
        with open(os.path.join(d, ".git"), "w") as fh:
            fh.write("gitdir: %s/.git/worktrees/gone\n" % self.main)
        a = os.path.join(wtd, "agent-0123abcd"); os.makedirs(a)
        shutil.copy(os.path.join(self.main, ".gitignore"), os.path.join(a, ".gitignore"))
        head = self.main_head()
        p = self.ts("prune", "--orphans", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0003-gamma (backend): not a registered worktree, 2 file(s), "
                      "every one a copy of a repo object -- would remove", p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/agent-0123abcd (backend): not a registered worktree, 1 file(s), "
                      "every one a copy of a repo object -- would remove", p.out)
        self.assertNotIn("t0001-alpha-ticket", p.out)
        self.assertTrue(os.path.isdir(d)); self.assertTrue(os.path.isdir(a)); self.assertEqual(self.main_head(), head, "dry-run commits nothing")
        p = self.ts("prune", "--orphans")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0003-gamma (backend): removed (2 file(s), every one a copy of a repo object)", p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/agent-0123abcd (backend): removed (1 file(s), every one a copy of a repo object)", p.out)
        self.assertFalse(os.path.exists(d)); self.assertFalse(os.path.exists(a)); self.assertTrue(os.path.isdir(wt))
        self.assertIn("worktree removed (ticket.sh prune): T-0003 (backend): orphan dir .claude/worktrees/t0003-gamma removed "
                      "(2 file(s), every one a copy of a repo object, left behind by an earlier worktree remove)", self.log_of("T-0003-gamma.md"))
        self.assertEqual(self.main_log().splitlines()[0], "tickets: prune (worktrees removed): T-0003")
        self.assertEqual(self.origin_head(), self.main_head()); self.assert_main_sane()
        p = self.ts("prune", "--orphans")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("nothing to remove", p.out)

    def test_prune_orphans_keeps_a_dir_with_unique_files_or_one_an_open_ticket_names(self):
        wtd = os.path.join(self.main, ".claude", "worktrees")
        d = os.path.join(wtd, "t0004-delta"); os.makedirs(d)
        shutil.copy(os.path.join(self.main, "tickets", "_TEMPLATE.md"), os.path.join(d, "_TEMPLATE.md"))
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("never committed\n")
        os.mkfifo(os.path.join(d, "pipe"))
        e = os.path.join(wtd, "t0002-beta-ticket"); os.makedirs(e)   # the in-dev ticket names it
        self.set_front("T-0002-beta-ticket.md", "worktree", ".claude/worktrees/t0002-beta-ticket"); self.commit_tickets()
        head = self.main_head()
        p = self.ts("prune", "--orphans")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0004-delta (backend): not a registered worktree; 2 of 3 file(s) have no copy "
                      "in the repo's objects -- left alone (look at these, then delete it by hand):\n    notes.txt\n    pipe (not a regular file)\n", p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0002-beta-ticket (backend): not a registered worktree, but open ticket(s) T-0002 "
                      "name it -- left alone", p.out)
        self.assertIn("nothing to remove", p.out)
        self.assertTrue(os.path.isfile(os.path.join(d, "notes.txt"))); self.assertTrue(os.path.isdir(e))
        self.assertEqual(self.main_head(), head)
        p = self.ts("prune", "--bogus")
        self.assertNotEqual(p.returncode, 0); self.assertIn("usage: prune [T-xxxx ...|--done] [--orphans] [--dry-run]", p.out)

    def test_prune_orphans_keeps_a_dir_it_cannot_list_in_full(self):
        """review of T-0196: a subdirectory find cannot read (mode 000: a root-owned volume is the real case) made the listing
        empty, so "nothing unique" and rm -rf would have taken a unique file with it -- a failed walk keeps the dir"""
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 directory")
        wtd = os.path.join(self.main, ".claude", "worktrees")
        d = os.path.join(wtd, "t0006-dark"); sub = os.path.join(d, "volume"); os.makedirs(sub)
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("never committed\n")
        os.chmod(sub, 0); self.addCleanup(os.chmod, sub, 0o755)
        p = self.ts("prune", "--orphans")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0006-dark (backend): not a registered worktree; 1 of ? file(s) have no copy in the "
                      "repo's objects -- left alone (look at these, then delete it by hand):\n    . (could not be listed in full -- an unreadable subdirectory?)\n", p.out)
        self.assertIn("nothing to remove", p.out)
        self.assertTrue(os.path.isfile(os.path.join(d, "notes.txt"))); self.assertTrue(os.path.isdir(sub))

    def test_prune_orphans_keeps_a_clone_and_a_live_worktree_pointer_but_not_a_stale_one(self):
        """review of T-0196: a top-level .git was skipped whatever it was, so a standalone clone under .claude/worktrees/ (unpushed
        history and all) counted as "nothing unique"; only git's stale `gitdir:` pointer file is nothing to keep"""
        wtd = os.path.join(self.main, ".claude", "worktrees"); os.makedirs(wtd, exist_ok=True)
        clone = os.path.join(wtd, "t0007-clone"); self.git("clone", "-q", self.main, clone)
        with open(os.path.join(clone, "unpushed.txt"), "w") as fh:
            fh.write("only here\n")
        self.git("-C", clone, "add", "unpushed.txt"); self.git("-C", clone, "commit", "-q", "-m", "unpushed work")
        live = os.path.join(wtd, "t0008-live"); os.makedirs(live)
        shutil.copy(os.path.join(self.main, "tickets", "_TEMPLATE.md"), os.path.join(live, "_TEMPLATE.md"))
        with open(os.path.join(live, ".git"), "w") as fh:
            fh.write("gitdir: %s/.git\n" % self.main)   # exists: somebody's worktree, not ours to judge
        stale = os.path.join(wtd, "t0009-stale"); os.makedirs(stale)
        shutil.copy(os.path.join(self.main, "tickets", "_TEMPLATE.md"), os.path.join(stale, "_TEMPLATE.md"))
        with open(os.path.join(stale, ".git"), "w") as fh:
            fh.write("gitdir: %s/.git/worktrees/gone\n" % self.main)
        p = self.ts("prune", "--orphans")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0007-clone (backend): not a registered worktree; 2 of", p.out)
        self.assertIn("\n    .git (a repository of its own -- a clone, not a worktree)\n    unpushed.txt\n", p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0008-live (backend): not a registered worktree; 1 of 1 file(s) have no copy in the "
                      "repo's objects -- left alone (look at these, then delete it by hand):\n    .git (points at a live gitdir: %s/.git)\n" % self.main, p.out)
        self.assertIn("prune: orphan dir .claude/worktrees/t0009-stale (backend): removed (1 file(s), every one a copy of a repo object)", p.out)
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git"))); self.assertTrue(os.path.isfile(os.path.join(live, "_TEMPLATE.md")))
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(self.git("-C", clone, "log", "--format=%s", "-1"), "unpushed work")

    def test_prune_deletes_only_under_the_worktrees_dir(self):
        """review of T-0196: worktree: is a path somebody wrote (an absolute one is allowed, a symlink possible); after a failed
        remove, prune resolves it and deletes only strictly under .claude/worktrees/ -- anywhere else the dir is kept and said so"""
        wt, br = self.merged_and_done()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.git("-C", self.main, "worktree", "move", wt, elsewhere)
        self.set_front("T-0001-alpha-ticket.md", "worktree", elsewhere); self.commit_tickets()
        with open(os.path.join(self.tmp, "leave_behind"), "w") as fh:
            fh.write("tickets/_TEMPLATE.md\n")
        p = self.ts("prune", "T-0001", env=self.wrap_env())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("prune: T-0001 (backend): git worktree remove dropped the registration but left %s (error: failed to delete '%s': "
                      "Directory not empty); it resolves to '%s', outside %s/.claude/worktrees/, the only place prune deletes in -- directory KEPT"
                      % (elsewhere, elsewhere, os.path.realpath(elsewhere), self.main), p.out)
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "tickets", "_TEMPLATE.md")))
        self.assertFalse(self.local_has_branch(br))
        self.assertIn("(worktree dir KEPT: outside .claude/worktrees/, not prune's to delete -- 'error: failed to delete", self.log_of("T-0001-alpha-ticket.md"))

    def test_prune_orphans_refuses_a_worktrees_dir_that_resolves_outside_the_repo(self):
        """review of T-0196: the loader checks the WORKTREES text; a symlink out of the repo it cannot see, managed_path does --
        nothing under it is removed, and the dry-run says so too"""
        outside = os.path.join(self.tmp, "outside"); d = os.path.join(outside, "t0010-out"); os.makedirs(d)
        shutil.copy(os.path.join(self.main, "tickets", "_TEMPLATE.md"), os.path.join(d, "_TEMPLATE.md"))   # a copy of a repo object: only the containment rule keeps it
        os.makedirs(os.path.join(self.main, ".claude"), exist_ok=True); os.symlink(outside, os.path.join(self.main, ".claude", "worktrees"))
        for args in (("prune", "--orphans", "--dry-run"), ("prune", "--orphans")):
            p = self.ts(*args)
            self.assertEqual(p.returncode, 0, p.out)
            self.assertIn("prune: orphan dir .claude/worktrees/t0010-out (backend): %s/.claude/worktrees does not resolve to a directory strictly inside "
                          "the repo and outside its .git, or .claude/worktrees/t0010-out is not strictly inside it -- left alone (prune deletes nowhere else)" % self.main, p.out)
            self.assertIn("nothing to remove", p.out)
        self.assertTrue(os.path.isfile(os.path.join(d, "_TEMPLATE.md")))

    def test_prune_handles_a_plain_named_two_repo_ticket_per_repo_and_skips_a_branchless_one(self):
        """merge refuses a plain branch on repos: [backend, frontend] (which repo?); prune looks in both repos, each on its
        own merits -- 14 of the real done tickets are like that. A done ticket that never had a branch says nothing."""
        wt, br = self.merged_and_done()
        self.set_front("T-0001-alpha-ticket.md", "repos", "[backend, frontend]")
        self.write_ticket("T-0003", "never-claimed", "done", "closed by declaration")
        self.commit_tickets("tickets: T-0001 two repos, T-0003 branchless")
        p = self.ts("prune", "--done", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("T-0001 (backend): would remove worktree", p.out)
        self.assertIn("T-0001 (frontend): repo %s not found -- left alone" % os.path.join(self.tmp, "no-frontend"), p.out)
        self.assertNotIn("T-0003", p.out); self.assertNotIn("fix the ticket", p.out)
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out); self.assertFalse(os.path.isdir(wt)); self.assertFalse(self.origin_has_branch(br))


    # ---- re-review of 20a8c9f (2026-10-12): the lock window, ignored state dirs, stale reads, trailerless merges --------
    def test_prune_rereads_the_ticket_under_the_lock(self):
        """should-fix 3 (probe N7): the ticket was reopened on origin after pass 1 read the local, stale copy"""
        wt, br = self.merged_and_done()
        self.git("-C", self.other, "pull", "-q", "--rebase", "origin", TRUNK)
        f = os.path.join(self.other, "tickets", "T-0001-alpha-ticket.md")
        with open(f, encoding="utf-8") as fh:
            txt = fh.read()
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(txt.replace("status: done", "status: in-dev", 1))
        self.git("-C", self.other, "commit", "-qam", "tickets: T-0001 reopened"); self.git("-C", self.other, "push", "-q", "origin", TRUNK)
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("would remove worktree", p.out, "pass 1 still saw the stale local copy")
        self.assertIn("is 'in-dev' now (changed since the check) -- left alone", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "in-dev", "main pulled the reopening")
        self.assert_main_sane()

    def test_merge_refuses_an_already_merged_branch_with_no_trailered_commit(self):
        """should-fix 4 (probe N3): recording the trunk tip as closed_by would name an unrelated commit"""
        wt = self.claim("T-0001"); br = self.push_branch(wt)
        self.to_ready_to_merge("T-0001")
        self.git("-C", wt, "fetch", "-q", "origin", TRUNK); self.git("-C", wt, "reset", "-q", "--hard", "origin/" + TRUNK)
        self.git("-C", wt, "push", "-q", "-f", "origin", br)
        p = self.ts("merge", "T-0001")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("no commit in", p.out); self.assertIn("carries 'Ticket: T-0001'", p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge"); self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[]")
        p = self.ts("merge", "T-0001", env=dict(self.env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "merged")
        self.assertIn("FORCED: no commit carries 'Ticket: T-0001'", self.log_of("T-0001-alpha-ticket.md"))
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s]" % self.git("-C", wt, "rev-parse", "--short=7", "HEAD"))

    def test_prune_leaves_a_frontend_ticket_alone_when_its_trunk_is_missing(self):
        """probes N8/N9: a missing local trunk, or a wrong TICKET_FE_TRUNK, used to kill the script with git's fatal (exit 128)"""
        fe_origin, fe_main, env = self.fe_world()
        self.git("-C", fe_main, "checkout", "-q", "-b", "t0009-fe")
        with open(os.path.join(fe_main, "u.js"), "w") as fh:
            fh.write("u\n")
        self.git("-C", fe_main, "add", "-A"); self.git("-C", fe_main, "commit", "-q", "-m", "fe unique\n\nTicket: T-0009")
        self.git("-C", fe_main, "push", "-q", "origin", "t0009-fe"); self.git("-C", fe_main, "checkout", "-q", TRUNK)
        self.write_ticket("T-0009", "fe", "done", "fe", fields={"repos": "[frontend]", "branch": "t0009-fe"})
        self.commit_tickets()
        self.git("-C", fe_main, "checkout", "-q", "-b", "elsewhere"); self.git("-C", fe_main, "branch", "-q", "-D", TRUNK)
        p = self.ts("prune", "T-0009", "--dry-run", env=dict(env, TICKET_AS=""))
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("trunk '%s' is not a branch of" % TRUNK, p.out); self.assertIn("left alone", p.out)
        p = self.ts("prune", "T-0009", env=dict(env, TICKET_FE_TRUNK="devv"))
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("trunk 'devv' is not a branch of", p.out)
        self.assertTrue(self.origin_has_branch("t0009-fe", fe_origin))

    def test_prune_shared_branch_match_is_exact_not_substring(self):
        """nit: T-0002 naming `t0001-alpha-ticket-extra` must not hold `t0001-alpha-ticket`"""
        wt, br = self.merged_and_done()
        self.set_front("T-0002-beta-ticket.md", "branch", br + "-extra")
        self.set_front("T-0002-beta-ticket.md", "worktree", ".claude/worktrees/" + os.path.basename(wt) + "-extra")
        self.commit_tickets("tickets: T-0002 names a superstring")
        p = self.ts("prune", "T-0001", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("would remove worktree", p.out); self.assertNotIn("also named", p.out)
        self.set_front("T-0002-beta-ticket.md", "branch", "{backend: %s, frontend: fe-x}" % br)
        self.commit_tickets("tickets: T-0002 names it in a map")
        p = self.ts("prune", "T-0001", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("also named by open ticket(s) T-0002", p.out)

    def test_prune_leaves_a_squash_merged_branch_alone(self):
        """documented behaviour (probe N1): a squash leaves no patch-equivalent copies, so the branch is listed, not deleted"""
        wt = self.claim("T-0001")
        self.branch_commit(wt, "a.txt", "a"); self.branch_commit(wt, "b.txt", "b"); br = self.push_branch(wt)
        self.git("-C", self.main, "merge", "-q", "--squash", br); self.git("-C", self.main, "commit", "-q", "-m", "squash of %s" % br)
        self.git("-C", self.main, "push", "-q", "origin", TRUNK)
        self.set_front("T-0001-alpha-ticket.md", "status", "done"); self.commit_tickets()
        p = self.ts("prune", "T-0001")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("2 commit(s) not in %s -- left alone" % TRUNK, p.out); self.assertIn(" a\n", p.out); self.assertIn(" b\n", p.out)
        self.assertTrue(os.path.isdir(wt)); self.assertTrue(self.origin_has_branch(br))


    # ---- third review of 17c3007 (2026-10-13): the delete is a compare-and-swap; trailerless already-merged refused before any merge
    def test_prune_branch_delete_is_a_compare_and_swap(self):
        """R1 (probe_c): a commit between the sha check and the delete used to be lost by the `branch -D` fallback for rebased copies"""
        wt, br = self.merged_and_done()
        a = self.branch_commit(wt, "late.txt", "late fix")
        self.git("-C", wt, "push", "-q", "--force", "origin", br)
        self.git("-C", self.main, "cherry-pick", a); self.git("-C", self.main, "push", "-q", "origin", TRUNK)   # a rebased copy: -d would refuse, -D would not
        p = self.ts("prune", "T-0001", env=self.race_env(wt))
        self.assertEqual(p.returncode, 0, p.out)
        racy = open(os.path.join(self.tmp, "racysha")).read().strip()
        self.assertIn("moved since the check -- branch kept", p.out)
        self.assertTrue(self.local_has_branch(br), "the branch with the racy commit survives")
        self.assertEqual(self.git("-C", self.main, "rev-parse", br), racy)
        self.assertFalse(os.path.isdir(wt), "the (clean) worktree itself went")
        self.assert_main_sane()

    def test_merge_refuses_a_two_repo_ticket_whole_when_the_frontend_half_has_no_trailered_commit(self):
        """R3 (probe_c): the frontend branch is already on its trunk without a trailered commit -> refused BEFORE the backend moves"""
        fe_origin, fe_main, env = self.fe_world()
        self.git("-C", fe_main, "branch", "t0001-fe"); self.git("-C", fe_main, "push", "-q", "origin", "t0001-fe")   # == fe trunk tip
        wt = self.claim("T-0001", env=env)
        self.branch_commit(wt, "x.txt", "x"); br = self.push_branch(wt); self.to_ready_to_merge("T-0001", env=env)
        self.set_front("T-0001-alpha-ticket.md", "branch", "{backend: %s, frontend: t0001-fe}" % br)
        self.set_front("T-0001-alpha-ticket.md", "repos", "[backend, frontend]")
        self.commit_tickets("tickets: T-0001 two-repo map")
        shas = self.rebase_and_push(wt)
        before = self.origin_head()
        p = self.ts("merge", "T-0001", env=env)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("(frontend): the branch is already on %s, but no commit in the last 50 commits below" % TRUNK, p.out)
        self.assertEqual(self.origin_head(), before, "the backend half did not move")
        self.assertNotIn("x.txt", self.git("--git-dir", self.origin, "ls-tree", "--name-only", TRUNK))
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready-to-merge")
        self.assertNotIn("PARTIAL", self.log_of("T-0001-alpha-ticket.md"))
        p = self.ts("merge", "T-0001", env=dict(env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        fe_tip = self.git("-C", fe_main, "rev-parse", "--short=7", "t0001-fe")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "closed_by"), "[%s, frontend %s]" % (shas[0], fe_tip))
        self.assertIn("frontend already on %s (tip %s; FORCED: no commit carries 'Ticket: T-0001')" % (TRUNK, fe_tip), self.log_of("T-0001-alpha-ticket.md"))
        self.assert_main_sane()


class HumanInboxHelpers(TicketShWorld):
    """the helpers of TestHumanInbox, shared by its generic tests here and its plugin-dependent tests in test_ticket_sh.py (T-0189)"""
    def fixture(self, tid, slug, status, title, fields=None, log=None, handoff=None, investigation=None, plain=None):
        path = self.write_ticket(tid, slug, status, title, fields=fields, plain=plain)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if log:
            text = text.replace("\n## Log\n", "\n## Log\n" + "".join("- %s\n" % l for l in log), 1)
        if investigation:
            text = text.replace("\n## Investigation\n", "\n## Investigation\n" + investigation + "\n", 1)
        if handoff:
            text = text.rstrip("\n") + "\n\n" + handoff + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def world(self):
        os.makedirs(os.path.join(self.main, "terraform"), exist_ok=True)
        with open(os.path.join(self.main, "terraform", "x.tfplan"), "w") as fh:
            fh.write("plan\n")
        block = ("## Deploy handoff -- batch 2026-10-12 (T-0003 + T-0004)\n"
                 "Trunk:     dev @ abc1234 (pushed).\n"
                 "Summary:   Plan: 0 to add, 3 to change, 0 to destroy.\n"
                 "  ~ aws_lambda_function.bridge   source_code_hash   T-0003\n"
                 "APPLY (human):   scripts/ticket.sh deploy-check x.tfplan --tickets T-0003,T-0004 --at abc1234 && terraform -chdir=terraform apply x.tfplan      (exit 1 = a FAIL line)\n"
                 "Verify:    aws lambda get-function-configuration --function-name bridge --query CodeSha256\n")
        self.fixture("T-0003", "batch-owner", "ready-to-deploy", "cached answers from the faster branch",
                     fields={"severity": "P1", "deploy": "terraform", "plan": "terraform/x.tfplan"},
                     log=["2026-10-12 17:55 qa: merged -> ready-to-deploy. handoff written"], handoff=block)
        self.fixture("T-0004", "batch-mate", "ready-to-deploy", "msa files for uploaded msas",
                     fields={"severity": "P3", "deploy": "terraform", "plan": "terraform/x.tfplan"},
                     log=["2026-10-12 17:56 qa: merged -> ready-to-deploy. ships in T-0003's plan"])
        self.fixture("T-0005", "needs-decision", "blocked", "which bucket for the uploads",
                     fields={"severity": "P2", "blocked_on": "which bucket: the artifacts one or a new one?"},
                     log=["2026-10-12 15:08 qa: ready -> blocked. needs the human"], investigation="Recommendation: the artifacts bucket, prefix uploads/")
        self.fixture("T-0006", "low-decision", "blocked", "colour of the banner", fields={"severity": "P3", "blocked_on": "blue or green?"})
        self.fixture("T-0008", "waits-on-ticket", "blocked", "waits on another ticket", fields={"severity": "P1", "blocked_on": "T-0005"})
        self.fixture("T-0007", "live-check", "deployed", "invite email goes out",
                     fields={"severity": "P2", "deploy": "terraform"},
                     log=["2026-10-12 16:00 qa: ready-to-deploy -> deployed. serial 334 -> 335"],
                     handoff="## Deploy handoff -- batch 2026-10-11 (T-0007)\nSummary:   Plan: 1 to add, 0 to change, 0 to destroy.\n"
                             "APPLY (human):   terraform -chdir=terraform apply w.tfplan\nVerify (human):   send yourself an invite and open the mail\n")
        self.commit_main("tickets: inbox fixtures")


class TestHumanInbox(HumanInboxHelpers):
    """T-0147: `ticket.sh human` is the human's list, derived from the tickets (applies per plan with the newest-plan
    rule, blocked_on questions, `human:` asks, live checks naming the human); `ticket.sh ask` sets the one-line
    `human:` field and a move clears it. RED on the base script: both commands are unknown there."""

    def test_ask_sets_clears_and_a_move_clears(self):
        p = self.ts("ask", "T-0002", "CHECK x | y", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("export TICKET_AS=<your ListAgents name>", p.out)
        for bad, msg in (("please check x | y", "starts with a verb"), ("CHECK x without the pipe", "needs at least"), ("CHECK x # y | z", "no '#'")):
            p = self.ts("ask", "T-0002", bad)
            self.assertEqual(p.returncode, 1, p.out); self.assertIn(msg, p.out)
        p = self.ts("ask", "T-0002", "")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("has no pending ask", p.out)
        p = self.ts("ask", "T-0002", "APPROVE one paid run, about $2 | reply \"T-0002 ok $2\" | nothing to see | T-0002 waits")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("asked:", p.out)
        text = self.ticket_text("T-0002-beta-ticket.md")
        self.assertRegex(text, r"(?m)^human: APPROVE one paid run, about \$2 \| reply \"T-0002 ok \$2\" \| nothing to see \| T-0002 waits   # qa 20\d\d-\d\d-\d\d \d\d:\d\d$")
        self.assertIn("ask (human:): APPROVE one paid run", text)
        self.assertIn("== 1 ask(s) pending for the human: scripts/ticket.sh human", self.ts("board", env=self.env_without_who()).out)
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "1")
        self.assertIn("APPROVE T-0002  P2  one paid run, about $2", self.ts("human", env=self.env_without_who()).out)
        p = self.ts("ask", "T-0002", "")
        self.assertEqual(p.returncode, 0, p.out)
        text = self.ticket_text("T-0002-beta-ticket.md")
        self.assertRegex(text, r"(?m)^human:\s*$"); self.assertIn("ask cleared.", text)
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "0")
        p = self.ts("ask", "T-0002", "CHECK the thing | open it | it works | nothing"); self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("move", "T-0002", "in-review", "qa: suites 3 passed; RED on base: assertion")
        self.assertEqual(p.returncode, 0, p.out)
        text = self.ticket_text("T-0002-beta-ticket.md")
        self.assertRegex(text, r"(?m)^human:\s*$"); self.assertIn("(the pending ask for the human was cleared by this move)", text)
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "0")
        self.assertEqual(self.origin_head(), self.main_head())
        self.assert_main_sane()

    def test_human_with_nothing_waiting(self):
        p = self.ts("human", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("Nothing is waiting for you.", p.out)
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "0")
        self.assertEqual(json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout), [])
        p = self.ts("human", "--bogus", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("usage: human", p.out)

    # ---- T-0167: the sitting protocol (T-0155) is withdrawn -- the inbox is continuous ----------------------------
    def test_human_header_has_no_sitting_and_an_ask_is_listed_at_once_by_priority_then_age(self):
        """User, 2026-10-13: "remove the logic of sitting and next sitting". The header is one line again -- no next
        sitting, no freeze -- `--next-sitting` is unknown, TICKET_SITTINGS is ignored, and an ask is on the list the
        moment it is made, ordered by priority, then kind, then the oldest first. RED on 1cfa389: line 2 of the header
        is "next sitting: ...", and --next-sitting prints a time."""
        env = dict(self.env_without_who(), TICKET_SITTINGS="*")   # nothing reads it any more: no "?" line, no error
        p = self.ts("human", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        lines = p.out.splitlines()
        self.assertTrue(lines[0].startswith("HUMAN INBOX  "), p.out)
        self.assertEqual(lines[1], "Nothing is waiting for you.", "the header is one line; nothing about a sitting under it:\n" + p.out)
        self.assertNotRegex(p.out, r"(?i)sitting|freez|froze")
        p = self.ts("human", "--next-sitting", env=env)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("usage: human [--count|--json]", p.out)
        self.assertNotRegex(p.out, r"\d\d:\d\d ET")
        # three asks already on tickets (a human: line carries its own stamp, as `ask` writes it), one more made now
        self.fixture("T-0003", "old-low", "ready", "old low ask",
                     fields={"severity": "P3", "human": "CHECK the old one | open a | it works | T-0003 waits   # qa 2026-10-12 09:00"})
        self.fixture("T-0004", "new-low", "ready", "newer low ask",
                     fields={"severity": "P3", "human": "CHECK the newer one | open b | it works | T-0004 waits   # qa 2026-10-12 11:00"})
        self.fixture("T-0005", "newest-high", "ready", "newest high ask",
                     fields={"severity": "P1", "human": "CHECK the high one | open c | it works | T-0005 waits   # qa 2026-10-12 12:00"})
        self.fixture("T-0006", "urgent", "in-dev", "a live regression", fields={"severity": "P0"})
        self.commit_main("tickets: ask fixtures")
        p = self.ts("ask", "T-0006", "DECIDE roll back or fix forward | reply in the tickets window | T-0006 fix forward | the site stays broken")
        self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("human", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        lines = p.out.splitlines()
        self.assertTrue(lines[1].startswith("4 item(s). Do them top to bottom."), p.out)
        self.assertNotRegex(p.out, r"(?i)sitting|freez|froze")
        items = json.loads(self.ts("human", "--json", env=env).stdout)
        self.assertEqual([(i["verb"], i["tickets"], i["priority"]) for i in items],
                         [("DECIDE", "T-0006", "P0"), ("CHECK", "T-0005", "P1"), ("CHECK", "T-0003", "P3"), ("CHECK", "T-0004", "P3")],
                         "the ask made this minute is listed at once; priority first, then the oldest: " + p.out)
        self.assertEqual([i["since"] for i in items][1:], ["2026-10-12 12:00", "2026-10-12 09:00", "2026-10-12 11:00"])
        self.assertEqual(self.ts("human", "--count", env=env).out.strip(), "4")
        self.assert_main_sane()

    # ---- review of 7715037 (2026-10-12): the inbox must never print a wrong or void command ------------------------
    def test_an_ask_command_gets_the_cd_prefix_only_when_it_is_repo_relative(self):
        """T-0192: `scripts/...`, `./...`, `python3 ...`, `bash ...`, `make ...` and `VAR=... cmd` asks are prefixed with `cd <main> &&`; a bare
        tool name is not -- `terraform ...` was in that list (a leftover of the deploy tooling in the core) and is not any more. RED on 0cac7ee."""
        self.write_ticket("T-0003", "a", "in-dev", "a"); self.write_ticket("T-0004", "b", "in-dev", "b"); self.write_ticket("T-0005", "c", "in-dev", "c")
        self.commit_main("tickets: three asks")
        for tid, cmd in (("T-0003", "scripts/check.sh --all"), ("T-0004", "terraform -chdir=infra plan"), ("T-0005", "python3 tools/x.py")):
            p = self.ts("ask", tid, "RUN the thing | %s | ok | waits" % cmd)
            self.assertEqual(p.returncode, 0, p.out)
        out = self.ts("human", env=self.env_without_who()).out
        self.assertIn("   cd %s && scripts/check.sh --all\n" % self.main, out)
        self.assertIn("   cd %s && python3 tools/x.py\n" % self.main, out)
        self.assertIn("   terraform -chdir=infra plan\n", out)
        self.assertNotIn("&& terraform", out)

    def test_ask_keeps_backslash_sequences_literal(self):
        """B5: awk -v turned a literal \\n or \\t into a real newline/tab and corrupted the front matter"""
        text = r"CHECK the log line a\nb | grep -c 'x\ty' /tmp/log | 1 | nothing"
        p = self.ts("ask", "T-0002", text)
        self.assertEqual(p.returncode, 0, p.out)
        ticket = self.ticket_text("T-0002-beta-ticket.md")
        self.assertEqual(ticket.count("\nhuman:"), 1)
        self.assertIn("\nhuman: " + text + "   # qa ", ticket)
        self.assertIn("ask (human:): " + text, ticket)
        fm = ticket.split("\n---", 1)[0]
        self.assertEqual(len([l for l in fm.splitlines() if l.startswith("human:")]), 1)
        p = self.ts("human", "--json", env=self.env_without_who())
        self.assertEqual(json.loads(p.stdout)[0]["command"], r"grep -c 'x\ty' /tmp/log")
        p = self.ts("set", "T-0002", "blocked_on", r"literal \n stays")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("\nblocked_on: literal \\n stays\n", self.ticket_text("T-0002-beta-ticket.md"))

    def test_ask_refuses_a_fifth_field_and_control_characters(self):
        """S1 + B5: a '|' inside the command would split the fields silently; a real newline would break the front matter"""
        p = self.ts("ask", "T-0002", "RUN count them | ls | wc -l | 3 | nothing")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("at most four fields", p.out)
        p = self.ts("ask", "T-0002", "RUN two lines | ls\nrm -rf x | ok | nothing")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("one line", p.out)
        self.assertRegex(self.ticket_text("T-0002-beta-ticket.md"), r"(?m)^human:\s*(#.*)?$")

    def test_human_prints_a_long_question_verbatim_and_wrapped(self):
        """S2: blocked_on was cut at 400 characters (T-0072 lost half its question)"""
        q = " ".join("word%03d" % i for i in range(120))   # 959 characters
        self.fixture("T-0005", "long-question", "blocked", "a long one", fields={"severity": "P2", "blocked_on": q})
        self.commit_main("tickets: long question")
        p = self.ts("human", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        body = p.out.split("question: ", 1)[1].split("\n   expect:", 1)[0]
        self.assertEqual(" ".join(body.split()), q, "verbatim, every word")
        self.assertTrue(all(len(l) <= 130 for l in body.splitlines()), "wrapped")
        self.assertEqual(json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)[0]["command"], "question: " + q)

    def test_human_lists_a_question_on_a_non_blocked_ticket(self):
        """S4: a backlog ticket with a question in blocked_on (T-0061) is the human's too"""
        self.fixture("T-0005", "backlog-question", "backlog", "which SES sender", fields={"severity": "P2", "blocked_on": "which verified SES sender for Cognito emails?"})
        self.fixture("T-0006", "p3-backlog-question", "backlog", "banner colour", fields={"severity": "P3", "blocked_on": "blue or green?"})
        self.commit_main("tickets: questions outside blocked")
        p = self.ts("human", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("DECIDE  T-0005  P2  which SES sender", p.out)
        self.assertIn("question: which verified SES sender for Cognito emails?", p.out)
        self.assertIn("if you do nothing: T-0005 stays backlog   [ticket is backlog, the question is in its blocked_on:]", p.out)
        self.assertIn("Blocked on you but not urgent (P3): T-0006", p.out)


    # ---- re-review of 7fdf9ba (2026-10-12): every plan a command names is void-checked; asks carry no placeholder ----------
    def test_human_json_items_carry_a_stable_id(self):
        """id = first 10 hex of sha1("verb|tickets|command|expect"): the same across re-renders, different once `ask`
        replaces the text; --count and the text output do not change. RED on 48c3630: no "id" key."""
        p = self.ts("ask", "T-0002", "CHECK the chip | open https://example.invalid/inference | the chip appears | nothing")
        self.assertEqual(p.returncode, 0, p.out)
        a = json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)
        b = json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)
        self.assertEqual(len(a), 1); self.assertEqual(a, b, "two renders, one list")
        item = a[0]
        self.assertEqual(list(item)[0], "id"); self.assertRegex(item["id"], r"^[0-9a-f]{10}$")
        want = hashlib.sha1(("%s|%s|%s|%s" % (item["verb"], item["tickets"], item["command"], item["expect"])).encode()).hexdigest()[:10]
        self.assertEqual(item["id"], want); self.assertEqual(item["command"], "open https://example.invalid/inference")
        p = self.ts("ask", "T-0002", "CHECK the chip | open https://example.invalid/inference | the chip appears, blue | nothing")
        self.assertEqual(p.returncode, 0, p.out)
        c = json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)
        self.assertNotEqual(c[0]["id"], item["id"], "a replaced ask is a new item"); self.assertEqual(c[0]["tickets"], "T-0002")
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "1")
        self.assertNotIn(c[0]["id"], self.ts("human", env=self.env_without_who()).out, "the id is for tools, not for the human's text")
        self.assert_main_sane()


class TestSkipped(TicketShWorld):
    """T-0157: `skipped` -- set aside on the user's word ("t72 - lets skip it for now, make a category called skipped
    for that"). Enters from any state before merged, leaves only to backlog or ready (the ready gate applies); the
    question in blocked_on is kept but never asked: `human` never lists a skipped ticket, `ask` refuses it, the board
    shows a counted SKIPPED section after BACKLOG; prune keeps its branch (TestMergeAndPrune.test_prune_treats_skipped_as_open).
    RED on the base script: "state 'skipped' not one of: ...", the inbox lists the parked question, no SKIPPED section."""

    def test_skipped_enters_before_merge_and_leaves_to_backlog_or_ready_only(self):
        parked = self.write_ticket("T-0003", "parked-question", "blocked", "web invite page", fields={"blocked_on": "who is a super admin?"})
        self.write_ticket("T-0004", "already-merged", "merged", "on trunk already", fields={"deploy": "none"})
        self.commit_main("tickets: skipped fixtures")
        p = self.ts("ask", "T-0003", "DECIDE who is a super admin | reply T-0003 and the group name | one line | stays blocked")
        self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("move", "T-0003", "skipped", 'user 2026-10-13: "t72 - lets skip it for now"')
        self.assertEqual(p.returncode, 0, p.out)
        with open(parked, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("\nstatus: skipped\n", text)
        self.assertIn("\nblocked_on: who is a super admin?\n", text, "the question is kept")
        self.assertRegex(text, r"(?m)^human:\s*$")
        self.assertIn('blocked -> skipped. user 2026-10-13: "t72 - lets skip it for now" (the pending ask for the human was cleared by this move)',
                      self.log_of("T-0003-parked-question.md"))
        for tid in ("T-0001", "T-0002"):  # ready and in-dev enter too
            p = self.ts("move", tid, "skipped", "user: skip it for now")
            self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("move", "T-0004", "skipped", "user: skip it for now")  # merged code ships with the next plan regardless
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("merged -> skipped skips a gate", p.out)
        for st in ("in-dev", "in-review", "done", "dropped", "blocked"):
            p = self.ts("move", "T-0003", st, "qa: straight out")
            self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("allowed from skipped: backlog ready", p.out)
        p = self.ts("move", "T-0003", "ready", "desk: back")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("skipped -> ready needs a written ## Investigation", p.out)
        p = self.ts("claim", "T-0001", "qa")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0001 is 'skipped', not 'ready'", p.out)
        p = self.ts("ask", "T-0001", "DECIDE anything | reply | one line | nothing")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0001 is skipped: set aside on the user's word, nothing is asked for it", p.out)
        p = self.ts("move", "T-0003", "backlog", "desk: back in the queue, user said go")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("skipped -> backlog. desk: back in the queue", self.log_of("T-0003-parked-question.md"))
        self.assert_main_clean()

    def test_human_never_lists_skipped_and_board_counts_them_after_backlog(self):
        self.write_ticket("T-0003", "parked-question", "skipped", "web invite page", fields={"severity": "P2", "blocked_on": "who is a super admin?"})
        self.write_ticket("T-0005", "parked-too", "skipped", "second parked one",
                          fields={"severity": "P1", "human": "DECIDE stale ask | reply T-0005 x | one line | nothing   # qa 2026-10-12 10:00"})
        self.write_ticket("T-0006", "in-backlog", "backlog", "a backlog question", fields={"severity": "P2", "blocked_on": "which colour?"})
        self.git("-C", self.main, "branch", "t0007-parked-with-work")  # parked from in-dev: the branch stays, and so must its stats
        self.write_ticket("T-0007", "parked-with-work", "skipped", "parked with work",
                          fields={"severity": "P2", "owner": "qa", "branch": "t0007-parked-with-work", "claimed": "2026-10-12 09:00"})
        self.write_ticket("T-0008", "waits-on-parked", "blocked", "waits on a parked ticket", fields={"severity": "P2", "blocked_on": "T-0003"})
        self.commit_main("tickets: skipped fixtures")
        p = self.ts("human", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        for absent in ("T-0003", "T-0005", "super admin", "stale ask"):
            self.assertNotIn(absent, p.out, p.out)
        self.assertIn("T-0006", p.out); self.assertIn("question: which colour?", p.out)  # a backlog question is still asked
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "1")
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        out = p.out
        self.assertIn("== SKIPPED (3): set aside on the user's word (T-0157); nothing is asked; back with: move T-xxxx backlog|ready", out)
        self.assertLess(out.index("== BACKLOG"), out.index("== SKIPPED (3)"))
        self.assertIn("T-0003  P2  web invite page", out); self.assertIn("blocked_on: who is a super admin?", out)
        self.assertIn("T-0005  P1  second parked one", out)
        self.assertNotIn("ask(s) pending for the human", out, "an ask left on a skipped ticket is not counted")
        row = [l for l in out.splitlines() if l.startswith("  T-0007 ")][0]
        self.assertIn("qa  behind 1  ahead 0", row, row)  # the fixtures commit left the parked branch one behind; unpushed work would show as ahead
        self.assertIn("claimed ", row)
        blocked_rows = [l for l in out.split("== BLOCKED", 1)[1].split("\n==", 1)[0].splitlines() if l.startswith("  T-")]
        self.assertEqual([l.split()[0] for l in blocked_rows], ["T-0008"], "only the waiting ticket is under BLOCKED, the parked one is not")
        self.assertIn("T-0008  P2  waits on a parked ticket", out)
        self.assertIn("blocked_on: T-0003  !! T-0003 is SKIPPED (parked on the user's word): nothing will unblock this", out)
        self.assertNotIn("T-0007", self.ts("human", env=self.env_without_who()).out)


class TestBoardJson(TicketShWorld):
    """T-0158: `board --json` is the board as ONE JSON object for tools (the ops panel, the status line): the front matter of
    every ticket, counts per state, pending asks, freeze, warnings -- front matter only, no git per branch (the text board
    takes 30 s on the real repo); `--slow` adds behind/ahead/dirty per in-progress ticket and the ORPHANS. The text board is
    unchanged. RED on 48c3630: `board` ignores its arguments and prints the text board, so json.loads fails."""
    STATES = ("backlog", "ready", "in-dev", "in-review", "ready-to-merge", "merged", "ready-to-deploy", "deployed", "done", "blocked", "dropped", "skipped")
    LIST_KEYS = ("repos", "areas", "affects", "depends_on", "siblings", "closed_by")
    KEYS = ("id title type severity status repos areas affects deploy owner worktree branch base claimed created depends_on "
            "siblings blocked_on human closed_by plan resolution").split()

    def text_board_counts(self, out):
        """-> ({state: rows under its header} + done/dropped from the summary line, pending asks) read off the text board"""
        counts, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^== ([A-Z-]+)(?: \((\d+)\))?(?::|$)", line)
            if m and m.group(1).lower() in self.STATES:
                cur = m.group(1).lower(); counts.setdefault(cur, 0); continue
            if line.startswith("== "):
                cur = None
            if cur and line.startswith("  T-"):
                counts[cur] += 1
        m = re.search(r"== DONE/DROPPED: (\d+) done, (\d+) dropped", out)
        counts["done"], counts["dropped"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"== (\d+) ask\(s\) pending for the human", out)
        return counts, int(m.group(1)) if m else 0

    def board_json(self, *args):
        t0 = time.time(); p = self.ts("board", "--json", *args, env=self.env_without_who()); dt = time.time() - t0
        self.assertEqual(p.returncode, 0, p.out)
        b = json.loads(p.stdout)   # on base this is the text board: the parse fails first
        self.assertEqual(p.stderr, "", "nothing on stderr for a tool: " + p.stderr)
        return b, dt

    def test_board_json_is_front_matter_only_typed_by_key_and_matches_the_text_board(self):
        # states and odd values as the real repo has them: a quoted title, a two-repo branch map, deploy written as a list,
        # a free-text closed_by, a '#'-commented human line, a done ticket whose stale ask must not count
        self.write_ticket("T-0003", "rtd", "ready-to-deploy", 'needs "quotes" and a \\ backslash',
                          fields={"severity": "P1", "plan": "terraform/x.tfplan", "deploy": "terraform", "closed_by": "[abc1234]"})
        self.write_ticket("T-0004", "two-repo", "in-review", "two repo work",
                          fields={"repos": "[backend, frontend]", "branch": "{backend: t0004-be, frontend: t0004-fe}", "owner": "qa",
                                  "deploy": "[terraform, frontend]", "siblings": "[T-0003, T-0005]"})
        self.write_ticket("T-0005", "merged", "merged", "merged one", fields={"closed_by": "579f5d7 8130835 (backend) ; 3328833 (frontend)"})
        self.write_ticket("T-0006", "blocked", "blocked", "needs a decision",
                          fields={"severity": "P2", "blocked_on": "which bucket?", "human": "DECIDE which bucket | reply | T-0006 a | waits   # qa 2026-10-12 09:00"})
        self.write_ticket("T-0007", "done", "done", "finished", fields={"human": "CHECK stale | x | y | z   # qa 2026-10-12 09:00"})
        self.write_ticket("T-0008", "skipped", "skipped", "parked", fields={"blocked_on": "T-0006"})
        self.write_ticket("T-0009", "dropped", "dropped", "will not do", fields={"resolution": "wontfix"})
        self.commit_main("tickets: board json fixtures"); before = self.main_head()
        b, dt = self.board_json()
        self.assertLess(dt, 2.0, "board --json must stay fast (no git per branch): %.1fs" % dt)
        self.assertEqual(list(b), ["version", "generated", "trunk", "trunk_sha", "slow", "freeze", "counts", "asks_pending", "warnings", "tickets", "orphans"])   # version first since T-0186
        self.assertRegex(b["generated"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual((b["trunk"], b["trunk_sha"]), (TRUNK, self.main_head()[:7]))
        self.assertFalse(b["slow"]); self.assertIsNone(b["freeze"]); self.assertIsNone(b["orphans"]); self.assertEqual(b["warnings"], [])
        self.assertEqual(b["counts"], {"backlog": 0, "ready": 1, "in-dev": 1, "in-review": 1, "ready-to-merge": 0, "merged": 1,
                                       "ready-to-deploy": 1, "deployed": 0, "done": 1, "blocked": 1, "dropped": 1, "skipped": 1})
        self.assertEqual(b["asks_pending"], 1, "T-0007 is done: its stale human: line is not pending")
        self.assertEqual([t["id"] for t in b["tickets"]], ["T-%04d" % i for i in range(1, 10)])
        for t in b["tickets"]:
            self.assertEqual(list(t), self.KEYS + ["plain", "stats"], t["id"]); self.assertIsNone(t["stats"])   # plain: T-0217
            for k in self.KEYS:
                self.assertIsInstance(t[k], list if k in self.LIST_KEYS else str, "%s.%s typed by key" % (t["id"], k))
        by = {t["id"]: t for t in b["tickets"]}
        self.assertEqual(by["T-0003"]["title"], 'needs "quotes" and a \\ backslash')
        self.assertEqual((by["T-0003"]["severity"], by["T-0003"]["plan"], by["T-0003"]["closed_by"]), ("P1", "terraform/x.tfplan", ["abc1234"]))
        self.assertEqual((by["T-0004"]["repos"], by["T-0004"]["siblings"]), (["backend", "frontend"], ["T-0003", "T-0005"]))
        self.assertEqual(by["T-0004"]["branch"], "{backend: t0004-be, frontend: t0004-fe}", "a map stays the text as written")
        self.assertEqual(by["T-0004"]["deploy"], "[terraform, frontend]", "deploy is a string key even when an old ticket wrote a list")
        self.assertEqual(by["T-0005"]["closed_by"], ["579f5d7 8130835 (backend) ; 3328833 (frontend)"], "free text in a list key = one element")
        self.assertEqual(by["T-0006"]["human"], "DECIDE which bucket | reply | T-0006 a | waits", "the '# who stamp' comment is stripped, as field() does")
        self.assertEqual((by["T-0006"]["blocked_on"], by["T-0008"]["blocked_on"], by["T-0009"]["resolution"]), ("which bucket?", "T-0006", "wontfix"))
        self.assertEqual((by["T-0001"]["owner"], by["T-0001"]["closed_by"], by["T-0001"]["depends_on"], by["T-0001"]["created"]), ("", [], [], "2026-10-08 10:00"))
        text = self.ts("board", env=self.env_without_who()).out
        counts, asks = self.text_board_counts(text)
        self.assertEqual({k: v for k, v in b["counts"].items() if v}, {k: v for k, v in counts.items() if v}, text)
        self.assertEqual(b["asks_pending"], asks)
        for args in (("--bogus",), ("--json", "extra"), ("--json", "--slow", "x"), ("--slow",)):
            p = self.ts("board", *args, env=self.env_without_who())
            self.assertEqual(p.returncode, 1, p.out); self.assertIn("usage: board [--json [--slow]]", p.out)
        self.assertEqual(self.main_head(), before, "board commits nothing"); self.assert_main_sane()

    def test_board_json_freeze_warnings_and_slow_stats_and_orphans_keep_the_text_board(self):
        p = self.ts("claim", "T-0001", "qa"); self.assertEqual(p.returncode, 0, p.out)   # a real branch + worktree, in-dev
        wt = os.path.join(self.main, ".claude", "worktrees", "t0001-alpha-ticket")
        with open(os.path.join(wt, "scratch.txt"), "w") as fh:
            fh.write("dirty\n")
        self.write_ticket("T-0003", "gone", "in-review", "branch and worktree gone", fields={"owner": "qa", "branch": "gone-branch", "worktree": ".claude/worktrees/gone"})
        self.write_ticket("T-0004", "two-repo", "in-review", "two repo work",
                          fields={"repos": "[backend, frontend]", "owner": "qa", "branch": "{backend: t0001-alpha-ticket, frontend: fe-x}",
                                  "worktree": "{backend: .claude/worktrees/t0001-alpha-ticket, frontend: no-frontend/.claude/worktrees/fe-x}"})
        self.write_freeze("T-0009\nT-0001   # hotfix\n")   # commits + pushes; the branch of T-0001 falls behind
        self.git("-C", self.other, "checkout", "-q", "-b", "stray")
        with open(os.path.join(self.other, "stray.txt"), "w") as fh:
            fh.write("x\n")
        self.git("-C", self.other, "add", "-A"); self.git("-C", self.other, "commit", "-q", "-m", "stray work")
        self.git("-C", self.main, "fetch", "-q", self.other, "stray:stray")   # a local branch no ticket names
        behind = int(self.git("-C", self.main, "rev-list", "--count", "t0001-alpha-ticket..%s" % TRUNK)); self.assertGreaterEqual(behind, 1)
        stray_behind = int(self.git("-C", self.main, "rev-list", "--count", "stray..%s" % TRUNK))
        b, _ = self.board_json()
        self.assertEqual(b["freeze"], ["T-0001", "T-0009"]); self.assertIsNone(b["orphans"])
        self.assertTrue(all(t["stats"] is None for t in b["tickets"]), "no git per branch without --slow")
        b, _ = self.board_json("--slow")
        self.assertTrue(b["slow"]); by = {t["id"]: t for t in b["tickets"]}
        self.assertEqual(by["T-0001"]["status"], "in-dev")
        self.assertEqual(by["T-0001"]["stats"], {"behind": behind, "ahead": 0, "dirty": 1, "branch_missing": False, "worktree_missing": False})
        self.assertEqual(by["T-0003"]["stats"], {"behind": None, "ahead": None, "dirty": None, "branch_missing": True, "worktree_missing": True})
        self.assertEqual(by["T-0004"]["stats"]["backend"], by["T-0001"]["stats"], "a map picks each repo's branch")
        self.assertEqual(by["T-0004"]["stats"]["frontend"], {"behind": None, "ahead": None, "dirty": None, "branch_missing": True, "worktree_missing": True})
        self.assertIsNone(by["T-0002"]["stats"], "in-dev without a branch has nothing to measure")
        self.assertEqual(b["orphans"], [{"branch": "stray", "ahead": 1, "behind": stray_behind}])
        text = self.ts("board", env=self.env_without_who()).out
        self.assertIn("  stray: 1 commits not in trunk, %d behind" % stray_behind, text, "the text board's ORPHANS line is unchanged")
        self.assertIn("behind %d  ahead 0  dirty 1 !" % behind, text)
        self.assertIn("== FREEZE is on (tickets/FREEZE): claim is limited to: T-0001 T-0009", text)
        with open(os.path.join(self.main, "untracked.txt"), "w") as fh:
            fh.write("x\n")
        b, _ = self.board_json()
        self.assertEqual(b["warnings"], ["MAIN WORKTREE IS DIRTY: 1 entries -- ticket.sh will refuse to run"])
        self.assertIn("!! MAIN WORKTREE IS DIRTY: 1 entries", self.ts("board", env=self.env_without_who()).out)
        os.remove(os.path.join(self.main, "untracked.txt"))
        self.assert_main_sane()
    def test_json_output_escapes_every_control_character(self):
        """Review of fb97be9: the panel parses both outputs, so \\, " and EVERY control character must round-trip through
        json.loads -- in a title (board) and in a blocked_on question (human). RED on fb97be9: \x01, \x0b, \x0c went out
        raw (invalid JSON) and \r was dropped. An ASK with a control character is refused instead: \x1f is the inbox's own
        field separator and would split the record (found while writing this test)."""
        raw = "ctl \x01 vt \x0b ff \x0c tab \t cr \r bs \\ q \" bar | end"
        self.write_ticket("T-0003", "ctl", "blocked", raw, fields={"severity": "P1", "blocked_on": "decide " + raw})
        self.commit_main("tickets: control characters")
        for bad in ("CHECK a\x01b | run it | ok | nothing", "CHECK a | run it | ok | nothing \x1f", "CHECK a | run\x7fit | ok | nothing"):
            p = self.ts("ask", "T-0002", bad)
            self.assertEqual(p.returncode, 1, p.out); self.assertIn("one line of printable text", p.out)
        b, _ = self.board_json()
        by = {t["id"]: t for t in b["tickets"]}
        self.assertEqual((by["T-0003"]["title"], by["T-0003"]["blocked_on"]), (raw, "decide " + raw))
        p = self.ts("human", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        items = {i["verb"]: i for i in json.loads(p.stdout)}
        self.assertEqual(list(items), ["DECIDE"], p.stdout)
        self.assertEqual((items["DECIDE"]["title"], items["DECIDE"]["command"]), (raw, "question: decide " + raw))
        self.assertEqual(self.ts("human", "--count", env=self.env_without_who()).out.strip(), "1")
        self.assertEqual(self.ts("human", env=self.env_without_who()).returncode, 0)
        self.assert_main_sane()


class TestPlainWords(HumanInboxHelpers):
    """T-0217 (core 1.2.0): a ticket's `## In plain words` section (T-0215: right after the front matter, 2-4 sentences
    the human can understand) reaches tools as `plain` -- one line, HTML comments and <placeholder> lines dropped -- in
    every `board --json` ticket and every `human --json` item, and backlog|skipped -> ready refuses a ticket without it
    (TICKET_FORCE=1 passes with FORCED in the log). RED on 1.1.3 (db5b765): no `plain` key anywhere; the move succeeds."""
    PLAIN = ("The invite page shows a blank box where the sign-in button should be.\n"
             "Until it is fixed nobody new can be invited; once it is done the button is back.\n"
             "Nothing to do on your side.")
    ONE_LINE = ("The invite page shows a blank box where the sign-in button should be. Until it is fixed nobody new can be "
                "invited; once it is done the button is back. Nothing to do on your side.")

    def test_board_json_tickets_carry_plain_as_one_line(self):
        self.write_ticket("T-0003", "with-plain", "backlog", "blank box", plain=self.PLAIN)
        self.write_ticket("T-0004", "comment-only", "backlog", "placeholder only",
                          plain="<!-- 2-4 sentences for the human:\n     what is wrong, why it matters -->\n<write them here>")
        self.write_ticket("T-0005", "tricky", "backlog", "escaping", plain='He said "stop" \\ twice\t-- and <!-- not this --> this.')
        self.commit_main("tickets: plain words")
        b = json.loads(self.ts("board", "--json", env=self.env_without_who()).stdout)
        by = {t["id"]: t for t in b["tickets"]}
        self.assertEqual(by["T-0003"]["plain"], self.ONE_LINE, "lines joined by one space")
        self.assertEqual(by["T-0004"]["plain"], "", "an HTML comment and a <placeholder> line are not plain words")
        self.assertEqual(by["T-0001"]["plain"], "", "no section: the key is there, empty")
        self.assertEqual(by["T-0005"]["plain"], 'He said "stop" \\ twice -- and this.', "escaped losslessly, whitespace squeezed")
        for t in b["tickets"]:
            self.assertEqual(list(t)[-2:], ["plain", "stats"], t["id"])
        self.assertEqual(by["T-0003"]["title"], "blank box", "the front matter is untouched")

    def test_human_json_items_carry_plain_and_the_item_id_ignores_it(self):
        fields = {"severity": "P2", "blocked_on": "which colour for the button?"}
        self.write_ticket("T-0003", "with-plain", "backlog", "blank box", fields=fields, plain=self.PLAIN)
        self.commit_main("tickets: a question with plain words")
        p = self.ts("ask", "T-0002", "CHECK the chip | open https://example.invalid/x | the chip appears | nothing")
        self.assertEqual(p.returncode, 0, p.out)
        items = {i["tickets"]: i for i in json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)}
        self.assertEqual(sorted(items), ["T-0002", "T-0003"])
        self.assertEqual(items["T-0003"]["plain"], self.ONE_LINE)
        self.assertEqual(items["T-0002"]["plain"], "", "no section: empty, the key is there")
        self.assertEqual(list(items["T-0003"])[:7], ["id", "verb", "tickets", "priority", "title", "plain", "since"])
        before = items["T-0003"]["id"]
        self.write_ticket("T-0003", "with-plain", "backlog", "blank box", fields=fields, plain="Shorter now.")
        self.commit_main("tickets: plain words rewritten")
        items = {i["tickets"]: i for i in json.loads(self.ts("human", "--json", env=self.env_without_who()).stdout)}
        self.assertEqual((items["T-0003"]["id"], items["T-0003"]["plain"]), (before, "Shorter now."),
                         "the id hashes verb|tickets|command|expect: rewriting the plain words keeps it")
        self.assertNotIn("Shorter now.", self.ts("human", env=self.env_without_who()).out, "the text inbox is unchanged; the panel renders plain")
        self.assert_main_sane()

    def test_move_to_ready_needs_plain_words(self):
        inv = "desk: looked at it, the box is a missing stylesheet."
        self.fixture("T-0003", "no-plain", "backlog", "no plain words", investigation=inv)
        self.fixture("T-0004", "placeholder-plain", "backlog", "placeholder", investigation=inv,
                     plain="<!-- say it for the human -->\n<2-4 sentences>")
        self.fixture("T-0005", "with-plain", "backlog", "with plain", investigation=inv, plain=self.PLAIN)
        self.commit_main("tickets: three in backlog")
        for tid, name in (("T-0003", "T-0003-no-plain.md"), ("T-0004", "T-0004-placeholder-plain.md")):
            p = self.ts("move", tid, "ready", "desk: investigated")
            self.assertNotEqual(p.returncode, 0, p.out)
            self.assertIn("%s: backlog -> ready needs a written ## In plain words section" % tid, p.out)
            self.assertIn("2-4 sentences the human can understand", p.out); self.assertIn("scripts/ticket.sh put %s" % tid, p.out)
            self.assertEqual(self.front(name, "status"), "backlog")
        p = self.ts("move", "T-0005", "ready", "desk: investigated")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front("T-0005-with-plain.md", "status"), "ready")
        p = self.ts("move", "T-0003", "ready", "desk: plain words come with the backfill", env=dict(self.env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("backlog -> ready. FORCED (no In plain words section): desk: plain words come with the backfill", self.log_of("T-0003-no-plain.md"))
        self.assertIn('items carry "plain"', self.ts("help").out, "the usage names the field"); self.assertIn('front matter + "plain" per ticket', self.ts("help").out)
        self.assert_main_clean()

    def test_a_fresh_ticket_from_the_template_is_refused_at_ready_until_the_placeholder_is_replaced(self):
        """the template carries the heading with a one-line <placeholder>: `new` yields plain "" and the gate refuses"""
        p = self.ts("new", "bug", "P2", "fresh from the template")
        self.assertEqual(p.returncode, 0, p.out)
        name = [f for f in os.listdir(os.path.join(self.main, "tickets")) if f.startswith("T-0003-")][0]
        text = self.ticket_text(name)
        self.assertIn("\n## In plain words\n<", text, "the template's section, placeholder untouched")
        self.assertLess(text.index("## In plain words"), text.index("## Problem"))
        with open(os.path.join(self.main, "tickets", name), encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("\n## Investigation\n", "\n## Investigation\ndesk: real, a missing stylesheet\n", 1)
        with open(os.path.join(self.main, "tickets", name), "w", encoding="utf-8") as fh:
            fh.write(text)
        self.commit_main("tickets: T-0003 investigated")
        b = json.loads(self.ts("board", "--json", env=self.env_without_who()).stdout)
        self.assertEqual({t["id"]: t["plain"] for t in b["tickets"]}["T-0003"], "", "a <placeholder> line is not plain words")
        p = self.ts("move", "T-0003", "ready", "desk: investigated")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0003: backlog -> ready needs a written ## In plain words section", p.out)
        self.set_plain(name, "The sign-in button is missing on the invite page; nobody new can be invited until it is back.")
        self.commit_main("tickets: T-0003 plain words")
        p = self.ts("move", "T-0003", "ready", "desk: investigated, plain words written")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front(name, "status"), "ready")
        self.assert_main_clean()


class TestPlainWordsEdges(HumanInboxHelpers):
    """T-0218 (core 1.2.1): the reviewer's five edges of the plain-words rule. RED on 1.2.0 (13e0787): a comment holding
    `--` leaks into plain and passes the gate; a heading after ## Log (or a second one) is read; a sentence starting with
    `<` is dropped; there is no cap; blocked -> ready skips both gates."""
    INV = "desk: real, a missing stylesheet."

    def plain_of(self, tid):
        b = json.loads(self.ts("board", "--json", env=self.env_without_who()).stdout)
        return {t["id"]: t["plain"] for t in b["tickets"]}[tid]

    def test_an_html_comment_holding_dashes_is_stripped(self):
        self.fixture("T-0003", "dashes", "backlog", "dashes", investigation=self.INV,
                     plain="<!-- a note -- with dashes, and an <angle> -->\nReal words for the human.")
        self.fixture("T-0004", "dashes-only", "backlog", "dashes only", investigation=self.INV,
                     plain="<!-- the filer left only this note -- with dashes -->")
        self.fixture("T-0005", "unterminated", "backlog", "unterminated", investigation=self.INV, plain="Kept.\n<!-- never closed\nnot this")
        self.commit_main("tickets: comments with dashes")
        self.assertEqual(self.plain_of("T-0003"), "Real words for the human.")
        self.assertEqual(self.plain_of("T-0004"), "")
        self.assertEqual(self.plain_of("T-0005"), "Kept.", "an unterminated comment runs to the end of the section")
        p = self.ts("move", "T-0004", "ready", "desk: investigated")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0004: backlog -> ready needs a written ## In plain words section", p.out)
        p = self.ts("move", "T-0003", "ready", "desk: investigated")
        self.assertEqual(p.returncode, 0, p.out)
        self.assert_main_clean()

    def test_only_the_first_section_before_the_log_counts(self):
        # (a) the template's empty section at the top, a sneaky one after the Log
        self.fixture("T-0003", "sneaky", "backlog", "sneaky", investigation=self.INV, handoff="## In plain words\nSneaky words at the bottom.")
        # (b) a real one at the top and a second one inside Problem
        path = self.fixture("T-0004", "twice", "backlog", "twice", investigation=self.INV, plain="First words.")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("\n## Problem\n", "\n## Problem\nsome jargon\n\n## In plain words\nSecond words.\n", 1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.commit_main("tickets: sections in odd places")
        self.assertEqual(self.plain_of("T-0003"), "", "a heading after ## Log is history, not the summary")
        self.assertEqual(self.plain_of("T-0004"), "First words.", "the first section counts, a second is ignored")
        p = self.ts("move", "T-0003", "ready", "desk: investigated")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0003: backlog -> ready needs a written ## In plain words section", p.out)
        self.assert_main_sane()

    def test_a_sentence_starting_with_an_angle_bracket_stays(self):
        self.fixture("T-0003", "angles", "backlog", "angles", investigation=self.INV,
                     plain="<b>Bold</b> start, then text.\n<a placeholder line>\n<Everyone> must click once.\n<br>")
        self.commit_main("tickets: angles")
        self.assertEqual(self.plain_of("T-0003"), "<b>Bold</b> start, then text. <Everyone> must click once.",
                         "only a line that is one <...> token is a placeholder")
        self.assert_main_sane()

    def test_plain_is_capped_at_1200_bytes_on_a_utf8_boundary(self):
        self.fixture("T-0003", "long-ascii", "backlog", "long", investigation=self.INV, plain=("word " * 400).strip())
        self.fixture("T-0004", "cut-in-a-char", "backlog", "cut", investigation=self.INV, plain="a" * 1199 + "é and more")
        self.fixture("T-0005", "whole-chars", "backlog", "whole", investigation=self.INV, plain="é" * 700)
        self.commit_main("tickets: long plain words")
        # T-0220: a UTF-8 locale for the script, so an awk that counts characters there (gawk) would show; the core runs
        # its awks under LC_ALL=C and counts bytes whatever the runner's awk (TICKET_WORLD_AWK=/path/to/gawk reproduces CI)
        p = self.ts("board", "--json", env=dict(self.env_without_who(), LANG="C.UTF-8", LC_ALL="C.UTF-8"))
        self.assertEqual(p.returncode, 0, p.out)
        by = {t["id"]: t["plain"] for t in json.loads(p.stdout)["tickets"]}
        self.assertEqual(len(by["T-0003"]), 1200); self.assertEqual(by["T-0003"], ("word " * 400)[:1200])
        self.assertEqual(by["T-0004"], "a" * 1199, "the cut would split the two-byte character: it goes, the text stays valid UTF-8")
        self.assertEqual(by["T-0005"], "é" * 600, "600 two-byte characters are exactly 1200 bytes")
        p = self.ts("ask", "T-0004", "CHECK the long one | open it | it shows | nothing")
        self.assertEqual(p.returncode, 0, p.out)
        items = {i["tickets"]: i for i in json.loads(self.ts("human", "--json", env=dict(self.env_without_who(), LANG="C.UTF-8", LC_ALL="C.UTF-8")).stdout)}
        self.assertEqual(items["T-0004"]["plain"], "a" * 1199, "the same cap on the inbox side")
        self.assert_main_sane()

    def test_blocked_to_ready_is_gated_like_backlog(self):
        q = {"severity": "P2", "blocked_on": "which colour?"}
        self.fixture("T-0003", "blocked-no-plain", "blocked", "no plain", fields=q, investigation=self.INV)
        self.fixture("T-0004", "blocked-no-inv", "blocked", "no investigation", fields=q)
        self.fixture("T-0005", "blocked-complete", "blocked", "complete", fields=q, investigation=self.INV, plain="The colour is decided; nothing to do.")
        self.commit_main("tickets: three blocked")
        p = self.ts("move", "T-0003", "ready", "user: blue")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0003: blocked -> ready needs a written ## In plain words section", p.out)
        p = self.ts("move", "T-0004", "ready", "user: blue")
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("T-0004: blocked -> ready needs a written ## Investigation", p.out)
        p = self.ts("move", "T-0005", "ready", "user: blue")
        self.assertEqual(p.returncode, 0, p.out); self.assertEqual(self.front("T-0005-blocked-complete.md", "status"), "ready")
        p = self.ts("move", "T-0003", "ready", "user: blue; plain words with the backfill", env=dict(self.env, TICKET_FORCE="1"))
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("blocked -> ready. FORCED (no In plain words section): user: blue; plain words with the backfill", self.log_of("T-0003-blocked-no-plain.md"))
        self.assert_main_clean()


class TestAbortRestoresFromHead(TicketShWorld):
    """T-0218: a concurrent git (`git status` takes index.lock for a moment) can make the ticket commit fail. The add and
    the commit are retried, only on index.lock, at most 5 x 1 s, with the same files (g_unlocked); when the lock stays,
    the run dies and puts the file back from HEAD, not from the index. On 1.2.0 (13e0787) there was no retry, and the
    staged edit survived while "main worktree left clean" was printed; every later call refused "main worktree is
    dirty" (T-0218's own claim and move, 2026-10-14). RED there."""
    def test_a_lock_held_once_is_waited_out_and_the_command_completes(self):
        env = self.wrap_env()
        open(os.path.join(self.tmp, "fail_commit_once"), "w").close()
        p = self.ts("claim", "T-0001", "qa", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("index.lock is busy (another git): retry 1/5 in 1 s", p.out)
        self.assertEqual(self.sleep_calls(), ["sleep 1"], "one second, once")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "in-dev")
        self.assertIn("tickets: T-0001 -> in-dev (qa)", self.main_log())
        self.assert_main_clean()

    def test_a_lock_that_stays_aborts_after_five_retries_with_main_clean_and_unstaged(self):
        env = self.wrap_env()
        flag = os.path.join(self.tmp, "fail_commit"); open(flag, "w").close()
        before = self.ticket_text("T-0001-alpha-ticket.md")
        p = self.ts("claim", "T-0001", "qa", env=env)
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("index.lock", p.out); self.assertIn("stayed busy through 5 retries", p.out)
        self.assertIn("aborted -- restored T-0001-alpha-ticket.md", p.out); self.assertIn("main worktree left clean", p.out)
        self.assertEqual(self.sleep_calls(), ["sleep 1"] * 5)
        self.assert_main_clean()
        self.assertEqual(self.ticket_text("T-0001-alpha-ticket.md"), before, "the file is HEAD's again, in the index and the tree")
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertNotIn("DIRTY", p.out)
        os.remove(flag)
        # the worktree and branch the claim made before the commit stay; the next claim finds and reuses them
        p = self.ts("claim", "T-0001", "qa", env=env)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "in-dev")
        self.assert_main_clean()

    def test_a_new_ticket_whose_commit_dies_is_gone_from_the_index_too(self):
        env = self.wrap_env()
        open(os.path.join(self.tmp, "fail_commit"), "w").close()
        p = self.ts("new", "bug", "P3", "born under a lock", env=env)
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("index.lock", p.out)
        self.assert_main_clean()
        self.assertFalse([f for f in os.listdir(os.path.join(self.main, "tickets")) if f.startswith("T-0003-")], "the file is gone")
        self.assertEqual(self.ts("board", env=self.env_without_who()).returncode, 0)

    def test_a_real_lock_held_through_the_restore_still_leaves_an_existing_ticket_clean(self):
        """reviewer of d1db45f: `checkout HEAD -- file` needs index.lock too, so with the lock held from the add through
        the restore the edit stayed (" M") and "left clean" was false. Now HEAD's bytes come from `git show`, no lock."""
        lock = os.path.join(self.main, ".git", "index.lock"); open(lock, "w").close()
        before = self.ticket_text("T-0001-alpha-ticket.md")
        try:
            p = self.ts("log", "T-0001", "a line under a held lock")
        finally:
            os.remove(lock)
        self.assertNotEqual(p.returncode, 0, p.out)
        self.assertIn("index.lock", p.out); self.assertIn("stayed busy through 5 retries", p.out)
        self.assertIn("aborted -- restored T-0001-alpha-ticket.md; main worktree left clean", p.out)
        self.assertNotIn("NOT clean", p.out)
        self.assert_main_clean()
        self.assertEqual(self.ticket_text("T-0001-alpha-ticket.md"), before)
        self.assertEqual(self.ts("board", env=self.env_without_who()).returncode, 0)
        p = self.ts("log", "T-0001", "a line once the lock is gone")
        self.assertEqual(p.returncode, 0, p.out); self.assert_main_clean()

    def test_a_real_lock_held_through_the_restore_leaves_no_trace_of_a_new_ticket(self):
        lock = os.path.join(self.main, ".git", "index.lock"); open(lock, "w").close()
        try:
            p = self.ts("new", "bug", "P3", "born under a held lock")
        finally:
            os.remove(lock)
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("index.lock", p.out)
        self.assertIn("main worktree left clean", p.out); self.assertNotIn("NOT clean", p.out)
        self.assert_main_clean()
        self.assertFalse([f for f in os.listdir(os.path.join(self.main, "tickets")) if f.startswith("T-0003-")], "the file is gone")
        p = self.ts("new", "bug", "P3", "born once the lock is gone")
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("T-0003", p.out); self.assert_main_clean()

    def test_any_other_commit_failure_is_final_at_once(self):
        """a failure that does not name index.lock is not retried: the message is passed through, no sleep"""
        env = self.wrap_env()
        # a pre-commit hook that refuses: not a lock, so no retry
        hooks = os.path.join(self.main, ".git", "hooks"); os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "pre-commit")
        with open(hook, "w") as fh:
            fh.write("#!/usr/bin/env bash\necho 'hook says no' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        try:
            p = self.ts("claim", "T-0001", "qa", env=env)
        finally:
            os.remove(hook)
        self.assertNotEqual(p.returncode, 0, p.out); self.assertIn("hook says no", p.out); self.assertNotIn("retry", p.out)
        self.assertEqual(self.sleep_calls(), [])
        self.assert_main_clean()
        self.assertEqual(self.front("T-0001-alpha-ticket.md", "status"), "ready")
