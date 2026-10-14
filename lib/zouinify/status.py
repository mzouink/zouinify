"""`status [--line|--json]`: inbox count and inbox P0s (from `ticket.sh human --json`: what the human acts
on, so a deployed P0 that asks nothing is not counted), credential time left and the board counts (front
matter). Cached 30 s in <state_dir>/status.json (the tmux status bar re-runs it on every redraw)."""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
import tempfile
import time

from zouinify import tickets as tk

CACHE_TTL_S = 30


def now_local(cfg) -> dt.datetime:  # noqa: ARG001 - the config no longer carries a time zone
    """Now, time-zone aware, in local time; $ZOUINIFY_NOW (an ISO time, with or without an offset) pins it for tests."""
    pinned = os.environ.get("ZOUINIFY_NOW")
    if pinned:
        t = dt.datetime.fromisoformat(pinned)
        return t if t.tzinfo else t.astimezone()
    return dt.datetime.now().astimezone()


def fmt_dur(mins: int) -> str:
    if mins <= 0:
        return "now"
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}" if h else f"{m}m"




def _parse_iso(s: str) -> dt.datetime | None:
    s = s.strip()
    if s.endswith("UTC"):
        s = s[:-3].strip() + "+00:00"
    elif s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


CREDS_TTL_S = 60
STS_TIMEOUT_S = 2.0
STS_LOGIN_RE = re.compile(r"(?i)token has expired|sso session|sso token|error loading sso|unauthorizedssotoken|refresh failed|"
                          r"expiredtoken|invalidclienttokenid|the security token included in the request is (?:invalid|expired)|"
                          r"unable to locate credentials|run: aws sso login|aws sso login")


