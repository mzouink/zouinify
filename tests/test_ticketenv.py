"""`zouinify config --env` (T-0188): tickets/ticket.env in the grammar the vendored ticket.sh core parses; printed by default,
written only with --write and never into a repository's main worktree; the three optional `tickets:` keys."""
import hashlib
import json
import os
import re
import subprocess
import unittest

from test_core import World

LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*=[A-Za-z0-9._/@:+ -]*$")      # the desk's grammar: bare values from one alphabet
KEYS = {"PROJECT", "TRUNK", "TICKETS_DIR", "WORKTREES", "REPOS", "PROTECTED_BRANCHES_EXTRA", "ASK_VERBS", "TICKET_TYPES", "DEPLOY_PLUGIN",
        "DEPLOY_KINDS"}


def parse(text):
    """The core's reading of the file: comment and blank lines dropped, KEY -> the bare value."""
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert LINE_RE.match(line), line
        k, v = line.split("=", 1)
        out[k] = v
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
                            ("  types: []\n", "tickets.types: a non-empty"), ("  states: custom\n", "12 states"),
                            ("  protected_branches_extra: [two words]\n", "protected_branches_extra"),
                            ("  dir: ../elsewhere\n", "tickets.dir"), ("  dir: /abs\n", "tickets.dir")):
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

    def test_values_outside_the_alphabet_are_refused_and_branch_fields_follow_git_rules(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        for bad in ("$(id)", "a;b", "a`b`", "a'b", 'a"b', "a=b", "a\\b", "a\u2028b", "a\u0085b", "a\tb", "a#b"):
            self.write(".ops/config.yaml", cfg.replace("project: {name: testproj,", f"project: {{name: {json.dumps('x' + bad)},"))
            r = self.nz("config", "--env")
            self.assertIn("PROJECT", r.stderr, bad)
            self.assertNotIn(bad, r.stdout, "nothing of the value reaches stdout")
        self.write(".ops/config.yaml", cfg.replace("project: {name: testproj,", "project: {name: a.b/c@d:e+f-g,"))
        self.assertEqual(parse(self.ok("config", "--env").stdout)["PROJECT"], "a.b/c@d:e+f-g")
        for bad in ("$(id)", "a b", "a..b", "a~b", "-x", "x/", "x.lock", "a//b", "@{x", ".hidden", "x\\y", "a;b", "a'b", "a#b", "a=b", "x\u2028y"):
            self.write(".ops/config.yaml", cfg.replace("trunk: dev, prod: main", f"trunk: {json.dumps(bad)}, prod: main"))
            r = self.nz("config", "--check")
            self.assertIn("repos[0].trunk: not a valid git branch name", r.stderr, bad)
        self.write(".ops/config.yaml", cfg.replace("trunk: dev, prod: main", "trunk: dev, prod: \"release/2026.10\""))
        self.assertEqual(parse(self.ok("config", "--env").stdout)["REPO_BACKEND_PROD"], "release/2026.10")
        self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none\n", "  protected_branches_extra: [ok-branch, \"bad branch\"]\n"))
        r = self.nz("config", "--check")
        self.assertIn("protected_branches_extra: valid git branch names", r.stderr)

    def test_every_line_is_a_comment_or_a_bare_grammar_line(self):
        """What the core's parser relies on: nothing but `# ...`, blank, or KEY=value from the alphabet; no quotes anywhere."""
        text = self.ok("config", "--env").stdout
        for line in text.splitlines():
            with self.subTest(line=line):
                self.assertTrue(line.startswith("#") or LINE_RE.match(line), line)
        self.assertNotIn('"', text)
        self.assertNotIn("'", text)
        self.assertNotIn("\\", text)

    def test_the_worktree_check_fails_closed_and_ignores_git_env(self):
        git(self.root, "init", "-q")
        r = self.nz("config", "--env", "--write", env=dict(self.env, GIT_DIR=os.path.join(self.tmp, "nowhere")))
        self.assertIn("main worktree", r.stderr, "GIT_DIR in the caller's environment does not redirect the answer")
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "git"), "w") as fh:
            fh.write("#!/bin/sh\necho 'fatal: detected dubious ownership in repository' >&2\nexit 128\n")
        os.chmod(os.path.join(fake, "git"), 0o755)
        r = self.nz("config", "--env", "--write", env=dict(self.env, PATH=fake + os.pathsep + self.env["PATH"]))
        self.assertIn("dubious ownership", r.stderr)
        self.assertIn("refusing to write", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "tickets", "ticket.env")))
        with open(os.path.join(fake, "git"), "w") as fh:
            fh.write("#!/bin/sh\necho 'fatal: not a git repository (or any of the parent directories): .git' >&2\nexit 128\n")
        r = self.ok("config", "--env", "--write", env=dict(self.env, PATH=fake + os.pathsep + self.env["PATH"]))
        self.assertIn("outside git", r.stdout, "only git's own 'not a git repository' means outside git")

    def test_write_breaks_a_hard_link_instead_of_writing_through_it(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        wt = os.path.join(self.tmp, "wt-t0002")
        git(self.root, "worktree", "add", "-q", "-b", "t0002-x", wt)
        tracked = os.path.join(self.root, "tickets", "WORKER_BRIEF.md")
        target = os.path.join(wt, "tickets", "hl.env")
        os.link(tracked, target)
        before = open(tracked).read()
        self.ok("--project", wt, "config", "--env", "--write", target, cwd=wt)
        self.assertEqual(open(tracked).read(), before, "main's tracked file is untouched")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertIn("TRUNK=dev", open(target).read())

    def test_env_flags_are_exclusive(self):
        r = self.z("config", "--env", "--json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--env goes alone", r.stderr)
        r = self.z("config", "--write")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--write needs --env", r.stderr)


if __name__ == "__main__":
    unittest.main()
