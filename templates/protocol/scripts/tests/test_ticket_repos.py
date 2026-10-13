"""T-0187: scripts/ticket.sh takes N repos from tickets/ticket.env (REPOS + REPO_<NAME>_PATH/TRUNK/PROD): claim, merge, prune
and both boards iterate them; repos: names are validated against the list; a name not in it keeps the T-0176 rule (an
existing worktree, no merge/prune). This project's two-repo behaviour is unchanged (test_ticket_sh.py).

RED on 271bf77 (T-0186's core): a 3-repo ticket.env dies with "until T-0187 this core takes the default repo and at most one more".

    python3 -m pytest -q scripts/tests/test_ticket_repos.py
"""

import json
import os
import subprocess
import unittest

from ticket_world import TicketShWorld, REAL_GIT, TRUNK

ENV3 = """# three repos (T-0187): backend (this checkout), frontend, auth
PROJECT=qa-three
TRUNK=dev
TICKETS_DIR=tickets
WORKTREES=.claude/worktrees
REPOS=backend frontend auth
REPO_BACKEND_PATH=.
REPO_BACKEND_TRUNK=dev
REPO_BACKEND_PROD=main
REPO_FRONTEND_PATH=../fe_main
REPO_FRONTEND_TRUNK=dev
REPO_FRONTEND_PROD=main
REPO_AUTH_PATH=../auth_main
REPO_AUTH_TRUNK=trunk
REPO_AUTH_PROD=
PROTECTED_BRANCHES_EXTRA=
ASK_VERBS=APPLY DEPLOY PUSH RUN DECIDE CHECK APPROVE SSO
TICKET_TYPES=bug feature process chore
DEPLOY_PLUGIN=none
DEPLOY_KINDS=none
"""


class ThreeRepoWorld(TicketShWorld):
    plugin = "none"

    def setUp(self):
        super().setUp()
        self.fe_origin, self.fe_main = self.repo_world("fe", "dev")
        self.auth_origin, self.auth_main = self.repo_world("auth", "trunk")
        self.write_ticket_env(ENV3)
        self.commit_main("tickets: three repos")
        self.env3 = self.env_without_who()
        self.env3.pop("TICKET_FE_REPO", None)   # the file names the repos; no legacy override
        self.who3 = dict(self.env3, TICKET_AS="qa")

    def repo_world(self, name, trunk):
        """an origin + main clone for another repo, next to the world's main (REPO_<NAME>_PATH=../<name>_main)"""
        origin = os.path.join(self.tmp, name + "_origin.git"); main = os.path.join(self.tmp, name + "_main")
        self.git("init", "-q", "--bare", "-b", trunk, origin)
        self.git("clone", "-q", origin, main); self.git("-C", main, "checkout", "-q", "-b", trunk)
        with open(os.path.join(main, ".gitignore"), "w") as fh:
            fh.write(".claude/\n")
        with open(os.path.join(main, name + ".txt"), "w") as fh:
            fh.write("v1\n")
        self.git("-C", main, "add", "-A"); self.git("-C", main, "commit", "-q", "-m", name + " init")
        self.git("-C", main, "push", "-q", "-u", "origin", trunk)
        return origin, main

    # the merge helpers of test_ticket_sh.TestMergeAndPrune, with the world's env
    def claim(self, tid, *extra):
        p = self.ts("claim", tid, "qa", *extra, env=self.who3)
        self.assertEqual(p.returncode, 0, p.out)
        return p.out.split("work in: ", 1)[1].split()[0]

    def branch_commit(self, wt, name, msg, trailer):
        with open(os.path.join(wt, name), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        self.git("-C", wt, "add", name); self.git("-C", wt, "commit", "-q", "-m", msg + "\n\n" + trailer)
        return self.git("-C", wt, "rev-parse", "--short=7", "HEAD")

    def push_branch(self, wt):
        br = self.git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD")
        self.git("-C", wt, "push", "-q", "-u", "origin", br)
        return br

    def to_ready_to_merge(self, tid):
        for st in ("in-review", "ready-to-merge"):
            p = self.ts("move", tid, st, "qa: -> %s" % st, env=self.who3)
            self.assertEqual(p.returncode, 0, p.out)

    def has_branch(self, repo, br):
        return subprocess.run([REAL_GIT, "-C", repo, "show-ref", "--verify", "-q", "refs/heads/" + br], env=self.env).returncode == 0

    def set_front(self, name, key, value):
        path = os.path.join(self.main, "tickets", name)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        lines = [("%s: %s" % (key, value)) if l.startswith(key + ":") else l for l in lines]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))


