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
import unicodedata
from pathlib import Path
from types import MappingProxyType

from zouinify import panelstate, readonly
from zouinify.panel.redact import FILE_EXT, redact

CHROME_RE = re.compile(r"^\s*(\[[A-Z][a-z]+ [0-9.]+\]|\U0001F4B0|⏵|─|╭|╰|│\s*$|❯\s*$|>\s*$)")
EVIDENCE_RE = re.compile(
    r"LIVE CHECK PASSED|\breload|\bserial\b|sha256|CodeSha256|arn:aws|s3://|invalidation|No changes\.|paid (run|job)|"
    r"\bweb[0-9a-f]{10,}\b|\bdescribe-[a-z]|\bget-[a-z]+-[a-z]|\blist-[a-z]+|(HTTP[/0-9.]*|->) *[1-5][0-9]{2}\b|smoke_release", re.I)
LANES = (("ready", ("ready",)), ("in-dev", ("in-dev",)), ("review", ("in-review", "ready-to-merge")),
         ("to deploy", ("ready-to-deploy", "merged")), ("deployed", ("deployed",)))
CHIPS = ("backlog", "blocked", "done", "dropped")
# frozen (T-0164): a panel module must not be able to widen the tier classifier or reorder the lanes at runtime
SEV = MappingProxyType({"P0": 0, "P1": 1, "P2": 2, "P3": 3})
STATE_ORDER = MappingProxyType({"waiting": 0, "working": 1, "idle": 2, "shell": 3, "gone": 4})
# a text that names CLAUDE.md or settings is tier 2 whatever the verb or the receipt kind: the human types it. Checked after NFKC
# normalisation, Unicode format characters removed and the common Cyrillic look-alikes of Latin letters folded (T-0162 review: a
# zero-width space inside CLAUDE.md, fullwidth letters, `setting.json`, `сlaude.md` all passed the old `CLAUDE\.md|settings`);
# the word is `settings` or `setting` (optionally `.json`) as a whole word, so "resetting the cache" keeps its button. Other
# look-alike alphabets still pass: the desk verifies every receipt anyway.
TIER2_TEXT_RE = re.compile(r"claude[\W_]*md|\bsettings?(?:\.json)?\b", re.I)
CONFUSABLES = str.maketrans({"с": "c", "а": "a", "е": "e", "о": "o", "ѕ": "s", "р": "p", "х": "x", "і": "i",
                             "С": "C", "А": "A", "Е": "E", "О": "O", "Ѕ": "S", "Р": "P", "Х": "X", "І": "I",
                             "т": "t", "у": "y", "һ": "h", "ԁ": "d", "ԛ": "q", "ј": "j", "ӏ": "l", "մ": "n"})
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
AWS_READONLY_OPS = frozenset({("s3", "ls"), ("s3", "presign"), ("sso", "login"), ("sso", "logout"), ("configure", "list"), ("configure", "get"),
                    ("configure", "list-profiles"), ("logs", "filter-log-events"), ("logs", "tail"), ("sts", "get-caller-identity")})
AWS_READONLY_PREFIXES = ("get-", "list-", "describe-", "head-", "batch-get-")
AWS_BOOLEAN_FLAGS = frozenset({"--debug", "--version", "--no-verify-ssl", "--no-paginate", "--no-cli-pager", "--no-sign-request",
                     "--no-cli-auto-prompt", "--cli-auto-prompt", "--dryrun", "--recursive", "--quiet", "--exact-timestamps", "--only-show-errors"})
SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||\||;|\n)\s*")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def tier2_text(text) -> bool:
    """Whether a text names CLAUDE.md or settings once Unicode tricks are normalised away (NFKC, format characters dropped)."""
    if not isinstance(text, str) or not text:
        return False
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf").translate(CONFUSABLES)
    return bool(TIER2_TEXT_RE.search(t))


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
    if verb == "APPROVE" or tier2_text(text):
        tier, hint = 2, f"type in the desk's tmux window: {it.get('expect') or it.get('command') or ''}".strip()
    elif verb in ("APPLY", "DEPLOY", "PUSH") or TIER2_COMMAND_RE.search(str(it.get("command", ""))) or aws_mutating(str(it.get("command", ""))):
        # a production apply, deploy or image push: the human runs the command in their terminal and types the
        # `ok` in the desk's window; never a button, in any version (T-0160 ticket text)
        tier, hint = 2, f"run it in your terminal, then type `{it.get('tickets') or 'T-xxxx'} ok` in the desk's tmux window"
    elif verb in ("RUN", "SSO"):
        tier, hint = 1, "run it in your terminal, then OK (a receipt the desk verifies)"
    elif verb == "CHECK":
        tier, hint = 1, "do the check, then OK (a receipt the desk verifies)"
    elif verb == "DECIDE":
        tier, hint = 1, "answer (a receipt the desk verifies; a relayed answer is never an approval)"
    else:
        tier, hint = 1, "OK (a receipt the desk verifies)"
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


