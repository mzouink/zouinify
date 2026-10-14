"""Brief templates (templates/briefs/*.md.tmpl) rendered with the project's config: `{{key}}` placeholders only, no
logic. Two sources make a brief: the template, which holds what every project's desk, manager, debug, chrome_debug or
worker needs, and the project's own notes, `<root>/.ops/briefs.project/<role>.md`, spliced verbatim into
`{{project.notes}}` (the brief analogue of `.ops/README.project.md`; a stub says where to write them while the file is
missing). Only what the config uses is rendered: a role template when `sessions.<role>.brief` is set, written to that
path (or to `<--out>/<role>.md` for a scratch render), the worker template only when `sessions.worker.brief` is (without
it the worker reads the protocol's tickets/WORKER_BRIEF.md, for which the worker template is the model).

A rendered brief is self-certifying. The renderer puts a header in front of every brief, one blockquote line naming the
two sources as the only places to edit and ending with `Body sha256 <16 hex>`, the digest of the text below it (the
pattern of tickets/ticket.env's `generated-from ... sha256` line), then a blank line. Render decides per existing target
from that: a header that is word for word the renderer's, names the target's own role and carries the body's digest is
an untouched generated file, so a changed template, notes or config renders straight through (`overwrote`, no flag); a
missing, reworded or garbled header, one naming another role, or a body that no longer matches, is a hand edit, refused
until it is folded back into a source, and `--force` is for that case only. A hand edit that
also recomputes the digest passes: the header guards against a hand edit made by accident, which is accepted. The
output is deterministic (the same sources give the same bytes)."""
from __future__ import annotations

import errno
import os
import re
import stat
import sys

from zouinify import ZouinifyError, generated
from zouinify.config import ROLE_SESSIONS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = os.path.join(REPO, "templates", "briefs")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
SUFFIX = ".md.tmpl"
NOTES_DIR_REL = os.path.join(".ops", "briefs.project")
# the generated-file header (zouinify.generated, shared with the protocol renderer since T-0219): the briefs' wording and bytes
# are those of T-0206/T-0207 -- the tool, the two sources per role, "one of those two", "brief"
TOOL = "zouinify briefs render"
NOUN, THOSE = "brief", "one of those two"
NOTES_STUB = ("_No project notes yet._ Write `.ops/briefs.project/{role}.md` (what only this project knows: its launch flags "
              "and dated decisions, its deploy and evidence commands, its lessons) and run `zouinify briefs render` again; it is "
              "spliced in here verbatim.")


def list_templates() -> list:
    if not os.path.isdir(TEMPLATES):
        return []
    return sorted(f[:-len(SUFFIX)] for f in os.listdir(TEMPLATES) if f.endswith(SUFFIX))


def template_path(name: str) -> str:
    return os.path.join(TEMPLATES, name + SUFFIX)


body_digest = generated.body_digest


def sources(role: str) -> str:
    return f"`templates/briefs/{role}.md.tmpl` and `.ops/briefs.project/{role}.md`"


def header(role: str, body: str) -> str:
    """The first two lines of every rendered brief: one blockquote line naming the two sources and carrying the digest
    of `body`, then a blank line."""
    return generated.header(TOOL, sources(role), NOUN, THOSE, body)


def untouched_render(text: str, role: str) -> bool:
    """True when `text` is the brief this renderer wrote for `role` and nobody edited it: the header is exactly ours, names
    that role (a manager brief copied over desk.md is not an untouched desk brief) and its digest is the body's."""
    return generated.untouched(text, TOOL, sources(role), NOUN, THOSE)


def notes_rel(role: str) -> str:
    return f".ops/briefs.project/{role}.md"


def notes_file(root: str, role: str) -> str | None:
    """The path of the project's notes for a role, or None when there is none. The directory and the file must be plain
    paths inside the realpath'd project root (the posture of initcmd.check_targets): a symlink, a non-file or anything
    resolving elsewhere is refused, never followed, because its text becomes part of the prompt a session is launched with."""
    root = os.path.realpath(root)
    d = os.path.join(root, NOTES_DIR_REL)
    rel = notes_rel(role)
    if os.path.islink(d):
        raise ZouinifyError(f"{NOTES_DIR_REL} is a symlink; project notes live under the project root only")
    if os.path.lexists(d) and not os.path.isdir(d):
        raise ZouinifyError(f"{NOTES_DIR_REL} exists and is not a directory")
    p = os.path.join(d, role + ".md")
    if os.path.islink(p):
        raise ZouinifyError(f"{rel} is a symlink; project notes live under the project root only")
    if not os.path.lexists(p):
        return None
    if not os.path.isfile(p):
        raise ZouinifyError(f"{rel} exists and is not a regular file")
    real = os.path.realpath(p)
    if not real.startswith(root + os.sep):
        raise ZouinifyError(f"{rel} resolves outside the project root ({real}); refusing to read it")
    return p


