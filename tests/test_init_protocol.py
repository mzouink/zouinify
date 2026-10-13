"""`zouinify init --protocol` (T-0185): the ticket protocol installed into a repository that has `.ops/config.yaml`, after a printed
plan and the human's typed yes on that terminal; never non-interactive, never from a session, never over an existing file, never
outside the root; init commits nothing."""
import os
import pty
import re
import select
import stat
import subprocess
import time
import unittest

from test_init import IDENTITY, InitWorld, snapshot
from test_core import HERE

MANIFEST = os.path.join(os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE), "templates", "protocol", "MANIFEST")


def core_version():
    """The vendored core's version from the manifest, so a re-pin never has to edit this file."""
    with open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("version "):
                return line.split(" ", 1)[1].strip()
    raise AssertionError("no version line in " + MANIFEST)

PROTOCOL_FILES = ("scripts/ticket.sh", "tickets/ticket.env", "tickets/plugins/none/plugin.sh", "tickets/plugins/none/docs/handoff.md",
                  "tickets/README.md", "tickets/_TEMPLATE.md", "tickets/WORKER_BRIEF.md")


class ProtocolWorld(InitWorld):
    def setUp(self):
        super().setUp()
        self.ok("init", "--non-interactive", *IDENTITY)          # the first half: config + briefs
        self.env = {k: v for k, v in self.env.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE")}   # not "inside a session"

    def tearDown(self):
        """InitWorld forbids a CLAUDE.md that init created; a test may plant one, so the check is: CLAUDE.md is exactly what the test wrote."""
        self.assertEqual(sorted(os.listdir(self.tmp)), self.outside_before, "init wrote next to the repository")
        self.assertEqual(os.listdir(self.home), [], "init touched $HOME")
        p = os.path.join(self.root, "CLAUDE.md")
        planted = getattr(self, "planted_claude", None)
        if planted is None:
            self.assertFalse(os.path.lexists(p), "init wrote CLAUDE.md")
        elif not os.path.islink(p):
            with open(p) as fh:
                self.assertEqual(fh.read(), planted, "init changed CLAUDE.md")
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant_claude(self, text):
        self.write("CLAUDE.md", text)
        self.planted_claude = text

    @staticmethod
    def without_config(snap):
        return {k: v for k, v in snap.items() if k != os.path.join(".ops", "config.yaml")}

    def drive(self, answer, args=("init", "--protocol")):
        """Run init --protocol on a pty, answer the yes question, return (exit status, text)."""
        from test_core import ZOUINIFY
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(self.root)
            os.execve(ZOUINIFY, [ZOUINIFY, *args], self.env)
        out = b""
        deadline = time.time() + 90
        answered = False
        status = None
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if b"Type yes" in out and not answered:
                    os.write(fd, answer.encode() + b"\n")
                    answered = True
            else:
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    pid = None
                    break
        if pid is not None:                                           # deadline hit: never hang the suite
            import signal
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        while True:                                                   # drain what the child wrote after the last select
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(fd)
        return os.waitstatus_to_exitcode(status) if status is not None else None, out.decode("utf-8", "replace")


class TestInitProtocol(ProtocolWorld):
    def test_refusals_before_anything_is_written(self):
        before = snapshot(self.root)
        r = self.z("init", "--protocol", "--non-interactive", *IDENTITY)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--protocol takes no --non-interactive", r.stderr)
        r = self.nz("init", "--protocol")
        self.assertIn("real terminal", r.stderr, "no tty: refused")
        r = self.nz("init", "--protocol", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("own terminal", r.stderr, "inside a Claude session: refused even before the tty check")
        r = self.ok("init", "--protocol", "--dry-run", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("(dry run: nothing written)", r.stdout, "a session may show the plan")
        self.assertEqual(snapshot(self.root), before)
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none", "  deploy_plugin: aws-terraform"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("which zouinify does not ship", r.stderr)
        self.assertIn("PLUGIN_HOOKS", r.stderr)
        self.write(".ops/config.yaml", cfg.replace("  dir: tickets", "  dir: work"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("fixed paths", r.stderr)
        self.write(".ops/config.yaml", cfg.replace("repos:\n", "repos:\n  - {name: a, path: ../a, trunk: main}\n  - {name: b, path: ../a, trunk: main}\n"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("resolve to the same directory", r.stderr)
        self.write(".ops/config.yaml", cfg.replace("repos:\n", "repos:\n  - {name: a, path: ../a, trunk: main}\n  - {name: b, path: ../b, trunk: main}\n"))
        r = self.ok("init", "--protocol", "--dry-run")
        self.assertIn("REPOS=demo a b", r.stdout, "the 1.1.x core takes N repos")
        self.write(".ops/config.yaml", cfg)
        os.remove(os.path.join(self.root, ".ops", "config.yaml"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("run `zouinify init` first", r.stderr)
        self.write(".ops/config.yaml", cfg)
        self.assertEqual(self.without_config(snapshot(self.root)), self.without_config(before), "the config was rewritten by the test; nothing else")

    def test_dry_run_prints_the_plan_the_env_and_the_claude_block_and_writes_nothing(self):
        before = snapshot(self.root)
        r = self.ok("init", "--protocol", "--dry-run")
        for rel in PROTOCOL_FILES:
            self.assertIn(f"write    {rel}", r.stdout)
        self.assertIn("scripts/ticket.sh  (", r.stdout)
        self.assertIn("mode 755", r.stdout)
        self.assertIn("append   .gitignore  (line `.claude/worktrees/`)", r.stdout, "init wrote .gitignore with .ops/state/ already")
        self.assertIn("paste    CLAUDE.md  (never written by init: no CLAUDE.md yet; create it with the block printed below)", r.stdout)
        self.assertIn("--- tickets/ticket.env ---\n# generated-from: .ops/config.yaml sha256 ", r.stdout)
        self.assertIn("\nPROJECT=demo\nTRUNK=dev\n", r.stdout)
        self.assertIn("DEPLOY_PLUGIN=none", r.stdout)
        self.assertIn("--- CLAUDE.md block (paste it yourself; a session must never edit CLAUDE.md, and neither does init) ---\n"
                      "<!-- zouinify:protocol start -->\n## Ticket protocol (demo;", r.stdout)
        self.assertIn("Only the ticket desk (`dm-desk`)", r.stdout)
        self.assertIn("Trunk is `dev`; `main` is production and nobody commits to it.", r.stdout)
        self.assertIn("<!-- zouinify:protocol end -->", r.stdout)
        self.assertIn("commit them yourself afterwards; init commits nothing", r.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_yes_on_the_terminal_installs_everything_once(self):
        self.plant_claude("# demo\n\nhouse rules\n")
        rc, text = self.drive("yes")
        self.assertEqual(rc, 0, text)
        for rel in PROTOCOL_FILES:
            self.assertIn(f"wrote      {rel}", text)
            self.assertTrue(os.path.isfile(os.path.join(self.root, rel)), rel)
        self.assertIn("paste    CLAUDE.md  (never written by init: paste the block printed below at the end of the file)", text)
        self.assertIn("paste      CLAUDE.md  (not written: add the block printed above yourself)", text)
        self.assertIn("next: paste the CLAUDE.md block printed above", text)
        self.assertIn("appended   .gitignore", text)
        self.assertIn(f"verify     scripts/ticket.sh version -> ticket.sh {core_version()}  plugin none", text, "the installed core parses its env")
        self.assertIn("next: paste the CLAUDE.md block printed above", text)
        mode = stat.S_IMODE(os.stat(os.path.join(self.root, "scripts", "ticket.sh")).st_mode)
        self.assertEqual(mode & 0o111, 0o111, "the core is executable")
        with open(os.path.join(self.root, "CLAUDE.md")) as fh:
            claude = fh.read()
        self.assertEqual(claude, "# demo\n\nhouse rules\n", "CLAUDE.md is never written by init")
        self.assertIn("5. Never, from any session, anything on the human-only list:\r\n  - production deploys and applies", text,
                      "the block is printed for the human to paste")
        with open(os.path.join(self.root, ".gitignore")) as fh:
            self.assertIn(".claude/worktrees/\n", fh.read())
        env_text = open(os.path.join(self.root, "tickets", "ticket.env")).read()
        self.assertRegex(env_text, r"(?m)^REPO_DEMO_PATH=\.$")
        readme = open(os.path.join(self.root, "tickets", "README.md")).read()
        self.assertIn("# tickets/ -- the work queue for proj\n", readme)
        self.assertNotIn("{{", readme)
        # the vendored core really runs here: version from the repo root, and from a subdirectory
        r = subprocess.run(["bash", "scripts/ticket.sh", "version"], cwd=self.root, capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"ticket.sh {core_version()}  plugin none", r.stdout)
        r = subprocess.run(["bash", os.path.join(self.root, "scripts", "ticket.sh"), "board"], cwd=self.root, capture_output=True, text=True,
                           env=dict(self.env, TICKET_AS="tester"), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # a second run: everything exists, no question asked, nothing changed
        before = snapshot(self.root)
        r = self.ok("init", "--protocol")                        # no tty needed: there is nothing to ask about
        self.assertIn("nothing to write: the protocol is installed", r.stdout)
        self.assertEqual(snapshot(self.root), before)
        self.assertIn("CLAUDE.md still lacks the block: paste it from above", r.stdout)
        self.plant_claude("# demo\n\n<!-- zouinify:protocol start -->\nmy block\n<!-- zouinify:protocol end -->\n")
        r = self.ok("init", "--protocol")
        self.assertIn("the block's markers are already there; nothing to paste", r.stdout)
        self.assertNotIn("still lacks", r.stdout)
        self.plant_claude("# demo\n\n<!-- zouinify:protocol start -->\nhalf\n")
        r = self.ok("init", "--protocol", "--dry-run")
        self.assertIn("WARNING: only one of the two markers is present", r.stdout)

    def test_a_symlinked_tickets_or_scripts_dir_is_refused_before_anything_is_written(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        self.outside_before = sorted(os.listdir(self.tmp))
        os.symlink(outside, os.path.join(self.root, "tickets"))
        before = snapshot(self.root)
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("tickets is a symlink", r.stderr)
        self.assertEqual(os.listdir(outside), [], "nothing created through the link")
        self.assertEqual(snapshot(self.root), before)
        os.unlink(os.path.join(self.root, "tickets"))
        os.makedirs(os.path.join(self.root, "tickets"))
        os.symlink(outside, os.path.join(self.root, "tickets", "plugins"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("tickets/plugins is a symlink", r.stderr)
        self.assertEqual(os.listdir(outside), [])
        os.unlink(os.path.join(self.root, "tickets", "plugins"))
        os.symlink(os.path.join(outside, "ticket.env"), os.path.join(self.root, "tickets", "ticket.env"))
        r = self.nz("init", "--protocol", "--dry-run")
        self.assertIn("tickets/ticket.env is a symlink", r.stderr)
        self.assertEqual(os.listdir(outside), [])
        with open(os.path.join(self.root, ".gitignore"), "a") as fh:
            fh.write(".claude/\n")
        os.unlink(os.path.join(self.root, "tickets", "ticket.env"))
        r = self.ok("init", "--protocol", "--dry-run")
        self.assertIn("exists   .gitignore  (line `.claude/worktrees/`)", r.stdout, "`.claude/` already covers the worktrees")

    def test_anything_but_yes_writes_nothing(self):
        before = snapshot(self.root)
        rc, text = self.drive("no")
        self.assertEqual(rc, 1, text)
        self.assertIn("nothing written.", text)
        self.assertEqual(snapshot(self.root), before)
        rc, text = self.drive("YES")
        self.assertEqual(rc, 1, "only a lowercase yes")
        self.assertEqual(snapshot(self.root), before)

    def test_existing_files_are_left_as_is_and_a_symlinked_claude_md_is_never_followed(self):
        self.write("tickets/README.md", "mine\n")
        self.write("scripts/ticket.sh", "#!/bin/sh\necho mine\n")
        os.symlink(os.path.join(self.tmp, "elsewhere.md"), os.path.join(self.root, "CLAUDE.md"))
        self.planted_claude = ""                                        # a dangling link: tearDown skips the content check
        self.outside_before = sorted(os.listdir(self.tmp))
        rc, text = self.drive("yes")
        self.assertEqual(rc, 0, text)
        self.assertIn("exists   tickets/README.md", text)
        self.assertIn("exists     scripts/ticket.sh  (left as is)", text)
        self.assertIn("verify     skipped: scripts/ticket.sh was already here and is not this run's to execute", text)
        self.assertNotIn("echo mine", text.split("Type yes")[1], "the foreign script is never run")
        self.assertIn("paste      CLAUDE.md  (not written", text)
        self.assertEqual(open(os.path.join(self.root, "tickets", "README.md")).read(), "mine\n")
        self.assertEqual(open(os.path.join(self.root, "scripts", "ticket.sh")).read(), "#!/bin/sh\necho mine\n")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere.md")), "nothing written through the link")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "tickets", "ticket.env")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "tickets", "WORKER_BRIEF.md")))


if __name__ == "__main__":
    unittest.main()
