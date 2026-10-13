"""T-0174: lib/zouinify/webauth.py, the request-side checks shared by the panel and the terminal page. Pure functions
over headers and a peer address, so they are tested without a server; the panel's own behaviour through them is
pinned by the request matrix in tests/test_intranet.py. The gate's allowlist for `from zouinify import webauth`
must equal webauth.__all__ (a name added to one side without the other fails here)."""
import importlib.util
import os
import sys
import unittest

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))

from zouinify import webauth  # noqa: E402

GOOD = "sample-" + "g" * 40
OTHER = "sample-" + "o" * 40


class H(dict):
    """A header bag with the .get() of http.server's message object (names case-insensitive)."""
    def __init__(self, **kw):
        super().__init__({k.lower(): v for k, v in kw.items()})

    def get(self, name, default=None):  # noqa: D102
        return super().get(name.lower(), default)


class TestHostAllowed(unittest.TestCase):
    def test_own_names_with_port_only(self):
        lan = {"lanhost.test", "10.0.0.5"}
        for host in ("127.0.0.1:4790", "localhost:4790", "lanhost.test:4790", "LANHOST.TEST:4790", "10.0.0.5:4790", " lanhost.test:4790 "):
            self.assertTrue(webauth.host_allowed(H(Host=host), 4790, lan), host)
        for host in ("", "lanhost.test", "lanhost.test:4791", "evil.example:4790", "0.0.0.0:4790", "[::1]:4790", "lanhost.test.:4790",
                     "lanhost.test:4790@evil", "evil#lanhost.test:4790"):
            self.assertFalse(webauth.host_allowed(H(Host=host), 4790, lan), host)

    def test_port_80_may_omit_the_port(self):
        self.assertTrue(webauth.host_allowed(H(Host="lanhost.test"), 80, {"lanhost.test"}))
        self.assertTrue(webauth.host_allowed(H(Host="lanhost.test:80"), 80, {"lanhost.test"}))
        self.assertFalse(webauth.host_allowed(H(Host="lanhost.test"), 8080, {"lanhost.test"}))

    def test_origin_must_be_one_of_the_same_names(self):
        good = H(Host="lanhost.test:4790", Origin="http://lanhost.test:4790")
        self.assertTrue(webauth.host_allowed(good, 4790, {"lanhost.test"}))
        self.assertTrue(webauth.host_allowed(H(Host="lanhost.test:4790", Origin="HTTP://127.0.0.1:4790"), 4790, {"lanhost.test"}))
        for origin in ("null", "http://evil.example", "https://lanhost.test:4790", "http://lanhost.test:4790/", "http://lanhost.test:4791"):
            self.assertFalse(webauth.host_allowed(H(Host="lanhost.test:4790", Origin=origin), 4790, {"lanhost.test"}), origin)
        self.assertFalse(webauth.host_allowed(H(Host="evil.example:4790", Origin="http://lanhost.test:4790"), 4790, {"lanhost.test"}),
                         "a good Origin does not excuse a foreign Host")

    def test_lan_names_are_normalised_and_empty_ones_ignored(self):
        self.assertTrue(webauth.host_allowed(H(Host="lanhost.test:1"), 1, [" LanHost.Test ", "", None]))


class TestTokens(unittest.TestCase):
    def test_bearer_token(self):
        self.assertEqual(webauth.bearer_token(H(Authorization="Bearer " + GOOD)), GOOD)
        self.assertEqual(webauth.bearer_token(H(Authorization="bearer   " + GOOD + " ")), GOOD)
        for auth in ("", "Bearer", "Bearer ", "Bearer short", "Bearer\t" + GOOD, "Bearer " + GOOD + " extra", "Basic " + GOOD,
                     "Bearer " + "A" * 257, "Bearer " + GOOD + "!"):
            self.assertIsNone(webauth.bearer_token(H(Authorization=auth)), repr(auth))

    def test_presented_token_prefers_the_header_and_never_falls_back_from_a_bad_bearer(self):
        self.assertEqual(webauth.presented_token(H(Authorization="Bearer " + GOOD)), (GOOD, "header"))
        self.assertEqual(webauth.presented_token(H(Cookie="zouinify_token=" + GOOD)), (GOOD, "cookie"))
        self.assertEqual(webauth.presented_token(H(Cookie="a=1; zouinify_token=" + GOOD + " ; b=2")), (GOOD, "cookie"))
        self.assertEqual(webauth.presented_token(H(Authorization="Bearer " + OTHER, Cookie="zouinify_token=" + GOOD)), (OTHER, "header"))
        self.assertEqual(webauth.presented_token(H(Authorization="Bearer junk", Cookie="zouinify_token=" + GOOD)), (None, "header"),
                         "a malformed Bearer suppresses the cookie")
        self.assertEqual(webauth.presented_token(H(Authorization="Basic xyz", Cookie="zouinify_token=" + GOOD)), (GOOD, "cookie"),
                         "a non-Bearer Authorization does not")
        self.assertEqual(webauth.presented_token(H()), (None, None))
        self.assertEqual(webauth.presented_token(H(Cookie="ZOUINIFY_TOKEN=" + GOOD)), (None, None), "cookie names are exact")
        self.assertEqual(webauth.presented_token(H(Cookie="zouinify_token=short")), (None, None))
        self.assertEqual(webauth.presented_token(H(Cookie="zouinify_term=" + GOOD), "zouinify_term"), (GOOD, "cookie"),
                         "the terminal page has its own cookie name")
        self.assertEqual(webauth.presented_token(H(Cookie="zouinify_token=" + GOOD), "zouinify_term"), (None, None))


