"""`zouinify init [--dry-run] [--non-interactive] [--name ...]`: bootstrap a repository for zouinify (T-0184, the first half of
the design's Z-9).

It writes, under the repository root only: `.ops/config.yaml` (its fields asked for on a real terminal, with proposals
taken from the repository), `.ops/briefs/*.md` rendered from templates/briefs, and the `.ops/state/` line in `.gitignore`.
Nothing else: `tickets/`, `scripts/ticket.sh` and the CLAUDE.md section are the second half (T-0185), written only after a
plan is shown and the human types yes in their own window. Rules: never overwrite an existing `.ops/` file (a second run
reports what exists and changes nothing), never write outside the realpath'd root, never touch `~/.claude` or `CLAUDE.md`.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import zlib

import yaml

from zouinify import ZouinifyError
from zouinify import briefs as briefsmod
from zouinify import config as cfgmod

FIELDS = ("name", "short", "trunk", "prod", "model", "port", "human")
IDENTITY_FIELDS = ("name", "short", "human")        # guesses only: --non-interactive needs them as flags
PORT_BASE, PORT_SPAN = 4700, 90
GITIGNORE_LINE = ".ops/state/"
HUMAN_ONLY_DEFAULT = ("production deploys and applies (every one goes through the human's own terminal)",
                      "image pushes to a registry",
                      "any write against a production account or service")
PROMPTS = {"name": "project name", "short": "short tag (1-8 lowercase letters or digits; prefixes session names)",
           "trunk": "trunk branch", "prod": "production branch (empty for none)", "model": "model for every session",
           "port": "panel port (the terminal page takes +1, the hook listener +2)", "human": "your name (the briefs address you)"}
SECOND_HALF = "tickets/, scripts/ticket.sh and the CLAUDE.md section are the second half of init (T-0185): a plan first, then your own yes"
TOP_KEYS = ("version", "project", "repos", "tickets", "human_only", "budget", "model", "sessions", "panel", "terminal", "contact")
GITIGNORE_COVERS = (".ops/state/", ".ops/state", "/.ops/state/", "/.ops/state", ".ops/", ".ops", "/.ops/", "/.ops")


def initials(name: str) -> str:
    words = [w for w in name.replace("_", "-").replace(".", "-").split("-") if w]
    s = "".join(w[0] for w in words) if len(words) >= 2 else name[:2]
    s = "".join(c for c in s.lower() if c.isalnum())
    return s[:4] or "pj"


def _git(root: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def git_toplevel(root: str) -> str | None:
    top = _git(root, "rev-parse", "--show-toplevel")
    return os.path.realpath(top) if top else None


def git_branch(root: str) -> str:
    return _git(root, "symbolic-ref", "--short", "HEAD") or "main"


def port_free(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def propose_port(root: str) -> int:
    """4700 + crc32(root) % 90, then the first base from there with its three ports (panel, terminal, hook) free."""
    start = PORT_BASE + zlib.crc32(root.encode("utf-8")) % PORT_SPAN
    for i in range(PORT_SPAN):
        p = PORT_BASE + (start - PORT_BASE + i) % PORT_SPAN
        if all(port_free(p + k) for k in (0, 1, 2)):
            return p
    return start


def proposals(root: str) -> dict:
    name = os.path.basename(root.rstrip(os.sep)) or "project"
    trunk = git_branch(root)
    return {"name": name, "short": initials(name), "trunk": trunk, "prod": "main" if trunk != "main" else "",
            "model": "fable", "port": propose_port(root), "human": os.environ.get("USER") or ""}


def validate(field: str, value):
    """Text fields: printable characters only (str.isprintable rejects every control character and every Unicode line
    or paragraph separator, so nothing can end a comment or a line of the rendered file), no double quote or backslash
    (the quoted scalars stay readable). Values go into the file through q(); only the project name appears raw, in
    a comment, and nothing printable ends a comment."""
    v = str(value).strip() if value is not None else ""
    if field in ("name", "short", "trunk", "prod", "model", "human") and (not v.isprintable() or '"' in v or "\\" in v):
        raise ZouinifyError(f"{field}: printable characters only, without double quotes or backslashes")
    if field in ("name", "trunk", "model", "human"):
        if not v:
            raise ZouinifyError(f"{field}: a non-empty value")
        return v
    if field == "short":
        if not (1 <= len(v) <= 8) or not v.isalnum() or not v.isascii() or v != v.lower():
            raise ZouinifyError("short: 1-8 lowercase letters or digits (it prefixes every session name)")
        return v
    if field == "prod":
        if v and " " in v:
            raise ZouinifyError("prod: a branch name (no spaces), or empty for no production branch")
        return v
    if field == "port":
        try:
            p = int(v)
        except ValueError:
            raise ZouinifyError("port: an integer") from None
        if not 1024 <= p <= 65533:
            raise ZouinifyError("port: between 1024 and 65533 (the terminal page takes port + 1, the hook listener port + 2)")
        return p
    raise ZouinifyError(f"unknown field {field}")


def collect(root: str, flags: dict, interactive: bool, out) -> dict:
    """Every field: a flag wins; else a question on the terminal (Enter takes the proposal); else the proposal, except
    the identity fields (name, short, human), which --non-interactive must receive as flags."""
    prop = proposals(root)
    values = {}
    for f in FIELDS:
        given = flags.get(f)
        if given is not None:
            values[f] = validate(f, given)
            continue
        if interactive:
            default = prop[f]
            shown = str(default) if default != "" else "none"
            while True:
                try:
                    answer = input(f"{PROMPTS[f]} [{shown}]: ").strip()
                except EOFError:
                    raise ZouinifyError("init: input ended before every field was answered; nothing written") from None
                try:
                    values[f] = validate(f, answer if answer else default)
                    break
                except ZouinifyError as e:
                    print(f"  {e}", file=out)
            continue
        if f in IDENTITY_FIELDS:
            raise ZouinifyError(f"--non-interactive needs --{f} (the proposal {prop[f]!r} is only a guess; "
                                f"--trunk --prod --model --port default to their proposals)")
        values[f] = validate(f, prop[f])
    return values


def q(s: str) -> str:
    """A YAML-safe scalar: JSON quoting is valid YAML."""
    return json.dumps(str(s))


def render_config(v: dict) -> str:
    sh, m = v["short"], v["model"]
    prod = f", prod: {q(v['prod'])}" if v["prod"] else ""
    prod_rule = ("  - " + q(f"git push to {v['prod']}; fast-forwarding it (it moves only at a release)") if v["prod"]
                 else "  - git push to the production branch; fast-forwarding it")
    human_only = "\n".join(f"  - {x}" for x in HUMAN_ONLY_DEFAULT) + "\n" + prod_rule
    return f"""# .ops/config.yaml -- {v['name']}: the zouinify configuration of this repository (schema v1), written by `zouinify init`.
