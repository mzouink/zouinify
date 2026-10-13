"""`zouinify config --env` (T-0188): tickets/ticket.env in the grammar the vendored ticket.sh core parses; printed by default,
written only with --write and never into a repository's main worktree; the three optional `tickets:` keys."""
import hashlib
import os
import re
import subprocess
import unittest

from test_core import World

LINE_RE = re.compile(r'^[A-Z][A-Z0-9_]*="[^"\\]*"$')
KEYS = {"PROJECT", "TRUNK", "TICKETS_DIR", "WORKTREES", "REPOS", "PROTECTED_BRANCHES_EXTRA", "ASK_VERBS", "TICKET_TYPES", "DEPLOY_PLUGIN",
        "DEPLOY_KINDS"}


def parse(text):
    """The core's reading of the file: comment and blank lines dropped, KEY -> value without the quotes."""
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert LINE_RE.match(line), line
        k, v = line.split("=", 1)
        out[k] = v[1:-1]
    return out


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True, env=env).stdout


class TestTicketEnv(World):
    def config_sha(self):
        with open(os.path.join(self.root, ".ops", "config.yaml"), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_prints_the_agreed_grammar_and_writes_nothing(self):
        r = self.ok("config", "--env")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], f"# generated-from: .ops/config.yaml sha256 {self.config_sha()}")
        self.assertRegex(lines[1], r"^# generated-by: zouinify \S+ -- regenerate with: zouinify config --env --write$")
        env = parse(r.stdout)
        self.assertEqual(set(env), KEYS | {"REPO_BACKEND_PATH", "REPO_BACKEND_TRUNK", "REPO_BACKEND_PROD"})
        self.assertEqual(env["PROJECT"], "testproj")
        self.assertEqual(env["TRUNK"], "dev")
        self.assertEqual(env["TICKETS_DIR"], "tickets")
        self.assertEqual(env["WORKTREES"], ".claude/worktrees")
        self.assertEqual(env["REPOS"], "backend")
        self.assertEqual(env["REPO_BACKEND_PATH"], ".")
        self.assertEqual(env["REPO_BACKEND_TRUNK"], "dev")
        self.assertEqual(env["REPO_BACKEND_PROD"], "main")
        self.assertEqual(env["PROTECTED_BRANCHES_EXTRA"], "")
        self.assertEqual(env["ASK_VERBS"], "RUN DECIDE CHECK")
        self.assertEqual(env["TICKET_TYPES"], "bug feature process chore")
        self.assertEqual(env["DEPLOY_PLUGIN"], "none")
        self.assertEqual(env["DEPLOY_KINDS"], "none")
        self.assertFalse(os.path.exists(os.path.join(self.root, "tickets", "ticket.env")), "stdout by default: nothing written")

    def test_repo_names_map_to_keys_and_collisions_are_refused(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        two = cfg.replace("  - {name: backend, path: ., trunk: dev, prod: main, default: true}",
                          "  - {name: my-app, path: ., trunk: dev, default: true}\n  - {name: lib_x, path: ../lib_x, trunk: main}")
        self.write(".ops/config.yaml", two)
        env = parse(self.ok("config", "--env").stdout)
        self.assertEqual(env["REPOS"], "my-app lib_x")
        self.assertEqual(env["REPO_MY_APP_PATH"], ".")
        self.assertEqual(env["REPO_MY_APP_PROD"], "", "written even when the repo has no production branch")
        self.assertEqual(env["REPO_LIB_X_TRUNK"], "main")
        self.write(".ops/config.yaml", two.replace("name: lib_x", "name: my_app").replace("my_app, path: ../lib_x", "my_app, path: ../other"))
        r = self.nz("config", "--env")
        self.assertIn("both map to REPO_MY_APP_", r.stderr)
        self.write(".ops/config.yaml", cfg.replace("name: backend", "name: Backend"))
        r = self.nz("config", "--env")
        self.assertIn("lowercase", r.stderr)

    def test_optional_tickets_keys_render_and_are_validated(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none\n",
                                                   "  deploy_plugin: aws-terraform\n  types: [bug, task]\n  protected_branches_extra: [release/x, hotfix]\n"))
        env = parse(self.ok("config", "--env").stdout)
        self.assertEqual(env["DEPLOY_PLUGIN"], "aws-terraform")
        self.assertEqual(env["TICKET_TYPES"], "bug task")
        self.assertEqual(env["PROTECTED_BRANCHES_EXTRA"], "release/x hotfix")
        r = self.ok("config", "--json")
        self.assertIn('"deploy_plugin": "aws-terraform"', r.stdout)
        self.assertIn('"protected_branches_extra": [', r.stdout)
        for bad, needle in (("  deploy_plugin: Bad Name\n", "tickets.deploy_plugin"), ("  types: [Bad]\n", "tickets.types"),
                            ("  states: custom\n", "12 states"), ("  protected_branches_extra: [two words]\n", "protected_branches_extra")):
            self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none\n", bad))
            r = self.nz("config", "--check")
            self.assertIn(needle, r.stderr + r.stdout, bad)
        self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none\n", "  states: default\n"))
        self.ok("config", "--check")

    def test_write_outside_git_then_unchanged(self):
        r = self.ok("config", "--env", "--write")
        target = os.path.join(self.root, "tickets", "ticket.env")
        self.assertIn("wrote", r.stdout)
        self.assertIn(target, r.stdout)
        with open(target) as fh:
            self.assertEqual(fh.read(), self.ok("config", "--env").stdout)
        r = self.ok("config", "--env", "--write")
        self.assertIn("unchanged", r.stdout)
        r = self.nz("config", "--env", "--write", os.path.join(self.tmp, "missing-dir", "ticket.env"))
        self.assertIn("not a directory", r.stderr)

    def test_write_refuses_a_main_worktree_and_a_symlink_but_takes_a_linked_worktree(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        r = self.nz("config", "--env", "--write")
        self.assertIn("main worktree", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "tickets", "ticket.env")))
        self.assertEqual(git(self.root, "status", "--porcelain"), "", "the main worktree stays clean")
        wt = os.path.join(self.tmp, "wt-t0001")
        git(self.root, "worktree", "add", "-q", "-b", "t0001-x", wt)
        r = self.ok("--project", wt, "config", "--env", "--write", cwd=wt)
        self.assertIn("wrote", r.stdout)
        self.assertIn("linked worktree", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(wt, "tickets", "ticket.env")))
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        r = self.nz("--project", wt, "config", "--env", "--write", os.path.join(self.root, "tickets", "ticket.env"), cwd=wt)
        self.assertIn("main worktree", r.stderr, "an explicit path into main is refused too")
        link = os.path.join(wt, "tickets", "link.env")
        os.symlink(os.path.join(self.tmp, "elsewhere.env"), link)
        r = self.nz("--project", wt, "config", "--env", "--write", link, cwd=wt)
        self.assertIn("symlink", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere.env")))

    def test_env_flags_are_exclusive(self):
        r = self.z("config", "--env", "--json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--env goes alone", r.stderr)
        r = self.z("config", "--write")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--write needs --env", r.stderr)


if __name__ == "__main__":
    unittest.main()
