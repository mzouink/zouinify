"""Request-side checks shared by the read-only panel (zouinify.panel.server) and the terminal page: the Host/Origin
allowlist, bearer and cookie parsing, the weighted per-address rate limiter and the authorization decision.

Pure functions over a request's headers and peer address, plus one small in-memory limiter: no file, no network,
no process, nothing of the panel's state. The decisions are those of the panel since T-0165, byte for byte (the
request matrix in tests/test_intranet.py pins them); T-0174 only moved them here so that a second server does not
have to import the gated panel package. The panel's AST gate (scripts/check_panel_ast.py) allowlists exactly the
names in __all__ for `from zouinify import webauth`; tests/test_webauth.py keeps the two lists equal.
"""
from __future__ import annotations

import re
import threading
import time

__all__ = ["LOOPBACK_PEERS", "TOKEN_RE", "PANEL_COOKIE", "RATE_LIMIT", "RATE_WINDOW_S", "NO_TOKEN_WEIGHT", "CSP",
           "unlock_csp", "host_allowed", "bearer_token", "presented_token", "RateLimiter", "authorize"]

LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
PANEL_COOKIE = "zouinify_token"
RATE_LIMIT = 10          # weighted refusals per window per address before 429 for the rest of the window: a wrong
NO_TOKEN_WEIGHT = 0.2    # Bearer counts 1; a missing token or a wrong cookie 0.2 (first visits, a junk cookie set by
                         # another server under this host name, a cookie left from before a rotation: not an attack)
RATE_WINDOW_S = 60.0
# The page policy of a server that serves itself only: no inline script or style, no other origin, never framed.
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'"


def unlock_csp(script_sha256_b64: str) -> str:
    """The policy of an unlock page whose only script is pinned by its hash: nothing else loads or runs, the page
    may only call its own origin (to hand the secret over once) and is never framed."""
    return ("default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'sha256-" + script_sha256_b64
            + "'; form-action 'none'; frame-ancestors 'none'")


def _hosts(port: int, lan_names) -> set:
    names = {"127.0.0.1", "localhost"} | {str(n).strip().lower() for n in (lan_names or ()) if str(n).strip()}
    hosts = {f"{n}:{port}" for n in names}
    if port == 80:
        hosts |= names
    return hosts


def host_allowed(headers, port: int, lan_names) -> bool:
    """Only the server's own names may address it: 127.0.0.1 / localhost with the port, plus the LAN names the process
    side computed when bound to the intranet. A page served under another name (DNS rebinding) and any foreign
    Origin are refused. `headers` is anything with .get() over header names (http.server's message object)."""
    hosts = _hosts(port, lan_names)
    host = (headers.get("Host") or "").strip().lower()
    if host not in hosts:
        return False
    origin = (headers.get("Origin") or "").strip().lower()
    return not origin or origin in {f"http://{h}" for h in hosts}


def bearer_token(headers):
    """The `Authorization: Bearer <token>` value when well-formed (TOKEN_RE), else None."""
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() != "bearer ":
        return None
    tok = auth[7:].strip()
    return tok if TOKEN_RE.match(tok) else None


def presented_token(headers, cookie_name: str = PANEL_COOKIE):
    """(token, source): the well-formed token the request carries and where from ("header" or "cookie"), or
    (None, None). A malformed Bearer never falls back to the cookie."""
    if (headers.get("Authorization") or "")[:7].lower() == "bearer ":
        return bearer_token(headers), "header"
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == cookie_name and TOKEN_RE.match(v.strip()):
            return v.strip(), "cookie"
    return None, None


class RateLimiter:
    """Weighted refusals per address inside a sliding window; at the budget the address waits the window out.
    Weights are keyed by the source of the refused credential: "header" (a wrong Bearer: 1), "cookie" (a wrong
    cookie, which a browser resends on every request) and None (no token at all), both NO_TOKEN_WEIGHT."""

    def __init__(self, budget: float = RATE_LIMIT, window_s: float = RATE_WINDOW_S, weights: dict | None = None):
        self.budget = float(budget)
        self.window_s = float(window_s)
        self.weights = {"header": 1.0, "cookie": NO_TOKEN_WEIGHT, None: NO_TOKEN_WEIGHT}
        self.weights.update(weights or {})
        self._failures: dict = {}
        self._lock = threading.Lock()

    def weight(self, source) -> float:
        return float(self.weights.get(source, 1.0))

    def rate_limited(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [(t, w) for t, w in self._failures.get(ip, []) if now - t < self.window_s]
            self._failures[ip] = recent
            return sum(w for _, w in recent) >= self.budget

    def note_failure(self, ip: str, weight: float = 1.0) -> None:
        with self._lock:
            lst = self._failures.setdefault(ip, [])
            lst.append((time.time(), float(weight)))
            del lst[:-100]                      # per address: never more than a window's worth
            if len(self._failures) > 1000:     # never grow without bound
                now = time.time()
                self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1][0] < self.window_s}


def authorize(peer_ip: str, headers, token_required: bool, matcher, limiter: RateLimiter, cookie_name: str = PANEL_COOKIE,
              loopback_exempt: bool = True, is_loopback: bool | None = None) -> tuple:
    """The one decision, as (status, reason, source):
      200 "loopback"      no token is required, or the peer is loopback and loopback_exempt (the panel's rule: a
                          same-user process can read everything directly; the terminal page passes False)
      429 "rate-limited"  the address spent its budget; nothing else is looked at
      401 "no-token"      no well-formed token (a malformed Bearer counts as none and never falls back to the cookie)
      200 "token"         matcher(token) is true (the caller's constant-time comparison)
      401 "bad-cookie"    a well-formed cookie that does not match (weighed like no token: browsers resend cookies)
      401 "bad-token"     a well-formed Bearer that does not match (weight 1)
    `source` is "header", "cookie" or None, for the caller's own log label. `is_loopback` overrides the peer
    classification (default: peer_ip in LOOPBACK_PEERS)."""
    if is_loopback is None:
        is_loopback = peer_ip in LOOPBACK_PEERS
    if not token_required or (loopback_exempt and is_loopback):
        return 200, "loopback", None
    if limiter.rate_limited(peer_ip):
        return 429, "rate-limited", None
    tok, source = presented_token(headers, cookie_name)
    if not tok:
        limiter.note_failure(peer_ip, limiter.weight(None))
        return 401, "no-token", source
    if matcher(tok):
        return 200, "token", source
    if source == "cookie":
        limiter.note_failure(peer_ip, limiter.weight("cookie"))
        return 401, "bad-cookie", source
    limiter.note_failure(peer_ip, limiter.weight("header"))
    return 401, "bad-token", source