# A session "waits for you" only on a REAL prompt (T-0177): an option dialog (the permission box, an AskUserQuestion)
# with its selection marker, a dialog footer, a y/n question or a bare "Do you want to proceed?" line. Prose that
# mentions permission, allow or Esc, a numbered list in an answer, a "› Message from" line and the human's own draft
# in the input box ("❯ some text" between the two rules) never count.
OPTION_RE = re.compile(r"^\s*│?\s*(?:[❯>]\s*)?[1-9]\.\s+\S")
# the selected option: `❯ 1.` at the margin (Claude Code indents a dialog by at most two columns; a quoted dialog in an
# answer is indented deeper) or `❯ 1.` / `> 1.` inside a box (`> 1.` at the margin is a quote or a prompt echo)
SELECTED_OPTION_RE = re.compile(r"^(?: {0,2}❯|\s*│\s*[❯>])\s*[1-9]\.\s+\S")
FOOTER_WORD = (r"(?:Enter to (?:select|confirm|submit|continue|send)|Esc to (?:cancel|exit|close|dismiss)|"
               r"↑/↓ to (?:navigate|select|choose|move)|Tab to (?:toggle|cycle|switch)|Space to (?:select|toggle))")
# the whole line is the footer: phrases joined by " · ", nothing else (prose quoting a footer has other words around)
FOOTER_RE = re.compile(rf"^\s*│?\s*{FOOTER_WORD}(?:\s·\s{FOOTER_WORD})+\s*│?\s*$", re.I)
YN_RE = re.compile(r"^(?: {0,2}\S|\s*│).*[(\[](?:y/n|Y/n|y/N)[)\]]\s*│?\s*$")   # at the margin or in a box: an indented one is quoted
BOX_BOTTOM_RE = re.compile(r"^\s*╰")
PROCEED_RE = re.compile(r"^\s*│\s*(?:Do you want to (?:proceed|allow|continue|make this edit|run)[^?]*\?|Allow (?:this|the|once|always)[^?]*\?)\s*│?\s*$", re.I)
BOX_RE = re.compile(r"^\s*│\s?|\s*│\s*$")
BOX_TOP_RE = re.compile(r"^\s*╭")
FENCE_RE = re.compile(r"^\s*(?:│\s*)?```")
DRAFT_RE = re.compile(r"^\s*[❯>›]\s")      # the input box, a previous prompt's echo, a cross-session message: never a prompt
MAX_DIALOG_LINES = 30                        # a live dialog sits in the last 30 lines of the screen, never higher; a box top ╭
                                             # inside that tail narrows the region further (nothing above the box is the dialog)


BOX_LEFT_RE = re.compile(r"^\s*│\s?")
NUMBER_RE = re.compile(r"[1-9]\.\s")


def indent_of(line: str) -> int:
    """Columns of indentation inside the box border, if any."""
    t = BOX_LEFT_RE.sub("", line, count=1)
    return len(t) - len(t.lstrip(" "))


def option_column(line: str) -> int:
    """The column of an option's number inside the box border (`❯ 1.` and `  2.` share it)."""
    t = BOX_LEFT_RE.sub("", line, count=1)
    m = NUMBER_RE.search(t)
    return m.start() if m else indent_of(line)


def first_option(lines: list, idx: int, floor: int = 0) -> int:
    """From the selected option at idx, the index of the dialog's first option: walks up over the other option lines
    and their descriptions, which are indented deeper than the options' number column (the cursor may sit on
    option 2; the question stands above option 1)."""
    col = option_column(lines[idx])
    j = idx
    while j - 1 >= floor:
        prev = lines[j - 1]
        if OPTION_RE.match(prev) or (prev.strip() and not CHROME_RE.match(prev) and indent_of(prev) > col):
            j -= 1
            continue
        break
    return j


def block_above(lines: list, idx: int) -> tuple:
    """The block of text right above line idx (box borders stripped; chrome, checkbox and option lines skipped), as
    (texts top-down, index of its first line); ([], idx) when nothing stands above."""
    above, top = [], idx
    for i in range(idx - 1, max(-1, idx - 9), -1):
        l = lines[i]
        if BOX_TOP_RE.match(l):
            break                                   # never reach past the dialog's own box top
        t = BOX_RE.sub("", l).strip()
        if not t or CHROME_RE.match(l) or t.startswith("☐") or t.startswith("☑") or OPTION_RE.match(l):
            if above:
                break
            continue
        above.append(t)
        top = i
    above.reverse()
    return above, top


def box_top_of(lines: list, idx: int, floor: int):
    """The index of the ╭ line of the box that contains line idx (no ╰ between), or None."""
    for i in range(idx - 1, floor - 1, -1):
        if BOX_BOTTOM_RE.match(lines[i]):
            return None
        if BOX_TOP_RE.match(lines[i]):
            return i
    return None