# It holds no secret: names, paths, branch names, window names, a budget number. Edit it to taste, then
# `zouinify config --check` and `zouinify briefs render --force`. Optional companions a project may add next to it,
# owned by the project: .ops/README.project.md (its own protocol block) and .ops/INVENTORY.md.
version: 1
project: {{name: {q(v['name'])}, short: {q(sh)}}}
repos:
  - {{name: {q(v['name'])}, path: ., trunk: {q(v['trunk'])}{prod}, default: true}}
tickets:
  dir: tickets
  worktrees: .claude/worktrees
  states: default                       # the 12 states of the core
  deploy_kinds: [none]                  # add terraform, image+terraform, frontend, manual-aws as the project needs them
  deploy_plugin: none                   # tickets/plugins/<plugin>.sh provides apply / deploy-check / the evidence regexes
  ask_verbs: [APPLY, DEPLOY, PUSH, RUN, DECIDE, CHECK, APPROVE, SSO]
human_only:                             # printed into every brief; the panel never runs them; add the project's own
{human_only}
budget: {{paid_usd_per_day: 10, approval_text: "T-{{id}} budget ok"}}
model: {q(m)}
sessions:
  desk:         {{name: {q(sh + '-desk')}, tmux: {q(sh + '-desk')}, model: {q(m)}, permission_mode: auto, brief: .ops/briefs/desk.md, always: true}}
  manager:      {{name: {q(sh + '-manager')}, tmux: {q(sh + '-manager')}, model: {q(m)}, permission_mode: auto, brief: .ops/briefs/manager.md, always: true}}
  debug:        {{name: {q(sh + '-debug')}, tmux: {q(sh + '-debug')}, model: {q(m)}, permission_mode: auto, brief: .ops/briefs/debug.md, always: true}}
  chrome_debug: {{name: {q(sh + '-chrome-debug')}, tmux: {q(sh + '-chrome-debug')}, model: {q(m)}, permission_mode: auto, brief: .ops/briefs/chrome_debug.md, flags: [--chrome], optional: true}}
  worker:       {{name: "t{{num}}-{{slug}}", tmux: "t{{num}}-{{slug}}", model: {q(m)}, permission_mode: auto,
                 prompt: "Read tickets/WORKER_BRIEF.md, then claim {{id}}."}}
