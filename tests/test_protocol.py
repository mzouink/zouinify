"""`zouinify protocol render|list` (T-0188): the ticket protocol files from templates/protocol/tickets, the deploy plugin's doc fragments
and the project-owned sections; written under --out only. Plus the 12-states fix."""
import os
import re
import sys
import unittest

from test_core import HERE, World

LIB = os.path.join(os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE), "lib")
REPO_UNDER_TEST = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)

README_S1 = ("### How you are asked", "one ask per message", "`tp-desk` tmux window", "expect:", "if you do nothing:", "`RUN`", "`DECIDE`",
             "`CHECK`", "`T-xxxx ok`", "scripts/ticket.sh human", "except a P0", "No other session ever asks you anything in its own window",
             "the moment a session makes it", "ordered by priority, then kind", "then the oldest first", "no fixed time, no freeze and no countdown",
             "one push per new", "Applies stay batched", "scripts/ticket.sh board --json", "`--slow`", "scripts/ticket.sh human --json",
             "stable `id`", "zouinify's `status --line`", "status-interval 30")
README_S3 = ("**One voice.**", "never address the human in your own window", "only your own permission prompts", "`expect:`",
             "`if you do nothing:`", "one ask at a time", "not the human's own say-so", "`CLAUDE.md` or settings edit", "tickets/WORKER_BRIEF.md")
BRIEF = ("export TICKET_AS=", "scripts/ticket.sh board", ".claude/worktrees/", "Never edit the main worktree", "Ticket: T-xxxx", "RED on base",
         "gets your diff, never your summary", "never address the human in your own window", "only your own permission prompts",
         "expect: <last line>", "if you do nothing:", "`backlog -> ready`", "Never claim a `backlog` ticket",
         "Read tickets/WORKER_BRIEF.md, then claim T-xxxx", "6. **", "7. **New bugs or ideas:**", "8. **Finish the loop.**",
         "your ask is on the human's list at once", "and only a `P0` -- a live regression", "so mark one only for that", "`dev`",
         "`main` is production")


def section(text, n):
    m = re.search(r"(?ms)^## %d\. .*?(?=^## \d+\. |\Z)" % n, text)
    return m.group(0) if m else ""


