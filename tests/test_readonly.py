"""The read-only facade (lib/zouinify/readonly.py): re-exports nothing it delegates to, refuses every
ticket.sh command that is not a read-only output mode, and never raises on a script that prints text
instead of JSON (the project's `board --json` does not exist until T-0158 lands)."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World  # noqa: E402


class TestReadonly(World):
    def setUp(self):
        super().setUp()
        from zouinify import config as cfgmod
        from zouinify import readonly
        self.readonly = readonly
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.cfg = cfgmod.load(self.root)

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()

    def test_no_reexports(self):
        for name in ("_tmux", "_tk", "subprocess", "tmux", "tickets", "launch", "registry"):
            self.assertFalse(hasattr(self.readonly, name), f"readonly must not expose {name}")

    def test_refuses_mutating_ticket_sh(self):
        from zouinify import ZouinifyError
        for args in (["move", "T-0001", "done", "x"], ["claim", "T-0001", "me"], ["log", "T-0001", "x"], ["human", "--clear"],
                     ["board", "--json", "--force"], [], ["apply", "x.tfplan"]):
            with self.assertRaises(ZouinifyError, msg=str(args)):
                self.readonly.read_ticket_sh(self.cfg, args)
        self.assertFalse([c for c in self.ticket_calls() if not c.startswith("human") and not c.startswith("board")])

    def test_human_and_board_parse_defensively(self):
        items = self.readonly.read_human_json(self.cfg)
        self.assertEqual(len(items), 4)
        self.assertIsNone(self.readonly.read_board_json(self.cfg), "the fake board prints its usage: not JSON, not a crash")
        text = os.path.join(self.tmp, "board.txt")
        with open(text, "w") as fh:
            fh.write("== READY\n  T-0001 ...\n")
        with mock.patch.dict(os.environ, {"FAKE_BOARD_JSON": text}):
            self.assertIsNone(self.readonly.read_board_json(self.cfg))
        good = os.path.join(self.tmp, "board.json")
        with open(good, "w") as fh:
            fh.write('{"tickets": [{"id": "T-0001", "status": "ready"}], "counts": {"ready": 1}}')
        with mock.patch.dict(os.environ, {"FAKE_BOARD_JSON": good}):
            self.assertEqual(self.readonly.read_board_json(self.cfg)["tickets"][0]["id"], "T-0001")
        with mock.patch.dict(os.environ, {"FAKE_HUMAN_JSON": text}):
            self.assertIsNone(self.readonly.read_human_json(self.cfg))

    def test_reads_work(self):
        self.ok("up", "desk")
        self.assertTrue(self.readonly.has_session("tp-desk"))
        self.assertIn("[Fable 5.1]", self.readonly.capture_pane("tp-desk"))
        self.assertEqual(self.readonly.model_on_screen(self.readonly.capture_pane("tp-desk"), self.cfg.model_re), "Fable 5.1")
        self.assertEqual([p["session"] for p in self.readonly.list_panes()], ["tp-desk"])
        self.assertEqual(self.readonly.trunk_sha(self.tmp), "")   # not a git repo: "" and no exception


if __name__ == "__main__":
    unittest.main()