def question_above(lines: list, idx: int) -> str:
    """The question a dialog asks: from the block above line idx, the line that ends with "?" if any, else its first."""
    above, _ = block_above(lines, idx)
    q = next((t for t in reversed(above) if t.endswith("?")), None) or (above[0] if above else "")
    return q[:160]


def fenced_lines(lines: list, start: int = 0) -> set:
    """Indices (from `start` on) inside ``` code fences: quoted text, never a prompt. Fences pair up within the window
    only, so an unclosed fence high on the screen cannot hide a real dialog at the bottom."""
    out, inside = set(), False
    for i in range(start, len(lines)):
        if FENCE_RE.match(lines[i]):
            inside = not inside
            out.add(i)
        elif inside:
            out.add(i)
    return out


def prompt_anchor(lines: list):
    """(kind, index of the anchor line) of a real prompt, or ("", None). The BOTTOM-most anchor of any kind wins and
    it must sit within the last MAX_DIALOG_LINES lines: a live dialog is drawn at the bottom of the screen, anything
    higher is history or quoted text, whatever it looks like (a planted `❯ 1. Fake` or a `╭` near the top cannot make
    the screen "the dialog"). A box top ╭ inside that tail narrows the region to the box. Kinds: a selected option
    (❯ 1., or > 1. inside a box), a dialog footer (two of Enter/Esc/arrows/Tab joined by " · " or in a box), a line
    ending in (y/n), a boxed "Do you want to proceed?"."""
    n = len(lines)
    floor = max(0, n - MAX_DIALOG_LINES)
    last_top = max((i for i in range(floor, n) if BOX_TOP_RE.match(lines[i])), default=None)
    if last_top is not None:
        floor = last_top
    fenced = fenced_lines(lines, floor)
    def is_dialog_option(i: int) -> bool:
        # a real dialog has at least two options: another numbered line within four lines (descriptions between)
        return any(OPTION_RE.match(lines[k]) for k in range(max(floor, i - 4), min(n, i + 5)) if k != i)

    for i in range(n - 1, floor - 1, -1):
        if i in fenced:
            continue
        l = lines[i]
        if SELECTED_OPTION_RE.match(l) and is_dialog_option(i):
            return "options", i
        if FOOTER_RE.match(l):
            # the footer closes an option dialog: its selected option, when still on screen, is the better anchor
            # (the question stands above the options, not above the footer)
            for j in range(i - 1, floor - 1, -1):
                if j not in fenced and SELECTED_OPTION_RE.match(lines[j]) and is_dialog_option(j):
                    return "options", j
            return "footer", i
        if DRAFT_RE.match(l):
            continue
        if YN_RE.match(l):
            return "yn", i
        if PROCEED_RE.match(l):
            return "proceed", i
    return "", None


def prompt_shape(screen: str) -> tuple:
    """(kind, question) of a real prompt on the screen, or ("", ""): "options" (a numbered option list with the ❯ or >
    marker on the selected one), "footer" (two of Enter/Esc/arrows/Tab on one line), "yn" (a line ending in (y/n)) or
    "proceed" (a bare "Do you want to proceed?" line). Bare words in prose and the human's own draft never count."""
    lines = screen.splitlines()
    kind, i = prompt_anchor(lines)
    if not kind:
        return "", ""
    if kind == "options":
        return kind, question_above(lines, first_option(lines, i, max(0, len(lines) - MAX_DIALOG_LINES)))
    if kind == "footer":
        return kind, question_above(lines, i)
    return kind, BOX_RE.sub("", lines[i]).strip()[:160]


def dialog_start(screen: str):
    """The index of the first screen line of the dialog a real prompt belongs to (the question block above the options
    or the footer, capped at 8 lines, else the prompt line itself), or None when the screen shows no prompt. For
    cutting another project's screen down to its dialog: everything above it is that project's business, so the
    anchor is the bottom-most one and the block never reaches past a box top."""
    lines = screen.splitlines()
    kind, i = prompt_anchor(lines)
    if not kind:
        return None
    floor = max(0, len(lines) - MAX_DIALOG_LINES)
    top = box_top_of(lines, i, floor)
    if top is not None:
        return top + 1                                    # a boxed dialog starts under its own ╭, blank box lines included
    if kind == "options":
        start = block_above(lines, first_option(lines, i, floor))[1]
    elif kind == "footer":
        start = block_above(lines, i)[1]
    else:
        start = i
    return max(start, floor)                              # the region start..bottom never exceeds MAX_DIALOG_LINES


def waiting_line(screen: str) -> str:
    """What a real prompt on the screen asks, in one line; "" when the screen shows no prompt."""
    return prompt_shape(screen)[1]


def project_roots(cfg) -> set:
    """The real paths a session's cwd must be under to belong to this project: the root and every configured repo."""
    return {os.path.realpath(cfg.root)} | {os.path.realpath(cfg.path(r["path"])) for r in cfg.repos}


