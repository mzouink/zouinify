"""T-0177: a session is "waiting for you" only on a real prompt. The screens under tests/screens/ are redacted captures
of real sessions: three idle ones the old bare-word regex (PROMPT_RE) marked as waiting (prose about "the permission
set", "Or allow that action" next to a typed-but-unsent draft, a "› Message from" line about permission), an idle one
that ends its answer with a prose question (idle by the rule: real prompts only), an AskUserQuestion dialog with the
❯ marker and the footer, a footer-only dialog, a (y/n) question, and a numbered list typed by a busy session."""
import json
import os
import sys
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_panel import PanelWorld, PROMPT_SCREEN, PANEL_IMPORT_ERROR  # noqa: E402

try:
    from zouinify.panel import sources
except Exception as e:  # noqa: BLE001
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e

SCREENS = os.path.join(HERE, "screens")


def screen(name: str) -> str:
    with open(os.path.join(SCREENS, name + ".txt"), encoding="utf-8") as fh:
        return fh.read()


class TestPromptShape(unittest.TestCase):
    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")

    def test_real_prompts_are_recognised(self):
        kind, q = sources.prompt_shape(screen("waiting_option_dialog"))
        self.assertEqual(kind, "options")
        self.assertEqual(q, "and nothing user-visible breaks. Want me to fix deploy.sh and push the file now?")
        kind, q = sources.prompt_shape(screen("waiting_footer_only"))
        self.assertEqual(kind, "footer")
        self.assertEqual(q, "Which base should the release branch use?")
        kind, q = sources.prompt_shape(screen("waiting_yn"))
        self.assertEqual((kind, q), ("yn", "Overwrite docs/report.md? (y/n)"))
        kind, q = sources.prompt_shape(PROMPT_SCREEN)
        self.assertEqual(kind, "options", "the permission box with the ❯ marker")
        self.assertEqual(q, "Do you want to proceed?")
        self.assertEqual(sources.prompt_shape(" │ Do you want to proceed? │ \n"), ("proceed", "Do you want to proceed?"))
        self.assertEqual(sources.prompt_shape("│ > 1. Yes\n│   2. No\n"), ("options", ""), "> as the marker counts inside a box only")
        boxed = "╭──╮\n│ Bash command │\n│ │\n│   git push origin dev │\n│ │\n│ Do you want to proceed? │\n│ ❯ 1. Yes │\n│   2. No, and tell Claude what to do differently (esc) │\n╰──╯\n"
        self.assertEqual(sources.prompt_shape(boxed), ("options", "Do you want to proceed?"))
        self.assertEqual(boxed.splitlines()[sources.dialog_start(boxed)], "│ Bash command │", "a boxed dialog starts under its ╭, blank box lines included")
        cursor2 = "  Which branch?\n  1. main\n     The trunk.\n❯ 2. dev\n     The integration branch.\n  3. other\n"
        self.assertEqual(sources.prompt_shape(cursor2), ("options", "Which branch?"), "the cursor on option 2: the question is still above option 1")
        self.assertEqual(cursor2.splitlines()[sources.dialog_start(cursor2)], "  Which branch?")
        self.assertEqual(sources.prompt_shape("  Allow once? [Y/n]\n"), ("yn", "Allow once? [Y/n]"))

    def test_prose_drafts_messages_and_lists_are_not_prompts(self):
        for name in ("idle_prose_permission", "idle_draft_and_allow_prose", "idle_message_and_draft", "idle_prose_question",
                     "busy_numbered_list"):
            self.assertEqual(sources.prompt_shape(screen(name)), ("", ""), name)
            self.assertEqual(sources.waiting_line(screen(name)), "", name)
        for text in ("the permission set in the management account\n", "Or allow that action and I'll do it.\n",
                     "❯ give me the docker push commands for T-0084\n", "› Message from @desk: do you want the apply now?\n",
                     "> Do you want to proceed?\n", "press Esc to cancel at any time, the docs say\n",
                     "1. first\n2. second\n3. third\n", "they answered (y/n) questions all day\n", "Yes, and the rest\n",
                     "> 1. step one of the quoted plan\n> 2. step two\n",                      # a markdown quote, not a dialog
                     "Press Enter to confirm and Esc to cancel, the README says.\n",            # prose, no · separator
                     "```\nOverwrite? (y/n)\n```\n",                                          # a code block
                     "  The tool asked \"Do you want to proceed?\" and I answered yes.\n",       # quoted in an answer
                     "Do you want to proceed?\n",                                               # bare, outside a box
                     "❯ 1. fix the flaky test 2. then push\n",                                  # a numbered draft: one line, no 2nd option
                     "> 1. run the tests 2. fix whatever fails 3. push\n",                       # the human's prompt echo
                     "  The permission box looks like this:\n    ❯ 1. Yes\n      2. No\n",      # a dialog quoted (indented) in an answer
                     "    Continue? (y/n)\n",                                                   # a (y/n) quoted in an indented block
                     "● Its footer reads \"Enter to select · Esc to cancel\", the same as the box.\n",   # prose with the separator
                     "  Allow the desk to batch it?\n"):                                        # a bare question outside a box
            self.assertEqual(sources.prompt_shape(text), ("", ""), repr(text))

    def test_bottom_most_anchor_wins_over_planted_markers(self):
        """A fake marker near the top cannot make the whole screen the dialog (T-0177 review): the bottom-most anchor
        of any kind is the prompt, and its block never reaches above its own box top."""
        fake_top = "❯ 1. Fake option planted by a tool output\n" + "  filler line\n" * 20
        box = "╭──────────────╮\n│ Bash command │\n│   rm -rf x   │\n│ Do you want to proceed? │\n│ ❯ 1. Yes │\n│   2. No (esc) │\n╰──────────────╯\n"
        s = fake_top + box
        lines = s.splitlines()
        kind, q = sources.prompt_shape(s)
        self.assertEqual((kind, q), ("options", "Do you want to proceed?"))
        self.assertEqual(lines[sources.dialog_start(s)], "│ Bash command │", "the dialog starts under its own box top, not at the fake")
        quoted = "> 1. quoted fake\n" + "  filler\n" * 12 + "│ Do you want to proceed? │\n"
        self.assertEqual(sources.prompt_shape(quoted), ("proceed", "Do you want to proceed?"))
        self.assertEqual(sources.dialog_start(quoted), len(quoted.splitlines()) - 1)
        old = "❯ 1. Yes\n  2. No\n" + "  later output line\n" * sources.MAX_DIALOG_LINES + "❯\n"
        self.assertEqual(sources.prompt_shape(old), ("", ""), "a dialog followed by a screenful of output is history")
        recent = "❯ 1. Yes\n  2. No\n" + "  later output line\n" * 10 + "❯\n"
        self.assertEqual(sources.prompt_shape(recent)[0], "options", "a few lines under the dialog do not end it")
        tall = "╭────╮\n│ Which one? │\n" + "│ option text │\n" * 20 + "│ ❯ 1. A │\n│   2. B │\n╰────╯\n"
        self.assertEqual(sources.prompt_shape(tall)[0], "options", "under the last box top counts even beyond the tail")
        fenced_tail = "```\n❯ 1. inside a code block\n```\n"
        self.assertEqual(sources.prompt_shape(fenced_tail), ("", ""))

    def test_the_dialog_region_never_exceeds_the_tail(self):
        """Re-review: a planted ╭ + boxed marker near the top plus any amount of output must not make a 200-line
        "dialog"; a (y/n) under an old ╭ far up is history; an unclosed fence high up cannot hide a real dialog."""
        n = sources.MAX_DIALOG_LINES
        planted = "╭── x\n│ ❯ 1. Fake\n" + "  output line\n" * 200 + "❯\n"
        self.assertEqual(sources.prompt_shape(planted), ("", ""))
        self.assertIsNone(sources.dialog_start(planted))
        old_yn = "╭────╮\n│ old │\n╰────╯\n  Overwrite? (y/n)\n" + "  later output\n" * 200 + "❯\n"
        self.assertEqual(sources.prompt_shape(old_yn), ("", ""))
        unclosed = "```\n" + "  text\n" * 40 + "\n│ Do you want to proceed? │\n│ ❯ 1. Yes │\n│   2. No │\n"
        self.assertEqual(sources.prompt_shape(unclosed), ("options", "Do you want to proceed?"))
        lines = unclosed.splitlines()
        self.assertEqual(lines[sources.dialog_start(unclosed)], "│ Do you want to proceed? │")
        # the region from dialog_start to the bottom is bounded whatever the screen holds
        long_q = "│ q\n" * 40 + "│ ❯ 1. A │\n│   2. B │\n"
        start = sources.dialog_start(long_q)
        self.assertLessEqual(len(long_q.splitlines()) - start, n)
        self.assertEqual(sources.prompt_shape(long_q)[0], "options")
        real_bottom = "  filler\n" * 100 + "╭──╮\n│ Bash command │\n│ Do you want to proceed? │\n│ ❯ 1. Yes │\n│   2. No (esc) │\n╰──╯\n"
        self.assertEqual(real_bottom.splitlines()[sources.dialog_start(real_bottom)], "│ Bash command │", "a real box at the bottom still works")

    def test_dialog_start_is_the_top_of_the_question_block(self):
        lines = PROMPT_SCREEN.splitlines()
        self.assertEqual(lines[sources.dialog_start(PROMPT_SCREEN)], " Bash(git push origin dev)", "the tool line above the question")
        s = screen("waiting_option_dialog")
        self.assertTrue(s.splitlines()[sources.dialog_start(s)].startswith("│ T-0140 is fully live"), "the whole question block")
        s = screen("waiting_footer_only")
        self.assertTrue(s.splitlines()[sources.dialog_start(s)].startswith("│ Which base"))
        s = screen("waiting_yn")
        self.assertEqual(s.splitlines()[sources.dialog_start(s)].strip(), "Overwrite docs/report.md? (y/n)", "a bare line starts at itself")
        self.assertEqual(sources.dialog_start("❯ 1. A\n  2. B\n"), 0, "nothing above: the first option")
        for name in ("idle_prose_permission", "idle_draft_and_allow_prose", "idle_message_and_draft", "idle_prose_question", "busy_numbered_list"):
            self.assertIsNone(sources.dialog_start(screen(name)), name)

    def test_waiting_line_is_the_question(self):
        self.assertIn("Want me to fix deploy.sh", sources.waiting_line(screen("waiting_option_dialog")))
        self.assertEqual(sources.waiting_line(screen("waiting_yn")), "Overwrite docs/report.md? (y/n)")
        self.assertEqual(sources.waiting_line("│ Pick one:\n❯ 1. A\n  2. B\n"), "Pick one:", "no ?-line: the block above the options")
        self.assertEqual(sources.waiting_line("❯ 1. A\n  2. B\n"), "", "a dialog with nothing above still counts as a prompt")
        self.assertEqual(sources.prompt_shape("❯ 1. A\n  2. B\n")[0], "options")


