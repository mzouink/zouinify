"""Repository guards (T-0159): the panel code may never launch, kill or type; the public repo holds no
private strings. Both are shell scripts under scripts/ that CI runs too; these tests pin their behaviour.

Hermetic: the public-guard tests never read ~/.config; they pass an explicit pattern file (or none).
The repo under test is $ZOUINIFY_REPO_UNDER_TEST (default: this checkout), so a base checkout without
the scripts gives the RED evidence."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
PANEL_GUARD = os.path.join(REPO, "scripts", "check_panel_guard.sh")
PUBLIC_GUARD = os.path.join(REPO, "scripts", "check_public.sh")
NO_PRIVATE = {"ZOUINIFY_PRIVATE_PATTERNS": os.path.join(tempfile.gettempdir(), "zouinify-no-such-pattern-file")}

# every way a panel could try to launch, kill, type, mutate a ticket or deploy; each one must fail the guard
OFFENDING = [
    ("server.py", "subprocess.run(['tmux', 'new-session', '-d'])"),
    ("server.py", "subprocess.run(['tmux', 'new', '-d', '-s', name])"),            # tmux abbreviation
    ("server.py", "os.system('tmux kill-session -t x')"),
    ("server.py", "os.system('tmux kill-server')"),
    ("server.py", "os.execvp('tmux', ['tmux', 'kill-pane'])"),
    ("server.py", "cmd = 'tmux send-keys -t tickets ok Enter'"),
    ("server.py", "args = shlex.split('tmux send')"),
    ("server.py", "verb = 'kill-' + 'session'"),
    ("server.py", "verb = 'new-' + 'session'"),
    ("server.py", "run(['tmux', 'respawn-pane', '-k', '-t', x])"),
    ("server.py", "Popen(['claude', '--resume', sid])"),
    ("server.py", "subprocess.check_output(['zouinify', 'launch', 'worker', tid])"),
    ("server.py", "run([BIN + '/bin/zouinify', 'down', name])"),
    ("server.py", "sh('scripts/ticket.sh move T-0001 done \"x\"')"),
    ("server.py", "tickets.ticket_sh(cfg, ['log', tid, 'clicked'])"),
    ("server.py", "Registry(cfg).add({'name': n, 'tmux': t})"),
    ("server.py", "from zouinify.launch import cmd_up"),
    ("server.py", "from .. import launch"),
    ("server.py", "from zouinify import tmux"),
    ("server.py", "import zouinify.tmux as t"),
    ("server.py", "tmux.new_session('x', '/', {}, [])"),
    ("server.py", "mod = importlib.import_module('zouinify.' + 'launch')"),
    ("server.py", "asyncio.create_subprocess_exec('tmux', 'kill-window')"),
    ("server.py", "run(['terraform', 'apply'])"),
    ("server.py", "run('git push origin dev')"),
    ("server.py", "run(['docker', 'push', img])"),
    ("server.py", "run(['aws', 'lambda', 'update-function-code'])"),
    ("app.ts", "import { spawn } from 'child_process'; spawn('tmux', ['kill-session'])"),
    ("app.mjs", "execSync('tmux send-keys -t x y Enter')"),
    ("app.js", "fetch('/api/x', {method: 'POST', body: 'tmux kill-session'})"),
]

ALLOWED = """# panel sources: observe only
import json
from zouinify import readonly
from zouinify import tickets as tk
from zouinify.registry import Registry
rows = readonly.capture_pane('x')
panes = readonly.list_panes()
alive = readonly.has_session('x')
r = readonly.read_ticket_sh(cfg, ['human', '--json'])
logs = glob('terraform/*.apply.log')
fm = tk.front_matter(path)
reg = Registry(cfg).load()
label = 'launch worker (available after receipts, T-0162)'
"""


class TestPanelGuard(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.access(PANEL_GUARD, os.X_OK), f"no executable guard at {PANEL_GUARD}")

    def run_guard(self, d):
        return subprocess.run([PANEL_GUARD, d], capture_output=True, text=True)

    def test_repo_passes(self):
        r = subprocess.run([PANEL_GUARD], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_every_offending_variant_fails(self):
        tmp = tempfile.mkdtemp()
        try:
            for fname, bad in OFFENDING:
                for f in os.listdir(tmp):
                    os.remove(os.path.join(tmp, f))
                with open(os.path.join(tmp, fname), "w") as fh:
                    fh.write(f"# panel\n{bad}\n")
                r = self.run_guard(tmp)
                self.assertNotEqual(r.returncode, 0, f"should fail on: {bad}")
                self.assertIn("FAIL", r.stdout, bad)
        finally:
            shutil.rmtree(tmp)

    def test_allowed_read_only_code_passes(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "sources.py"), "w") as fh:
                fh.write(ALLOWED)
            r = self.run_guard(tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_missing_dir_passes(self):
        r = self.run_guard(os.path.join(tempfile.gettempdir(), "zouinify-no-such-dir"))
        self.assertEqual(r.returncode, 0)


class TestPublicGuard(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.access(PUBLIC_GUARD, os.X_OK), f"no executable guard at {PUBLIC_GUARD}")

    def run_public(self, cwd, env_extra):
        return subprocess.run([PUBLIC_GUARD], cwd=cwd, capture_output=True, text=True, env=dict(os.environ, **env_extra))

    def test_tracked_files_pass_generic(self):
        r = self.run_public(REPO, NO_PRIVATE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("generic patterns only", r.stdout)

    def test_tracked_files_pass_with_a_private_pattern_that_is_absent(self):
        pats = os.path.join(tempfile.gettempdir(), "zouinify-pats-%d" % os.getpid())
        with open(pats, "w") as fh:
            fh.write("# a name that must never appear\n" + "zz-" + "private-corp-zz\n")   # split so this file does not contain it
        try:
            r = self.run_public(REPO, {"ZOUINIFY_PRIVATE_PATTERNS": pats})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            os.remove(pats)

    def test_private_strings_fail(self):
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            # the fake key id and secret are assembled at runtime so that no credential-shaped literal
            # sits in this file (GitHub push protection scans the pushed blobs, tests included)
            fake_key_id = "AKIA" + "ABCDEFGHIJ" + "KLMNOP"
            fake_secret = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCY" + "EXAMPLEKEY12"
            for bad in ("account 123456789012", f"key {fake_key_id}", "mail someone@example.org",
                        "path /home/someone/project", f"aws_secret_access_key = {fake_secret}"):
                with open(os.path.join(tmp, "f.md"), "w") as fh:
                    fh.write(bad + "\n")
                subprocess.run(["git", "-C", tmp, "add", "f.md"], check=True)
                r = self.run_public(tmp, NO_PRIVATE)
                self.assertNotEqual(r.returncode, 0, f"should fail on: {bad}")
            # a credential-shaped string under tests/ fails too (push protection does not exempt tests)
            os.makedirs(os.path.join(tmp, "tests"))
            with open(os.path.join(tmp, "f.md"), "w") as fh:
                fh.write("fine\n")
            with open(os.path.join(tmp, "tests", "t.py"), "w") as fh:
                fh.write(f'KEY = "{fake_key_id}"\n')
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
            r = self.run_public(tmp, NO_PRIVATE)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            with open(os.path.join(tmp, "tests", "t.py"), "w") as fh:
                fh.write('MAIL = "someone@example.org"  # e-mails are fine in tests\n')
            with open(os.path.join(tmp, "f.md"), "w") as fh:
                fh.write("a 10-digit number 1234567890 and a path /abs/path/to/my-project are fine\n")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
            r = self.run_public(tmp, NO_PRIVATE)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # private patterns come from an explicit file OUTSIDE the repo and apply to every file
            pats = os.path.join(tempfile.gettempdir(), "zouinify-private-patterns-" + os.path.basename(tmp))
            with open(pats, "w") as fh:
                fh.write("# names never to publish\nsecretcorp\n")
            with open(os.path.join(tmp, "tests", "t.py"), "w") as fh:
                fh.write("x = 'mentions SecretCorp here'\n")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
            r = self.run_public(tmp, {"ZOUINIFY_PRIVATE_PATTERNS": pats})
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("secretcorp", r.stdout)
            os.remove(pats)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