def project_notes(root: str, role: str) -> str:
    """The notes spliced verbatim (one trailing newline stripped, nothing else touched: a `{{...}}` in them stays as
    written), or the stub when the project has not written them yet. The file is opened O_NOFOLLOW and the open
    descriptor must be the regular file notes_file() checked (same device and inode): a swap for a link or a non-file
    between the check and the open is caught. A same-uid writer changing the bytes meanwhile is out of scope: it can
    write the file directly, so no check here would add anything."""
    p = notes_file(root, role)
    if p is None:
        return NOTES_STUB.format(role=role)
    rel = notes_rel(role)
    try:
        checked = os.lstat(p)
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ZouinifyError(f"{rel} is a symlink; project notes live under the project root only") from None
        raise ZouinifyError(f"{rel}: {e.strerror}") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise ZouinifyError(f"{rel} changed while it was being read; run again")
        with os.fdopen(fd, encoding="utf-8") as fh:
            fd = -1
            text = fh.read()
    except UnicodeDecodeError:
        raise ZouinifyError(f"{rel}: not UTF-8 text") from None
    finally:
        if fd >= 0:
            os.close(fd)
    return text[:-1] if text.endswith("\n") else text


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


def role_context(cfg, name: str, ctx: dict | None = None) -> dict:
    """The shared context plus this role's `project.notes` (read from the project root, whatever --out says)."""
    return {**(context(cfg) if ctx is None else ctx), "project.notes": project_notes(cfg.root, name)}


def render(text: str, ctx: dict) -> tuple:
    missing = []

    def sub(m):
        k = m.group(1)
        if k in ctx:
            return ctx[k]
        missing.append(k)
        return m.group(0)
    return VAR_RE.sub(sub, text), sorted(set(missing))


def render_template(name: str, ctx: dict) -> tuple:
    """(the rendered brief with its header first, the placeholders the context lacks)."""
    with open(template_path(name), encoding="utf-8") as fh:
        text, missing = render(fh.read(), ctx)
    return header(name, text) + text, missing


def plan(briefs: dict, tickets_dir: str = "tickets") -> tuple:
    """Which templates a config uses: `briefs` maps a session role to its configured `brief` path (or None). Returns
    ([names to render], [(name, why it is skipped)]); the worker template is skipped without `sessions.worker.brief`,
    because the worker's prompt points it at the protocol's WORKER_BRIEF.md instead."""
    todo, skipped = [], []
    for name in list_templates():
        if briefs.get(name):
            todo.append(name)
        elif name == "worker":
            skipped.append((name, f"sessions.worker has no brief: the worker reads {tickets_dir}/WORKER_BRIEF.md"))
        else:
            skipped.append((name, f"sessions.{name} has no brief"))
    return todo, skipped


def plan_for(cfg) -> tuple:
    """plan() for a loaded config, with the targets checked: two roles may not share one brief path (the second render would
    overwrite the first), and no brief may live under .ops/briefs.project/ (--force would overwrite the project's notes with
    a render of them). The config already refuses a brief outside the project."""
    todo, skipped = plan({role: s.brief for role, s in cfg.sessions.items()}, cfg.tickets_dir)
    # the notes directory as a path under the real root, not resolved: a symlinked notes directory is notes_file's refusal,
    # and resolving it here would make every brief under its target look like it lived among the notes
    notes_dir = os.path.join(os.path.realpath(cfg.root), NOTES_DIR_REL)
    seen = {}
    for name in todo:
        brief = cfg.sessions[name].brief
        real = os.path.realpath(cfg.path(brief))
        if real == notes_dir or real.startswith(notes_dir + os.sep):
            raise ZouinifyError(f"sessions.{name}.brief: {brief} is under {NOTES_DIR_REL}/, the project's notes; a rendered brief "
                                f"cannot live there (its render would overwrite the notes)")
        if real in seen:
            raise ZouinifyError(f"sessions.{name}.brief and sessions.{seen[real]}.brief are the same file ({brief}); every role "
                                f"renders its own template, so each needs its own path")
        seen[real] = name
    return todo, skipped