panel:
  port: {v['port']}                            # proposed as 4700 + crc32(root) % 90 with its three ports free; `up` refuses a held port
  hook_port: {v['port'] + 2}                       # the Claude Code hook listener, 127.0.0.1 only (port + 2; port + 1 is the terminal page)
  bind: loopback                        # loopback | intranet (intranet = 0.0.0.0, browser token mandatory; see docs/REFERENCE.md)
  link_host: auto
  state_dir: .ops/state                 # gitignored: pid file, token hash, logs, events
  refresh_s: 30
  redact: default
  logs: []                              # log tails on the dashboard, e.g. [{{label: "deploy", glob: "deploy/*.log"}}]
  plans_glob: ""                        # e.g. "terraform/*.tfplan": the newest matching file is the only applicable plan
terminal:                               # the terminal page: type into one of this project's WORKER panes from the browser
  enabled: false                        # `zouinify term secret rotate` once in your own terminal, then `zouinify term start`
  port: {v['port'] + 1}                            # panel.port + 1
  bind: loopback                        # loopback | intranet -- its own flag, independent of panel.bind
  tls: auto                             # auto (self-signed HTTPS on intranet) | off
  autostart: false
  targets: workers                      # the only value: the desk, manager, debug and chrome_debug panes are never reachable
  exclude: []
  idle_close_s: 300
  max_open_s: 1800
  unlock_idle_s: 600
  unlock_max_s: 28800
