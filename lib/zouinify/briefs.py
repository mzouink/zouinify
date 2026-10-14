"""Brief templates (templates/briefs/*.md.tmpl) rendered with the project's config: `{{key}}` placeholders
only, no logic. `zouinify briefs render --out .ops/briefs` writes desk/manager/debug/chrome_debug briefs
(the worker template is the model for the project's tickets/WORKER_BRIEF.md)."""
from __future__ import annotations

import os
import re
import sys

from zouinify import ZouinifyError
from zouinify.config import ROLE_SESSIONS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = os.path.join(REPO, "templates", "briefs")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
SUFFIX = ".md.tmpl"


def list_templates() -> list:
    if not os.path.isdir(TEMPLATES):
        return []
    return sorted(f[:-len(SUFFIX)] for f in os.listdir(TEMPLATES) if f.endswith(SUFFIX))


def context(cfg) -> dict:
    desk = cfg.sessions.get("desk")
    b = cfg.budget or {}
    if b.get("paid_usd_per_day") is not None:
        budget = (f"{b['paid_usd_per_day']} USD/day of paid runs, approved once by the human in the inbox"
                  + (f" as `{b['approval_text']}`" if b.get("approval_text") else ""))
    else:
        budget = "no paid-run budget is configured: every paid run needs the human's own typed OK"
    ctx = {
        "project.name": cfg.name, "project.short": cfg.short, "project.root": cfg.root,
        "trunk": cfg.trunk, "prod": cfg.prod or "(no production branch configured)",
        "tickets_dir": cfg.tickets_dir, "tickets_script": cfg.tickets_script, "worktrees": cfg.worktrees,
        "ask_verbs": ", ".join(cfg.ask_verbs), "model": cfg.model or (desk.model if desk else "the configured model"),
        "human_only": "\n".join(f"  - {x}" for x in cfg.human_only) or "  - (nothing listed under human_only in .ops/config.yaml)",
        "budget": budget,
        "repos": "\n".join(f"  - {r['name']}: {r['path']} (trunk {r['trunk']}" + (f", prod {r['prod']})" if r.get("prod") else ")")
                           for r in cfg.repos),
        "human": str((cfg.contact or {}).get("human") or "the human"),
        "aws_profile": str((cfg.aws or {}).get("profile") or "default"),
    }
    for role in ROLE_SESSIONS + ("worker",):
        s = cfg.sessions.get(role)
        ctx[f"{role}.name"] = s.name if s and s.name else f"({role}: not configured)"
        ctx[f"{role}.tmux"] = s.tmux if s and s.tmux else f"({role}: not configured)"
    return ctx


def render(text: str, ctx: dict) -> tuple:
    missing = []

    def sub(m):
        k = m.group(1)
        if k in ctx:
            return ctx[k]
        missing.append(k)
        return m.group(0)
    return VAR_RE.sub(sub, text), sorted(set(missing))


def cmd_list(out=None) -> int:
    out = out or sys.stdout
    for name in list_templates():
        print(f"{name:<14} {os.path.join(TEMPLATES, name + SUFFIX)}", file=out)
    return 0


def cmd_render(cfg, out_dir: str | None = None, force: bool = False, out=None) -> int:
    out = out or sys.stdout
    names = list_templates()
    if not names:
        raise ZouinifyError(f"no templates under {TEMPLATES}")
    out_dir = cfg.path(out_dir or os.path.join(".ops", "briefs"))
    ctx = context(cfg)
    rendered = {}
    for name in names:
        with open(os.path.join(TEMPLATES, name + SUFFIX), encoding="utf-8") as fh:
            text, missing = render(fh.read(), ctx)
        if missing:
            raise ZouinifyError(f"{name}{SUFFIX}: unknown placeholder(s) {', '.join('{{' + m + '}}' for m in missing)}")
        rendered[name] = text
    conflicts = []
    for name, text in rendered.items():
        target = os.path.join(out_dir, name + ".md")
        if os.path.exists(target) and not force:
            with open(target, encoding="utf-8") as fh:
                if fh.read() != text:
                    conflicts.append(target)
    if conflicts:
        raise ZouinifyError("refusing to overwrite edited briefs (use --force):\n  " + "\n  ".join(conflicts))
    os.makedirs(out_dir, exist_ok=True)
    for name, text in rendered.items():
        target = os.path.join(out_dir, name + ".md")
        verb = "wrote"
        if os.path.exists(target):
            with open(target, encoding="utf-8") as fh:
                verb = "unchanged" if fh.read() == text else "overwrote"
        if verb != "unchanged":
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
        print(f"{verb:<10} {target}", file=out)
    return 0
