"""T-0186: scripts/ticket.sh is a project-free core; the project's values are PARSED from tickets/ticket.env (never
sourced), the deploy code is tickets/plugins/<DEPLOY_PLUGIN>/plugin.sh (aws-terraform here; none = the no-op set).

RED on the base script (TICKET_SH_UNDER_TEST=<07277e0 copy>): `version` is an unknown command, board --json has no
version key, ticket.env is never read (a `touch` line in it is simply not executed, but no warning is printed and
TRUNK stays the built-in dev), a bad plugin name is not refused.

    python3 -m pytest -q scripts/tests/test_ticket_env.py
"""

import json
import os
import re
import shutil
import stat
import unittest

from ticket_world import TicketShWorld, _REPO, SCRIPT, TICKET_ENV

DOCS_REPO = os.environ.get("TICKET_DOCS_REPO") or _REPO


def core_version():
    m = re.search(r'^TICKET_SH_VERSION="([^"]+)"', open(SCRIPT, encoding="utf-8").read(), re.M)
    return m.group(1) if m else None


class EnvWorld(TicketShWorld):
    def env_lines(self, drop=(), extra=""):
        lines = [l for l in TICKET_ENV.format(plugin=self.plugin).splitlines() if not any(l.startswith(d + "=") for d in drop)]
        return "\n".join(lines) + "\n" + extra

    def set_env(self, text):
        self.write_ticket_env(text)
        self.commit_main("tickets: ticket.env")

    def plugin_file(self, name, body):
        d = os.path.join(self.main, "tickets", "plugins", name)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "plugin.sh")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p


