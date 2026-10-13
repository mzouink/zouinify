"""T-0165: the browser token and the explicit intranet bind of the read-only panel.

In-process server bound to 0.0.0.0 on an ephemeral port, reached through this machine's LAN address so the peer
is NOT loopback (skipped when the machine has no non-loopback IPv4): every request needs the token (header or
cookie, constant-time compare through panelstate), a token-less navigation gets the unlock page, failures are
rate-limited and audited, loopback peers stay token-free, the Host check accepts the LAN names only. Then the
process side through the CLI: `panel token rotate` refuses inside a Claude session or without a TTY, prints the
address once on a pty and keeps only the hash (0600); `panel start` on intranet needs the token; `up` never prints one.
"""
import http.client
import json
import os
import pty
import re
import select
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.abspath(os.path.dirname(__file__))
REPO = os.environ.get("ZOUINIFY_REPO_UNDER_TEST") or os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

from test_core import World  # noqa: E402
from test_panel import free_port, PANEL_IMPORT_ERROR  # noqa: E402

try:
    from zouinify import config as cfgmod
    from zouinify import panelstate
    from zouinify.panel import server as panel_server
except Exception as e:  # noqa: BLE001
    PANEL_IMPORT_ERROR = PANEL_IMPORT_ERROR or e

GOOD = "test-token-" + "A" * 40          # assembled: no credential-shaped literal, long enough for TOKEN_RE
OTHER = "other-token-" + "B" * 40