def under(cwd: str, roots) -> bool:
    """Whether an ABSOLUTE path lies under one of the roots; a relative path never does (it would resolve against the
    panel's own cwd, which is the project root: T-0163 review)."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    rc = os.path.realpath(cwd or "")
    return any(rc == r or rc.startswith(r + os.sep) for r in roots)


def pane_index(panes: list) -> dict:
    """pane pid -> pane row, for the /proc parent walk from a session's pid to its tmux window."""
    by_pid: dict = {}
    for p in panes:
        try:
            by_pid[int(p["pane_pid"])] = p
        except (ValueError, KeyError):
            pass
    return by_pid


def registered_projects() -> list:
    """`~/.zouinify/projects.json`, written whenever one of this user's project panels starts; read-only here.
    Rows: {name, short, root, port, ...}. Missing or unreadable -> []."""
    path = os.path.join(os.path.expanduser("~"), ".zouinify", "projects.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("root")]


def project_label(cwd: str, projects: list) -> tuple:
    """(label, registered): a registered project's name when `cwd` is under its root, else the cwd's last path
    segment only; the home directory, or any segment holding an `@` (a login such as user@org), shows as `~`. Nothing
    more of a foreign path is ever exposed (T-0175 merge gate: the panel may serve the LAN)."""
    rc = os.path.realpath(cwd or "")
    for p in projects:
        root = os.path.realpath(str(p.get("root") or ""))
        if root and (rc == root or rc.startswith(root + os.sep)):
            return str(p.get("name") or os.path.basename(root)), True
    if rc == os.path.realpath(os.path.expanduser("~")):
        return "~", False
    base = os.path.basename(rc.rstrip(os.sep))
    if "@" in base:
        base = "~"
    return (base or "?"), False


ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff\u00ad]")
BAD_CHAR_RE = re.compile(r"[/\\~@]")
ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]{8,}")
TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9+=_-]{16,}")
DIGITS_RE = re.compile(r"\d{9,}")
HOSTY_RE = re.compile(rf"(?i)(?<![\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?!{FILE_EXT}\b)[a-z]{{2,}}\b")
COOKIE_WORD_RE = re.compile(r"(?i)\bcookies?\b|\bset-cookie\b")
# the keyword DENYLIST of the final review: any credential word, login/auth word, `sudo -S`, or a -p/-P/-k/-W/-b flag not
# followed by a digit withholds the line (`docker login -u bob -p hunter2`, `gpg --passphrase x`, `password is hunter2`,
# `token swordfish`, `zip -P`, `openssl enc -k`, `htpasswd -b`); unbounded on purpose ("passed" too: withholding costs little).
# T-0179 adds `--stdin` (sudo), `-N` (ssh-keygen), a -u/-U value holding `,` `%` or `:` (lftp -u a,b, smbclient -U a%b,
# curl -u a:b), and redis-cli's -a; `sort -u`, a VCS commit's `-a`, `ls -a` stay clean (the panel guard forbids naming that VCS verb here)
DENY_WORD_RE = re.compile(r"(?i)pass|pwd|secret|token|credential|auth|login|\bkey\b|sudo\s+-S\b|(?<!\S)-[pPkWb](?![0-9])"
                          r"|--stdin\b|(?<!\S)-N\b|(?<!\S)-[uU]\s*\S*[,%:]|redis-cli\b[^\n]*\s-a\s")
# case-sensitive: `PIN 1234`, `-e PW=x` (the words "pin" and "pw" in prose stay); T-0179
DENY_UPPER_RE = re.compile(r"\bPIN\b|\bPW\b")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{1,4}(?![0-9a-f:])|(?<![0-9a-f])::[0-9a-f]{1,4}\b")
FOREIGN_LINE_MAX = 400            # a line longer than this is withheld; a clean line is shown to FOREIGN_LINE_SHOWN
FOREIGN_LINE_SHOWN = 160
FOREIGN_LOOKBACK = 6
FOREIGN_OPTION_LINES = 16


def clean_line(line: str) -> str:
    """NFKC-normalised, zero-width characters and box borders stripped, trimmed."""
    t = unicodedata.normalize("NFKC", ZERO_WIDTH_RE.sub("", line))
    return BOX_RE.sub("", t).strip()


