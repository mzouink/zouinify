"""Repository guards (T-0159): the panel code may never launch, kill or type; the public repo holds no
private strings. Both are shell scripts under scripts/ that CI runs too; these tests pin their behaviour.

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


class TestPanelGuard(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.access(PANEL_GUARD, os.X_OK), f"no executable guard at {PANEL_GUARD}")

    def run_guard(self, d):
        return subprocess.run([PANEL_GUARD, d], capture_output=True, text=True)

    def test_repo_passes(self):
        r = subprocess.run([PANEL_GUARD], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_offending_lines_fail(self):
        tmp = tempfile.mkdtemp()
        try:
            for bad in ("subprocess.run(['tmux', 'new-session', '-d'])",
                        "os.system('tmux kill-session -t x')",
                        "cmd = 'tmux send-keys -t tickets ok Enter'",
                        "sh('scripts/ticket.sh move T-0001 done \"x\"')",
                        "from zouinify.launch import cmd_up",
                        "tmux.new_session('x', '/', {}, [])",
                        "run(['terraform', 'apply'])",
                        "run('git push origin dev')"):
                with open(os.path.join(tmp, "server.py"), "w") as fh:
                    fh.write(f"# panel\n{bad}\n")
                r = self.run_guard(tmp)
                self.assertNotEqual(r.returncode, 0, f"should fail on: {bad}")
            with open(os.path.join(tmp, "server.py"), "w") as fh:
                fh.write("import json\nrows = tmux.capture_pane('x')\nlogs = glob('terraform/*.apply.log')\n")
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

    def test_tracked_files_pass(self):
        r = subprocess.run([PUBLIC_GUARD], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_private_strings_fail(self):
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            env = dict(os.environ, ZOUINIFY_PRIVATE_PATTERNS=os.path.join(tmp, "no-such-file"))
            # the fake key id and secret are assembled at runtime so that no credential-shaped literal
            # sits in this file (GitHub push protection scans the pushed blobs, tests included)
            fake_key_id = "AKIA" + "ABCDEFGHIJ" + "KLMNOP"
            fake_secret = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCY" + "EXAMPLEKEY12"
            for bad in ("account 123456789012", f"key {fake_key_id}", "mail someone@example.org",
                        "path /home/someone/project", f"aws_secret_access_key = {fake_secret}"):
                with open(os.path.join(tmp, "f.md"), "w") as fh:
                    fh.write(bad + "\n")
                subprocess.run(["git", "-C", tmp, "add", "f.md"], check=True)
                r = subprocess.run([PUBLIC_GUARD], cwd=tmp, capture_output=True, text=True, env=env)
                self.assertNotEqual(r.returncode, 0, f"should fail on: {bad}")
            with open(os.path.join(tmp, "f.md"), "w") as fh:
                fh.write("a 10-digit number 1234567890 and a path /abs/path/to/my-project are fine\n")
            subprocess.run(["git", "-C", tmp, "add", "f.md"], check=True)
            r = subprocess.run([PUBLIC_GUARD], cwd=tmp, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # extra patterns come from a file OUTSIDE the repo (where a project keeps its own names)
            pats = os.path.join(tempfile.gettempdir(), "zouinify-private-patterns-" + os.path.basename(tmp))
            with open(pats, "w") as fh:
                fh.write("# names never to publish\nsecretcorp\n")
            with open(os.path.join(tmp, "f.md"), "w") as fh:
                fh.write("mentions SecretCorp here\n")
            subprocess.run(["git", "-C", tmp, "add", "f.md"], check=True)
            r = subprocess.run([PUBLIC_GUARD], cwd=tmp, capture_output=True, text=True,
                               env=dict(os.environ, ZOUINIFY_PRIVATE_PATTERNS=pats))
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("secretcorp", r.stdout)
            os.remove(pats)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