class TestRateLimiter(unittest.TestCase):
    def test_budget_weights_and_window(self):
        lim = webauth.RateLimiter(budget=2, window_s=60)
        self.assertEqual((lim.weight("header"), lim.weight("cookie"), lim.weight(None), lim.weight("other")), (1.0, 0.2, 0.2, 1.0))
        for _ in range(9):
            lim.note_failure("10.0.0.1", lim.weight(None))
        self.assertFalse(lim.rate_limited("10.0.0.1"), "9 x 0.2 < 2")
        lim.note_failure("10.0.0.1", 1.0)
        self.assertTrue(lim.rate_limited("10.0.0.1"))
        self.assertFalse(lim.rate_limited("10.0.0.2"), "per address")
        short = webauth.RateLimiter(budget=1, window_s=0.0)
        short.note_failure("x", 1.0)
        self.assertFalse(short.rate_limited("x"), "an expired window counts nothing")

    def test_custom_weights_and_bounded_memory(self):
        lim = webauth.RateLimiter(budget=10, window_s=60, weights={"cookie": 1.0})
        self.assertEqual(lim.weight("cookie"), 1.0)
        for i in range(1200):
            lim.note_failure(f"10.1.{i // 250}.{i % 250}", 1.0)
        self.assertLessEqual(len(lim._failures), 1200)
        for _ in range(500):
            lim.note_failure("10.9.9.9", 1.0)
        self.assertLessEqual(len(lim._failures["10.9.9.9"]), 100, "per address the list is capped")
        self.assertTrue(lim.rate_limited("10.9.9.9"))