def line_ok(t: str) -> str:
    """The allowlist a line of another project's dialog must pass (desk + reviewer, T-0175): the reason it fails, else ''.
    Printable text under 400 characters (checked whole; shown to 160) with no path, home or address character (`/ \\ ~ @`), no alphanumeric run of 8+
    mixing letters and digits (keys, ids, shas, split secrets), no token-shaped run of 16+ with a digit or symbol, no
    9+ digit number, no dotted host name, no cookie text, no credential/login/flag word (the review's denylist), no IPv4 or
    IPv6 address, and nothing the redactor would change."""
    if len(t) > FOREIGN_LINE_MAX:
        return "a line longer than 400 characters"
    if not t.isprintable():
        return "a control character"
    if BAD_CHAR_RE.search(t):
        return "a path, address or home marker"
    for m in ALNUM_RUN_RE.finditer(t):
        w = m.group(0)
        if re.search(r"[A-Za-z]", w) and re.search(r"[0-9]", w):
            return "an identifier-shaped string"
    for m in TOKEN_RUN_RE.finditer(t):
        if re.search(r"[0-9+=_-]", m.group(0)):
            return "a token-shaped string"
    if DIGITS_RE.search(t):
        return "a long number"
    if HOSTY_RE.search(t):
        return "a host name"
    if COOKIE_WORD_RE.search(t):
        return "cookie text"
    if DENY_WORD_RE.search(t) or DENY_UPPER_RE.search(t):
        return "a credential, login or flag word"
    if IPV4_RE.search(t) or any(re.search(r"(?i)[a-f]|::", m.group(0)) for m in IPV6_RE.finditer(t)):
        return "a network address"
    if redact(t) != t:
        return "a credential-shaped value"
    return ""


def foreign_dialog(screen: str) -> tuple:
    """(text, withheld_reason) for another project's screen: an ALLOWLIST, not a redaction. Shown are only the question
    block right above the first option line (contiguous text, at most 6 lines up) and the option lines with their
    continuation lines (until a blank or chrome line, at most 16), each cleaned by `clean_line` and accepted by `line_ok`;
    with no option list, the first prompt line alone. One failing line withholds the whole dialog; no prompt or dialog
    withholds it too. The capture is the last visible screen only, never scrollback."""
    lines = screen.splitlines()
    kind, anchor = prompt_anchor(lines)            # T-0177: the real prompt at the bottom of the screen, or none
    if not kind:
        return "", "no prompt or dialog on the visible screen"
    floor = max(0, len(lines) - MAX_DIALOG_LINES)
    picked: list = []
    if kind in ("options", "footer"):
        head = first_option(lines, anchor, floor) if kind == "options" else anchor
        above: list = []
        for i in range(head - 1, max(floor - 1, head - FOREIGN_LOOKBACK - 1), -1):
            if BOX_TOP_RE.match(lines[i]):
                break
            t = clean_line(lines[i])
            if not t or CHROME_RE.match(lines[i]) or t.startswith("☐") or t.startswith("☑"):
                if above:
                    break
                continue
            above.append(t)
        picked = list(reversed(above))
        if kind == "options":
            for l in lines[head:head + FOREIGN_OPTION_LINES]:
                t = clean_line(l)
                if not t or CHROME_RE.match(l):
                    break
                picked.append(t)
        if not picked:
            return "", "no question above the dialog footer"
    else:
        picked = [clean_line(lines[anchor])]
    for t in picked:
        why = line_ok(t)
        if why:
            return "", why
    return "\n".join(t if len(t) <= FOREIGN_LINE_SHOWN else t[:FOREIGN_LINE_SHOWN - 1] + "\u2026" for t in picked), ""


def foreign_waiting_source(cfg, events=None) -> dict:
    """The user's Claude sessions OUTSIDE this project that `claude agents --json` reports as waiting (T-0175, "Waiting for
    me"): name, tmux window (pane list + /proc walk), project label, the prompt's first line from a redacted capture. Only
    these few panes are captured (a capture costs real time with dozens of sessions), so a foreign dialog that `claude
    agents` still calls idle is not seen; this project's rows come from the sessions source, with its screen heuristics.
    A foreign row carries the project label only, never its cwd. `since` is filled by the Snapshot (first seen waiting).
    The page shows the window name for the human to attach to from a terminal; the panel names no tmux command."""
    agents = readonly.list_agents()
    roots = project_roots(cfg)
    waiting = [a for a in agents if str(a.get("status") or "") == "waiting" and a.get("name")
               and not under(str(a.get("cwd") or ""), roots)]
    if not waiting:
        return {"rows": []}
    pane_by_pid = pane_index(readonly.list_panes())
    projects = registered_projects()
    rows: list = []
    seen: set = set()
    for a in waiting:
        name = str(a.get("name"))
        if name in seen:
            continue
        seen.add(name)
        pane = walk_to_pane(a.get("pid"), pane_by_pid)
        tmux_name = pane["session"] if pane else None
        screen = redact(readonly.capture_pane(tmux_name)) if tmux_name else ""
        portion, why = foreign_dialog(screen) if screen else ("", "")
        label, registered = project_label(str(a.get("cwd") or ""), projects)
        ev = events.state(a.get("sessionId")) if events is not None else None      # a foreign event: time only, never text
        rows.append({"name": name, "tmux": tmux_name, "project": label, "registered": registered, "in_project": False,
                     "role": "", "ticket": "", "since": "", "since_exact": ev["since"] if ev and ev["waiting"] else "",
                     "started_at": a.get("startedAt"), "withheld": why,
                     "waiting_for": "waiting (screen withheld)" if why else (waiting_line(portion) or last_body_line(portion))})
    rows.sort(key=lambda r: (r["project"], r["name"]))
    return {"rows": rows}