class TestTicketEnv(EnvWorld):
    def test_version_command_and_board_json_version_key_first(self):
        v = core_version()
        self.assertTrue(v, "the core names TICKET_SH_VERSION")
        p = self.ts("version", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("ticket.sh %s  plugin %s  (" % (v, self.plugin), p.stdout)
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        obj = json.loads(p.stdout)
        self.assertEqual(list(obj)[0], "version", "the version is the FIRST key: a reader checks it before anything else")
        self.assertEqual(obj["version"], v)
        self.assertEqual(obj["trunk"], "dev")
        p = self.ts("human", "--json", env=self.env_without_who())
        self.assertIsInstance(json.loads(p.stdout), list, "human --json stays an array (schema frozen; zouinify reads a list)")
        p = self.ts("help", env=self.env_without_who())
        self.assertEqual(p.returncode, 1)
        self.assertIn("# Ticket desk for qa-world -- scripts/ticket.sh core %s, plugin %s." % (v, self.plugin), p.stdout)
        self.assertIn("scripts/ticket.sh deploy-check <plan>", p.stdout); self.assertIn("scripts/ticket.sh version", p.stdout)

    def test_the_file_is_parsed_never_sourced(self):
        marker = os.path.join(self.tmp, "executed")
        self.set_env(self.env_lines(extra=(
            "touch %s\n" % marker +                      # a command line: not KEY=value
            "export PROJECT=exported\n" +                # 'export' is not a key
            ": ${TRUNK:=main}\n" +                       # a sourced-file idiom
            "  TRUNK=main\n" +                           # indentation
            "project=lower\n" +                          # lower-case key
            'EVIL="$(touch %s)"\n' % marker +            # an unknown key whose value would run if sourced
            'TOUCHY=`touch %s`\n' % marker)))
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertFalse(os.path.exists(marker), "a line of ticket.env was executed")
        self.assertEqual(json.loads(p.stdout)["trunk"], "dev", "TRUNK was not overridden by the ignored lines")
        for n, why in ((18, "not KEY=value"), (19, "not KEY=value"), (20, "not KEY=value"), (21, "not KEY=value"), (22, "not KEY=value"),
                       (23, "EVIL has a character outside"), (24, "TOUCHY has a character outside")):
            with self.subTest(line=n):
                self.assertIn("ticket.sh: ticket.env:%d: ignored: %s" % (n, why), p.stderr)
        self.assertEqual(len([l for l in p.stderr.splitlines() if "ignored:" in l]), 7, p.stderr)

    def test_values_have_a_fixed_character_set_duplicates_and_stray_repo_keys_are_reported(self):
        """the desk's grammar: bare KEY=value, value = [A-Za-z0-9._/@:+ -]*; quotes, $, backticks, backslashes, tabs are not
        values -- the line is reported and ignored, nothing is ever evaluated"""
        self.set_env(self.env_lines(drop=("PROJECT",), extra=(
            'PROJECT=plain name.v2+x/y:z@h\n' +          # every allowed character class
            'PROJECT=second\n' +                          # a duplicate: the last one wins, reported
            'PROJECT="quoted"\n' +                        # quotes are not part of the grammar
            'DEPLOY_KINDS=$HOME\n' +                      # nothing is expanded; the earlier value stays
            'TICKET_TYPES=`x`\n' +
            'WORKTREES=a\\b\n' +
            'ASK_VERBS=a\tb\n' +
            'REPO_OTHER_PATH=../x\n')))                    # a repo REPOS does not list
        p = self.ts("help", env=self.env_without_who())
        self.assertIn("# Ticket desk for second --", p.stdout)
        self.assertIn("ticket.env:18: ignored: PROJECT repeats line 17 (the last one wins)", p.stderr)
        for n, key in ((19, "PROJECT"), (20, "DEPLOY_KINDS"), (21, "TICKET_TYPES"), (22, "WORKTREES"), (23, "ASK_VERBS")):
            with self.subTest(line=n):
                self.assertIn("ticket.env:%d: ignored: %s has a character outside [A-Za-z0-9._/@:+ -]" % (n, key), p.stderr)
        self.assertIn("ticket.env:24: ignored: REPO_OTHER_PATH names a repo that REPOS does not list (backend frontend)", p.stderr)
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, "the ignored lines left the world's values in place: " + p.out)
        self.set_env(self.env_lines(drop=("PROJECT",), extra='PROJECT=plain name.v2+x/y:z@h\n'))
        p = self.ts("help", env=self.env_without_who())
        self.assertIn("# Ticket desk for plain name.v2+x/y:z@h --", p.stdout)
        self.assertNotIn("ignored:", p.stderr)

    def test_missing_file_dies_except_version(self):
        os.remove(os.path.join(self.main, "tickets", "ticket.env"))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("no %s/tickets/ticket.env" % self.main, p.out); self.assertIn("zouinify config --env --write", p.out)
        p = self.ts("version", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("ticket.sh %s  (no tickets/ticket.env in %s)" % (core_version(), self.main), p.stdout)
        self.set_env(self.env_lines(drop=("TRUNK",)))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("ticket.env: TRUNK is empty", p.out)
        self.set_env(self.env_lines(drop=("REPO_FRONTEND_TRUNK",)))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPO_FRONTEND_TRUNK is missing", p.out)

    def test_exported_overrides_win_after_the_parse_and_fe_aliases_hold(self):
        env = self.env_without_who(TICKET_PROJECT="from-the-environment")
        p = self.ts("help", env=env)
        self.assertIn("# Ticket desk for from-the-environment --", p.stdout)
        self.write_ticket("T-0009", "fe-done", "done", "fe done", fields={"repos": "[frontend]", "branch": "t0009-fe"})
        self.commit_main("tickets: fe done")
        for var in ("TICKET_REPO_FRONTEND_PATH", "TICKET_FE_REPO"):
            with self.subTest(var=var):
                p = self.ts("prune", "T-0009", "--dry-run", env=self.env_without_who(**{var: os.path.join(self.tmp, "elsewhere")}))
                self.assertEqual(p.returncode, 0, p.out)
                self.assertIn("T-0009 (frontend): repo %s not found -- left alone" % os.path.join(self.tmp, "elsewhere"), p.out)
        p = self.ts("prune", "T-0009", "--dry-run", env=self.env_without_who())
        self.assertIn("repo %s not found" % os.path.join(self.tmp, "no-frontend"), p.out, "the file's ../no-frontend, joined onto main's parent by text")

    def test_repo_names_hyphens_limits_and_the_default_repo(self):
        txt = self.env_lines(drop=("REPOS", "REPO_FRONTEND_PATH", "REPO_FRONTEND_TRUNK", "REPO_FRONTEND_PROD"),
                             extra='REPOS=backend my-fe\nREPO_MY_FE_PATH=../no-frontend\nREPO_MY_FE_TRUNK=dev\nREPO_MY_FE_PROD=\n')
        self.set_env(txt)
        self.write_ticket("T-0009", "fe-done", "done", "fe done", fields={"repos": "[my-fe]", "branch": "t0009-fe"})
        self.commit_main("tickets: my-fe ticket")
        p = self.ts("prune", "T-0009", "--dry-run", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("T-0009 (my-fe): repo %s not found -- left alone" % os.path.join(self.tmp, "no-frontend"), p.out, "REPO_MY_FE_* is the key of repo my-fe")
        self.set_env(self.env_lines(drop=("REPOS",), extra='REPOS=backend frontend third\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPOS lists 3 repos (backend frontend third); until T-0187", p.out)
        self.set_env(self.env_lines(drop=("REPO_BACKEND_PATH",), extra='REPO_BACKEND_PATH=../other\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("the default repo backend resolves to %s/other, but this script's main checkout is %s" % (self.tmp, self.main), p.out)
        self.set_env(self.env_lines(drop=("REPOS",), extra='REPOS=Backend\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("repo name 'Backend' in REPOS is not [a-z][a-z0-9_-]*", p.out)

    def test_generated_from_header_warns_on_a_stale_config_never_dies(self):
        import hashlib
        cfg = os.path.join(self.main, ".ops", "config.yaml"); os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w") as fh:
            fh.write("version: 1\n")
        sha = hashlib.sha256(open(cfg, "rb").read()).hexdigest()
        self.set_env("# generated-from: .ops/config.yaml sha256 %s\n" % ("0" * 64) + self.env_lines())
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertIn("generated from another .ops/config.yaml (its sha256 differs)", p.stderr)
        self.set_env("# generated-from: .ops/config.yaml sha256 %s\n" % sha + self.env_lines())
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertNotIn("sha256", p.stderr)
        os.remove(cfg); os.rmdir(os.path.dirname(cfg))
        self.set_env("# generated-from: .ops/config.yaml sha256 %s\n" % ("0" * 64) + self.env_lines())
        p = self.ts("board", "--json", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out); self.assertNotIn("sha256", p.stderr, "no config.yaml: nothing to compare, silent")

    def test_ticket_types_and_protected_branches_come_from_the_file(self):
        self.set_env(self.env_lines(drop=("TICKET_TYPES", "PROTECTED_BRANCHES_EXTRA"), extra='TICKET_TYPES=bug idea\nPROTECTED_BRANCHES_EXTRA=keep/me\n'))
        p = self.ts("new", "chore", "P3", "x")
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("type must be bug|idea", p.out)
        self.write_ticket("T-0009", "kept", "done", "kept", fields={"branch": "keep/me"})
        self.write_ticket("T-0010", "old", "done", "old", fields={"branch": "feat/specialist-bridge-wiring"})
        self.commit_main("tickets: protected")
        p = self.ts("prune", "T-0009", "T-0010", "--dry-run", env=self.env_without_who())
        self.assertIn("branch 'keep/me' -- refused: protected name", p.out)
        self.assertNotIn("feat/specialist-bridge-wiring' -- refused: protected name", p.out, "no longer listed: an ordinary (missing) branch")


class TestEnvHardening(EnvWorld):
    """review of 56b53fd: locale-independent character classes, overrides validated before anything is derived, no leading '-' in a ref"""
    UTF8 = {"LC_ALL": "en_US.utf8", "LANG": "en_US.utf8", "LANGUAGE": ""}

    def test_character_classes_are_ascii_under_a_utf8_locale(self):
        """finding 2: under en_US.UTF-8 the [a-z] range matched é, ß, fullwidth s and the Arabic digit ١, so DEPLOY_PLUGIN=é passed.
        The classes are explicit ASCII lists now: such a value is refused as an override, and in the file an unusable line for a
        required key (TRUNK, REPOS, DEPLOY_PLUGIN) is a death, never a silent fall-back to the none plugin."""
        env = self.env_without_who(**self.UTF8)
        for name in ("\u00e9", "\u00df", "\uff53", "x\u0661"):
            with self.subTest(override=name):
                p = self.ts("board", env=dict(env, TICKET_DEPLOY_PLUGIN=name))
                self.assertEqual(p.returncode, 1, p.out); self.assertIn("TICKET_DEPLOY_PLUGIN (an exported override) has a character outside", p.out)
        for name in ("\u00e9", "\u00df", "\uff53", "x\u0661"):
            with self.subTest(file=name):
                self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra="DEPLOY_PLUGIN=%s\n" % name))
                p = self.ts("board", env=env)
                self.assertEqual(p.returncode, 1, p.out)
                self.assertIn("ticket.env:17: ignored: DEPLOY_PLUGIN has a character outside", p.out)
                self.assertIn("ticket.env:17: DEPLOY_PLUGIN has an unusable value (reported above) -- a required key is never skipped or defaulted", p.out)
                self.assertNotIn("plugin none", p.out)
        self.set_env(self.env_lines())   # a sound file again: the override is what is under test
        p = self.ts("board", env=dict(env, TICKET_REPOS="back\u00e9nd frontend"))
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("TICKET_REPOS (an exported override) has a character outside", p.out)
        self.set_env(self.env_lines(drop=("REPOS",), extra="REPOS=back\u00e9nd frontend\n"))
        p = self.ts("board", env=env)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("REPOS has an unusable value", p.out)
        self.set_env(self.env_lines(drop=("PROJECT",), extra="PROJECT=stra\u00dfe\nPROJ\u00c9CT=x\nPROJECT=ascii\n"))
        p = self.ts("help", env=env)
        self.assertIn("ticket.env:17: ignored: PROJECT has a character outside", p.stderr)
        self.assertIn("ticket.env:18: ignored: not KEY=value", p.stderr)
        self.assertIn("# Ticket desk for ascii --", p.stdout, "an optional key's bad line is skipped; the next good one counts")
        self.set_env(self.env_lines(drop=("TRUNK",), extra="TRUNK=d\u00e9v\n"))
        p = self.ts("board", env=env)
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("TRUNK has an unusable value", p.out)

    def test_overrides_are_validated_before_anything_is_derived(self):
        """finding 3: TICKET_TRUNK='dev$(...)' was accepted; TICKET_REPOS skipped the name check and landed after REPO0/REPO1"""
        marker = os.path.join(self.tmp, "ran")
        p = self.ts("board", env=self.env_without_who(TICKET_TRUNK="dev$(touch %s)" % marker))
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("TICKET_TRUNK (an exported override) has a character outside", p.out); self.assertFalse(os.path.exists(marker))
        for var, val, why in (("TICKET_REPOS", "backend frontend third", "REPOS lists 3 repos (backend frontend third); until T-0187"),
                              ("TICKET_REPOS", "Backend", "repo name 'Backend' in REPOS is not"),
                              ("TICKET_REPOS", "", "REPOS=backend frontend"),   # an empty override is no override: the file's value stands
                              ("TICKET_FE_REPO", "/x;rm -rf /", "TICKET_FE_REPO (an exported override) has a character outside"),
                              ("TICKET_REPO_FRONTEND_PATH", "../no-frontend$(id)", "TICKET_REPO_FRONTEND_PATH (an exported override) has a character outside"),
                              ("TICKET_DEPLOY_PLUGIN", "../none", "is not a plugin name")):
            with self.subTest(var=var, val=val):
                p = self.ts("board", "--json", env=self.env_without_who(**{var: val}))
                if why.startswith("REPOS="):
                    self.assertEqual(p.returncode, 0, p.out)
                else:
                    self.assertEqual(p.returncode, 1, p.out); self.assertIn(why, p.out)
                for bash_error in ("unbound variable", "syntax error", "command not found"):
                    self.assertNotIn(bash_error, p.out, "a raw bash error instead of the core's own refusal")
        p = self.ts("board", "--json", env=self.env_without_who(TICKET_REPOS="backend"))
        self.assertEqual(p.returncode, 0, "one repo is a valid override, no second repo: " + p.out)
        self.write_ticket("T-0009", "fe-done", "done", "fe done", fields={"repos": "[frontend]", "branch": "t0009-fe"})
        self.commit_main("tickets: fe")
        p = self.ts("prune", "T-0009", "--dry-run", env=self.env_without_who(TICKET_REPOS="backend"))
        self.assertIn("repos: names 'frontend', which ticket.sh cannot merge or prune (only: backend)", p.out)

    def test_a_ref_never_starts_with_a_dash(self):
        """finding 4: TRUNK, a repo's trunk or prod, a protected name -- '-x' would reach git as an option"""
        for text, bad in ((self.env_lines(drop=("TRUNK",), extra="TRUNK=-x\n"), "-x"),
                          (self.env_lines(drop=("REPO_FRONTEND_PROD",), extra="REPO_FRONTEND_PROD=-p\n"), "-p"),
                          (self.env_lines(drop=("PROTECTED_BRANCHES_EXTRA",), extra="PROTECTED_BRANCHES_EXTRA=keep/me -rf\n"), "-rf")):
            with self.subTest(bad=bad):
                self.set_env(text)
                p = self.ts("board", env=self.env_without_who())
                self.assertEqual(p.returncode, 1, p.out); self.assertIn("'%s' is not a branch name (a leading '-'" % bad, p.out)
        self.set_env(self.env_lines())
        p = self.ts("board", env=self.env_without_who(TICKET_TRUNK="--force"))
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("'--force' is not a branch name", p.out)


class TestPluginLoading(EnvWorld):
    def test_plugin_name_is_a_slug_under_tickets_plugins(self):
        for name, why in (("../none", "is not a plugin name"), ("None", "is not a plugin name"), ("missing", "no plugin 'missing'"), ("", "is not a plugin name")):
            with self.subTest(name=name):
                self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra='DEPLOY_PLUGIN=%s\n' % name))
                p = self.ts("board", env=self.env_without_who())
                self.assertEqual(p.returncode, 1, p.out); self.assertIn(why, p.out)

    def test_a_symlink_out_of_the_plugins_dir_is_refused(self):
        outside = os.path.join(self.tmp, "outside"); os.makedirs(outside)
        shutil.copy(os.path.join(self.main, "tickets", "plugins", "none", "plugin.sh"), os.path.join(outside, "plugin.sh"))
        os.symlink(outside, os.path.join(self.main, "tickets", "plugins", "evil"))
        self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra='DEPLOY_PLUGIN=evil\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("no plugin 'evil'", p.out); self.assertIn("is a symlink", p.out)
        os.remove(os.path.join(self.main, "tickets", "plugins", "evil"))
        d = os.path.join(self.main, "tickets", "plugins", "evil2"); os.makedirs(d)
        os.symlink(os.path.join(outside, "plugin.sh"), os.path.join(d, "plugin.sh"))
        self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra='DEPLOY_PLUGIN=evil2\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out); self.assertIn("no plugin 'evil2'", p.out)

    def test_a_symlinked_plugins_dir_is_refused_and_nothing_runs(self):
        """review finding 1 (56b53fd): readlink -f followed a symlinked tickets/plugins, so an outside plugin.sh was sourced
        and ran; the literal path is checked now and a symlink at any of the three levels is refused"""
        marker = os.path.join(self.tmp, "ran")
        outside = os.path.join(self.tmp, "outside", "evil"); os.makedirs(outside)
        with open(os.path.join(outside, "plugin.sh"), "w") as fh:
            fh.write("touch %s\n" % marker + open(os.path.join(self.main, "tickets", "plugins", "none", "plugin.sh")).read())
        real = os.path.join(self.main, "tickets", "plugins")
        shutil.move(real, real + ".real"); os.symlink(os.path.join(self.tmp, "outside"), real)
        self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra="DEPLOY_PLUGIN=evil\n"))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("%s is a symlink" % real, p.out); self.assertFalse(os.path.exists(marker), "the outside plugin ran")
        os.remove(real); shutil.move(real + ".real", real)
        os.symlink(os.path.join(self.tmp, "outside", "evil", "plugin.sh"), os.path.join(real, "none", "plugin.sh.lnk"))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, "evil is gone with the real dir back, so the file is missing: " + p.out)
        self.assertIn("no plugin 'evil'", p.out); self.assertIn("is not a regular file", p.out)
        self.assertFalse(os.path.exists(marker))

    def test_a_plugin_missing_a_hook_is_refused_with_the_list(self):
        self.plugin_file("half", "plugin_evidence_check() { :; }\nplugin_plan_key() { :; }\n")
        self.set_env(self.env_lines(drop=("DEPLOY_PLUGIN",), extra='DEPLOY_PLUGIN=half\n'))
        p = self.ts("board", env=self.env_without_who())
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("plugin half (", p.out); self.assertIn("does not define: plugin_plan_void plugin_command_void_reason plugin_ask_check", p.out)
        self.assertIn("plugin_apply", p.out); self.assertNotIn("plugin_evidence_check", p.out.split("does not define:")[1])

    def test_the_hook_list_in_the_core_matches_both_shipped_plugins(self):
        core = open(SCRIPT, encoding="utf-8").read()
        hooks = re.search(r'^PLUGIN_HOOKS="([^"]+)"', core, re.M).group(1).split()
        self.assertEqual(len(hooks), 13, hooks)
        for name in ("aws-terraform", "none"):
            body = open(os.path.join(_REPO, "tickets", "plugins", name, "plugin.sh"), encoding="utf-8").read()
            defined = set(re.findall(r"(?m)^(plugin_[a-z_]+)\(\)", body))
            with self.subTest(plugin=name):
                self.assertEqual(defined, set(hooks), "every hook, nothing else named plugin_*")
        for h in hooks:
            with self.subTest(hook=h):
                self.assertIn(h + " ", core.replace(h + '="', "x") if False else core, "the core calls it")


class TestNonePlugin(EnvWorld):
    plugin = "none"

    def test_the_generic_core_runs_without_deploy_rules(self):
        p = self.ts("version", env=self.env_without_who()); self.assertIn("plugin none", p.stdout)
        p = self.ts("move", "T-0001", "in-dev", "qa: generic move"); self.assertEqual(p.returncode, 0, p.out)
        self.write_ticket("T-0003", "shipped", "ready-to-deploy", "shipped", fields={"deploy": "manual", "plan": "terraform/x.tfplan"})
        self.write_ticket("T-0004", "shipped-too", "ready-to-deploy", "shipped too", fields={"deploy": "manual", "plan": "terraform/x.tfplan"})
        self.commit_main("tickets: ready-to-deploy")
        p = self.ts("human", env=self.env_without_who())
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("RUN     T-0003", p.out); self.assertIn("RUN     T-0004", p.out, "no shared plan key: one item per ticket, nothing VOID")
        self.assertNotIn("do NOT run", p.out)
        p = self.ts("move", "T-0003", "deployed", "qa: shipped by hand, nothing an AWS lint would recognise")
        self.assertEqual(p.returncode, 0, "no evidence rule beyond the core's gates: " + p.out)
        p = self.ts("ask", "T-0004", "APPLY the thing | cd /x && make ship | ok | waits")
        self.assertEqual(p.returncode, 0, "no APPLY-names-a-plan rule: " + p.out)
        human = self.env_without_who(TICKET_APPLY_NO_TTY_OK="1")   # the human's shell as the apply gate sees it (no Claude Code markers)
        for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT"):
            human.pop(k, None)
        for args in (("apply", "x.tfplan"), ("deploy-check", "x.tfplan")):
            with self.subTest(args=args):
                p = self.ts(*args, env=human)
                self.assertEqual(p.returncode, 1, p.out); self.assertIn("no deploy plugin", p.out)


class TestDocsFragments(unittest.TestCase):
    """The deploy-specific prose travels with the plugin (zouinify renders README/WORKER_BRIEF/_TEMPLATE from generic
    templates plus these fragments): every fragment is verbatim in this project's rendered file."""
    WHERE = {"rule-": "tickets/README.md", "evidence-": "tickets/README.md", "verbs": "tickets/README.md",
             "brief-": "tickets/WORKER_BRIEF.md", "handoff": "tickets/_TEMPLATE.md"}

    def test_every_aws_terraform_fragment_is_verbatim_in_its_document(self):
        d = os.path.join(DOCS_REPO, "tickets", "plugins", "aws-terraform", "docs")
        names = sorted(f for f in os.listdir(d) if f.endswith(".md"))
        self.assertEqual(names, ["brief-live-mutations.md", "brief-paid-runs.md", "evidence-deployed.md", "evidence-done.md", "handoff.md",
                                 "rule-after-deploy.md", "rule-deploy-evidence.md", "rule-no-live-mutations.md", "rule-protect-artifacts.md", "verbs.md"])
        for f in names:
            doc = next(v for k, v in self.WHERE.items() if f.startswith(k))
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                frag = fh.read()
            with open(os.path.join(DOCS_REPO, doc), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(fragment=f):
                self.assertTrue(frag.strip(), "empty fragment")
                self.assertIn(frag.rstrip("\n"), text, "%s is not verbatim in %s" % (f, doc))
        none = os.path.join(DOCS_REPO, "tickets", "plugins", "none", "docs")
        self.assertEqual(sorted(os.listdir(none)), names, "the none plugin ships the same fragment names")


if __name__ == "__main__":
    unittest.main()
