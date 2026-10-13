"""`zouinify init --protocol [--dry-run]`: install the ticket protocol into a repository that already has `.ops/config.yaml`
(T-0185, the second half of the design's Z-9).

It writes, under the realpath'd repository root only and never over an existing file: `scripts/ticket.sh` (the vendored core,
byte-identical to the pinned upstream commit, mode 755), `tickets/ticket.env` (this project's values, the parsed grammar),
`tickets/plugins/none/` (the no-op deploy plugin with its docs), `tickets/README.md`, `tickets/_TEMPLATE.md` and
`tickets/WORKER_BRIEF.md` (rendered), and the worktrees line in `.gitignore`. Every one of those is a tracked file, so the
run shows the plan first and goes on only after the human types `yes` on that terminal: never `--non-interactive`, never
inside a Claude session. That gate stops accidents (a session, a pipe, a relayed answer); it cannot stop a deliberate
process of the same user, which is why `CLAUDE.md` -- the one file that changes what every future session is told -- is
NEVER written here (desk decision, T-0185 review): the marked block is printed, and the human pastes it themselves.
`--dry-run` prints the plan anywhere. init commits nothing: the human commits.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from zouinify import ZouinifyError
from zouinify import config as cfgmod
from zouinify import initcmd, protocol, ticketenv
from zouinify.panelctl import in_claude_session

MARK_START = "<!-- zouinify:protocol start -->"
MARK_END = "<!-- zouinify:protocol end -->"
CLAUDE_TMPL = os.path.join(protocol.PROTOCOL, "CLAUDE.section.md.tmpl")
CORE_REL = os.path.join("scripts", "ticket.sh")


def claude_block(cfg, fragments, sections) -> str:
    ctx = protocol.context(cfg, fragments, sections)
    with open(CLAUDE_TMPL, encoding="utf-8") as fh:
        text, missing = protocol.briefsmod.render(fh.read(), ctx)
    if missing:
        raise ZouinifyError(f"CLAUDE.section.md.tmpl: unknown placeholder(s) {', '.join(missing)}")
    return text.rstrip("\n") + "\n"


def claude_state(root: str) -> str:
    """Informational only (init never writes CLAUDE.md): absent | present (both markers) | malformed (one marker) | missing-block."""
    p = os.path.join(root, "CLAUDE.md")
    if not os.path.lexists(p):
        return "absent"
    if not os.path.isfile(p):
        return "missing-block"
    with open(p, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    has_start, has_end = MARK_START in text, MARK_END in text
    if has_start and has_end:
        return "present"
    if has_start or has_end:
        return "malformed"
    return "missing-block"


def line_state(root: str, line: str) -> str:
    p = os.path.join(root, ".gitignore")
    if os.path.islink(p):
        return "symlink"
    if not os.path.lexists(p):
        return "create"
    if not os.path.isfile(p) or os.stat(p).st_nlink > 1:
        return "skip"
    with open(p, encoding="utf-8", errors="replace") as fh:
        lines = [ln.strip() for ln in fh.read().splitlines()]
    covers = {line, line.rstrip("/"), "/" + line, "/" + line.rstrip("/")}
    top = line.split("/")[0]
    if top and top != line.rstrip("/"):
        covers |= {top, top + "/", "/" + top, "/" + top + "/"}            # `.claude/` already covers `.claude/worktrees/`
    return "exists" if any(ln in covers for ln in lines) else "append"


def check_supported(cfg) -> None:
    if cfg.tickets_dir != "tickets":
        raise ZouinifyError(f"the vendored core reads tickets/ticket.env and tickets/plugins/ at fixed paths; tickets.dir is "
                            f"{cfg.tickets_dir!r} -- set it to tickets")
    if cfg.tickets_script != CORE_REL:
        raise ZouinifyError(f"tickets.script is {cfg.tickets_script!r}; init --protocol installs {CORE_REL}")
    seen = {}
    for r in cfg.repos:                                       # the core refuses two repos that resolve to one directory; say it first
        real = os.path.realpath(cfg.path(r["path"]))
        if real in seen:
            raise ZouinifyError(f"repos {seen[real]} and {r['name']} resolve to the same directory ({real}); the core refuses that")
        seen[real] = r["name"]
    default = next((r for r in cfg.repos if r.get("default")), cfg.repos[0])
    if os.path.realpath(cfg.path(default["path"])) != cfg.root:
        raise ZouinifyError(f"the default repo's path ({default['path']}) must be the repository root itself: the core reads the "
                            f"tickets from the checkout it runs in")
    if cfg.deploy_plugin != "none" and not os.path.isdir(os.path.join(protocol.PLUGINS, cfg.deploy_plugin)):
        raise ZouinifyError(f"tickets.deploy_plugin is {cfg.deploy_plugin!r}, which zouinify does not ship (only `none` is public; a "
                            f"project's deploy plugin lives in its own repository). Set deploy_plugin: none for the install, then add "
                            f"tickets/plugins/{cfg.deploy_plugin}/plugin.sh yourself: it must define every hook of PLUGIN_HOOKS in "
                            f"scripts/ticket.sh (copy plugins/none/plugin.sh to start) and a docs/ directory with the same file names as "
                            f"plugins/none/docs/; then set deploy_plugin back and regenerate ticket.env (zouinify config --env).")


def check_target_dirs(root: str, rels: list) -> None:
    """Before anything is written: every existing component on the way to a target is a real directory inside the root
    (a symlinked tickets/ or scripts/ would carry the files elsewhere; initcmd.resolve_root does the same for .ops)."""
    for rel in rels:
        parts = rel.split(os.sep)[:-1]
        cur = root
        for part in parts:
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                raise ZouinifyError(f"{os.path.relpath(cur, root)} is a symlink; init --protocol writes under the repository root only")
            if os.path.lexists(cur) and not os.path.isdir(cur):
                raise ZouinifyError(f"{os.path.relpath(cur, root)} exists and is not a directory")
            if os.path.lexists(cur) and not initcmd.inside(root, cur):
                raise ZouinifyError(f"{os.path.relpath(cur, root)} resolves outside the repository root; refusing")


def build_plan(cfg, out) -> list:
    """[(rel, content_bytes, mode, state)] in write order; state write|exists; the CLAUDE.md and .gitignore items last."""
    items = []
    core = os.path.join(protocol.PROTOCOL, CORE_REL)
    with open(core, "rb") as fh:
        items.append((CORE_REL, fh.read(), 0o755))
    items.append((os.path.join("tickets", "ticket.env"), ticketenv.render(cfg).encode("utf-8"), 0o644))
    plugins = ["none"] + ([cfg.deploy_plugin] if cfg.deploy_plugin != "none" else [])
    for name in plugins:
        pdir = os.path.join(protocol.PLUGINS, name)
        for dirpath, dirs, files in os.walk(pdir):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                rel = os.path.join("tickets", "plugins", os.path.relpath(full, protocol.PLUGINS))
                with open(full, "rb") as fh:
                    items.append((rel, fh.read(), 0o755 if f.endswith(".sh") else 0o644))
    rendered = protocol.render_all(cfg, out=out)
    for name in protocol.FILES:
        items.append((os.path.join("tickets", name), rendered[name].encode("utf-8"), 0o644))
    check_target_dirs(cfg.root, [rel for rel, _c, _m in items])
    plan = []
    for rel, content, mode in items:
        path = os.path.join(cfg.root, rel)
        if os.path.islink(path):
            raise ZouinifyError(f"{rel} is a symlink; init --protocol never writes through a link and never replaces one")
        plan.append((rel, content, mode, "exists" if os.path.lexists(path) else "write"))
    return plan


def print_plan(cfg, plan, block, gi_line, gi_state, cl_state, out) -> None:
    print(f"init --protocol plan for {cfg.root} (deploy plugin {cfg.deploy_plugin}; core pinned in templates/protocol/MANIFEST):", file=out)
    for rel, content, mode, state in plan:
        print(f"  {state:<8} {rel}  ({len(content)} bytes{', mode 755' if mode == 0o755 else ''})", file=out)
    print(f"  {gi_state:<8} .gitignore  (line `{gi_line}`)", file=out)
    print(f"  {'paste':<8} CLAUDE.md  (never written by init: " + {
        "absent": "no CLAUDE.md yet; create it with the block printed below",
        "present": "the block's markers are already there; nothing to paste",
        "malformed": "WARNING: only one of the two markers is present; repair the block by hand",
        "missing-block": "paste the block printed below at the end of the file"}[cl_state] + ")", file=out)
    print("  every file above is tracked: commit them yourself afterwards; init commits nothing", file=out)
    env_text = next(c for r, c, m, s in plan if r == os.path.join("tickets", "ticket.env")).decode("utf-8")
    print("\n--- tickets/ticket.env ---", file=out)
    out.write(env_text)
    print("--- CLAUDE.md block (paste it yourself; a session must never edit CLAUDE.md, and neither does init) ---", file=out)
    out.write(block)


def cmd_protocol(target: str | None, dry_run: bool = False, out=None) -> int:
    out = out or sys.stdout
    try:
        return _cmd_protocol(target, dry_run, out)
    except OSError as e:
        raise ZouinifyError(f"init --protocol: {e}") from None


def _cmd_protocol(target, dry_run, out) -> int:
    root = initcmd.resolve_root(target)
    if not os.path.isfile(os.path.join(root, cfgmod.CONFIG_REL)):
        raise ZouinifyError(f"no {cfgmod.CONFIG_REL} here: run `zouinify init` first, then `init --protocol`")
    cfg = cfgmod.load(root)
    check_supported(cfg)
    if not dry_run and in_claude_session():
        raise ZouinifyError("init --protocol writes tracked files: the human runs it in their own terminal and types yes there "
                            "(and pastes the printed CLAUDE.md block); a session may only show the plan with --dry-run")
    plan = build_plan(cfg, out)
    fragments = protocol.load_fragments(protocol.plugin_docs_dir(cfg, None))
    sections = protocol.read_project_readme(cfg, None)
    block = claude_block(cfg, fragments, sections)
    gi_line = cfg.worktrees.rstrip("/") + "/"
    gi_state = line_state(root, gi_line)
    cl_state = claude_state(root)
    print_plan(cfg, plan, block, gi_line, gi_state, cl_state, out)
    todo = [p for p in plan if p[3] == "write"]
    if not todo and gi_state in ("exists", "symlink", "skip"):
        print("\nnothing to write: the protocol is installed (init --protocol never overwrites)." +
              ("" if cl_state == "present" else " CLAUDE.md still lacks the block: paste it from above."), file=out)
        return 0
    if dry_run:
        print("\n(dry run: nothing written)", file=out)
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ZouinifyError("init --protocol asks for your yes on a real terminal; --dry-run shows the plan anywhere")
    try:
        answer = input("\nType yes to write the files marked `write` above (anything else writes nothing): ")
    except EOFError:
        answer = ""
    if answer.strip() != "yes":
        print("nothing written.", file=out)
        return 1
    created = []
    try:
        for rel, content, mode, state in plan:
            if state != "write":
                print(f"exists     {rel}  (left as is)", file=out)
                continue
            path = os.path.join(root, rel)
            check_target_dirs(root, [rel])                            # again, right before the write: nothing moved meanwhile
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not initcmd.inside(root, os.path.dirname(path)):
                raise ZouinifyError(f"refusing to write outside the repository root: {rel}")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            except FileExistsError:
                raise ZouinifyError(f"{rel} appeared meanwhile; nothing overwritten") from None
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.chmod(path, mode)
            created.append(rel)
            print(f"wrote      {rel}", file=out)
        if gi_state in ("create", "append"):
            p = os.path.join(root, ".gitignore")
            with os.fdopen(os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644), "a", encoding="utf-8") as fh:
                if gi_state == "append" and not initcmd._ends_with_newline(p):
                    fh.write("\n")
                fh.write(gi_line + "\n")
            print(f"{'created' if gi_state == 'create' else 'appended':<10} .gitignore  (`{gi_line}`)", file=out)
        else:
            print(f"{'exists' if gi_state == 'exists' else 'skipped':<10} .gitignore  (line `{gi_line}`" +
                  ("" if gi_state == "exists" else "; a symlink or hard link: add it yourself") + ")", file=out)
        print(f"{'paste':<10} CLAUDE.md  (not written: " + ("the block is already there" if cl_state == "present" else
                                                       "add the block printed above yourself") + ")", file=out)
    except BaseException:
        initcmd.undo(root, created, out)
        raise
    if CORE_REL in created:
        verify_core(root, out)
    else:
        print(f"verify     skipped: {CORE_REL} was already here and is not this run's to execute", file=out)
    print(f"\nnext: paste the CLAUDE.md block printed above (init never edits CLAUDE.md), review and commit the files "
          f"(`git add {CORE_REL} tickets .gitignore CLAUDE.md && git commit`), then `{cfg.tickets_script} board`. Changing .ops/config.yaml "
          f"later: `zouinify config --env` and commit tickets/ticket.env through a ticket.", file=out)
    return 0


def verify_core(root: str, out) -> None:
    """`scripts/ticket.sh version`: the installed core parses the env file and names its plugin (read-only; never a death)."""
    bash = shutil.which("bash")
    if not bash:
        print("verify     skipped: no bash on PATH", file=out)
        return
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run([bash, os.path.join(root, CORE_REL), "version"], cwd=root, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"verify     could not run the core: {e}", file=out)
        return
    line = (r.stdout.strip() or r.stderr.strip()).splitlines()[-1] if (r.stdout.strip() or r.stderr.strip()) else "(no output)"
    print(f"verify     {CORE_REL} version -> {line}" + ("" if r.returncode == 0 else f"  (exit {r.returncode})"), file=out)
