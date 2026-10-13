"""`status [--line|--json]`: inbox count and inbox P0s (from `ticket.sh human --json`: what the human acts
on, so a deployed P0 that asks nothing is not counted), credential time left and the board counts (front
matter). Cached 30 s in <state_dir>/status.json (the tmux status bar re-runs it on every redraw)."""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
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


def creds(cfg, now: dt.datetime | None = None) -> dict:
    """Time left on the cloud credentials, without reading any key material: the SSO cache's expiresAt
    when one exists, else the credentials file's mtime plus aws.assumed_token_hours (an estimate)."""
    aws = cfg.aws
    if not aws:
        return {"left": "n/a", "source": "no aws block in .ops/config.yaml"}
    now = now or now_local(cfg)
    now_utc = now.astimezone(dt.timezone.utc) if now.tzinfo else dt.datetime.now(dt.timezone.utc)
    exp = None
    for f in sorted(glob.glob(os.path.join(os.path.expanduser(aws["sso_cache"]), "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        e = d.get("expiresAt") if isinstance(d, dict) else None
        t = _parse_iso(e) if isinstance(e, str) else None
        if t and (exp is None or t > exp):
            exp = t
    if exp:
        mins = int((exp - now_utc).total_seconds() // 60)
        return {"left": "EXPIRED" if mins <= 0 else fmt_dur(mins), "source": "sso cache expiresAt",
                "expires_at": exp.isoformat()}
    credf = os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.expanduser("~/.aws/credentials")
    try:
        mtime = os.stat(credf).st_mtime
    except OSError:
        return {"left": "none", "source": "no sso cache entry and no credentials file"}
    hours = aws["assumed_token_hours"]
    if not hours:
        return {"left": "?", "source": "credentials file present; aws.assumed_token_hours unset"}
    mins = int((mtime + hours * 3600 - time.time()) // 60)
    return {"left": ("EXPIRED" if mins <= 0 else fmt_dur(mins)) + " est",
            "source": f"credentials file mtime + {hours:g} h (estimate)"}


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
    by, p0_open = tk.counts(cfg)
    items = tk.human_items(cfg)
    inbox = len(items) if items is not None else None
    p0 = sum(1 for it in items if str(it.get("priority", "")).upper() == "P0") if items is not None else 0
    data = {"inbox": inbox, "p0": p0, "p0_open_tickets": p0_open, "creds": creds(cfg, now),
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
            f"{c.get('in-dev', 0)} dev {c.get('in-review', 0)} rev {c.get('ready-to-merge', 0)} rtd {c.get('deployed', 0)} dep")


def long(data: dict) -> str:
    c = data.get("counts") or {}
    rows = [f"{data.get('project')} @ {data.get('generated')}  (trunk {data.get('trunk')})",
            f"inbox   {data.get('inbox') if data.get('inbox') is not None else '? (ticket.sh human --json failed)'}",
            f"P0      {data.get('p0', 0)} in the inbox ({data.get('p0_open_tickets', 0)} open P0 ticket(s) on the board)",
            f"creds   {data['creds']['left']}  ({data['creds']['source']})",
            "board   " + ("  ".join(f"{k} {v}" for k, v in sorted(c.items())) or "no tickets")]
    return "\n".join(rows)