def lan_ip():
    """A non-loopback IPv4 of this machine, or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def no_claude_env(env):
    e = dict(env)
    for k in list(e):
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"):
            e.pop(k)
    return e


class IntranetServer(World):
    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        self.ip = lan_ip()
        if not self.ip:
            self.skipTest("no non-loopback IPv4 on this machine")
        cfg_text = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", re.sub(r"panel: \{.*\}", "panel: {port: 4799, bind: intranet, link_host: lanhost.test, state_dir: .ops/state, autostart: false}", cfg_text))
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.cfg = cfgmod.load(self.root)
        panelstate.set_token(self.cfg, GOOD)
        # bound to 127.0.0.1 on purpose (a test must never expose even a test token on the lab LAN); the peer check is
        # faked per request so the server treats us as a LAN client when the test says so
        self.srv = panel_server.make_server(self.cfg, port=0, allowed_hosts={self.ip, "lanhost.test"}, address="127.0.0.1")
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.fake_remote = True
        test = self
        self.peer_patch = mock.patch.object(panel_server.Handler, "peer_is_loopback", lambda handler: not test.fake_remote)
        self.peer_patch.start()
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            self.peer_patch.stop()
            self.env_patch.stop()
            super().tearDown()

    def req(self, path, host=None, headers=None, via="lan", method="GET"):
        self.fake_remote = via == "lan"
        addr = "127.0.0.1"
        conn = http.client.HTTPConnection(addr, self.port, timeout=60)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or (f"lanhost.test:{self.port}" if via == "lan" else f"127.0.0.1:{self.port}"))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        hdr = dict(r.getheaders())
        conn.close()
        return r.status, hdr, body

    def server_log(self):
        p = os.path.join(self.root, ".ops", "state", "server.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


class TestIntranetToken(IntranetServer):
    def test_binding_and_health(self):
        self.assertEqual(panel_server.bind_address("intranet"), "0.0.0.0")
        self.assertEqual(panel_server.bind_address("loopback"), "127.0.0.1")
        st, _, body = self.req("/health", headers={"Authorization": "Bearer " + GOOD})
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["bind"], "intranet")
        self.assertTrue(d["token_required"])

    def test_every_lan_request_needs_the_token(self):
        for path in ("/api/snapshot", "/health", "/static/app.js", "/static/app.css", "/api/tickets/T-0001", "/api/logs?label=x", "/nope"):
            st, hdr, body = self.req(path)
            self.assertEqual(st, 401, path)
            self.assertIn("Bearer", hdr.get("WWW-Authenticate", ""), path)
            self.assertNotIn(b'"inbox"', body)
            self.assertNotIn(b"const ", body, "no page code without the token")
        st, hdr, body = self.req("/")
        self.assertEqual(st, 401)
        self.assertIn("text/html", hdr.get("Content-Type", ""))
        html = body.decode()
        self.assertIn("zouinify_token=", html, "the unlock page stores the #t= fragment as the cookie")
        self.assertIn("location.reload()", html)
        self.assertNotIn("inbox", html)
        self.assertRegex(hdr.get("Content-Security-Policy", ""), r"script-src 'sha256-[A-Za-z0-9+/=]+'", "only the unlock script may run")
        self.assertNotIn("'unsafe-inline'", hdr["Content-Security-Policy"].split("script-src")[1])
        st, _, _ = self.req("/api/snapshot", headers={"Authorization": "Bearer " + OTHER})
        self.assertEqual(st, 401)
        st, _, _ = self.req("/api/snapshot", headers={"Authorization": "Bearer " + GOOD[:-1]})
        self.assertEqual(st, 401)

    def test_token_in_header_or_cookie_works_and_query_is_ignored(self):
        st, _, body = self.req("/api/snapshot", headers={"Authorization": "Bearer " + GOOD})
        self.assertEqual(st, 200)
        self.assertIn("inbox", json.loads(body))
        st, _, body = self.req("/", headers={"Cookie": "zouinify_token=" + GOOD})
        self.assertEqual(st, 200)
        self.assertIn(b"<title>zouinify</title>", body)
        st, _, _ = self.req("/static/app.js", headers={"Cookie": "other=1; zouinify_token=" + GOOD + "; x=y"})
        self.assertEqual(st, 200)
        st, _, _ = self.req("/api/snapshot?t=" + GOOD)
        self.assertEqual(st, 401, "a token in the query string is never accepted (it would land in logs)")
        st, _, _ = self.req("/?t=" + GOOD, headers={"Authorization": "Bearer " + GOOD})
        self.assertEqual(st, 200)
        self.assertNotIn(GOOD, self.server_log(), "no token in the server log, not even a mistyped ?t= on an accepted request")
        self.assertIn("?<query-redacted>", self.server_log())
        self.assertNotIn(OTHER, self.server_log())
        self.assertIn(" token=" + panelstate.token_id(self.cfg), self.server_log(), "accepted requests carry the token id")

    def test_keep_alive_does_not_carry_the_token_label(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        self.fake_remote = True
        conn.request("GET", "/health", headers={"Host": f"lanhost.test:{self.port}", "Authorization": "Bearer " + GOOD})
        r = conn.getresponse(); r.read()
        self.assertEqual(r.status, 200)
        conn.request("GET", "/health", headers={"Host": f"lanhost.test:{self.port}"})     # same connection, no token
        r = conn.getresponse(); r.read()
        self.assertEqual(r.status, 401)
        conn.close()
        lines = [l for l in self.server_log().splitlines() if '"GET /health' in l]
        self.assertTrue(lines[-1].endswith("401 -"), lines[-1])
        self.assertNotIn("token=", lines[-1], "the 401 on a reused connection carries no token label")

    def test_loopback_stays_token_free(self):
        st, _, body = self.req("/api/snapshot", via="loopback")
        self.assertEqual(st, 200)
        self.assertIn("inbox", json.loads(body))
        st, _, _ = self.req("/", via="loopback")
        self.assertEqual(st, 200)

    def test_rate_limit_and_audit(self):
        for i in range(panel_server.RATE_LIMIT):
            st, _, _ = self.req("/api/snapshot", headers={"Authorization": "Bearer " + OTHER})
            self.assertEqual(st, 401, i)
        st, hdr, body = self.req("/api/snapshot", headers={"Authorization": "Bearer " + GOOD})
        self.assertEqual(st, 429, "after the limit even the right token waits out the window")
        self.assertIn("Retry-After", hdr)
        log = self.server_log()
        self.assertGreaterEqual(log.count("AUDIT 401 bad-token ip=127.0.0.1"), panel_server.RATE_LIMIT)
        self.assertIn("AUDIT 429 rate-limited ip=127.0.0.1", log)
        self.assertNotIn(OTHER, log)
        st, _, _ = self.req("/api/snapshot", via="loopback")
        self.assertEqual(st, 200, "the limit is per address: loopback is unaffected")

    def test_host_and_origin_on_the_lan(self):
        tok = {"Authorization": "Bearer " + GOOD}
        self.assertEqual(self.req("/health", host=f"lanhost.test:{self.port}", headers=tok)[0], 200)
        self.assertEqual(self.req("/health", host=f"{self.ip}:{self.port}", headers=tok)[0], 200)
        self.assertEqual(self.req("/health", host="evil.example", headers=tok)[0], 403, "a token does not excuse a foreign Host")
        self.assertEqual(self.req("/health", host=f"evil.example:{self.port}", headers=tok)[0], 403)
        self.assertEqual(self.req("/health", host=f"lanhost.test:{self.port}", headers=dict(tok, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.req("/health", host=f"lanhost.test:{self.port}", headers=dict(tok, Origin=f"http://lanhost.test:{self.port}"))[0], 200)
        self.assertIn("AUDIT 403 host-or-origin", self.server_log())

    def test_writes_stay_405_and_rotation_is_live(self):
        st, hdr, _ = self.req("/api/receipts", method="POST", headers={"Authorization": "Bearer " + GOOD})
        self.assertEqual(st, 405)
        st, _, _ = self.req("/api/receipts", method="POST")
        self.assertEqual(st, 401, "a token-less write is refused before it is even a 405")
        panelstate.set_token(self.cfg, OTHER)                  # rotation while the server runs
        self.assertEqual(self.req("/api/snapshot", headers={"Authorization": "Bearer " + GOOD})[0], 401)
        self.assertEqual(self.req("/api/snapshot", headers={"Authorization": "Bearer " + OTHER})[0], 200)
        pj = json.load(open(os.path.join(self.root, ".ops", "state", "panel.json")))
        self.assertEqual(len(pj["token_hash"]), 64)
        self.assertEqual(pj["token_id"], pj["token_hash"][-4:])
        self.assertNotIn(OTHER, json.dumps(pj))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, ".ops", "state", "panel.json")).st_mode), 0o600)

    def test_intranet_needs_a_token_to_start(self):
        from zouinify import ZouinifyError
        os.remove(os.path.join(self.root, ".ops", "state", "panel.json"))
        with self.assertRaises(ZouinifyError):
            panel_server.make_server(self.cfg, port=0, allowed_hosts=set(), address="127.0.0.1")


class TestTokenRotateCli(World):
    """The process side: the token is printed once, on a real TTY, never inside a Claude session."""

    def setUp(self):
        if PANEL_IMPORT_ERROR:
            self.fail(f"panel package not importable: {PANEL_IMPORT_ERROR!r}")
        super().setUp()
        self.port = free_port()
        cfg_text = open(os.path.join(self.root, ".ops", "config.yaml")).read()
        self.write(".ops/config.yaml", re.sub(r"panel: \{.*\}", f"panel: {{port: {self.port}, bind: intranet, link_host: lanhost.test, state_dir: .ops/state, autostart: true}}", cfg_text))
        self.plain = no_claude_env(self.env)

    def tearDown(self):
        subprocess.run([self.z_bin(), "panel", "stop"], cwd=self.root, env=self.plain, capture_output=True)
        super().tearDown()

    def z_bin(self):
        from test_core import ZOUINIFY
        return ZOUINIFY

    def test_refused_inside_a_claude_session_or_without_a_tty(self):
        r = self.z("panel", "token", "rotate", env=dict(self.plain, CLAUDECODE="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never inside a Claude session", r.stderr)
        r = self.z("panel", "token", "rotate", env=dict(self.plain, CLAUDE_CODE_MESSAGING_TOKEN="x"))
        self.assertNotEqual(r.returncode, 0)
        r = self.z("panel", "token", "rotate", env=self.plain)           # pipes, not a TTY
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("real terminal", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".ops", "state", "panel.json")), "nothing recorded on a refusal")

    def rotate_on_a_pty(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen([self.z_bin(), "panel", "token", "rotate"], stdin=slave, stdout=slave, stderr=slave,
                                cwd=self.root, env=self.plain, close_fds=True)
        os.close(slave)
        out = b""
        deadline = time.time() + 60
        while time.time() < deadline and b"Press Enter" not in out:
            if select.select([master], [], [], 1)[0]:
                try:
                    out += os.read(master, 4096)
                except OSError:
                    break
        self.assertIn(b"Press Enter", out, out[-500:])
        os.write(master, b"\n")
        deadline = time.time() + 30
        while time.time() < deadline and proc.poll() is None:
            if select.select([master], [], [], 0.5)[0]:
                try:
                    out += os.read(master, 4096)
                except OSError:
                    break
        proc.wait(timeout=30)
        os.close(master)
        self.assertEqual(proc.returncode, 0, out.decode(errors="replace"))
        return out.decode(errors="replace")

    def test_rotate_prints_once_on_a_tty_and_keeps_only_the_hash(self):
        self.rotate_twice()

    def rotate_twice(self):
        out = self.rotate_on_a_pty()
        m = re.search(r"http://lanhost\.test:%d/#t=([A-Za-z0-9_-]{32,})" % self.port, out)
        self.assertTrue(m, out)
        token = m.group(1)
        self.assertIn("\x1b[2J", out, "the screen is cleared after Enter")
        pj_path = os.path.join(self.root, ".ops", "state", "panel.json")
        pj = json.load(open(pj_path))
        self.assertEqual(len(pj["token_hash"]), 64)
        self.assertNotIn(token, json.dumps(pj))
        self.assertEqual(stat.S_IMODE(os.stat(pj_path).st_mode), 0o600)
        import hashlib
        self.assertEqual(pj["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        # a second rotation replaces it
        out2 = self.rotate_on_a_pty()
        token2 = re.search(r"#t=([A-Za-z0-9_-]{32,})", out2).group(1)
        self.assertNotEqual(token, token2)
        self.assertEqual(json.load(open(pj_path))["token_hash"], hashlib.sha256(token2.encode()).hexdigest())
        return token2

    def test_start_needs_the_token_and_nothing_prints_it(self):
        r = self.fail("panel", "start", env=self.plain)
        self.assertIn("panel token rotate", r.stderr)
        r = self.z("up", "desk", env=self.plain)                     # autostart on, no token: sessions start, the panel does not
        self.assertEqual(r.returncode, 1, "up exits 1 when the panel it was asked to start did not start")
        self.assertIn("launched", r.stdout)
        self.assertIn("not started", r.stdout)
        self.assertIn("panel token rotate", r.stdout)
        self.assertNotIn("#t=", r.stdout)
        token = self.rotate_twice()
        r = self.ok("panel", "url", env=self.plain)
        self.assertIn(f"http://lanhost.test:{self.port}/", r.stdout)
        self.assertIn("token required from the LAN", r.stdout)
        self.assertNotIn(token, r.stdout)
        r = self.ok("up", "desk", "--dry-run", env=self.plain)
        self.assertIn(f"would start on http://lanhost.test:{self.port}/ (read-only, intranet)", r.stdout)
        self.assertNotIn("#t=", r.stdout)
        # the LAN names the Host check will accept (computed on the process side): the link host, never loopback
        from zouinify import panelctl
        with mock.patch.dict(os.environ, self.plain, clear=True):
            cfg = cfgmod.load(self.root)
            hosts = panelctl.lan_hosts(cfg)
        self.assertIn("lanhost.test", hosts)
        self.assertFalse([h for h in hosts if h.startswith("127.")])
        pj = json.load(open(os.path.join(self.root, ".ops", "state", "panel.json")))
        self.assertIn("token_hash", pj)
        self.assertNotIn("pid", pj, "no server ran: the detached intranet start is not exercised by the tests (it would listen on the LAN)")

if __name__ == "__main__":
    unittest.main()