def waiting_row(cfg, r: dict) -> dict:
    """A row of this project's waiting list in the shape of the "Waiting for me" page."""
    return {"name": r["name"], "tmux": r.get("tmux"), "project": cfg.name, "registered": True, "in_project": True,
            "role": r.get("role", ""), "ticket": r.get("ticket", ""), "waiting_for": r.get("waiting_for", ""),
            "since": r.get("since", ""), "started_at": r.get("started_at"), "last_reply": r.get("last_reply", ""),
            "last_event": r.get("last_event", "")}


def apply_events(row: dict, events, session_id) -> dict:
    """The hook events of this session (T-0163) decide its waiting state and carry its last reply: exact and instant.
    Without any event for the session the screen heuristic stands. `claude agents` saying waiting still wins."""
    ev = events.state(session_id) if events is not None else None
    row["last_event"], row["last_event_at"], row["last_reply"], row["last_reply_at"] = "", "", "", ""
    if not ev:
        return row
    row["last_event"], row["last_event_at"] = ev["last_event"], ev["last_at"]
    row["last_reply"], row["last_reply_at"] = ev["last_reply"], ev["last_reply_at"]
    if row.get("status") == "busy":
        return row
    if ev["waiting"]:
        row["state"] = "waiting"
        row["waiting_for"] = ev["waiting_for"] or row.get("waiting_for") or row.get("last_line", "")
        row["since_exact"] = ev["since"]
    elif row.get("status") != "waiting":
        row["state"] = "idle" if row["state"] == "waiting" else row["state"]
        row["waiting_for"] = ""
    return row


def sessions_source(cfg, events=None) -> dict:
    agents = readonly.list_agents()
    panes = readonly.list_panes()
    pane_by_pid = pane_index(panes)
    pane_cmd = {p["session"]: p.get("command", "") for p in panes}
    reg = readonly.registry_entries(cfg)
    reg_by_name = {e["name"]: e for e in reg}
    roles = cfg.role_sessions()
    role_by_name = {s.name: r for r, s in roles.items() if s.name}
    role_by_tmux = {s.tmux: r for r, s in roles.items() if s.tmux}
    roots = project_roots(cfg)

    def mine(cwd: str) -> bool:
        return under(cwd, roots)

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
        kind, waiting_for = prompt_shape(screen)
        if status == "waiting" or (kind and status != "busy"):      # `claude agents` says so, or a real prompt is on screen
            state = "waiting"
            waiting_for = waiting_for or last_body_line(screen)
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
        apply_events(rows[-1], events, a.get("sessionId"))
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


def sso_source(cfg) -> dict:
    """Who waits on the AWS login (T-0195): the open tickets whose newest log line is `waits-on: aws-sso -- resume: <step>`."""
    return {"waiting": readonly.waits_on(cfg, "aws-sso")}


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


