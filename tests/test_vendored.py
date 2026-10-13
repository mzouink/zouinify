"""templates/protocol/ (T-0185): the ticket protocol core vendored byte-identical from the upstream project at a pinned commit.
MANIFEST pins the commit, the core's version, its hook list and every file's sha256; when $ZOUINIFY_UPSTREAM_REPO names a checkout
of the upstream repository, every file is also diffed against `git show <pin>:<path>` there. The vendored harness and
test_ticket_env.py run against the vendored core with the none plugin."""
import hashlib
import os
import re
import subprocess
import sys
import unittest

from test_core import HERE, World

REPO_UNDER_TEST = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
PROTOCOL = os.path.join(REPO_UNDER_TEST, "templates", "protocol")
MANIFEST = os.path.join(PROTOCOL, "MANIFEST")
UPSTREAM = os.environ.get("ZOUINIFY_UPSTREAM_REPO")


def manifest():
    m = {"sha256": {}}
    with open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            key, rest = line.split(" ", 1)
            if key == "sha256":
                digest, path = rest.split(" ", 1)
                m["sha256"][path] = digest
            else:
                m[key] = rest
    return m


def sha256_of(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestManifest(unittest.TestCase):
    def test_every_vendored_file_matches_its_recorded_sha256_and_nothing_else_is_vendored(self):
        m = manifest()
        self.assertRegex(m["pin"], r"^[0-9a-f]{40}$")
        self.assertTrue(m["sha256"], "the manifest lists files")
        for path, digest in m["sha256"].items():
            with self.subTest(path=path):
                full = os.path.join(PROTOCOL, path)
                self.assertTrue(os.path.isfile(full), f"{path} missing under templates/protocol")
                self.assertEqual(sha256_of(full), digest)
        vendored_dirs = {"scripts", os.path.join("tickets", "plugins")}
        on_disk = set()
        for d in vendored_dirs:
            for dirpath, _dirs, files in os.walk(os.path.join(PROTOCOL, d)):
                for f in files:
                    if f.endswith(".pyc") or "__pycache__" in dirpath:
                        continue
                    on_disk.add(os.path.relpath(os.path.join(dirpath, f), PROTOCOL))
        self.assertEqual(on_disk, set(m["sha256"]), "every file under scripts/ and tickets/plugins/ is pinned, and only those")
        self.assertIn("scripts/ticket.sh", m["sha256"])
        self.assertIn("tickets/plugins/none/plugin.sh", m["sha256"])
        self.assertNotIn("tickets/plugins/aws-terraform/plugin.sh", m["sha256"], "a project's own deploy plugin is never vendored")

    def test_version_and_hook_list_are_pinned_and_the_none_plugin_defines_exactly_them(self):
        m = manifest()
        core = open(os.path.join(PROTOCOL, "scripts", "ticket.sh"), encoding="utf-8").read()
        self.assertIn(f'\nTICKET_SH_VERSION="{m["version"]}"', core)
        self.assertIn(f'\nPLUGIN_HOOKS="{m["hooks"]}"', core)
        hooks = m["hooks"].split()
        self.assertEqual(len(hooks), 13)
        plugin = open(os.path.join(PROTOCOL, "tickets", "plugins", "none", "plugin.sh"), encoding="utf-8").read()
        defined = re.findall(r"(?m)^(plugin_[a-z_]+)\(\)", plugin)
        self.assertEqual(sorted(defined), sorted(hooks))
        docs = sorted(os.listdir(os.path.join(PROTOCOL, "tickets", "plugins", "none", "docs")))
        self.assertEqual(docs, ["brief-live-mutations.md", "brief-paid-runs.md", "evidence-deployed.md", "evidence-done.md", "handoff.md",
                                "rule-after-deploy.md", "rule-deploy-evidence.md", "rule-no-live-mutations.md", "rule-protect-artifacts.md",
                                "verbs.md"])
        self.assertTrue(os.access(os.path.join(PROTOCOL, "scripts", "ticket.sh"), os.X_OK), "the core is executable in the template")

    def test_upstream_diff_when_a_checkout_is_named(self):
        if not UPSTREAM:
            self.skipTest("ZOUINIFY_UPSTREAM_REPO not set: the upstream checkout is private; the sha256 manifest stands in")
        m = manifest()
        for path in m["sha256"]:
            with self.subTest(path=path):
                r = subprocess.run(["git", "-C", UPSTREAM, "show", f"{m['pin']}:{path}"], capture_output=True, timeout=30)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                with open(os.path.join(PROTOCOL, path), "rb") as fh:
                    self.assertEqual(fh.read(), r.stdout, f"{path} differs from upstream {m['pin'][:7]}")


class TestVendoredSuite(World):
    """The vendored harness drives the vendored core with the none plugin: what a fresh project gets from `init --protocol`."""

    def test_vendored_test_ticket_env_passes_against_the_none_plugin(self):
        out = os.path.join(self.tmp, "rendered")
        self.ok("protocol", "render", "--out", out)
        env = dict(self.env, TICKET_WORLD_PLUGIN="none", TICKET_SH_UNDER_TEST=os.path.join(PROTOCOL, "scripts", "ticket.sh"),
                   TICKET_PLUGINS_UNDER_TEST=os.path.join(PROTOCOL, "tickets", "plugins"),
                   TICKET_TEMPLATE_UNDER_TEST=os.path.join(out, "_TEMPLATE.md"), PYTHONDONTWRITEBYTECODE="1")
        for k in ("TICKET_AS", "TICKET_FORCE", "TICKET_TRUNK"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            os.path.join(PROTOCOL, "scripts", "tests", "test_ticket_env.py"),
                            "-k", "not TestDocsFragments and not matches_both_shipped_plugins"],
                           cwd=self.tmp, env=env, capture_output=True, text=True, timeout=900)
        self.assertEqual(r.returncode, 0, r.stdout[-6000:] + r.stderr[-2000:])
        self.assertRegex(r.stdout, r"\b\d+ passed\b")
        self.assertNotIn(" failed", r.stdout)


if __name__ == "__main__":
    unittest.main()