class TestThreeRepos(ThreeRepoWorld):
    def test_three_repos_load_and_the_board_names_them(self):
        p = self.ts("version", env=self.env3)
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("plugin none", p.stdout)
        p = self.ts("board", "--json", env=self.env3)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(json.loads(p.stdout)["trunk"], "dev")
        self.write_ticket("T-0005", "auth-done", "done", "auth done", fields={"repos": "[auth]", "branch": "t0005-auth"})
        self.write_ticket("T-0006", "other", "done", "other", fields={"repos": "[zouinify]", "branch": "t0006-z"})
        self.commit_main("tickets: done ones")
        p = self.ts("prune", "T-0005", "T-0006", "--dry-run", env=self.env3)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("T-0005 (auth): nothing left for t0005-auth", p.out, "a plain branch of an auth-only ticket is looked for in the auth repo")
        self.assertIn("repos: names 'zouinify', which ticket.sh cannot merge or prune (only: backend frontend auth)", p.out)

    def test_claim_of_a_third_repo_ticket_creates_its_worktree_there(self):
        self.write_ticket("T-0003", "auth-ticket", "ready", "auth ticket", fields={"repos": "[auth]"})
        self.write_ticket("T-0004", "fe-and-auth", "ready", "fe and auth", fields={"repos": "[auth, frontend]"})
        self.commit_main("tickets: auth tickets")
        wt = self.claim("T-0003")
        self.assertEqual(wt, os.path.join(self.auth_main, ".claude", "worktrees", "t0003-auth-ticket"))
        self.assertEqual(self.git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD"), "t0003-auth-ticket")
        self.assertEqual(self.git("-C", wt, "rev-parse", "HEAD"), self.git("-C", self.auth_main, "rev-parse", "trunk"), "cut from the auth repo's trunk")
        self.assertEqual(self.front("T-0003-auth-ticket.md", "base"), self.git("-C", self.auth_main, "rev-parse", "--short", "trunk"))
        p = self.ts("move", "T-0003", "in-review", "qa: back", env=self.who3); self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("move", "T-0003", "ready", "qa: back", env=dict(self.who3, TICKET_FORCE="1")); self.assertEqual(p.returncode, 0, p.out)
        wt = self.claim("T-0004")
        self.assertTrue(wt.startswith(self.fe_main + "/"), "two other repos named: the first in REPOS order (frontend) gets the worktree: " + wt)

    def test_a_repo_not_in_the_list_still_needs_an_existing_worktree(self):
        self.write_ticket("T-0003", "z-ticket", "ready", "z ticket", fields={"repos": "[zouinify]"})
        self.commit_main("tickets: z")
        p = self.ts("claim", "T-0003", "qa", env=self.who3)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("T-0003 is a zouinify ticket: ticket.sh creates nothing in that repo", p.out)
        self.assert_main_sane()

    def test_merge_lands_in_all_three_repos_in_order_and_records_each(self):
        self.write_ticket("T-0003", "three", "ready", "three", fields={"repos": "[backend, frontend, auth]"})
        self.commit_main("tickets: three")
        wt_be = self.claim("T-0003")
        sha_be = self.branch_commit(wt_be, "be.txt", "be change", "Ticket: T-0003"); br = self.push_branch(wt_be)
        wts = {}
        for repo, tr in ((self.fe_main, "dev"), (self.auth_main, "trunk")):
            w = os.path.join(repo, ".claude", "worktrees", "t0003-three")
            self.git("-C", repo, "worktree", "add", "-q", w, "-b", "t0003-three", tr)
            wts[repo] = w
        sha_fe = self.branch_commit(wts[self.fe_main], "fe.txt", "fe change", "Ticket: T-0003"); self.push_branch(wts[self.fe_main])
        sha_au = self.branch_commit(wts[self.auth_main], "au.txt", "auth change", "Ticket: T-0003"); self.push_branch(wts[self.auth_main])
        self.set_front("T-0003-three.md", "branch", "{backend: %s, frontend: t0003-three, auth: t0003-three}" % br)
        self.set_front("T-0003-three.md", "worktree", "{backend: .claude/worktrees/t0003-three, frontend: fe_main/.claude/worktrees/t0003-three, auth: auth_main/.claude/worktrees/t0003-three}")
        self.commit_main("tickets: maps")
        self.to_ready_to_merge("T-0003")
        self.git("-C", wt_be, "fetch", "-q", "origin", TRUNK); self.git("-C", wt_be, "rebase", "-q", "origin/" + TRUNK)
        self.git("-C", wt_be, "push", "-q", "--force-with-lease", "origin", br)
        sha_be = self.git("-C", wt_be, "rev-parse", "--short=7", "HEAD")
        p = self.ts("board", env=self.env3)
        self.assertRegex(p.out, r"T-0003 .*backend: behind 0  ahead 1.*\| frontend: behind 0  ahead 1.*\| auth: behind 0  ahead 1", "the text board shows every repo of the map")
        p = self.ts("board", "--json", "--slow", env=self.env3)
        stats = next(t for t in json.loads(p.stdout)["tickets"] if t["id"] == "T-0003")["stats"]
        self.assertEqual(sorted(stats), ["auth", "backend", "frontend"]); self.assertEqual(stats["auth"]["ahead"], 1)
        p = self.ts("merge", "T-0003", env=self.who3)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertEqual(self.front("T-0003-three.md", "closed_by"), "[%s, frontend %s, auth %s]" % (sha_be, sha_fe, sha_au))
        for repo, origin, tr in ((self.fe_main, self.fe_origin, "dev"), (self.auth_main, self.auth_origin, "trunk")):
            self.assertEqual(self.git("-C", repo, "rev-parse", "HEAD"), self.git("-C", repo, "rev-parse", "t0003-three"), repo)
            self.assertEqual(self.git("--git-dir", origin, "rev-parse", tr), self.git("-C", repo, "rev-parse", "HEAD"), "pushed: " + repo)
        self.assertIn("merged (auth): t0003-three -> trunk", p.out)
        p = self.ts("move", "T-0003", "done", "deploy: none, merged", env=self.who3); self.assertEqual(p.returncode, 0, p.out)
        p = self.ts("prune", "T-0003", env=self.who3)
        self.assertEqual(p.returncode, 0, p.out)
        for repo in (self.main, self.fe_main, self.auth_main):
            self.assertFalse(os.path.isdir(os.path.join(repo, ".claude", "worktrees", "t0003-three")), repo)
            self.assertFalse(self.has_branch(repo, br if repo == self.main else "t0003-three"), repo)
        self.assertIn("T-0003 (auth): removed worktree", p.out)

    def test_a_failure_in_the_third_repo_after_two_landed_is_logged_as_partial(self):
        self.write_ticket("T-0003", "three", "ready", "three", fields={"repos": "[backend, frontend, auth]"})
        self.commit_main("tickets: three")
        wt_be = self.claim("T-0003")
        self.branch_commit(wt_be, "be.txt", "be change", "Ticket: T-0003"); br = self.push_branch(wt_be)
        for repo, tr in ((self.fe_main, "dev"), (self.auth_main, "trunk")):
            w = os.path.join(repo, ".claude", "worktrees", "t0003-three")
            self.git("-C", repo, "worktree", "add", "-q", w, "-b", "t0003-three", tr)
            self.branch_commit(w, "x.txt", "change", "Ticket: T-0003"); self.push_branch(w)
        self.set_front("T-0003-three.md", "branch", "{backend: %s, frontend: t0003-three, auth: t0003-three}" % br)
        self.commit_main("tickets: maps")
        self.to_ready_to_merge("T-0003")
        self.git("-C", wt_be, "fetch", "-q", "origin", TRUNK); self.git("-C", wt_be, "rebase", "-q", "origin/" + TRUNK)
        self.git("-C", wt_be, "push", "-q", "--force-with-lease", "origin", br)
        # the auth push fails: its origin declines every push
        hook = os.path.join(self.auth_origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/usr/bin/env bash\necho 'auth origin is down' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        p = self.ts("merge", "T-0003", env=self.who3)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("(auth): push of trunk failed after the merge; the local merge was undone", p.out)
        log = self.log_of("T-0003-three.md")
        self.assertIn("PARTIAL merge (ticket.sh merge): backend+frontend ff-merged", log); self.assertIn("auth NOT merged", log)
        self.assertEqual(self.front("T-0003-three.md", "status"), "ready-to-merge")
        self.assertEqual(self.git("--git-dir", self.fe_origin, "rev-parse", "dev"), self.git("-C", self.fe_main, "rev-parse", "dev"), "the frontend half landed and was pushed")
        os.remove(hook)
        p = self.ts("merge", "T-0003", env=self.who3)
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("merged (backend): %s was already on dev -- recorded, nothing moved" % br, p.out)
        self.assertIn("merged (frontend): t0003-three was already on dev -- recorded, nothing moved", p.out)
        self.assertIn("merged (auth): t0003-three -> trunk", p.out)
        self.assertRegex(self.front("T-0003-three.md", "closed_by"), r"^\[[0-9a-f]{7}, frontend [0-9a-f]{7}, auth [0-9a-f]{7}\]$")

    def test_env_rules_for_n_repos(self):
        env = dict(self.env3)
        p = self.ts("board", env=dict(env, TICKET_REPOS="backend frontend auth frontend"))
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPOS names 'frontend' twice", p.out)
        lines = [l for l in ENV3.splitlines() if not l.startswith("REPO_AUTH_TRUNK=")]
        self.write_ticket_env("\n".join(lines) + "\n"); self.commit_main("tickets: no auth trunk")
        p = self.ts("board", env=env)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPO_AUTH_TRUNK is missing (REPOS names auth)", p.out)
        self.write_ticket_env(ENV3); self.commit_main("tickets: back")
        p = self.ts("board", env=dict(env, TICKET_FE_REPO=os.path.join(self.tmp, "elsewhere")))
        self.assertEqual(p.returncode, 0, "the legacy alias names the SECOND repo (frontend), nothing breaks: " + p.out)
        self.write_ticket("T-0009", "fe-done", "done", "fe done", fields={"repos": "[frontend]", "branch": "t0009-fe"})
        self.commit_main("tickets: fe done")
        p = self.ts("prune", "T-0009", "--dry-run", env=dict(env, TICKET_FE_REPO=os.path.join(self.tmp, "elsewhere")))
        self.assertIn("T-0009 (frontend): repo %s not found -- left alone" % os.path.join(self.tmp, "elsewhere"), p.out)
        p = self.ts("prune", "T-0009", "--dry-run", env=dict(env, TICKET_REPO_AUTH_PATH=os.path.join(self.tmp, "nowhere")))
        self.assertIn("T-0009 (frontend): nothing left for t0009-fe", p.out, "an auth override does not touch the frontend ticket")

    # ---- the three gaps of T-0187's review (folded into T-0189) ----
    def test_prune_removes_a_three_repo_ticket_everywhere_and_a_plain_auth_branch_in_auth(self):
        """gap 1: a dedicated prune test -- a done ticket with a three-repo map loses its worktree and branch in each repo, local and
        origin; a done auth-only ticket with a plain branch is pruned in the auth repo only"""
        branches = {}
        for repo, tr in ((self.main, TRUNK), (self.fe_main, "dev"), (self.auth_main, "trunk")):
            w = os.path.join(repo, ".claude", "worktrees", "t0003-three")
            self.git("-C", repo, "worktree", "add", "-q", w, "-b", "t0003-three", tr)
            self.git("-C", repo, "push", "-q", "-u", "origin", "t0003-three")   # nothing outside trunk: prunable
            branches[repo] = w
        w = os.path.join(self.auth_main, ".claude", "worktrees", "t0004-auth"); self.git("-C", self.auth_main, "worktree", "add", "-q", w, "-b", "t0004-auth", "trunk")
        self.write_ticket("T-0003", "three", "done", "three", fields={"repos": "[backend, frontend, auth]", "branch": "{backend: t0003-three, frontend: t0003-three, auth: t0003-three}",
                          "worktree": "{backend: .claude/worktrees/t0003-three, frontend: fe_main/.claude/worktrees/t0003-three, auth: auth_main/.claude/worktrees/t0003-three}"})
        self.write_ticket("T-0004", "auth-only", "done", "auth only", fields={"repos": "[auth]", "branch": "t0004-auth", "worktree": ".claude/worktrees/t0004-auth"})
        self.commit_main("tickets: done three + auth")
        p = self.ts("prune", "T-0003", "T-0004", "--dry-run", env=self.env3)
        self.assertEqual(p.returncode, 0, p.out)
        for lbl in ("backend", "frontend", "auth"):
            self.assertIn("prune: T-0003 (%s): would remove worktree" % lbl, p.out)
        self.assertIn("prune: T-0004 (auth): would remove worktree %s branch t0004-auth" % w, p.out)
        self.assertNotIn("T-0004 (backend)", p.out); self.assertNotIn("T-0004 (frontend)", p.out)
        p = self.ts("prune", "T-0003", "T-0004", env=self.who3)
        self.assertEqual(p.returncode, 0, p.out)
        for repo, wt in branches.items():
            self.assertFalse(os.path.isdir(wt), wt); self.assertFalse(self.has_branch(repo, "t0003-three"), repo)
        for origin in (self.origin, self.fe_origin, self.auth_origin):
            self.assertNotEqual(subprocess.run([REAL_GIT, "--git-dir", origin, "show-ref", "--verify", "-q", "refs/heads/t0003-three"], env=self.env).returncode, 0, origin)
        self.assertFalse(os.path.isdir(w)); self.assertFalse(self.has_branch(self.auth_main, "t0004-auth"))
        log = self.log_of("T-0003-three.md")
        for lbl in ("(backend): removed worktree", "(frontend): removed worktree", "(auth): removed worktree"):
            self.assertIn(lbl, log)
        self.assert_main_sane()

    def test_claim_picks_the_first_configured_repo_in_repos_order_whatever_the_field_order(self):
        """gap 2: repos: [frontend, auth] and repos: [auth, frontend] both go to frontend (REPOS order), never to the field's first token"""
        self.write_ticket("T-0003", "fe-first", "ready", "fe first", fields={"repos": "[frontend, auth]"})
        self.write_ticket("T-0004", "auth-first", "ready", "auth first", fields={"repos": "[auth, frontend]"})
        self.write_ticket("T-0005", "with-default", "ready", "with default", fields={"repos": "[auth, backend]"})
        self.commit_main("tickets: orders")
        for tid, slug in (("T-0003", "t0003-fe-first"), ("T-0004", "t0004-auth-first")):
            with self.subTest(tid=tid):
                wt = self.claim(tid)
                self.assertEqual(wt, os.path.join(self.fe_main, ".claude", "worktrees", slug))
                p = self.ts("move", tid, "in-review", "qa: park", env=self.who3); self.assertEqual(p.returncode, 0, p.out)
        wt = self.claim("T-0005")
        self.assertEqual(wt, os.path.join(self.main, ".claude", "worktrees", "t0005-with-default"), "the default repo wins when named, whatever its position")

    def test_two_repos_sharing_one_path_are_refused(self):
        """gap 3: REPO_FRONTEND_PATH and REPO_AUTH_PATH resolving to the same directory would make merge land one ticket twice and
        prune look in the wrong place; the core refuses it after resolving the paths"""
        lines = [l for l in ENV3.splitlines() if not l.startswith("REPO_AUTH_PATH=")] + ["REPO_AUTH_PATH=../fe_main"]
        self.write_ticket_env("\n".join(lines) + "\n"); self.commit_main("tickets: shared path")
        p = self.ts("board", env=self.env3)
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("ticket.env: REPO_FRONTEND_PATH and REPO_AUTH_PATH resolve to the same directory (%s)" % self.fe_main, p.out)
        self.write_ticket_env(ENV3); self.commit_main("tickets: back")
        p = self.ts("board", env=dict(self.env3, TICKET_REPO_AUTH_PATH=self.main))
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPO_BACKEND_PATH and REPO_AUTH_PATH resolve to the same directory", p.out)


if __name__ == "__main__":
    unittest.main()