def hook_status(events=None, hook_port: int = 0) -> dict:
    installed, path = panelstate.hook_installed()
    return {"installed": installed, "path": path, "snippet": panelstate.hook_snippet(), "hook_port": hook_port,
            "events_seen": events.count() if events is not None else 0,
            "note": "add the snippet yourself: a session must not edit your settings. The listener is 127.0.0.1 only; "
                    "events are data (a session's last reply, what it waits for, since when), the panel never acts on one"}


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
        self.events = None                         # the hook EventStore, attached by the server (T-0163)
        self.receipts = None                       # the ReceiptStore, attached by the server (T-0162)
        self.hook_port = 0
        self.src = {
            "inbox": {"fn": inbox_source, "ttl": r}, "board": {"fn": board_source, "ttl": r},
            "sessions": {"fn": lambda c: sessions_source(c, self.events), "ttl": r},
            "foreign_waiting": {"fn": lambda c: foreign_waiting_source(c, self.events), "ttl": r},
            "creds": {"fn": creds_source, "ttl": 60}, "sso": {"fn": sso_source, "ttl": r},
            "logs": {"fn": logs_source, "ttl": 20}, "releases": {"fn": releases_source, "ttl": 60},
            "freeze": {"fn": freeze_source, "ttl": r}, "trunk": {"fn": trunk_sha, "ttl": 60},
        }
        for s in self.src.values():
            s.update({"data": None, "at": 0.0, "error": None, "loaded": False, "lock": threading.Lock()})
        self.stop_event = threading.Event()
        self.thread = None
        self.creds_definite = None                 # the last definite credentials state seen (ok / LOGIN / ERR; never ? or n/a), T-0195
        # when each session was first seen waiting, per source (derived from the refreshes; forgotten when it stops waiting)
        self.waiting_since: dict = {"sessions": {}, "foreign_waiting": {}}

    def attach_events(self, store, hook_port: int) -> None:
        """The hook listener's store: an event invalidates the session sources so the next tick re-reads them."""
        self.events, self.hook_port = store, hook_port
        store.listeners.append(self.invalidate_sessions)

    def attach_receipts(self, store) -> None:
        self.receipts = store

    def receipts_meta(self) -> dict:
        if self.receipts is None:
            return {"write_path": False, "token_configured": False, "pollers": 0, "polling": False, "presence": "n/a", "accepted_1m": 0,
                    "refused_1m": 0, "pending": 0, "total": 0, "recent": [], "rate": {}}
        return self.receipts.meta()

    def invalidate_sessions(self) -> None:
        for name in ("sessions", "foreign_waiting"):
            self.src[name]["at"] = 0.0

    def mark_waiting(self, data: dict, source: str = "sessions") -> dict:
        now = panelstate.now_iso()
        seen_since = self.waiting_since[source]
        names = set()
        for r in (data or {}).get("rows", []):
            if r.get("state", "waiting") == "waiting":
                names.add(r["name"])
                exact = r.pop("since_exact", "")
                r["since"] = exact or seen_since.setdefault(r["name"], now)      # a hook event's time is exact (T-0163)
                if exact:
                    seen_since[r["name"]] = exact
            else:
                r.pop("since_exact", None)
                r["since"] = ""
        for n in [n for n in seen_since if n not in names]:
            seen_since.pop(n, None)
        return data

    # -- refresh --------------------------------------------------------------------------------------
    def refresh(self, name: str) -> None:
        s = self.src[name]
        if not s["lock"].acquire(blocking=False):
            return
        try:
            try:
                before = s["data"]
                s["data"] = s["fn"](self.cfg)
                if name in self.waiting_since:
                    s["data"] = self.mark_waiting(s["data"], name)
                if name == "creds":
                    self.creds_transition(before, s["data"])
                s["error"] = None
            except Exception as e:  # noqa: BLE001 - a failing source is shown as an error, never a crash
                s["error"] = f"{type(e).__name__}: {e}"[:300]
            s["at"] = time.time()
            s["loaded"] = True
        finally:
            s["lock"].release()

    def creds_transition(self, before, after, emit: bool = True) -> None:
        """T-0195: the credentials probe (every CREDS_TTL_S, the refresher) saw the login come back (the last DEFINITE state
        was LOGIN, the new one is ok; a `?` from a slow first probe after `aws sso login` in between does not hide it):
        the human logged in without clicking. One synthetic `creds` event goes through the poll, pending like a receipt
        until the desk acknowledges it (for at most an hour), so the desk resumes the waiting sessions the same way as
        after a click. An sso click's own probe calls this with emit=False: the click is the receipt."""
        now = (after or {}).get("left") if isinstance(after, dict) else None
        if now not in ("ok", "LOGIN", "ERR"):
            return
        prev, self.creds_definite = self.creds_definite, now
        if emit and prev == "LOGIN" and now == "ok" and self.receipts is not None:
            self.receipts.emit_event("creds", "LOGIN -> ok: the login came back without a click", panel_saw="ok")

    def creds_seen(self, probe: dict) -> None:
        """An sso click re-probed the credentials: the header's source takes the fresh answer at once (under the source's
        lock, so a refresher mid-probe cannot overwrite it with an older one afterwards) and the definite state follows,
        without emitting a creds event -- the click itself is the receipt the desk acts on."""
        s = self.src["creds"]
        with s["lock"]:
            s["data"], s["error"], s["at"], s["loaded"] = probe, None, time.time(), True
            self.creds_transition(None, probe, emit=False)

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
        foreign, _, foreign_err = self.get("foreign_waiting", fresh)
        creds, _, _ = self.get("creds", fresh)
        logs, _, _ = self.get("logs", fresh)
        releases, _, _ = self.get("releases", fresh)
        freeze, _, _ = self.get("freeze", fresh)
        trunk, _, _ = self.get("trunk", fresh)
        sso, _, sso_err = self.get("sso", fresh)
        sso_rows = (sso or {}).get("waiting", [])
        creds_left = str((creds or {}).get("left") or "?")
        inbox = inbox or {"items": [], "count": None, "p0": 0}
        board = board or {"cards": [], "lanes": {n: [] for n, _ in LANES}, "chips": {k: 0 for k in CHIPS}, "counts": {}, "other": {},
                          "p0": [], "newest_plan": "", "void_plans": [], "source": "", "warnings": [], "total": 0}
        rows = (sessions or {}).get("rows", [])
        now = readonly.now_local(cfg)
        waiting = [r for r in rows if r["state"] == "waiting"]
        foreign_rows = (foreign or {}).get("rows", [])
        waiting_all = [waiting_row(cfg, r) for r in waiting] + foreign_rows
        by_state: dict = {}
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        desk_sess = cfg.sessions.get("desk")
        desk_row = next((r for r in rows if r["role"] == "desk"), None)
        counts = board["counts"]
        receipts = self.receipts_meta()
        return {
            "generated": panelstate.now_iso(), "version": panelstate.version(), "loading": self.loading(),
            "project": {"name": cfg.name, "short": cfg.short, "root": cfg.root, "trunk": cfg.trunk, "prod": cfg.prod, "trunk_sha": trunk or ""},
            "panel": {"port": self.port, "bind": self.bind, "refresh_s": cfg.panel.get("refresh_s", 30), "started_at": self.started_at,
                      "version": panelstate.version(), "write_path": receipts["write_path"], "receipts": receipts,
                      "note": "a click is a receipt the desk fetches and verifies before acting; tier 2 is typed in the desk's window"},
            "header": {"trunk_sha": trunk or "", "freeze": freeze or {"on": False, "ids": []}, "creds": creds or {"left": "?", "source": ""},
                       "desk": {"name": desk_sess.name if desk_sess else "", "tmux": desk_sess.tmux if desk_sess else "",
                                "state": desk_row["state"] if desk_row else "gone", "model": desk_row["model"] if desk_row else "?",
                                "last_line": desk_row["last_line"] if desk_row else "", "last_reply": desk_row.get("last_reply", "") if desk_row else "",
                                "presence": receipts["presence"], "polling": receipts["polling"]}},
            "kpis": {"inbox": inbox.get("count"), "p0": inbox.get("p0", 0), "p0_open_tickets": len(board["p0"]),
                     "waiting": by_state.get("waiting", 0), "working": by_state.get("working", 0), "idle": by_state.get("idle", 0),
                     "gone": by_state.get("gone", 0), "shell": by_state.get("shell", 0), "waiting_elsewhere": len(foreign_rows),
                     "to_deploy": len(board["lanes"].get("to deploy", [])), "deployed": counts.get("deployed", 0),
                     "sessions": len(rows), "tickets": board["total"]},
            "p0": board["p0"], "inbox": {"items": inbox["items"], "count": inbox.get("count"), "error": inbox_err},
            "board": {k: board[k] for k in ("lanes", "chips", "counts", "other", "newest_plan", "void_plans", "source", "warnings", "total")},
            "tickets": board["cards"], "sessions": {"rows": rows, "error": sess_err}, "waiting": waiting,
            "waiting_all": {"rows": waiting_all, "elsewhere": len(foreign_rows), "error": foreign_err},
            "creds": creds, "logs": logs or {}, "releases": releases or [], "live_checks": live_checks(cfg, board),
            # T-0195: the SSO updated button: shown when the login is LOGIN / ERR / ? (a probe that ran, on a project with an aws
            # block) or anyone waits on it; hidden when ok and nobody waits, while the source warms up, and on a project without AWS
            "sso": {"waiting": sso_rows, "creds": creds_left,
                    "show_button": bool(sso_rows) or (cfg.aws is not None and creds is not None and creds_left in ("LOGIN", "ERR", "?")),
                    "profile": str((cfg.aws or {}).get("profile") or "default") if cfg.aws else "", "error": sso_err,
                    "marker": "waits-on: aws-sso -- resume: <exact step>"},
            "config": {k: v for k, v in cfg.to_dict().items() if k != "warnings"}, "hook": hook_status(self.events, self.hook_port),
            "terminal": panelstate.term_info(cfg),
            "sources": self.sources_meta(), "errors": {"inbox": inbox_err, "board": board_err, "sessions": sess_err, "foreign_waiting": foreign_err},
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

    def waiting_detail(self, name: str, fresh: bool = False):
        """One row of the "Waiting for me" list with its redacted screen; None when that session is not waiting now."""
        mine = ((self.get("sessions", fresh)[0] or {}).get("rows", []))
        row = next((r for r in mine if r["name"] == name and r.get("state") == "waiting"), None)
        if row:
            d = waiting_row(self.cfg, row)
        else:
            rows = ((self.get("foreign_waiting", fresh)[0] or {}).get("rows", []))
            row = next((r for r in rows if r["name"] == name), None)
            if not row:
                return None
            d = dict(row)
            screen = redact(readonly.capture_pane(row["tmux"])) if row.get("tmux") else ""
            d["screen"], d["withheld"] = foreign_dialog(screen) if screen else ("", "")
            return d
        d["screen"] = redact(readonly.capture_pane(row["tmux"])) if row.get("tmux") else ""
        d["withheld"] = ""
        return d

    def session(self, name: str, fresh: bool = False):
        rows = ((self.get("sessions", fresh)[0] or {}).get("rows", []))
        row = next((r for r in rows if r["name"] == name), None)
        if not row:
            return None
        d = dict(row)
        d["screen"] = redact(readonly.capture_pane(row["tmux"])) if row.get("tmux") else ""
        d["registry"] = readonly.registry_entry(self.cfg, name)
        return d