class TestAuthorize(unittest.TestCase):
    def setUp(self):
        self.lim = webauth.RateLimiter()
        self.matcher = lambda tok: tok == GOOD

    def auth(self, peer, headers, token_required=True, **kw):
        kw.setdefault("loopback_exempt", True)            # the panel's rule; the default is tested on its own below
        return webauth.authorize(peer, headers, token_required, self.matcher, self.lim, **kw)

    def test_default_is_closed_on_loopback(self):
        """Nobody gets the loopback exemption without asking for it: a server that forgets the argument fails closed."""
        lim = webauth.RateLimiter()
        self.assertEqual(webauth.authorize("127.0.0.1", H(), True, self.matcher, lim), (401, "no-token", None))
        self.assertEqual(webauth.authorize("::1", H(Authorization="Bearer " + OTHER), True, self.matcher, lim), (401, "bad-token", "header"))
        self.assertEqual(webauth.authorize("127.0.0.1", H(Authorization="Bearer " + GOOD), True, self.matcher, lim), (200, "token", "header"))
        self.assertEqual(webauth.authorize("127.0.0.1", H(), True, self.matcher, lim, is_loopback=True), (401, "no-token", None),
                         "is_loopback alone does not exempt")
        self.assertEqual(webauth.authorize("127.0.0.1", H(), True, self.matcher, lim, loopback_exempt=True), (200, "loopback", None))
        self.assertEqual(webauth.authorize("127.0.0.1", H(), False, self.matcher, lim), (200, "loopback", None), "no token required at all")

    def test_the_panel_rules(self):
        lan, loop = "10.0.0.7", "127.0.0.1"
        self.assertEqual(self.auth(loop, H(Authorization="Bearer " + OTHER)), (200, "loopback", None), "loopback is exempt whatever it carries")
        self.assertEqual(self.auth(lan, H(), token_required=False), (200, "loopback", None), "no token required: nothing is checked")
        self.assertEqual(self.auth(lan, H()), (401, "no-token", None))
        self.assertEqual(self.auth(lan, H(Authorization="Bearer junk", Cookie="zouinify_token=" + GOOD)), (401, "no-token", "header"))
        self.assertEqual(self.auth(lan, H(Authorization="Bearer " + GOOD)), (200, "token", "header"))
        self.assertEqual(self.auth(lan, H(Cookie="zouinify_token=" + GOOD)), (200, "token", "cookie"))
        self.assertEqual(self.auth(lan, H(Authorization="Bearer " + OTHER)), (401, "bad-token", "header"))
        self.assertEqual(self.auth(lan, H(Cookie="zouinify_token=" + OTHER)), (401, "bad-cookie", "cookie"))
        self.assertEqual(self.auth(lan, H(Authorization="Bearer " + OTHER, Cookie="zouinify_token=" + GOOD)), (401, "bad-token", "header"))
        for peer in webauth.LOOPBACK_PEERS:
            self.assertEqual(self.auth(peer, H())[1], "loopback", peer)

    def test_weights_reach_the_limit_as_the_panel_does(self):
        lan = "10.0.0.8"
        for _ in range(12):                                  # 2.4 of the budget: first visits and junk cookies are cheap
            self.assertEqual(self.auth(lan, H())[0], 401)
        for _ in range(12):
            self.assertEqual(self.auth(lan, H(Cookie="zouinify_token=" + OTHER))[0], 401)
        self.assertEqual(self.auth(lan, H(Authorization="Bearer " + GOOD))[0], 200, "4.8 < 10: still open")
        wrong = 0
        while self.auth(lan, H(Authorization="Bearer " + OTHER))[1] == "bad-token":
            wrong += 1
            self.assertLessEqual(wrong, webauth.RATE_LIMIT)
        self.assertEqual(self.auth(lan, H(Authorization="Bearer " + GOOD)), (429, "rate-limited", None))
        self.assertEqual(self.auth("10.0.0.9", H(Authorization="Bearer " + GOOD))[0], 200, "another address is unaffected")

    def test_terminal_rules_loopback_not_exempt_and_own_cookie(self):
        loop = "127.0.0.1"
        kw = {"cookie_name": "zouinify_term", "loopback_exempt": False}
        self.assertEqual(self.auth(loop, H(), **kw), (401, "no-token", None), "the terminal page unlocks on loopback too")
        self.assertEqual(webauth.authorize(loop, H(), True, self.matcher, self.lim, "zouinify_term"), (401, "no-token", None),
                         "and gets the same without saying so: the default is closed")
        self.assertEqual(self.auth(loop, H(Cookie="zouinify_token=" + GOOD), **kw), (401, "no-token", None), "the panel cookie is not its secret")
        self.assertEqual(self.auth(loop, H(Cookie="zouinify_term=" + GOOD), **kw), (200, "token", "cookie"))
        self.assertEqual(self.auth(loop, H(Authorization="Bearer " + GOOD), **kw), (200, "token", "header"))
        self.assertEqual(self.auth(loop, H(Cookie="zouinify_term=" + OTHER), **kw), (401, "bad-cookie", "cookie"))

    def test_is_loopback_override(self):
        self.assertEqual(self.auth("127.0.0.1", H(), is_loopback=False), (401, "no-token", None), "a handler may classify the peer itself")
        self.assertEqual(self.auth("10.0.0.1", H(), is_loopback=True), (200, "loopback", None))

    def test_matcher_is_only_called_with_a_well_formed_token(self):
        seen = []
        lim = webauth.RateLimiter()
        webauth.authorize("10.0.0.1", H(Authorization="Bearer junk"), True, seen.append, lim)
        webauth.authorize("10.0.0.1", H(Cookie="zouinify_token=short"), True, seen.append, lim)
        webauth.authorize("10.0.0.1", H(Authorization="Bearer " + GOOD), True, seen.append, lim)
        self.assertEqual(seen, [GOOD])


class TestPolicyStringsAndGate(unittest.TestCase):
    def test_csp_strings(self):
        self.assertIn("frame-ancestors 'none'", webauth.CSP)
        self.assertNotIn("unsafe", webauth.CSP)
        u = webauth.unlock_csp("abc=")
        self.assertIn("script-src 'sha256-abc='", u)
        self.assertIn("connect-src 'self'", u)
        self.assertTrue(u.startswith("default-src 'none'"))
        self.assertIn("frame-ancestors 'none'", u)

    def test_gate_allowlist_equals_the_public_api(self):
        spec = importlib.util.spec_from_file_location("check_panel_ast", os.path.join(REPO, "scripts", "check_panel_ast.py"))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        self.assertIn("webauth", gate.ZOUINIFY_NAMES)
        self.assertEqual(set(gate.MODULES["zouinify.webauth"]), set(webauth.__all__))
        for name in webauth.__all__:
            self.assertTrue(hasattr(webauth, name), name)
        self.assertFalse([n for n in webauth.__all__ if n.startswith("_")])


if __name__ == "__main__":
    unittest.main()