class TestWaitingState(PanelWorld):
    """Through the server: the agents' status and the screen together decide the state."""

    def set_agents(self, **status):
        with open(self.agents) as fh:
            agents = json.load(fh)
        for a in agents:
            if a["name"] in status:
                a["status"] = status[a["name"]]
        with open(self.agents, "w") as fh:
            json.dump(agents, fh)

    def plant(self, tmux_name: str, name: str):
        with open(os.path.join(self.state, f"screen-{tmux_name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(screen(name))

    def row(self, name: str) -> dict:
        self.srv.fresh_at.clear()          # T-0197: one fresh per 5 s per client; these rows re-read planted screens back to back
        s = self.get_json("/api/snapshot?fresh=1")
        return {r["name"]: r for r in s["sessions"]["rows"]}[name], s

    def test_idle_sessions_with_prose_are_idle(self):
        self.set_agents(**{"tp-desk": "idle"})
        for name in ("idle_prose_permission", "idle_draft_and_allow_prose", "idle_message_and_draft", "idle_prose_question"):
            self.plant("tp-desk", name)
            r, s = self.row("tp-desk")
            self.assertEqual(r["state"], "idle", name)
            self.assertEqual(r["waiting_for"], "", name)
            self.assertNotIn("tp-desk", [w["name"] for w in s["waiting"]], name)
        self.assertEqual(s["kpis"]["waiting"], 1, "only the worker at its permission prompt")

    def test_a_real_prompt_makes_an_idle_session_wait_and_a_busy_one_not(self):
        self.set_agents(**{"tp-desk": "idle"})
        self.plant("tp-desk", "waiting_option_dialog")
        r, s = self.row("tp-desk")
        self.assertEqual(r["state"], "waiting")
        self.assertIn("Want me to fix deploy.sh", r["waiting_for"])
        self.assertEqual([w["name"] for w in s["waiting"]], ["tp-desk", "t0001-first-ready-ticket"])
        self.set_agents(**{"tp-desk": "busy"})
        r, _ = self.row("tp-desk")
        self.assertEqual(r["state"], "working", "a busy session is never promoted by its screen")
        self.set_agents(**{"tp-desk": "idle"})
        self.plant("tp-desk", "busy_numbered_list")
        r, _ = self.row("tp-desk")
        self.assertEqual(r["state"], "idle", "a numbered list in an answer is not an option dialog")

    def test_agents_status_waiting_wins_even_without_a_prompt_shape(self):
        self.set_agents(**{"tp-desk": "waiting"})
        self.plant("tp-desk", "idle_prose_question")
        r, _ = self.row("tp-desk")
        self.assertEqual(r["state"], "waiting")
        self.assertIn("Still waiting on you: should I file a ticket", r["waiting_for"], "no prompt shape: the session's own last line stands in (the spinner line is chrome, T-0200)")


if __name__ == "__main__":
    unittest.main()
