"""The panel's data sources, each cached with its age: the human inbox (`ticket.sh human --json`), the
board (`ticket.sh board --json` when the project's script has it, else the front matter), the sessions
(`claude agents --json` + tmux panes + the launcher registry + redacted captures), credentials, the
log tails and plans (from `panel.logs` / `panel.plans_glob`), releases, FREEZE, the
trunk sha. Everything is read through zouinify.readonly; this module may not import anything else of
zouinify (scripts/check_panel_ast.py). No ticket is written, no session is touched."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from zouinify import panelstate, readonly
from zouinify.panel.redact import redact

PROMPT_RE = re.compile(r"Do you want|Allow |permission|\(y/n\)|Yes, and|Esc to|Enter to confirm|\[Y/n\]|\(esc\)", re.I)
CHROME_RE = re.compile(r"^\s*(\[[A-Z][a-z]+ [0-9.]+\]|\U0001F4B0|⏵|─|╭|╰|│\s*$|❯\s*$|>\s*$)")
EVIDENCE_RE = re.compile(
    r"LIVE CHECK PASSED|\breload|\bserial\b|sha256|CodeSha256|arn:aws|s3://|invalidation|No changes\.|paid (run|job)|"
    r"\bweb[0-9a-f]{10,}\b|\bdescribe-[a-z]|\bget-[a-z]+-[a-z]|\blist-[a-z]+|(HTTP[/0-9.]*|->) *[1-5][0-9]{2}\b|smoke_release", re.I)
LANES = (("ready", ("ready",)), ("in-dev", ("in-dev",)), ("review", ("in-review", "ready-to-merge")),
         ("to deploy", ("ready-to-deploy", "merged")), ("deployed", ("deployed",)))
CHIPS = ("backlog", "blocked", "done", "dropped")
SEV = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STATE_ORDER = {"waiting": 0, "working": 1, "idle": 2, "shell": 3, "gone": 4}
TIER2_DECIDE_RE = re.compile(r"CLAUDE\.md|settings", re.I)
# a command that mutates production however the ask is labelled: the human runs it, never a button (any version)
FLAGS = r"(?:-\S*\s+(?:\S+\s+)?)*"   # `-chdir=x -auto-approve`, `-C dir`, `-f file` between a tool and its verb
TIER2_COMMAND_RE = re.compile(
    rf"\bterraform\s+{FLAGS}(?:apply|destroy|import|taint|state\s+(?:rm|mv|push))\b|\bterraform\b[^\n]*\b(?:apply|destroy)\b|\bterraform\s+\$|"
    rf"\b(?:docker|podman)\s+{FLAGS}push\b|\baws\s+ecr\s+(?:put-image|batch-delete-image)\b|\baws\s+s3\s+(?:rm|cp|mv|sync|rb)\b|"
    rf"\bdeploy_[a-z_]*\.sh\b|\bticket\.sh\s+apply\b|\btf\.sh\s+\S+\s+apply\b|\bgit\s+{FLAGS}push\b|\bgh\s+pr\s+merge\b|"
    rf"\bgh\s+release\s+(?:create|delete)\b|\bgh\s+api\s+[^\n]*-X\s+(?:POST|PUT|DELETE|PATCH)\b|\bmake\s+{FLAGS}deploy\w*\b|"
    rf"\bnpm\s+run\s+deploy\w*\b|\bkubectl\s+{FLAGS}(?:apply|delete|rollout|scale|patch)\b|\bhelm\s+(?:install|upgrade|uninstall|rollback)\b|"
    rf"\bansible-playbook\b|\bgh\s+workflow\s+(?:run|enable|disable)\b|\bgh\s+(?:secret|variable)\s+set\b|\bgh\s+repo\s+(?:delete|rename|edit|archive)\b",
    re.I)
# `aws ...`: every operation mutates unless it is one of these (global flags may sit anywhere before the service)
AWS_READONLY_OPS = {("s3", "ls"), ("s3", "presign"), ("sso", "login"), ("sso", "logout"), ("configure", "list"), ("configure", "get"),
                    ("configure", "list-profiles"), ("logs", "filter-log-events"), ("logs", "tail"), ("sts", "get-caller-identity")}
AWS_READONLY_PREFIXES = ("get-", "list-", "describe-", "head-", "batch-get-")
AWS_BOOLEAN_FLAGS = {"--debug", "--version", "--no-verify-ssl", "--no-paginate", "--no-cli-pager", "--no-sign-request",
                     "--no-cli-auto-prompt", "--cli-auto-prompt", "--dryrun", "--recursive", "--quiet", "--exact-timestamps", "--only-show-errors"}
SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||\||;|\n)\s*")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def aws_mutating(command: str) -> bool:
    """True when any `aws` invocation in the command runs an operation that is not read-only."""
    for segment in SEGMENT_RE.split(command or ""):
        tokens = [t for t in segment.split() if t]
        while tokens and ENV_ASSIGN_RE.match(tokens[0]):
            tokens = tokens[1:]
        if not tokens or not (tokens[0] == "aws" or tokens[0].endswith("/aws")):
            continue
        service, op, i = None, None, 1
        while i < len(tokens):
            t = tokens[i]
            if t.startswith("-"):
                i += 1
                takes_value = "=" not in t and not t.startswith("--no-") and t not in AWS_BOOLEAN_FLAGS
                if takes_value and i < len(tokens) and not tokens[i].startswith("-"):
                    i += 1
                continue
            if service is None:
                service = t.lower()
            else:
                op = t.lower()
                break
            i += 1
        if service in (None, "help"):
            continue
        if op is None:
            return True
        if (service, op) in AWS_READONLY_OPS or op.startswith(AWS_READONLY_PREFIXES) or op == "help":
            continue
        return True
    return False
TICKET_IN_NAME_RE = re.compile(r"^t(\d{4})-")
CARD_KEYS = ("id", "severity", "title", "status", "owner", "claimed", "created", "plan", "deploy", "blocked_on", "closed_by", "repos", "type")


def item_id(it: dict) -> str:
    """The stable id T-0158 defines: first 10 hex of sha1(verb|tickets|command|expect)."""
    key = "|".join(str(it.get(k, "")) for k in ("verb", "tickets", "command", "expect"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def classify_item(it: dict) -> dict:
    verb = str(it.get("verb", "")).upper()
    text = " ".join(str(it.get(k, "")) for k in ("title", "command", "expect"))
    options = re.findall(r'"([^"]+)"', str(it.get("command", ""))) if verb == "DECIDE" else []
    if verb == "APPROVE" or (verb == "DECIDE" and TIER2_DECIDE_RE.search(text)):
        tier, hint = 2, f"type in the desk's tmux window: {it.get('expect') or it.get('command') or ''}".strip()
    elif verb in ("APPLY", "DEPLOY", "PUSH") or TIER2_COMMAND_RE.search(str(it.get("command", ""))) or aws_mutating(str(it.get("command", ""))):
        # a production apply, deploy or image push: the human runs the command in their terminal and types the
        # `ok` in the desk's window; never a button, in any version (T-0160 ticket text)
        tier, hint = 2, f"run it in your terminal, then type `{it.get('tickets') or 'T-xxxx'} ok` in the desk's tmux window"
    elif verb in ("RUN", "SSO"):
        tier, hint = 1, "run it in your terminal, then OK (available after receipts, T-0162)"
    elif verb == "CHECK":
        tier, hint = 1, "do the check, then OK (available after receipts, T-0162)"
    elif verb == "DECIDE":
        tier, hint = 1, "answer (available after receipts, T-0162)"
    else:
        tier, hint = 1, "available after receipts (T-0162)"
    return {"tier": tier, "hint": hint, "options": options}


def rel_glob(cfg, pattern: str) -> list:
    """Files matching a project-relative glob, newest first."""
    paths = [p for p in Path(cfg.root).glob(pattern) if p.is_file()]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in paths]


# ---------------------------------------------------------------- sources -----------------------------------------

def inbox_source(cfg) -> dict:
    data = readonly.read_human_json(cfg)
    if data is None:
        raise RuntimeError("ticket.sh human --json failed or printed no JSON list")
    items = []
    for it in data:
        row = {k: it.get(k, "") for k in ("verb", "tickets", "priority", "title", "since", "command", "expect", "if_nothing", "note")}
        row["id"] = str(it.get("id") or item_id(it))
        row["void"] = bool(it.get("void")) or "do NOT run" in str(it.get("note", ""))
        row.update(classify_item(it))
        items.append(row)
    return {"items": items, "count": len(items), "p0": sum(1 for it in items if str(it.get("priority", "")).upper() == "P0")}


def trunk_sha(cfg) -> str:
    return readonly.trunk_sha(cfg.root)


def card_of(fm: dict, path: str, root: str) -> dict:
    c = {k: fm.get(k, "") for k in CARD_KEYS}
    c["human"] = bool(fm.get("human"))
    c["file"] = os.path.relpath(path, root) if path else ""
    return c


def board_source(cfg) -> dict:
    bj = readonly.read_board_json(cfg)
    cards = []
    if bj:
        for t in bj["tickets"]:
            if isinstance(t, dict) and t.get("id"):
                c = {k: t.get(k, "") for k in CARD_KEYS}
                c["human"] = bool(t.get("human"))
                c["file"] = ""
                cards.append(c)
        source, warnings = "ticket.sh board --json", list(bj.get("warnings") or [])
    else:
        for f in readonly.ticket_files(cfg):
            cards.append(card_of(readonly.front_matter(f), f, cfg.root))
        source, warnings = "front matter", []
    newest, void = "", set()
    plans_glob = cfg.panel.get("plans_glob")
    if plans_glob:
        plans = rel_glob(cfg, plans_glob)
        if plans:
            newest = os.path.basename(plans[0])
            void = {os.path.basename(p) for p in plans[1:]}
    for c in cards:
        c["plan_void"] = bool(c.get("plan")) and os.path.basename(str(c["plan"])) in void
    cards.sort(key=lambda c: (SEV.get(c.get("severity", ""), 9), c.get("id", "")))
    lanes = {name: [c for c in cards if c["status"] in states] for name, states in LANES}
    lanes["to deploy"] = [c for c in lanes["to deploy"] if c["status"] != "merged" or (c.get("deploy") and c["deploy"] != "none")]
    counts: dict = {}
    for c in cards:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    chips = {k: counts.get(k, 0) for k in CHIPS}
    lane_states = {s for _, states in LANES for s in states}
    other = {s: n for s, n in counts.items() if s not in lane_states and s not in CHIPS}
    merged_no_deploy = [c for c in cards if c["status"] == "merged" and (not c.get("deploy") or c["deploy"] == "none")]
    if merged_no_deploy:
        other["merged (no deploy)"] = len(merged_no_deploy)
    p0 = [c for c in cards if c.get("severity") == "P0" and c["status"] not in readonly.TERMINAL]
    return {"cards": cards, "lanes": lanes, "chips": chips, "counts": counts, "other": other, "p0": p0,
            "newest_plan": newest, "void_plans": sorted(void), "source": source, "warnings": warnings, "total": len(cards)}


def walk_to_pane(pid, pane_by_pid: dict, depth: int = 5):
    p = pid
    for _ in range(depth):
        if not p:
            return None
        if p in pane_by_pid:
            return pane_by_pid[p]
        p = readonly.proc_parent(p)
    return None


def last_body_line(screen: str) -> str:
    body = [l for l in screen.splitlines() if l.strip() and not CHROME_RE.match(l)]
    return body[-1].strip()[:160] if body else ""


def waiting_line(screen: str) -> str:
    for l in screen.splitlines():
        if PROMPT_RE.search(l):
            return l.strip()[:160]
    return ""


def sessions_source(cfg) -> dict:
    agents = readonly.list_agents()
    panes = readonly.list_panes()
    pane_by_pid = {}
    for p in panes:
        try:
            pane_by_pid[int(p["pane_pid"])] = p
        except (ValueError, KeyError):
            pass
    pane_cmd = {p["session"]: p.get("command", "") for p in panes}
    reg = readonly.registry_entries(cfg)
    reg_by_name = {e["name"]: e for e in reg}
    roles = cfg.role_sessions()
    role_by_name = {s.name: r for r, s in roles.items() if s.name}
    role_by_tmux = {s.tmux: r for r, s in roles.items() if s.tmux}
    roots = {os.path.realpath(cfg.root)} | {os.path.realpath(cfg.path(r["path"])) for r in cfg.repos}

    def mine(cwd: str) -> bool:
        rc = os.path.realpath(cwd or "")
        return any(rc == r or rc.startswith(r + os.sep) for r in roots)

    owners: dict = {}
    for f in readonly.ticket_files(cfg):
        fm = readonly.front_matter(f)
        if fm.get("owner") and fm.get("status") in ("in-dev", "in-review", "ready-to-merge"):
            owners.setdefault(fm["owner"], fm.get("id", ""))
    rows: list = []
    seen: dict = {}
    for a in agents:
        if not mine(str(a.get("cwd") or "")):
            continue
        name = str(a.get("name") or "")
        if not name or name in seen:
            continue
        seen[name] = True
        e = reg_by_name.get(name, {})
        tmux_name = e.get("tmux") or next((s.tmux for s in roles.values() if s.name == name), None)
        if not tmux_name:
            pane = walk_to_pane(a.get("pid"), pane_by_pid)
            tmux_name = pane["session"] if pane else None
        m = TICKET_IN_NAME_RE.match(name)
        role = e.get("role") or role_by_name.get(name) or (role_by_tmux.get(tmux_name) if tmux_name else None) \
            or ("worker" if m else "other")
        ticket = e.get("ticket") or owners.get(name) or (f"T-{m.group(1)}" if m else "")
        screen = redact(readonly.capture_pane(tmux_name)) if tmux_name else ""
        status = str(a.get("status") or "unknown")
        waiting_for = waiting_line(screen)
        if status == "waiting" or (waiting_for and status != "busy"):
            state = "waiting"
        elif status == "busy":
            state = "working"
        elif status == "shell" or (tmux_name and pane_cmd.get(tmux_name, "claude") not in ("claude", "node", "")):
            state = "shell"
        elif status == "idle":
            state = "idle"
        else:
            state = status
        want = cfg.sessions[role].model if role in cfg.sessions else (cfg.model or "")
        label = readonly.model_on_screen(screen, cfg.model_re)
        rows.append({"name": name, "role": role, "tmux": tmux_name, "ticket": ticket, "state": state, "status": status,
                     "model": label or "?", "model_expected": want,
                     "model_mismatch": bool(label and readonly.alias_of(want) and readonly.model_word(label) != readonly.alias_of(want)),
                     "last_line": last_body_line(screen), "waiting_for": waiting_for if state == "waiting" else "", "since": "",
                     "started_at": a.get("startedAt"), "pid": a.get("pid"), "cwd": a.get("cwd"), "session_id": a.get("sessionId"),
                     "launched_at": e.get("launched_at"), "by": e.get("by"), "registered": bool(e)})
    for e in reg:
        if e["name"] not in seen:
            seen[e["name"]] = True
            rows.append({"name": e["name"], "role": e.get("role", "other"), "tmux": e.get("tmux"), "ticket": e.get("ticket") or "",
                         "state": "gone", "status": "gone", "model": "?", "model_expected": e.get("model", ""), "model_mismatch": False,
                         "last_line": "", "waiting_for": "", "since": "",
                         "tmux_alive": readonly.has_session(e["tmux"]) if e.get("tmux") else False,
                         "launched_at": e.get("launched_at"), "by": e.get("by"), "registered": True})
    for role, s in roles.items():
        if s.kind == "claude" and s.name and s.name not in seen:
            seen[s.name] = True
            rows.append({"name": s.name, "role": role, "tmux": s.tmux, "ticket": "", "state": "gone", "status": "gone", "model": "?",
                         "model_expected": s.model, "model_mismatch": False, "last_line": "", "waiting_for": "", "since": "",
                         "tmux_alive": readonly.has_session(s.tmux) if s.tmux else False, "registered": s.name in reg_by_name})
    rows.sort(key=lambda r: (0 if r["role"] == "desk" else 1, STATE_ORDER.get(r["state"], 9), r["name"]))
    return {"rows": rows}


def creds_source(cfg) -> dict:
    return readonly.creds(cfg)


def logs_source(cfg) -> dict:
    out = {}
    for spec in cfg.panel.get("logs") or []:
        files = rel_glob(cfg, spec["glob"])
        if not files:
            out[spec["label"]] = {"path": "", "tail": [], "mtime": None, "glob": spec["glob"]}
            continue
        p = files[0]
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()[-40:]
        except OSError:
            lines = []
        out[spec["label"]] = {"path": os.path.relpath(p, cfg.root), "tail": [redact(l) for l in lines],
                              "mtime": dt.datetime.fromtimestamp(Path(p).stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                              "glob": spec["glob"]}
    return out


def releases_source(cfg) -> list:
    rows = []
    for f in sorted(Path(readonly.tickets_dir(cfg), "releases").glob("R-*.md"), reverse=True):
        fm = readonly.front_matter(str(f))
        fm.setdefault("id", f.name[:-3])
        rows.append(fm)
    return rows


def freeze_source(cfg) -> dict:
    return readonly.freeze(cfg)


def hook_status() -> dict:
    installed, path = panelstate.hook_installed()
    snippet = json.dumps({"hooks": {ev: [{"hooks": [{"type": "command", "command": "~/.claude/skills/zouinify/templates/hooks/zouinify-hook.sh",
                                                     "timeout": 5}]}] for ev in ("Stop", "Notification", "SessionStart", "SessionEnd")}}, indent=1)
    return {"installed": installed, "path": path, "snippet": snippet,
            "note": "the hook script arrives with T-0163; add the snippet yourself: a session must not edit your settings"}


def ticket_sections(path: str) -> dict:
    """The `## Log` lines (newest first) and the `## Deploy handoff` block of a ticket file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {"log": [], "handoff": "", "problem": ""}
    sections: dict = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            cur = m.group(1)
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    log = [l for l in sections.get("Log", []) if l.strip()]
    log_rows = []
    for l in reversed(log):
        m = re.match(r"^-\s*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)\s+([^:]+):\s*(.*)$", l)
        log_rows.append({"at": m.group(1) if m else "", "by": m.group(2).strip() if m else "", "text": redact(l.lstrip("- ").strip()),
                         "evidence": bool(EVIDENCE_RE.search(l))})
    return {"log": log_rows, "handoff": redact("\n".join(sections.get("Deploy handoff", [])).strip()),
            "problem": redact("\n".join(sections.get("Problem", [])).strip()[:2000])}