def check_target(target: str) -> None:
    """A brief is written to a plain path only: a symlinked target or parent directory, or a target that is not a
    regular file, is refused before anything is written, --force or not (the posture of initcmd.check_targets). Only the
    immediate parent is checked: a symlink higher up a --out path (`--out link/sub`) is followed, because --out is the
    caller's own choice of directory; the configured brief paths are already confined to the project by the config."""
    parent = os.path.dirname(target)
    if os.path.islink(parent):
        raise ZouinifyError(f"{parent} is a symlink; refusing to write briefs through it")
    if os.path.lexists(parent) and not os.path.isdir(parent):
        raise ZouinifyError(f"{parent} exists and is not a directory")
    if os.path.islink(target):
        raise ZouinifyError(f"{target} is a symlink; refusing to write through it (--force does not either)")
    if os.path.lexists(target) and not os.path.isfile(target):
        raise ZouinifyError(f"{target} exists and is not a regular file")


def write_brief(target: str, text: str) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def cmd_list(out=None) -> int:
    out = out or sys.stdout
    for name in list_templates():
        print(f"{name:<14} {template_path(name)}", file=out)
    print(f"{'notes':<14} <project>/{NOTES_DIR_REL}/<role>.md  (the project's own, spliced into {{{{project.notes}}}}; a stub when absent)",
          file=out)
    return 0


def cmd_render(cfg, out_dir: str | None = None, force: bool = False, out=None) -> int:
    out = out or sys.stdout
    if not list_templates():
        raise ZouinifyError(f"no templates under {TEMPLATES}")
    scratch = cfg.path(out_dir) if out_dir else None
    todo, skipped = plan_for(cfg)
    if scratch is not None:
        notes_dir = os.path.join(os.path.realpath(cfg.root), NOTES_DIR_REL)
        real = os.path.realpath(scratch)
        if real == notes_dir or real.startswith(notes_dir + os.sep):
            raise ZouinifyError(f"--out {out_dir}: that is {NOTES_DIR_REL}/, the project's notes; a render cannot go there "
                                f"(--force would overwrite the notes with a render of them)")
    ctx = context(cfg)
    rendered, targets = {}, {}
    for name in todo:
        text, missing = render_template(name, role_context(cfg, name, ctx))
        if missing:
            raise ZouinifyError(f"{name}{SUFFIX}: unknown placeholder(s) {', '.join('{{' + m + '}}' for m in missing)}")
        rendered[name] = text
        targets[name] = os.path.join(scratch, name + ".md") if scratch else cfg.path(cfg.sessions[name].brief)
    verbs, conflicts = {}, []
    for name, text in rendered.items():
        target = targets[name]
        check_target(target)
        if not os.path.lexists(target):
            verbs[name] = "wrote"
            continue
        with open(target, encoding="utf-8", errors="replace") as fh:
            current = fh.read()
        if current == text:
            verbs[name] = "unchanged"
        elif force or untouched_render(current, name):
            verbs[name] = "overwrote"
        else:
            conflicts.append(target)
    if conflicts:
        raise ZouinifyError("refusing to overwrite hand-edited briefs (no generated header, a reworded one or one naming another "
                            "role, or a body that no longer matches its digest): fold the edit into templates/briefs/<role>.md.tmpl "
                            "or .ops/briefs.project/<role>.md, then render; --force overwrites them (use --force):\n  "
                            + "\n  ".join(conflicts))
    for name, text in rendered.items():
        target = targets[name]
        if verbs[name] != "unchanged":
            os.makedirs(os.path.dirname(target), exist_ok=True)
            write_brief(target, text)
        print(f"{verbs[name]:<10} {target}", file=out)
    for name, why in skipped:
        stale = os.path.join(scratch or cfg.path(os.path.join(".ops", "briefs")), name + ".md")
        print(f"{'skipped':<10} {name} ({why})" + (f"; {stale} is not touched" if os.path.lexists(stale) else ""), file=out)
    return 0
