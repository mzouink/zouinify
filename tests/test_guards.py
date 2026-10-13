"""Repository guards (T-0159): the panel code may never launch, kill or type; the public repo holds no
private strings. Both are shell scripts under scripts/ that CI runs too; these tests pin their behaviour.

Hermetic: the public-guard tests never read ~/.config; they pass an explicit pattern file (or none).
The repo under test is $ZOUINIFY_REPO_UNDER_TEST (default: this checkout), so a base checkout without
the scripts gives the RED evidence."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
PANEL_GUARD = os.path.join(REPO, "scripts", "check_panel_guard.sh")
PANEL_AST = os.path.join(REPO, "scripts", "check_panel_ast.py")
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

    def check_offending(self, fname, bad):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, fname), "w") as fh:
                fh.write(f"# panel\n{bad}\n")
            r = self.run_guard(tmp)
            self.assertNotEqual(r.returncode, 0, f"should fail on: {bad}")
            self.assertIn("FAIL", r.stdout, bad)
        finally:
            shutil.rmtree(tmp)

    def test_one_visible_case_per_offending_variant(self):
        """T-0164: every planted variant is its own test method, so a dropped variant shows in the pytest count."""
        self.assertEqual(len([m for m in dir(type(self)) if m.startswith("test_offending_")]), len(OFFENDING))

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





# every string trick the desk planted against the regex guard; each one must fail the AST allowlist
AST_OFFENDING = [
    "import subprocess",
    "from zouinify import panelstate\nh = panelstate.read_panel_json(cfg)",      # returns the token hash
    "from zouinify import panelstate\npanelstate.set_token(cfg, 'x')",
    "from zouinify import webauth\nx = webauth.not_in_the_api(1)",                 # T-0174: exactly __all__
    "from zouinify import webauth\nt = webauth.threading.Thread",
    "from zouinify import webauth\nw = webauth._hosts(80, [])",
    "from zouinify import webauth\nwebauth.host_allowed = len",                    # T-0164: modules are read-only
    "from zouinify import panelstate\npanelstate.token_matches = lambda c, t: True",
    "from zouinify import readonly\ndel readonly.capture_pane",
    "from zouinify import webauth\nwebauth.RATE_LIMIT += 1",
    "from zouinify import webauth\nwebauth.RateLimiter.weight = len",
    "import os\nos.path.isfile = lambda p: True",
    "import re\nre.compile = None",
    "from http.server import BaseHTTPRequestHandler\nimport json\njson.dumps = str",
    "import os\nos.system('tmux kill-server')",
    "import sys\nsys.modules['o' + 's'].system('x')",
    "import os\nf = getattr(os, 'sys' + 'tem')",
    "m = __import__('os')",
    "import asyncio\nf = getattr(asyncio, 'create_sub' + 'process_exec')",
    "from zouinify import cli\ncli.main(['up'])",
    "from zouinify import commands",
    "from zouinify import readonly\nreadonly._tmux.kill_session('x')",
    "from zouinify import tickets as tk\nf = tk.ticket_sh",
    "from zouinify.registry import Registry\nRegistry(c).add({})",
    "from pathlib import Path\nPath('.ops/state/sessions.json').write_text('[]')",
    "import os\nos.kill(1, 9)",
    "import signal\nsignal.pthread_kill(1, 9)",
    "import socket\nsocket.socket(socket.AF_UNIX).connect('/tmp/tmux-1/default')",
    "import runpy\nrunpy.run_path('x.py')",
    "import code\ncode.InteractiveInterpreter().runsource('1')",
    "import webbrowser\nwebbrowser.open('http://127.0.0.1:1/')",
    "import os\nos.chmod('x.sh', 0o755)",
    "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:1/api/receipts', data=b'{}')",
    "import http.client\nhttp.client.HTTPConnection('127.0.0.1', 1).request('POST', '/api/receipts')",
    "open('x', 'w').write('y')",
    "from pathlib import Path\nPath('x').open('a').write('y')",
    "with open('x', mode='w') as fh:\n    fh.write('y')",
    "r = eval('1')",
    "exec('1')",
    "c = compile('1', 'x', 'eval')",
    "import importlib\nimportlib.import_module('zouinify.launch')",
    "x = object().__class__.__subclasses__()",
    "from os import system",
    "import os\nx = os.environ['PATH']",
    "import os\nos.getpid()",
    "from zouinify import launch",
    "from zouinify import tmux",
    "from zouinify import readonly\nreadonly.read_ticket_sh.__globals__['subprocess']",
    "import ctypes",
    "import shlex\nshlex.split('tmux send')",
    "import os\nf = os.popen('tmux kill-server')",
    "from zouinify.panel import sources\nsources.__dict__['os']",
    "import threading\nthreading.Thread(target=exec, args=('1',)).start()",
    "def f(m):\n    return getattr(m, 'k' + 'ill')",
    "import tempfile",
    "import glob",
    # the desk's second round (T-0160): walks through allowed modules, aliasing, star args, quoted modes
    "import http.server\nhttp.server.shutil.rmtree('/x')",
    "from http import server\nserver.shutil.copyfile('a', 'b')",
    "import http.client\nhttp.client.HTTPConnection('h').request('POST', '/api/receipts')",
    "import http\nhttp.client.HTTPConnection('h')",
    "o = open\no('p', 'w')",
    "open('p', **{'mode': 'a'})",
    "open(*['p', 'w'])",
    "from pathlib import Path\nf = Path('p').open\nf('w')",
    "from pathlib import Path\nPath('a').replace('b')",
    "from pathlib import Path\nPath('a').symlink_to('b')",
    "from pathlib import Path\nPath('a').hardlink_to('b')",
    "from pathlib import Path\nPath('a').lchmod(0o777)",
    "import os as o\no.killpg(1, 9)",
    "import os as o\no.remove('x')",
    "import os as o\no.replace('a', 'b')",
    "import re\nre.functools.partial(open, mode='w')",
    "from zouinify import panelstate\npanelstate.tempfile.mkstemp()",
    "from zouinify import panelstate\npanelstate.json.dump({}, open('x'))",
    "from zouinify import panelstate\npanelstate.os.remove('x')",
    "from zouinify import readonly\nreadonly.os.system('x')",
    "import os\nm = os\nm.system('x')",
    "from zouinify import readonly\nr = readonly\nr.fail('x')",
    "p = print\np('x')",
    "import json\nj = json",
    "import threading\nthreading.excepthook = None",
    "import datetime\ndatetime.sys.exit(1)",
    "import os\nos.path.os.system('x')",
    "with open('x') as fh:\n    fh.write('y')",
    "import time\ntime.os.system('x')",
    # the desk's third round (T-0160): relative imports and zouinify imports
    "from ..tickets import ticket_sh as t\nt(cfg, ['move', 'T-0001', 'done', 'x'])",
    "from ..launch import cmd_launch",
    "from .. import panelctl",
    "from ..tmux import _run",
    "from .. import tickets",
    "from ...zouinify import launch",
    "import zouinify.panel.sources",
    "import zouinify",
    "from zouinify.launch import cmd_up",
    "from zouinify.panel import nonexistent_module",
    "from .nonexistent import x",
    # the desk's fourth round (T-0160): imports inside bodies, more process/file primitives, .open on anything
    "class A:\n    import os as o\nA.o.spawnvpe(0, 'x', [], {})",
    "def f():\n    import os\n    return os.getpid()",
    "if True:\n    import json as j2",
    "try:\n    import tempfile\nexcept ImportError:\n    pass",
    "import os\nos.execlpe('x', 'x', {})",
    "import os\nos.pwrite(1, b'x', 0)",
    "import os\nos.dup2(1, 2)",
    "import os\nos.fchmod(1, 0o777)",
    "import os\nos.mkfifo('x')",
    "from pathlib import Path\nPath('x').open()",
    "x = Path('x').open",
    "import io\nio.open('x', 'w')",
]

# variants that need the real sibling files next to them (planted into a copy of the real panel dir)
AST_OFFENDING_WITH_SIBLINGS = [
    "from .sources import readonly as r\nr.json.dumps({})",
    "from . import server\nserver.readonly.json.dumps({})",
    "from . import sources\nsources.os.getpid()",
    "from . import sources\nsources.readonly.os.system('x')",
    "from .sources import nonexistent_name",
    "from .sources import panelstate as p\np.tempfile.mkstemp()",
    "from . import sources\nx = sources",
    "import zouinify.panel.sources\nzouinify.panel.sources.readonly.fail('x')",
    "from . import redact\nredact.redact = str",                                      # T-0164: a sibling is read-only too
    "from .sources import readonly as r\nr.capture_pane = lambda n: ''",
]
AST_ALLOWED_WITH_SIBLINGS = """from . import sources
from .sources import Snapshot, classify_item, item_id
from zouinify.panel.redact import redact
x = sources.item_id({"verb": "RUN"})
y = sources.readonly.version()
z = redact(classify_item({"verb": "RUN"})["hint"])
"""

AST_ALLOWED = """\"\"\"a panel module\"\"\"
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zouinify import panelstate, readonly, webauth