# aws: {{profile: default, sso_cache: ~/.aws/sso/cache, assumed_token_hours: 2}}   # add when the project has cloud credentials to show
contact: {{human: {q(v['human'])}}}
"""


def gitignore_state(root: str) -> str:
    """create | append | exists (a line already covers .ops/state/) | symlink | directory | hard link (the last three are
    skipped: a write through them lands outside the file the repository tracks)."""
    p = os.path.join(root, ".gitignore")
    if os.path.islink(p):
        return "symlink"
    if not os.path.lexists(p):
        return "create"
    if not os.path.isfile(p):
        return "directory"
    if os.stat(p).st_nlink > 1:
        return "hard link"
    with open(p, encoding="utf-8", errors="replace") as fh:
        lines = [ln.strip() for ln in fh.read().splitlines()]
    return "exists" if any(ln in GITIGNORE_COVERS for ln in lines) else "append"


def inside(root: str, path: str) -> bool:
    rp = os.path.realpath(path)
    return rp == root or rp.startswith(root + os.sep)


def write_new(root: str, rel: str, content: str, out) -> None:
    """Create a file under root that does not exist yet: the parent must resolve inside root, the file is opened
    O_EXCL|O_NOFOLLOW, so nothing is ever overwritten or written through a link."""
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not inside(root, os.path.dirname(path)):
        raise ZouinifyError(f"refusing to write outside the repository root: {rel} resolves elsewhere")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        raise ZouinifyError(f"{rel} appeared meanwhile (another init running here?); nothing overwritten") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote      {rel}", file=out)


def report_existing(root: str, out) -> int:
    print(f"init: {os.path.join(root, cfgmod.CONFIG_REL)} exists; nothing written (init never overwrites an .ops file).", file=out)
    ops = os.path.join(root, ".ops")
    state = os.path.join(".ops", "state")
    for dirpath, dirs, files in os.walk(ops):
        dirs[:] = sorted(d for d in dirs if os.path.relpath(os.path.join(dirpath, d), root) != state)
        for f in sorted(files):
            print(f"exists     {os.path.relpath(os.path.join(dirpath, f), root)}", file=out)
    g = gitignore_state(root)
    print(f"{'exists' if g == 'exists' else 'missing':<10} .gitignore line `{GITIGNORE_LINE}`" + ("" if g == "exists" else "  (add it yourself)"),
          file=out)
    try:
        cfg = cfgmod.load(root)
    except cfgmod.ConfigError as e:
        print(f"config     INVALID: {e}", file=out)
        return 1
    print(f"config     ok: {cfgmod.summary(cfg)}", file=out)
    return 0


def _ends_with_newline(path: str) -> bool:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            return True
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def add_gitignore_line(root: str, state: str, out) -> None:
    if state == "exists":
        print(f"exists     .gitignore already lists `{GITIGNORE_LINE}`", file=out)
        return
    if state in ("symlink", "directory", "hard link"):
        print(f"skipped    .gitignore is a {state}; add the line `{GITIGNORE_LINE}` yourself", file=out)
        return
    p = os.path.join(root, ".gitignore")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    with os.fdopen(os.open(p, flags, 0o644), "a", encoding="utf-8") as fh:
        if state == "append" and not _ends_with_newline(p):
            fh.write("\n")
        fh.write(GITIGNORE_LINE + "\n")
    print(f"{'created' if state == 'create' else 'appended':<10} .gitignore  (`{GITIGNORE_LINE}`)", file=out)


def undo(root: str, created: list, out) -> None:
    """Remove only what this run created (O_EXCL above proves it was ours); errors here are reported, not raised."""
    for rel in reversed(created):
        try:
            os.unlink(os.path.join(root, rel))
            print(f"removed    {rel}  (this run wrote it; the error below stopped init)", file=out)
        except OSError as e:
            print(f"left       {rel}  (could not remove: {e})", file=out)


def resolve_root(target: str | None) -> str:
    root = os.path.realpath(os.path.expanduser(target)) if target else os.path.realpath(os.getcwd())
    if not os.path.isdir(root):
        raise ZouinifyError(f"not a directory: {root}")
    top = git_toplevel(root)
    if top is None:
        raise ZouinifyError(f"{root} is not a git repository: init runs at a repository's root (.ops/ belongs next to .git, and "
                            "find_root would make this directory the project of every command under it); `git init` first, or "
                            "pass --project <repo>")
    if top != root:
        raise ZouinifyError(f"run init at the repository root ({top}), or pass --project {top}")
    ops = os.path.join(root, ".ops")
    if os.path.islink(ops):
        raise ZouinifyError(f"{ops} is a symlink; init writes under the repository root only")
    if os.path.lexists(ops) and not os.path.isdir(ops):
        raise ZouinifyError(f"{ops} exists and is not a directory")
    return root


def check_rendered(config_text: str) -> None:
    """The rendered file must parse and have exactly the top-level keys init writes: a value that still managed to
    open a new key is refused before anything is written."""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError as e:
        raise ZouinifyError(f"the rendered config is not valid YAML ({e}); check the values given") from None
    if not isinstance(doc, dict) or tuple(doc) != TOP_KEYS:
        raise ZouinifyError("the rendered config does not have exactly the expected top-level keys; check the values given")


def check_targets(root: str, briefs: list) -> None:
    """Before the first write: every path init will create or leave alone must be a plain path inside root, so no
    failure can come after .ops/config.yaml exists (a second run would only report it)."""
    bd = os.path.join(root, ".ops", "briefs")
    if os.path.islink(bd):
        raise ZouinifyError(".ops/briefs is a symlink; init writes under the repository root only")
    if os.path.lexists(bd) and not os.path.isdir(bd):
        raise ZouinifyError(".ops/briefs exists and is not a directory")
    for _name, rel in briefs:
        p = os.path.join(root, rel)
        if os.path.islink(p):
            raise ZouinifyError(f"{rel} is a symlink; the config would point at a brief outside the project")
        if os.path.lexists(p) and not os.path.isfile(p):
            raise ZouinifyError(f"{rel} exists and is not a regular file")


def cmd_init(target: str | None, flags: dict, dry_run: bool = False, non_interactive: bool = False, out=None) -> int:
    try:
        return _cmd_init(target, flags, dry_run, non_interactive, out or sys.stdout)
    except OSError as e:
        raise ZouinifyError(f"init: {e}") from None


def _cmd_init(target, flags, dry_run, non_interactive, out) -> int:
    root = resolve_root(target)
    if os.path.lexists(os.path.join(root, cfgmod.CONFIG_REL)):
        return report_existing(root, out)
    interactive = not non_interactive
    if interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ZouinifyError("init asks its questions on a real terminal; without one pass --non-interactive with --name, --short "
                            "and --human (--trunk --prod --model --port override the proposals)")
    values = collect(root, flags, interactive, out)
    config_text = render_config(values)
    check_rendered(config_text)
    briefs = [(name, os.path.join(".ops", "briefs", name + ".md")) for name in briefsmod.list_templates()]
    check_targets(root, briefs)
    gi = gitignore_state(root)

    print(f"init plan for {root}:", file=out)
    print(f"  write    {cfgmod.CONFIG_REL}  ({len(config_text)} bytes)", file=out)
    for name, rel in briefs:
        state = "exists" if os.path.lexists(os.path.join(root, rel)) else "write"
        print(f"  {state:<8} {rel}  (from templates/briefs/{name}{briefsmod.SUFFIX})", file=out)
    print(f"  {gi:<8} .gitignore  (line `{GITIGNORE_LINE}`)", file=out)
    print(f"  nothing else: {SECOND_HALF}", file=out)
    if dry_run:
        print(f"\n--- {cfgmod.CONFIG_REL} (dry run: nothing written) ---", file=out)
        out.write(config_text)
        return 0

    created = []
    try:
        write_new(root, cfgmod.CONFIG_REL, config_text, out)
        created.append(cfgmod.CONFIG_REL)
        cfg = cfgmod.load(root)                               # the validator judges what was written
        ctx = briefsmod.context(cfg)
        for name, rel in briefs:
            if os.path.lexists(os.path.join(root, rel)):
                print(f"exists     {rel}  (left as is)", file=out)
                continue
            with open(os.path.join(briefsmod.TEMPLATES, name + briefsmod.SUFFIX), encoding="utf-8") as fh:
                text, missing = briefsmod.render(fh.read(), ctx)
            if missing:                                        # a template defect: keep going, the config is already right
                print(f"skipped    {rel}: unknown placeholder(s) {', '.join(missing)} in the template; `zouinify briefs render` later",
                      file=out)
                continue
            write_new(root, rel, text, out)
            created.append(rel)
        add_gitignore_line(root, gi, out)
    except Exception:
        undo(root, created, out)                               # a rerun must not find a half-written .ops/
        raise

    cfg = cfgmod.load(root)                                   # again, now that the briefs exist
    print(f"\nconfig     ok: {cfgmod.summary(cfg)}", file=out)
    for w in cfg.warnings:
        print(f"  warning: {w}", file=out)
    print("\nwhat `zouinify up` would do (dry run):", file=out)
    try:
        from zouinify import launch
        launch.cmd_up(cfg, dry_run=True, out=out)
    except Exception as e:  # noqa: BLE001 - the preview must not undo a finished init
        print(f"  (up --dry-run could not run here: {e})", file=out)
    print(f"\nnext: edit {cfgmod.CONFIG_REL} (human_only, repos, budget, deploy kinds), `zouinify briefs render --force` to refresh the "
          f"briefs, `zouinify config --check`, then `zouinify up`. {SECOND_HALF}.", file=out)
    return 0