def live_checks(cfg, board: dict) -> list:
    rows = []
    for c in board.get("lanes", {}).get("deployed", []):
        path = readonly.file_of(cfg, c["id"]) if c.get("id") else None
        sec = ticket_sections(path) if path else {"log": []}
        ev = next((l for l in sec["log"] if l["evidence"]), None)
        rows.append({"id": c["id"], "title": c["title"], "severity": c["severity"], "deploy": c.get("deploy", ""),
                     "evidence": ev["text"] if ev else "", "evidence_at": ev["at"] if ev else "", "evidence_by": ev["by"] if ev else "",
                     "has_evidence": bool(ev), "last": sec["log"][0]["text"] if sec["log"] else ""})
    return rows


class Snapshot:
    """Cached sources with ages; a background thread keeps them fresh so a request never waits on a capture or on ticket.sh."""

    def __init__(self, cfg, port: int, bind: str, started_at: str):
        self.cfg, self.port, self.bind, self.started_at = cfg, port, bind, started_at
        r = int(cfg.panel.get("refresh_s") or 30)
        self.src = {
            "inbox": {"fn": inbox_source, "ttl": r}, "board": {"fn": board_source, "ttl": r}, "sessions": {"fn": sessions_source, "ttl": r},
            "creds": {"fn": creds_source, "ttl": 60}, "logs": {"fn": logs_source, "ttl": 20}, "releases": {"fn": releases_source, "ttl": 60},
            "freeze": {"fn": freeze_source, "ttl": r}, "trunk": {"fn": trunk_sha, "ttl": 60},
        }
        for s in self.src.values():
            s.update({"data": None, "at": 0.0, "error": None, "loaded": False, "lock": threading.Lock()})
        self.stop_event = threading.Event()
        self.thread = None
        # when each session was first seen waiting (derived from the refreshes; forgotten when it stops waiting)
        self.waiting_since: dict = {}

    def mark_waiting(self, data: dict) -> dict:
        now = panelstate.now_iso()
        names = set()
        for r in (data or {}).get("rows", []):
            if r.get("state") == "waiting":
                names.add(r["name"])
                r["since"] = self.waiting_since.setdefault(r["name"], now)
            else:
                r["since"] = ""
        for n in [n for n in self.waiting_since if n not in names]:
            self.waiting_since.pop(n, None)
        return data

    # -- refresh --------------------------------------------------------------------------------------
    def refresh(self, name: str) -> None:
        s = self.src[name]
        if not s["lock"].acquire(blocking=False):
            return
        try:
            try:
                s["data"] = s["fn"](self.cfg)
                if name == "sessions":
                    s["data"] = self.mark_waiting(s["data"])
                s["error"] = None
            except Exception as e:  # noqa: BLE001 - a failing source is shown as an error, never a crash
                s["error"] = f"{type(e).__name__}: {e}"[:300]
            s["at"] = time.time()
            s["loaded"] = True
        finally:
            s["lock"].release()

    def refresher_alive(self) -> bool:
        return bool(self.thread is not None and self.thread.is_alive())

    def get(self, name: str, fresh: bool = False):
        """(data, age_s, error). With the refresher running, a request never blocks: a source that is not
        loaded yet comes back as (None, None, None) and `build()` lists it under `loading`; without a
        refresher (tests, one-shot use) the source is loaded synchronously."""
        s = self.src[name]
        if fresh:
            self.refresh(name)
        elif not s["loaded"] or time.time() - s["at"] > 3 * s["ttl"]:
            if self.refresher_alive() and not s["loaded"]:
                return None, None, None
            self.refresh(name)
        if not s["loaded"]:
            with s["lock"]:  # another thread is loading it right now: wait for it
                pass
        return s["data"], (round(time.time() - s["at"], 1) if s["loaded"] else None), s["error"]

    def loading(self) -> list:
        return [n for n, s in self.src.items() if not s["loaded"]]

    def tick(self) -> None:
        for name, s in self.src.items():
            if s["loaded"] and time.time() - s["at"] > s["ttl"]:
                self.refresh(name)

    def start_refresher(self) -> None:
        """Warm every source once in the background (the cheap ones first), then keep them fresh."""
        order = ["freeze", "trunk", "creds", "releases", "logs", "sessions", "board", "inbox"]

        def loop():
            for name in order + [n for n in self.src if n not in order]:
                if self.stop_event.is_set():
                    return
                try:
                    self.refresh(name)
                except Exception:  # noqa: BLE001
                    pass
            while not self.stop_event.wait(2.0):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    pass
        self.thread = threading.Thread(target=loop, name="zouinify-refresh", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    # -- views ----------------------------------------------------------------------------------------
    def sources_meta(self) -> dict:
        return {n: {"age_s": (round(time.time() - s["at"], 1) if s["loaded"] else None), "ttl_s": s["ttl"], "error": s["error"],
                    "loading": not s["loaded"], "stale": bool(s["loaded"] and time.time() - s["at"] > 3 * s["ttl"])}
                for n, s in self.src.items()}

    def build(self, fresh: bool = False) -> dict:
        cfg = self.cfg
        inbox, _, inbox_err = self.get("inbox", fresh)
        board, _, board_err = self.get("board", fresh)
        sessions, _, sess_err = self.get("sessions", fresh)
        creds, _, _ = self.get("creds", fresh)
        logs, _, _ = self.get("logs", fresh)
        releases, _, _ = self.get("releases", fresh)
        freeze, _, _ = self.get("freeze", fresh)
        trunk, _, _ = self.get("trunk", fresh)
        inbox = inbox or {"items": [], "count": None, "p0": 0}
        board = board or {"cards": [], "lanes": {n: [] for n, _ in LANES}, "chips": {k: 0 for k in CHIPS}, "counts": {}, "other": {},
                          "p0": [], "newest_plan": "", "void_plans": [], "source": "", "warnings": [], "total": 0}
        rows = (sessions or {}).get("rows", [])
        now = readonly.now_local(cfg)
        waiting = [r for r in rows if r["state"] == "waiting"]
        by_state: dict = {}
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        desk_sess = cfg.sessions.get("desk")
        desk_row = next((r for r in rows if r["role"] == "desk"), None)
        counts = board["counts"]
        return {
            "generated": panelstate.now_iso(), "version": panelstate.version(), "loading": self.loading(),
            "project": {"name": cfg.name, "short": cfg.short, "root": cfg.root, "trunk": cfg.trunk, "prod": cfg.prod, "trunk_sha": trunk or ""},
            "panel": {"port": self.port, "bind": self.bind, "refresh_s": cfg.panel.get("refresh_s", 30), "started_at": self.started_at,
                      "version": panelstate.version(), "write_path": False,
                      "note": "read-only v1: every control is disabled until receipts (T-0162)"},
            "header": {"trunk_sha": trunk or "", "freeze": freeze or {"on": False, "ids": []}, "creds": creds or {"left": "?", "source": ""},
                       "desk": {"name": desk_sess.name if desk_sess else "", "tmux": desk_sess.tmux if desk_sess else "",
                                "state": desk_row["state"] if desk_row else "gone", "model": desk_row["model"] if desk_row else "?",
                                "last_line": desk_row["last_line"] if desk_row else "",
                                "presence": "n/a (the desk polls receipts from T-0162 on)"}},
            "kpis": {"inbox": inbox.get("count"), "p0": inbox.get("p0", 0), "p0_open_tickets": len(board["p0"]),
                     "waiting": by_state.get("waiting", 0), "working": by_state.get("working", 0), "idle": by_state.get("idle", 0),
                     "gone": by_state.get("gone", 0), "shell": by_state.get("shell", 0),
                     "to_deploy": len(board["lanes"].get("to deploy", [])), "deployed": counts.get("deployed", 0),
                     "sessions": len(rows), "tickets": board["total"]},
            "p0": board["p0"], "inbox": {"items": inbox["items"], "count": inbox.get("count"), "error": inbox_err},
            "board": {k: board[k] for k in ("lanes", "chips", "counts", "other", "newest_plan", "void_plans", "source", "warnings", "total")},
            "tickets": board["cards"], "sessions": {"rows": rows, "error": sess_err}, "waiting": waiting,
            "creds": creds, "logs": logs or {}, "releases": releases or [], "live_checks": live_checks(cfg, board),
            "config": {k: v for k, v in cfg.to_dict().items() if k != "warnings"}, "hook": hook_status(),
            "sources": self.sources_meta(), "errors": {"inbox": inbox_err, "board": board_err, "sessions": sess_err},
        }

    def ticket(self, tid: str):
        if not readonly.is_ticket_id(tid):
            return None
        path = readonly.file_of(self.cfg, tid)
        if not path:
            return None
        fm = readonly.front_matter(path)
        sec = ticket_sections(path)
        return {"id": tid, "file": os.path.relpath(path, self.cfg.root), "front_matter": fm, "log": sec["log"][:10], "log_total": len(sec["log"]),
                "handoff": sec["handoff"], "problem": sec["problem"]}

    def session(self, name: str, fresh: bool = False):
        rows = ((self.get("sessions", fresh)[0] or {}).get("rows", []))
        row = next((r for r in rows if r["name"] == name), None)
        if not row:
            return None
        d = dict(row)
        d["screen"] = redact(readonly.capture_pane(row["tmux"])) if row.get("tmux") else ""
        d["registry"] = readonly.registry_entry(self.cfg, name)
        return d