LIMITER = webauth.RateLimiter(webauth.RATE_LIMIT, webauth.RATE_WINDOW_S)

def allowed(headers, port):
    return webauth.host_allowed(headers, port, set()) and webauth.authorize("127.0.0.1", headers, True, bool, LIMITER, webauth.PANEL_COOKIE)[0] == 200

def read(p):
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()

def rows(cfg):
    for f in Path(cfg.root).glob("tickets/*.md"):
        fm = readonly.front_matter(str(f))
        yield os.path.join(cfg.root, os.path.basename(str(f))), fm.get("id"), f.stat().st_mtime
    panelstate.log(cfg, html.escape("ok " + hashlib.sha1(b"x").hexdigest()))
    return redact(readonly.capture_pane("x")), json.dumps({"t": time.time(), "d": dt.datetime.now().isoformat()}), re.compile("x")
"""


def _slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:n]


def _variant_tests(cls, prefix: str, variants, check: str) -> None:
    """One test method per planted variant (T-0164): `pytest -q` counts them, a dropped variant changes the number."""
    for i, v in enumerate(variants):
        label = v[1] if isinstance(v, tuple) else v
        args = v if isinstance(v, tuple) else (v,)

        def test(self, _args=args):
            getattr(self, check)(*_args)
        test.__doc__ = f"planted: {label!r}"
        name = f"test_{prefix}_{i:02d}_{_slug(label)}"
        assert not hasattr(cls, name), name
        setattr(cls, name, test)


class TestPanelAst(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.access(PANEL_AST, os.X_OK), f"no executable AST gate at {PANEL_AST}")

    def run_ast(self, *dirs):
        return subprocess.run([PANEL_AST, *dirs], capture_output=True, text=True)

    def test_real_panel_dir_passes(self):
        r = subprocess.run([PANEL_AST], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("panel ast ok", r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(REPO, "lib", "zouinify", "panel")), "the real panel dir must exist and be checked")

    def test_real_panel_dir_fails_with_a_planted_file(self):
        tmp = tempfile.mkdtemp()
        try:
            dst = os.path.join(tmp, "panel")
            shutil.copytree(os.path.join(REPO, "lib", "zouinify", "panel"), dst)
            r = self.run_ast(dst)
            self.assertEqual(r.returncode, 0, r.stdout)
            with open(os.path.join(dst, "clicks.py"), "w") as fh:
                fh.write("import os\n\ndef on_click(name):\n    os.system('tmux kill-session -t ' + name)\n")
            r = self.run_ast(dst)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("clicks.py", r.stdout)
        finally:
            shutil.rmtree(tmp)

    def check_sibling_variant(self, bad):
        tmp = tempfile.mkdtemp()
        try:
            dst = os.path.join(tmp, "panel")
            shutil.copytree(os.path.join(REPO, "lib", "zouinify", "panel"), dst)
            with open(os.path.join(dst, "clicks.py"), "w") as fh:
                fh.write(bad + "\n")
            r = self.run_ast(dst)
            self.assertNotEqual(r.returncode, 0, f"should fail on: {bad!r}\n{r.stdout}")
            self.assertIn("clicks.py", r.stdout, bad)
        finally:
            shutil.rmtree(tmp)

    def test_sibling_use_passes(self):
        tmp = tempfile.mkdtemp()
        try:
            dst = os.path.join(tmp, "panel")
            shutil.copytree(os.path.join(REPO, "lib", "zouinify", "panel"), dst)
            with open(os.path.join(dst, "clicks.py"), "w") as fh:
                fh.write(AST_ALLOWED_WITH_SIBLINGS)
            r = self.run_ast(dst)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp)

    def check_variant(self, bad):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "server.py"), "w") as fh:
                fh.write(bad + "\n")
            r = self.run_ast(tmp)
            self.assertNotEqual(r.returncode, 0, f"should fail on: {bad!r}\n{r.stdout}")
            self.assertIn("FAIL", r.stdout, bad)
        finally:
            shutil.rmtree(tmp)

    def test_one_visible_case_per_variant(self):
        """T-0164: every planted variant is its own test method, so a dropped variant shows in the pytest count."""
        names = dir(type(self))
        self.assertEqual(len([m for m in names if m.startswith("test_ast_offending_")]), len(AST_OFFENDING))
        self.assertEqual(len([m for m in names if m.startswith("test_ast_sibling_")]), len(AST_OFFENDING_WITH_SIBLINGS))

    def test_module_attributes_are_read_only(self):
        """The T-0174 review's gap: rebinding a checked function through its module passed the gate."""
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "server.py"), "w") as fh:
                fh.write("from zouinify import webauth\nwebauth.host_allowed = len\n")
            r = self.run_ast(tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("assignment to zouinify.webauth.host_allowed", r.stdout)
            with open(os.path.join(tmp, "server.py"), "w") as fh:
                fh.write("from zouinify import readonly\ndel readonly.capture_pane\n")
            r = self.run_ast(tmp)
            self.assertIn("deletion of zouinify.readonly.capture_pane", r.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_allowed_panel_code_passes(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "sources.py"), "w") as fh:
                fh.write(AST_ALLOWED)
            r = self.run_ast(tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_syntax_error_fails(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "x.py"), "w") as fh:
                fh.write("def (:\n")
            r = self.run_ast(tmp)
            self.assertNotEqual(r.returncode, 0)
        finally:
            shutil.rmtree(tmp)


_variant_tests(TestPanelGuard, "offending", OFFENDING, "check_offending")
_variant_tests(TestPanelAst, "ast_offending", AST_OFFENDING, "check_variant")
_variant_tests(TestPanelAst, "ast_sibling", AST_OFFENDING_WITH_SIBLINGS, "check_sibling_variant")


if __name__ == "__main__":
    unittest.main()