class TestProtocol(World):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "out")

    def read_out(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as fh:
            return fh.read()

    def render(self, *more):
        return self.ok("protocol", "render", "--out", self.out, *more)

    def test_list_and_render_write_three_files_under_out_only(self):
        r = self.ok("protocol", "list")
        for needle in ("README.md", "_TEMPLATE.md", "WORKER_BRIEF.md", "plugins/none/docs", "tickets.deploy_plugin: none", "absent"):
            self.assertIn(needle, r.stdout)
        r = self.z("protocol", "render")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--out", r.stderr)
        before = sorted(os.listdir(self.root)), sorted(os.listdir(os.path.join(self.root, "tickets")))
        r = self.render()
        self.assertEqual(sorted(os.listdir(self.out)), ["README.md", "WORKER_BRIEF.md", "_TEMPLATE.md"])
        self.assertEqual((sorted(os.listdir(self.root)), sorted(os.listdir(os.path.join(self.root, "tickets")))), before, "nothing in the project")
        for name in ("README.md", "WORKER_BRIEF.md", "_TEMPLATE.md"):
            self.assertIn(f"wrote      {os.path.join(self.out, name)}", r.stdout)
            self.assertNotIn("{{", self.read_out(name), name)
        r = self.render()
        self.assertEqual(r.stdout.count("unchanged"), 3)
        with open(os.path.join(self.out, "README.md"), "a") as fh:
            fh.write("edited\n")
        r = self.nz("protocol", "render", "--out", self.out)
        self.assertIn("use --force", r.stderr)
        self.assertIn("edited", self.read_out("README.md"))
        r = self.render("--force")
        self.assertIn("overwrote", r.stdout)
        self.assertNotIn("edited", self.read_out("README.md"))

    def test_readme_structure_and_the_protocol_needles(self):
        self.render()
        readme = self.read_out("README.md")
        heads = re.findall(r"(?m)^## (\d+)\. ", readme)
        self.assertEqual(heads, [str(i) for i in range(9)], "sections 0-8 in order")
        self.assertTrue(readme.startswith("# tickets/ -- the work queue for proj\n"), readme[:80])
        s1 = section(readme, 1)
        for needle in README_S1:
            with self.subTest(needle=needle):
                self.assertIn(needle, s1)
        s3 = section(readme, 3)
        for needle in README_S3:
            with self.subTest(needle=needle):
                self.assertIn(needle, s3)
        self.assertIn("`WORKER_BRIEF.md` -- the two-minute brief", section(readme, 8))
        self.assertIn("Read\ntickets/WORKER_BRIEF.md, then claim T-xxxx", section(readme, 6).replace("`", ""))
        self.assertIn("(`tp-desk`, tmux `tmux-desk`)".replace("tmux-desk", "tp-desk"), section(readme, 6))
        self.assertNotIn("sitting", readme.lower())
        self.assertIn("test it (the `tp-debug` session does this)", s1)
        self.assertIn(f"(`{self.root}`)", section(readme, 4), "rule 1 names the repo's absolute path")
        self.assertIn("2. **Never run anything on the human-only list**", section(readme, 4), "the none plugin's rule 2 (upstream text)")
        self.assertIn("8. **Verify a deploy where its users see it**", section(readme, 4))
        self.assertIn("\n| `deployed` | after the human deploys | names the real output of the deploy", section(readme, 5), "the whole row")
        self.assertIn("\n| `done` | owner or the human | checked live by a non-author", section(readme, 5))
        self.assertIn("The verbs are `APPLY`, `DEPLOY`", s1, "none ships verbs.md, which replaces the generated sentence")
        self.assertIn("**`main`'s HEAD is always a release**", readme)
        self.assertIn("- `releases/R-YYYY-MM-DD-N.md`", section(readme, 8))
        self.assertTrue(readme.endswith("and why.\n"), repr(readme[-60:]))
        brief = self.read_out("WORKER_BRIEF.md")
        for needle in BRIEF:
            with self.subTest(needle=needle):
                self.assertIn(needle, brief)
        self.assertNotIn("\n9. ", brief)
        self.assertIn("working in `proj`\nat the same time. The ticket desk (`tp-desk`, tmux `tp-desk`)", brief)
        self.assertIn("5. **No live mutations.** Nothing on the human-only list runs from a session", brief)
        self.assertIn("6. **Anything that costs money goes through the desk.**", brief)
        tmpl = self.read_out("_TEMPLATE.md")
        self.assertIn("type: bug                      # bug | feature | process | chore", tmpl)
        self.assertIn("status: backlog                # backlog ready in-dev in-review ready-to-merge merged ready-to-deploy deployed done blocked dropped skipped", tmpl)
        self.assertIn("repos: [backend]               # backend\n", tmpl, "one repo: no 'may list several'")
        self.assertIn("deploy: none                   # none\n", tmpl)
        self.assertIn("## Deploy handoff -- YYYY-MM-DD (T-xxxx)\nTrunk:   <trunk> @ <sha> (pushed).", tmpl)

    def test_project_sections_are_stubbed_then_spliced_verbatim(self):
        self.render()
        readme = self.read_out("README.md")
        self.assertIn("## 0. Environments\n\n_Not written yet._", readme)
        self.assertIn("## 7. Deploying (all human-run; sessions prepare the handoff)\n\n_Not written yet._", readme)
        self.write(".ops/README.project.md", "# preamble that must not appear\n\nsome words\n\n## 0. Environments\n\n| Name | Where |\n|---|---|\n"
                                            "| prod | the moon |\n\n---\n\n## 7. Deploying (all human-run)\n\n- **`cargo`** -- ship it with care.\n")
        self.render("--force")
        readme = self.read_out("README.md")
        self.assertNotIn("preamble", readme)
        self.assertNotIn("_Not written yet._", readme)
        self.assertIn("## 0. Environments\n\n| Name | Where |\n|---|---|\n| prod | the moon |\n\n---\n\n## 1. ", readme)
        self.assertIn("## 7. Deploying (all human-run)\n\n- **`cargo`** -- ship it with care.\n\n## 8. Files", readme)
        r = self.nz("protocol", "render", "--out", self.out, "--project-readme", os.path.join(self.tmp, "nope.md"))
        self.assertIn("not a file", r.stderr)

    def test_section_splitter_edge_cases(self):
        self.write(".ops/README.project.md", "## 0. Environments\r\n\r\n```bash\r\n## 1. not a heading\r\n```\r\n\r\nrun x -- then say deployed --\r\n\r\n---\r\n\r\n"
                                            "## 7. Deploying\r\n\r\n~~~\r\n## 8. nor this\r\n~~~\r\n")
        self.render()
        readme = self.read_out("README.md")
        self.assertNotIn("\r", readme, "CRLF input is normalized")
        self.assertIn("## 0. Environments\n\n```bash\n## 1. not a heading\n```\n\nrun x -- then say deployed --\n\n---\n\n## 1. For the human", readme,
                      "a heading inside a fence stays, the trailing `--` of a line stays, only the rule line is dropped")
        self.assertIn("## 7. Deploying\n\n~~~\n## 8. nor this\n~~~\n\n## 8. Files", readme)
        order = [readme.index(h) for h in ("## 0. Environments", "## 1. For the human", "## 6. The ticket desk", "## 7. Deploying", "## 8. Files")]
        self.assertEqual(order, sorted(order), "the real sections stay in order; the fenced look-alikes are content")
        self.write(".ops/README.project.md", "## 0. A\n\nx\n\n## 0. B\n\ny\n")
        r = self.nz("protocol", "render", "--out", self.out, "--force")
        self.assertIn("appears twice", r.stderr)

    def test_configured_plugin_without_docs_warns_and_uses_the_generic_prose(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("  deploy_plugin: none\n", "  deploy_plugin: aws-terraform\n"))
        r = self.render()
        self.assertIn("warning: no docs shipped for deploy plugin aws-terraform", r.stdout)
        self.assertIn("2. **Never run anything on the human-only list**", self.read_out("README.md"))
        self.assertIn("`plugins/aws-terraform/` -- the deploy plugin", self.read_out("README.md"))

    def test_plugin_docs_fill_the_deploy_prose_and_missing_fragments_are_refused(self):
        docs = os.path.join(self.tmp, "docs")
        os.makedirs(docs)
        for name in ("rule-no-live-mutations", "rule-protect-artifacts", "rule-deploy-evidence", "rule-after-deploy", "evidence-deployed",
                     "evidence-done", "brief-live-mutations", "brief-paid-runs"):
            with open(os.path.join(docs, name + ".md"), "w") as fh:
                fh.write(f"FRAGMENT {name}\n")
        r = self.nz("protocol", "render", "--out", self.out, "--plugin-docs", docs)
        self.assertIn("lack handoff.md", r.stderr)
        self.assertFalse(os.path.exists(self.out))
        with open(os.path.join(docs, "handoff.md"), "w") as fh:
            fh.write("## Deploy handoff -- FRAGMENT handoff\nCommand: cargo ship\n")
        with open(os.path.join(docs, "verbs.md"), "w") as fh:
            fh.write("Verbs: `RUN` (cargo), `DECIDE`, `CHECK` (the moon).\n")
        self.render("--plugin-docs", docs)
        readme = self.read_out("README.md")
        self.assertIn("2. FRAGMENT rule-no-live-mutations\n3. FRAGMENT rule-protect-artifacts\n", readme)
        self.assertIn("the failing assertion pasted. FRAGMENT rule-deploy-evidence\n", readme)
        self.assertIn("8. FRAGMENT rule-after-deploy\n", readme)
        self.assertIn("| `merged` | `ticket.sh merge` only", readme)
        self.assertIn("\nFRAGMENT evidence-deployed\nFRAGMENT evidence-done\n| `blocked` |", readme, "the two rows are inserted whole")
        self.assertIn("Verbs: `RUN` (cargo), `DECIDE`, `CHECK` (the moon). A\ndecision names", readme, "verbs.md replaces the generated sentence")
        brief = self.read_out("WORKER_BRIEF.md")
        self.assertIn("5. FRAGMENT brief-live-mutations\n6. FRAGMENT brief-paid-runs\n7. **New bugs", brief)
        self.assertIn("## Deploy handoff -- FRAGMENT handoff\nCommand: cargo ship\n-->", self.read_out("_TEMPLATE.md"))
        r = self.nz("protocol", "render", "--out", self.out, "--plugin-docs", os.path.join(self.tmp, "nowhere"))
        self.assertIn("not a directory", r.stderr)

    def test_without_a_production_branch_and_with_several_repos(self):
        cfg = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", cfg.replace("  - {name: backend, path: ., trunk: dev, prod: main, default: true}",
                                                   "  - {name: backend, path: ., trunk: dev, default: true}\n  - {name: web, path: ../web, trunk: dev}"))
        os.makedirs(os.path.join(self.tmp, "web"))
        self.render()
        readme = self.read_out("README.md")
        self.assertIn("the work queue for proj + web\n", readme)
        self.assertIn("for both repos is one markdown file here, on trunk (`dev` in both repos).\nThere is no separate production branch", readme)
        self.assertNotIn("releases/", readme)
        self.assertTrue(readme.endswith("-- tickets/`.\n"), repr(readme[-40:]))
        brief = self.read_out("WORKER_BRIEF.md")
        self.assertIn("working in `proj` and `web`\n", brief)
        self.assertIn("(`dev`; there is no separate production branch)", brief)
        tmpl = self.read_out("_TEMPLATE.md")
        self.assertIn("# backend | web (one ticket may list several)", tmpl)

    def test_twelve_states_everywhere(self):
        with open(os.path.join(REPO_UNDER_TEST, "templates", "ops", "config.yaml.example"), encoding="utf-8") as fh:
            example = fh.read()
        self.assertIn("the 12 states of the core", example)
        self.assertNotIn("11 states", example)
        self.assertIn("# types: [bug, feature, process, chore]", example)
        self.assertIn("# protected_branches_extra: []", example)
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        from zouinify import initcmd
        text = initcmd.render_config({"name": "d", "short": "d", "trunk": "main", "prod": "", "model": "fable", "port": 4700, "human": "h"})
        self.assertIn("the 12 states of the core", text)
        self.assertNotIn("11 states", text)


if __name__ == "__main__":
    unittest.main()