def sso_token_left(aws: dict, now_utc: dt.datetime) -> str:
    """Minutes left on the SSO ACCESS token (info only): the CLI and the SDK renew it silently with the refresh
    token, so it says nothing about when `aws sso login` is due. Reads only the cache files that hold an
    accessToken key (presence, never the value); the client registration (`botocore-client-id-*.json`, about
    90 days) is not a token."""
    exp = None
    for f in sorted(glob.glob(os.path.join(os.path.expanduser(aws["sso_cache"]), "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or "accessToken" not in d:
            continue
        t = _parse_iso(d["expiresAt"]) if isinstance(d.get("expiresAt"), str) else None
        if t and (exp is None or t > exp):
            exp = t
    if exp is None:
        return "none"
    mins = int((exp - now_utc).total_seconds() // 60)
    return "expired" if mins <= 0 else fmt_dur(mins)


def sts_probe(profile: str) -> dict:
    """One read-only `aws sts get-caller-identity` with a short timeout: {"state": "ok" | "LOGIN" | "ERR" | "?",
    "detail": <why, in words>}. The identity it returns (account, ARN) is discarded, never stored or shown. Like
    any CLI call it refreshes the cached access token as a side effect, which is the point."""
    cmd = ["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json", "--no-cli-pager"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=STS_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"state": "?", "detail": "aws CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"state": "?", "detail": f"aws sts get-caller-identity timed out after {STS_TIMEOUT_S:g} s"}
    except OSError as e:
        return {"state": "?", "detail": f"aws could not run: {type(e).__name__}"}
    if r.returncode == 0:
        return {"state": "ok", "detail": "aws sts get-caller-identity succeeded"}
    if STS_LOGIN_RE.search(r.stderr or ""):
        return {"state": "LOGIN", "detail": "aws sts get-caller-identity: the SSO token is expired or missing; run `aws sso login`"}
    return {"state": "ERR", "detail": f"aws sts get-caller-identity failed (exit {r.returncode}); not an SSO token error"}


def creds(cfg, now: dt.datetime | None = None, use_cache: bool = True) -> dict:
    """Whether the cloud credentials work, from a cached read-only `aws sts get-caller-identity --profile
    <aws.profile>` (T-0178): `ok`, `LOGIN` (an SSO token error: the human must run `aws sso login`), `ERR` (another
    failure) or `?` (timeout or no aws CLI). Cached CREDS_TTL_S seconds in <state_dir>/creds.json. The SSO access
    token's minutes left are information for the long form only: the CLI renews that token silently."""
    aws = cfg.aws
    if not aws:
        return {"left": "n/a", "source": "no aws block in .ops/config.yaml"}
    now = now or now_local(cfg)
    now_utc = now.astimezone(dt.timezone.utc) if now.tzinfo else dt.datetime.now(dt.timezone.utc)
    profile = str(aws.get("profile") or "default")
    cache = os.path.join(cfg.state_dir, "creds.json")
    probe = None
    if use_cache:
        try:
            with open(cache, encoding="utf-8") as fh:
                c = json.load(fh)
            if time.time() - float(c.get("at", 0)) < CREDS_TTL_S and c.get("profile") == profile and isinstance(c.get("probe"), dict):
                probe = c["probe"]
        except (OSError, ValueError):
            pass
    if probe is None:
        probe = sts_probe(profile)
        try:
            os.makedirs(cfg.state_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cfg.state_dir, prefix=".creds.", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"at": time.time(), "profile": profile, "probe": probe}, fh)
            os.replace(tmp, cache)
        except OSError:
            pass
    token = sso_token_left(aws, now_utc)
    return {"left": probe["state"], "source": f"{probe['detail']} (profile {profile}, checked at most every {CREDS_TTL_S} s); "
                                              f"sso access token {token} left (rolling: the CLI renews it, info only)",
            "token_left": token}


def collect(cfg, use_cache: bool = True) -> dict:
    cache = os.path.join(cfg.state_dir, "status.json")
    if use_cache:
        try:
            with open(cache, encoding="utf-8") as fh:
                c = json.load(fh)
            if time.time() - float(c.get("at", 0)) < CACHE_TTL_S and isinstance(c.get("data"), dict):
                return c["data"]
        except (OSError, ValueError):
            pass
    now = now_local(cfg)
    by, p0_open, p0_verifying = tk.counts(cfg)
    items = tk.human_items(cfg)
    inbox = len(items) if items is not None else None
    p0 = sum(1 for it in items if str(it.get("priority", "")).upper() == "P0") if items is not None else 0
    data = {"inbox": inbox, "p0": p0, "p0_open_tickets": p0_open, "p0_verifying": p0_verifying, "creds": creds(cfg, now, use_cache=use_cache),
            "counts": by, "generated": now.replace(microsecond=0).isoformat(), "trunk": cfg.trunk, "project": cfg.name}
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cfg.state_dir, prefix=".status.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "data": data}, fh)
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def line(data: dict) -> str:
    c = data.get("counts") or {}
    inbox = data.get("inbox")
    return (f"inbox {inbox if inbox is not None else '?'} | P0 {data.get('p0', 0)} | creds {data['creds']['left']} | "
            f"{c.get('in-dev', 0)} dev {c.get('in-review', 0) + c.get('ready-to-merge', 0)} rev "
            f"{c.get('ready-to-deploy', 0)} rtd {c.get('deployed', 0)} dep")    # rtd = the human's apply queue (T-0178)


def long(data: dict) -> str:
    c = data.get("counts") or {}
    rows = [f"{data.get('project')} @ {data.get('generated')}  (trunk {data.get('trunk')})",
            f"inbox   {data.get('inbox') if data.get('inbox') is not None else '? (ticket.sh human --json failed)'}",
            f"P0      {data.get('p0', 0)} in the inbox ({data.get('p0_open_tickets', 0)} open P0 ticket(s) on the board"
            + (f", {data['p0_verifying']} deployed, verification pending" if data.get("p0_verifying") else "") + ")",
            f"creds   {data['creds']['left']}  ({data['creds']['source']})",
            "board   " + ("  ".join(f"{k} {v}" for k, v in sorted(c.items())) or "no tickets")]
    return "\n".join(rows)
