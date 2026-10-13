"""`zouinify init` (T-0184): .ops/config.yaml from questions or flags, the briefs from the templates, the .gitignore line;
never an overwrite, never a byte outside the repository root, never ~/.claude or CLAUDE.md."""
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import zlib

from test_core import FAKES, HERE, ZOUINIFY, World, _chmod_x

LIB = os.path.join(os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE), "lib")   # the repo under test, as test_term

IDENTITY = ("--name", "demo", "--short", "dm", "--human", "pat")


def snapshot(root):
    """{relative path: (size, mtime_ns)} of every file under root except .git."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class InitWorld(World):
    """A bare git repository (branch dev, no commit) instead of World's configured project."""

    def setUp(self):
        self.assertTrue(os.path.isfile(ZOUINIFY) and os.access(ZOUINIFY, os.X_OK), f"no executable CLI at {ZOUINIFY}")
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="zouinify-init-"))
        self.root = os.path.join(self.tmp, "proj")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "tmux-state")
        for d in (self.root, self.home, self.state):
            os.makedirs(d)
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "symbolic-ref", "HEAD", "refs/heads/dev"], check=True)
        for f in ("tmux", "claude"):
            _chmod_x(os.path.join(FAKES, f))
        env = dict(os.environ)
        for k in ("ZOUINIFY_PROJECT", "CLAUDECODE"):
            env.pop(k, None)
        env.update(PATH=FAKES + os.pathsep + env.get("PATH", ""), FAKE_TMUX_STATE=self.state, HOME=self.home, USER="tester",
                   FAKE_AWS_LOG=os.path.join(self.tmp, "aws.log"), FAKE_TICKET_LOG=os.path.join(self.tmp, "t.log"),
                   TICKET_AS="tester-session", ZOUINIFY_NOW="2026-10-13T07:00:00-04:00")
        self.env = env
        self.outside_before = sorted(os.listdir(self.tmp))

    def tearDown(self):
        self.assertFalse([c for c in self.tmux_calls() if c and c[0] == "send-keys"], "the CLI must never run tmux send-keys")
        self.assertEqual(sorted(os.listdir(self.tmp)), self.outside_before, "init wrote next to the repository")
        self.assertEqual(os.listdir(self.home), [], "init touched $HOME")
        self.assertFalse(os.path.exists(os.path.join(self.root, "CLAUDE.md")), "init wrote CLAUDE.md")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return fh.read()

    def init(self, *more, **kw):
        return self.ok("init", "--non-interactive", *IDENTITY, *more, **kw)

    def proposed_port(self):
        """The formula, then the same skip loop init runs (checked here, right before init runs)."""
        start = 4700 + zlib.crc32(self.root.encode()) % 90
        for i in range(90):
            p = 4700 + (start - 4700 + i) % 90
            if all(self._free(p + k) for k in (0, 1, 2)):
                return p
        return start

    @staticmethod
    def _free(port):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class TestInit(InitWorld):
    def test_refuses_to_ask_without_a_terminal(self):
        r = self.nz("init")
        self.assertIn("--non-interactive", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops")))

    def test_non_interactive_needs_the_identity_flags_and_validates_them(self):
        r = self.nz("init", "--non-interactive", "--name", "demo")
        self.assertIn("--short", r.stderr)
        for bad in (("--short", "ABC"), ("--short", "toolongtag"), ("--port", "80"), ("--port", "x"), ("--name", 'a"b'),
                    ("--prod", "two words"), ("--name", "foo\u2028model_re: x"), ("--human", "a\x0bb"), ("--model", "a\\b")):
            r = self.nz("init", "--non-interactive", *IDENTITY, *bad)
            self.assertIn(f"zouinify: {bad[0].lstrip('-')}:", r.stderr, bad)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops")), "a refused init writes nothing")
        r = self.nz("init", "--non-interactive", *IDENTITY, "--prod", "rel#1")
        self.assertIn("zouinify: prod: not a valid git branch name", r.stderr, "branch names follow git's rules plus a sane alphabet")
        r = self.ok("init", "--dry-run", "--non-interactive", "--name", "demo", "--short", "dm", "--human", "Pat O'Brien", "--prod", "release/2026.10")
        self.assertIn("contact: {human: \"Pat O'Brien\"}", r.stdout, "an apostrophe is fine: the scalars are quoted")
        self.assertIn('  - "git push to release/2026.10; fast-forwarding it', r.stdout)

    def test_dry_run_prints_the_plan_and_the_config_and_writes_nothing(self):
        before = snapshot(self.root)
        r = self.init("--dry-run")
        self.assertIn("write    .ops/config.yaml", r.stdout)
        self.assertIn("write    .ops/briefs/desk.md", r.stdout)
        self.assertIn("create   .gitignore", r.stdout)
        self.assertIn("dry run: nothing written", r.stdout)
        self.assertIn('project: {name: "demo", short: "dm"}', r.stdout)
        self.assertIn("T-0185", r.stdout, "the plan says what init does not write")
        self.assertEqual(snapshot(self.root), before)

    def test_writes_config_briefs_and_gitignore_and_everything_validates(self):
        self.write(".gitignore", "node_modules/")                         # no trailing newline on purpose
        r = self.init()
        for rel in (".ops/config.yaml", ".ops/briefs/desk.md", ".ops/briefs/manager.md", ".ops/briefs/debug.md",
                    ".ops/briefs/chrome_debug.md", ".ops/briefs/worker.md"):
            self.assertIn(f"wrote      {rel}", r.stdout)
            self.assertTrue(os.path.isfile(os.path.join(self.root, rel)), rel)
        self.assertEqual(self.read(".gitignore"), "node_modules/\n.ops/state/\n")
        cfg = self.read(".ops/config.yaml")
        port = self.proposed_port()
        self.assertIn('trunk: "dev", prod: "main"', cfg, "trunk from the checked-out branch, prod main when trunk is not")
        self.assertIn(f"\n  port: {port} ", cfg, "4700 + crc32(root) % 90, the first base with its three ports free")
        m = re.search(r"^  port: (\d+)", cfg, re.M)
        p = int(m.group(1))
        self.assertIn(f"hook_port: {p + 2}", cfg)
        self.assertRegex(cfg, rf"terminal:.*\n  enabled: false .*\n  port: {p + 1} ", "the terminal block is proposed off")
        self.assertIn('contact: {human: "pat"}', cfg)
        self.assertIn('name: "dm-desk"', cfg)
        self.assertIn("deploy_kinds: [none]", cfg)
        self.assertIn("README.project.md", cfg, "the comment points at the optional consumer-owned files")
        self.assertIn("config     ok: project demo/dm", r.stdout)
        self.assertIn("would launch:", r.stdout, "init ends with up's dry run")
        self.assertIn("tmux dm-desk", r.stdout)
        self.assertNotIn("warning:", r.stdout, "the written config has no warning")
        briefs = "".join(self.read(f".ops/briefs/{r}.md") for r in ("desk", "manager", "debug", "chrome_debug", "worker"))
        self.assertIn("pat", briefs, "the briefs are rendered with this config (the human's name)")
        self.assertIn("dm-desk", briefs)
        self.assertNotIn("{{", briefs, "no placeholder left")
        # what init wrote is exactly what the other commands expect
        self.ok("config", "--check")
        r = self.ok("briefs", "render")
        self.assertEqual(r.stdout.count("unchanged"), 5, r.stdout)
        r = self.ok("up", "--dry-run")
        self.assertIn("would launch:", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, "tickets")), "the protocol files are T-0185's")
        self.assertFalse(os.path.exists(os.path.join(self.root, "scripts")))

    def test_second_run_reports_and_changes_nothing(self):
        self.init()
        before = snapshot(self.root)
        time.sleep(0.01)
        r = self.ok("init", "--non-interactive", "--name", "other", "--short", "xx", "--human", "someone-else")
        self.assertIn("nothing written", r.stdout)
        self.assertIn("exists     .ops/config.yaml", r.stdout)
        self.assertIn("exists     .ops/briefs/desk.md", r.stdout)
        self.assertIn("config     ok: project demo/dm", r.stdout)
        self.assertEqual(snapshot(self.root), before)
        r = self.ok("init")                                               # no flags, no terminal: still only a report
        self.assertIn("nothing written", r.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_an_existing_brief_is_left_as_is(self):
        self.write(".ops/briefs/desk.md", "my own desk brief\n")
        r = self.init()
        self.assertIn("exists   .ops/briefs/desk.md", r.stdout)
        self.assertIn("exists     .ops/briefs/desk.md  (left as is)", r.stdout)
        self.assertEqual(self.read(".ops/briefs/desk.md"), "my own desk brief\n")
        self.assertIn("wrote      .ops/briefs/manager.md", r.stdout)

    def test_gitignore_already_listing_the_state_dir_is_untouched(self):
        self.write(".gitignore", "# mine\n.ops/state/\n")
        r = self.init()
        self.assertIn("already lists", r.stdout)
        self.assertEqual(self.read(".gitignore"), "# mine\n.ops/state/\n")

    def test_gitignore_directory_is_skipped(self):
        os.makedirs(os.path.join(self.root, ".gitignore"))
        r = self.init()
        self.assertIn("skipped    .gitignore is a directory", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".ops", "config.yaml")))

    def test_a_brief_symlink_is_refused_before_anything_is_written(self):
        elsewhere = os.path.join(self.tmp, "elsewhere.md")
        with open(elsewhere, "w") as fh:
            fh.write("x\n")
        self.outside_before = sorted(os.listdir(self.tmp))
        os.makedirs(os.path.join(self.root, ".ops", "briefs"))
        os.symlink(elsewhere, os.path.join(self.root, ".ops", "briefs", "desk.md"))
        r = self.nz("init", "--non-interactive", *IDENTITY)
        self.assertIn("desk.md is a symlink", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "config.yaml")), "refused before the first write")
        os.unlink(os.path.join(self.root, ".ops", "briefs", "desk.md"))
        os.symlink(os.path.join(self.tmp, "nowhere.md"), os.path.join(self.root, ".ops", "briefs", "desk.md"))   # dangling
        r = self.nz("init", "--non-interactive", *IDENTITY)
        self.assertIn("desk.md is a symlink", r.stderr)
        os.unlink(os.path.join(self.root, ".ops", "briefs", "desk.md"))
        os.rmdir(os.path.join(self.root, ".ops", "briefs"))
        os.symlink(self.tmp, os.path.join(self.root, ".ops", "briefs"))
        r = self.nz("init", "--non-interactive", *IDENTITY)
        self.assertIn(".ops/briefs is a symlink", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "config.yaml")))

    def test_gitignore_symlink_is_skipped_not_followed(self):
        target = os.path.join(self.root, "real-ignore")
        self.write("real-ignore", "a\n")
        os.symlink(target, os.path.join(self.root, ".gitignore"))
        r = self.init()
        self.assertIn("skipped    .gitignore is a symlink", r.stdout)
        self.assertEqual(self.read("real-ignore"), "a\n")

    def test_port_follows_the_formula_skips_held_ports_and_honours_the_flag(self):
        p = self.proposed_port()
        with socket.socket() as held:
            held.bind(("127.0.0.1", p))
            held.listen(1)
            expected = self.proposed_port()                               # the same loop, now skipping p
            self.assertNotEqual(expected, p)
            r = self.init("--dry-run")
            m = re.search(r"^  port: (\d+)", r.stdout, re.M)
            chosen = int(m.group(1))
            self.assertEqual(chosen, expected, "a held port is skipped to the next base with three free ports")
            self.assertTrue(4700 <= chosen < 4790, chosen)
        r = self.init("--dry-run", "--port", "5555")
        self.assertIn("\n  port: 5555 ", r.stdout)
        self.assertIn("hook_port: 5557", r.stdout)
        self.assertIn("\n  port: 5556 ", r.stdout)

    def test_refuses_an_ops_symlink_and_a_subdirectory(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.outside_before = sorted(os.listdir(self.tmp))
        os.symlink(elsewhere, os.path.join(self.root, ".ops"))
        r = self.nz("init", "--non-interactive", *IDENTITY)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(os.listdir(elsewhere), [], "nothing written through the link")
        os.unlink(os.path.join(self.root, ".ops"))
        sub = os.path.join(self.root, "src")
        os.makedirs(sub)
        r = self.nz("init", "--non-interactive", *IDENTITY, cwd=sub)
        self.assertIn("repository root", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(sub, ".ops")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops")))
        r = self.ok("--project", self.root, "init", "--non-interactive", *IDENTITY, cwd=sub)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".ops", "config.yaml")), "--project names the root")

    def test_a_repository_on_main_proposes_main_and_no_prod(self):
        subprocess.run(["git", "-C", self.root, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        r = self.init("--dry-run")
        self.assertIn('trunk: "main", default: true}', r.stdout)
        self.assertNotIn("prod:", r.stdout)
        self.assertIn("git push to the production branch", r.stdout)

    def test_a_directory_without_git_is_refused(self):
        shutil.rmtree(os.path.join(self.root, ".git"))
        r = self.nz("init", "--non-interactive", *IDENTITY)
        self.assertIn("not a git repository", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".gitignore")))
        r = self.nz("--project", self.home, "init", "--non-interactive", *IDENTITY)   # $HOME is the classic footgun
        self.assertIn("not a git repository", r.stderr)

    def test_gitignore_hard_link_is_skipped_not_written_through(self):
        outside = os.path.join(self.tmp, "outside-ignore")
        with open(outside, "w") as fh:
            fh.write("theirs\n")
        self.outside_before = sorted(os.listdir(self.tmp))
        os.link(outside, os.path.join(self.root, ".gitignore"))
        r = self.init()
        self.assertIn("skipped    .gitignore is a hard link", r.stdout)
        with open(outside) as fh:
            self.assertEqual(fh.read(), "theirs\n")

    def test_a_file_appearing_between_plan_and_write_is_reported_and_the_run_undone(self):
        import io
        import sys
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        from zouinify import ZouinifyError, initcmd
        self.write(".ops/briefs/manager.md", "theirs\n")
        out = io.StringIO()
        with self.assertRaises(ZouinifyError) as cm:
            initcmd.write_new(self.root, os.path.join(".ops", "briefs", "manager.md"), "mine\n", out)
        self.assertIn("appeared meanwhile", str(cm.exception))
        self.assertEqual(self.read(".ops/briefs/manager.md"), "theirs\n")
        created = [os.path.join(".ops", "config.yaml")]
        self.write(".ops/config.yaml", "half\n")
        initcmd.undo(self.root, created, out)
        self.assertIn("removed    .ops/config.yaml", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "config.yaml")))

    def test_questions_on_a_terminal_take_enter_as_the_proposal_and_ask_again_on_a_bad_value(self):
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(self.root)
            os.execve(ZOUINIFY, [ZOUINIFY, "init"], self.env)
        answers = iter([b"\n", b"Bad!\n", b"zz\n", b"\n", b"\n", b"\n", b"\n", b"pat\n"])
        out = b""
        deadline = time.time() + 60
        asked = 0
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
                while out.count(b"]: ") > asked:
                    asked += 1
                    os.write(fd, next(answers))
            else:
                wpid, _status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    pid = None
                    break
        if pid is not None:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        os.close(fd)
        text = out.decode("utf-8", "replace")
        self.assertEqual(asked, 8, text)
        self.assertIn("project name [proj]:", text)
        self.assertIn("short: 1-8 lowercase letters or digits", text, "a bad answer is explained and asked again")
        self.assertIn("trunk branch [dev]:", text)
        self.assertIn("production branch (empty for none) [main]:", text)
        self.assertIn("your name (the briefs address you) [tester]:", text)
        self.assertIn("wrote      .ops/config.yaml", text)
        cfg = self.read(".ops/config.yaml")
        self.assertIn('project: {name: "proj", short: "zz"}', cfg)
        self.assertIn('contact: {human: "pat"}', cfg)
        self.assertIn('trunk: "dev", prod: "main"', cfg)
        self.ok("config", "--check")


if __name__ == "__main__":
    unittest.main()
