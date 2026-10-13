"""Load and validate the per-project configuration: <root>/.ops/config.yaml (schema v1).

Everything project-specific (names, paths, window names, a budget number) lives in that
file; the skill itself is project-agnostic. The commented example is templates/ops/config.yaml.example.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from zouinify import ZouinifyError

CONFIG_REL = os.path.join(".ops", "config.yaml")
ROLE_SESSIONS = ("desk", "manager", "debug", "chrome_debug")  # the fixed roles: `down` never kills them
MODEL_WORDS = ("fable", "opus", "sonnet", "haiku")
DEFAULT_WORKER_PROMPT_TMPL = "Read {tickets_dir}/WORKER_BRIEF.md, then claim {{id}}."
# the Claude Code status bar, e.g. `[Fable 5.1] 📁 repo | 🌿 branch`; group 1 is the model label (config key `model_re`)
DEFAULT_MODEL_RE = r"\[((?:Fable|Opus|Sonnet|Haiku|Claude)[^\]\n]*)\]"
BIND_ALIASES = {"loopback": "loopback", "127.0.0.1": "loopback", "localhost": "loopback",
                "intranet": "intranet", "0.0.0.0": "intranet"}
DEFAULT_ASK_VERBS = ["APPLY", "DEPLOY", "PUSH", "RUN", "DECIDE", "CHECK", "APPROVE", "SSO"]
DEFAULT_TICKET_TYPES = ["bug", "feature", "process", "chore"]
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9-]+$")           # tickets/plugins/<name>/plugin.sh (T-0186 grammar)
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BRANCH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")          # what ticket.sh may put on a command line unquoted
BRANCH_BAD_RE = re.compile(r"\.\.|@\{|//")                      # git check-ref-format, the rules the alphabet does not already cover


def valid_branch_name(name) -> bool:
    """git's ref rules (`git check-ref-format --branch`) plus a sane alphabet, because ticket.sh passes branch names to git
    unquoted: letters, digits and . _ / @ + - only (git itself would also take $ ( ) ; and friends), no `..`, `@{` or
    `//`, no leading `-` or `/`, no trailing `/`, `.` or `.lock`, no component starting with `.`, not `@`."""
    if not isinstance(name, str) or name == "@" or not BRANCH_CHARS_RE.match(name):
        return False
    if BRANCH_BAD_RE.search(name) or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


class ConfigError(ZouinifyError):
    pass


def find_root(start: str | None = None) -> str:
    """The project root: $ZOUINIFY_PROJECT, else the nearest ancestor of `start` holding .ops/config.yaml."""
    env = os.environ.get("ZOUINIFY_PROJECT")
    if env:
        if os.path.isfile(os.path.join(env, CONFIG_REL)):
            return os.path.realpath(env)
        raise ConfigError(f"ZOUINIFY_PROJECT={env} has no {CONFIG_REL}")
    d = os.path.realpath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, CONFIG_REL)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise ConfigError(f"no {CONFIG_REL} in this directory or any parent "
                              "(cd into the project, pass --project, or set ZOUINIFY_PROJECT)")
        d = parent


@dataclass
class Session:
    role: str
    name: str                    # the `-n` name and TICKET_AS (a pattern for the worker)
    tmux: str                    # the tmux session name (a pattern for the worker)
    model: str
    permission_mode: str | None  # passed to claude only when set: no default (T-0150 is the human's call)
    brief: str | None            # a file under the project, its contents become the prompt
    prompt: str | None           # or a literal prompt ({id} for the worker)
    flags: list
    always: bool
    optional: bool
    kind: str                    # claude | human-shell

    def to_dict(self):
        return {"role": self.role, "name": self.name, "tmux": self.tmux, "model": self.model,
                "permission_mode": self.permission_mode, "brief": self.brief, "prompt": self.prompt,
                "flags": list(self.flags), "always": self.always, "optional": self.optional, "kind": self.kind}


@dataclass
class Config:
    root: str
    name: str
    short: str
    repos: list
    trunk: str
    prod: str | None
    tickets_dir: str
    tickets_script: str
    worktrees: str
    ask_verbs: list
    deploy_kinds: list
    deploy_plugin: str
    ticket_types: list
    protected_branches_extra: list
    human_only: list
    budget: dict
    model: str | None
    model_re: str
    sessions: dict
    panel: dict
    terminal: dict
    aws: dict | None
    contact: dict
    warnings: list = field(default_factory=list)

    @property
    def config_path(self) -> str:
        return os.path.join(self.root, CONFIG_REL)

    @property
    def state_dir(self) -> str:
        """<root>/.ops/state, fixed in code: the panel's pid file and log, the launcher registry and the status
        cache live there; `panel.state_dir` in the config may only say the same (desk condition, T-0160)."""
        return os.path.join(self.root, ".ops", "state")

    def path(self, rel: str) -> str:
        rel = os.path.expanduser(rel)
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def role_sessions(self) -> dict:
        """Every configured session except the per-ticket worker (the fixed roles and any extra role)."""
        return {r: s for r, s in self.sessions.items() if r != "worker"}

    def to_dict(self) -> dict:
        return {"version": 1,
                "project": {"name": self.name, "short": self.short, "root": self.root},
                "repos": self.repos, "trunk": self.trunk, "prod": self.prod,
                "tickets": {"dir": self.tickets_dir, "script": self.tickets_script, "worktrees": self.worktrees,
                            "ask_verbs": self.ask_verbs, "deploy_kinds": self.deploy_kinds, "deploy_plugin": self.deploy_plugin,
                            "types": self.ticket_types, "protected_branches_extra": self.protected_branches_extra},
                "human_only": self.human_only, "budget": self.budget,
                "model": self.model, "model_re": self.model_re, "sessions": {r: s.to_dict() for r, s in self.sessions.items()},
                "panel": self.panel, "terminal": self.terminal, "aws": self.aws, "contact": self.contact, "warnings": self.warnings}


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _strlist(v, where, errs, default):
    if v is None:
        return list(default)
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        errs.append(f"{where}: a list of strings")
        return list(default)
    return [x for x in v]


def _load_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - depends on the machine
        raise ConfigError("PyYAML is required to read .ops/config.yaml (pip install pyyaml)") from e
    with open(path, encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return data


def load(root: str | None = None) -> Config:
    """Load and validate; raises ConfigError listing every problem at once."""
    root = os.path.realpath(os.path.expanduser(root)) if root else find_root()
    path = os.path.join(root, CONFIG_REL)
    if not os.path.isfile(path):
        raise ConfigError(f"{path}: not found")
    raw = _load_yaml(path)
    errs: list[str] = []
    warns: list[str] = []

    if raw.get("version") != 1:
        errs.append(f"version: must be 1 (got {raw.get('version')!r})")

    proj = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    if not isinstance(raw.get("project"), dict):
        errs.append("project: a mapping {name, short} is required")
    name, short = proj.get("name"), proj.get("short")
    if not _is_str(name):
        errs.append("project.name: required")
    if not _is_str(short):
        errs.append("project.short: required (a short tag used in session names)")
    if _is_str(proj.get("root")) and os.path.realpath(os.path.expanduser(proj["root"])) != root:
        warns.append(f"project.root {proj['root']} is not where this config lives ({root}); the config's location wins")

    repos = raw.get("repos")
    if not isinstance(repos, list) or not repos:
        errs.append("repos: a non-empty list of {name, path, trunk[, prod, default]} is required")
        repos = []
    default_repo = None
    clean_repos = []
    for i, r in enumerate(repos):
        if not isinstance(r, dict) or not _is_str(r.get("name")) or not _is_str(r.get("path")) or not _is_str(r.get("trunk")):
            errs.append(f"repos[{i}]: name, path and trunk are required")
            continue
        for fld in ("trunk", "prod"):
            if r.get(fld) not in (None, "") and not valid_branch_name(r.get(fld)):
                errs.append(f"repos[{i}].{fld}: not a valid git branch name for ticket.sh (git's rules plus letters, digits and "
                            f". _ / @ + -): {r.get(fld)!r}")
        clean_repos.append(dict(r))
        if r.get("default") and default_repo is None:
            default_repo = r
    if default_repo is None and clean_repos:
        default_repo = clean_repos[0]
    trunk = str((default_repo or {}).get("trunk") or "main")
    prod = (default_repo or {}).get("prod")
    prod = str(prod) if _is_str(prod) else None

    tk = raw.get("tickets") if isinstance(raw.get("tickets"), dict) else {}
    tickets_dir = str(tk.get("dir") or "tickets")
    if os.path.isabs(tickets_dir) or tickets_dir.startswith("~") or ".." in tickets_dir.split("/") or tickets_dir.strip("/") == "":
        errs.append("tickets.dir: a relative directory inside the project (default tickets)")
        tickets_dir = "tickets"
    tickets_dir = tickets_dir.rstrip("/")
    tickets_script = str(tk.get("script") or "scripts/ticket.sh")
    worktrees = str(tk.get("worktrees") or ".claude/worktrees")
    ask_verbs = _strlist(tk.get("ask_verbs"), "tickets.ask_verbs", errs, DEFAULT_ASK_VERBS)
    deploy_kinds = _strlist(tk.get("deploy_kinds"), "tickets.deploy_kinds", errs, ["none"])
    if "none" not in deploy_kinds:
        errs.append("tickets.deploy_kinds: must include none (a ticket without a deploy goes merged -> done on it)")
    deploy_plugin = tk.get("deploy_plugin")
    if deploy_plugin is None or deploy_plugin == "":
        deploy_plugin = "none"
    elif not isinstance(deploy_plugin, str) or not PLUGIN_NAME_RE.match(deploy_plugin):
        errs.append("tickets.deploy_plugin: a plugin name of lowercase letters, digits and dashes (tickets/plugins/<name>/plugin.sh), "
                    "or none")
        deploy_plugin = "none"
    ticket_types = _strlist(tk.get("types"), "tickets.types", errs, DEFAULT_TICKET_TYPES)
    if not ticket_types or any(not IDENT_RE.match(t) for t in ticket_types):
        errs.append("tickets.types: a non-empty list of single lowercase words (default: bug feature process chore)")
        ticket_types = list(DEFAULT_TICKET_TYPES)
    protected_extra = _strlist(tk.get("protected_branches_extra"), "tickets.protected_branches_extra", errs, [])
    if any(not valid_branch_name(b) for b in protected_extra):
        errs.append("tickets.protected_branches_extra: valid git branch names (git's rules plus letters, digits and . _ / @ + -)")
    if tk.get("states") not in (None, "default"):
        errs.append("tickets.states: only `default` (the 12 states of the core) exists in this version")

    human_only = _strlist(raw.get("human_only"), "human_only", errs, [])
    if not human_only:
        warns.append("human_only is empty: the briefs will say nothing is reserved for the human")

    if "sittings" in raw:
        warns.append("sittings: ignored (the sitting protocol was removed on 2026-10-13: the inbox is continuous); drop the key")

    budget = dict(raw.get("budget")) if isinstance(raw.get("budget"), dict) else {}
    model = raw.get("model")
    if model is not None and not _is_str(model):
        errs.append("model: a string (e.g. fable)")
        model = None
    model_re = raw.get("model_re", DEFAULT_MODEL_RE)
    if not _is_str(model_re):
        errs.append("model_re: a regular expression over the visible screen whose group 1 is the model label")
        model_re = DEFAULT_MODEL_RE
    else:
        try:
            if re.compile(model_re).groups < 1:
                errs.append("model_re: needs one capturing group (the model label)")
        except re.error as e:
            errs.append(f"model_re: not a valid regular expression ({e})")
            model_re = DEFAULT_MODEL_RE
    default_worker_prompt = DEFAULT_WORKER_PROMPT_TMPL.format(tickets_dir=tickets_dir)

    sessions: dict[str, Session] = {}
    raw_sessions = raw.get("sessions")
    if not isinstance(raw_sessions, dict) or not raw_sessions:
        errs.append("sessions: a mapping role -> {name, tmux, model, permission_mode, brief|prompt} is required (at least desk)")
        raw_sessions = {}
    for role, s in raw_sessions.items():
        role = str(role)
        if not isinstance(s, dict):
            errs.append(f"sessions.{role}: must be a mapping")
            continue
        kind = str(s.get("kind") or "claude")
        s_name, s_tmux = s.get("name"), s.get("tmux")
        if kind == "claude" and not _is_str(s_name):
            errs.append(f"sessions.{role}.name: required (the -n name and TICKET_AS)")
        if not _is_str(s_tmux):
            errs.append(f"sessions.{role}.tmux: required (the tmux session name)")
        s_model = s.get("model", model)
        if kind == "claude" and not _is_str(s_model):
            errs.append(f"sessions.{role}.model: required (or a top-level `model`)")
        pm = s.get("permission_mode")
        if pm is not None and (not isinstance(pm, str) or not pm.strip()):
            if pm != "" and pm is not None:
                errs.append(f"sessions.{role}.permission_mode: a string when set (it is passed to claude only when set)")
            pm = None  # "" means unset
        brief, prompt = s.get("brief"), s.get("prompt")
        if brief is not None:
            if not _is_str(brief):
                errs.append(f"sessions.{role}.brief: a file path relative to the project")
                brief = None
            else:
                b = str(brief).strip()
                real = os.path.realpath(os.path.join(root, b))
                if os.path.isabs(b) or b.startswith("~") or not real.startswith(root + os.sep):
                    errs.append(f"sessions.{role}.brief: must be a relative path inside the project, not {b!r}")
                    brief = None
                else:
                    brief = os.path.relpath(real, root)
        if prompt is not None and not _is_str(prompt):
            errs.append(f"sessions.{role}.prompt: a string")
            prompt = None
        flags = _strlist(s.get("flags"), f"sessions.{role}.flags", errs, [])
        if role == "worker":
            prompt = prompt or default_worker_prompt
            if "{id}" not in prompt:
                errs.append("sessions.worker.prompt: must contain {id}")
            for k, v in (("name", s_name), ("tmux", s_tmux)):
                if _is_str(v) and "{num}" not in v and "{slug}" not in v:
                    errs.append(f"sessions.worker.{k}: must contain {{num}} or {{slug}}")
        elif kind == "claude":
            if not brief and not prompt:
                errs.append(f"sessions.{role}: brief (a file under the project) or prompt is required")
            if brief and not os.path.isfile(os.path.join(root, os.path.expanduser(brief))):
                warns.append(f"sessions.{role}.brief: {brief} does not exist yet (`zouinify briefs render` writes the templates)")
            if role == "chrome_debug" and "--chrome" not in flags:
                flags = flags + ["--chrome"]
        sessions[role] = Session(role=role, name=str(s_name or ""), tmux=str(s_tmux or ""), model=str(s_model or ""),
                                 permission_mode=str(pm) if pm is not None else None,
                                 brief=str(brief) if brief else None, prompt=str(prompt) if prompt else None,
                                 flags=flags, always=bool(s.get("always", False)), optional=bool(s.get("optional", False)),
                                 kind=kind)
    if raw_sessions and "desk" not in sessions:
        errs.append("sessions.desk: required (the ticket desk is the one voice to the human)")
    if "worker" not in sessions:
        sessions["worker"] = Session("worker", "t{num}-{slug}", "t{num}-{slug}", str(model or ""), None, None,
                                     default_worker_prompt, [], False, False, "claude")
        warns.append("sessions.worker: missing, using the default pattern t{num}-{slug} and the default prompt")
    seen: dict[str, str] = {}
    for role, s in sessions.items():
        if role == "worker" or not s.tmux:
            continue
        if s.tmux in seen:
            errs.append(f"sessions.{role}.tmux: {s.tmux} is also sessions.{seen[s.tmux]}.tmux (tmux names must be unique)")
        seen[s.tmux] = role

    pn = raw.get("panel") if isinstance(raw.get("panel"), dict) else {}
    port = pn.get("port", 4790)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        errs.append("panel.port: an integer between 1024 and 65535")
        port = 0
    default_hook = port + 2 if port and port + 2 <= 65535 else 0      # T-0163: the loopback-only hook listener; +1 is the terminal page
    hook_port = pn.get("hook_port", default_hook)
    if port and "hook_port" not in pn and not default_hook:
        errs.append("panel.hook_port: set it explicitly (panel.port + 2 is out of range)")
    elif isinstance(hook_port, bool) or not isinstance(hook_port, int) or not 1024 <= hook_port <= 65535 or hook_port in (port, port + 1):
        if port:
            errs.append("panel.hook_port: an integer between 1024 and 65535, not panel.port or panel.port + 1 (the terminal page); "
                        "default panel.port + 2")
        hook_port = 0
    bind = BIND_ALIASES.get(str(pn.get("bind", "loopback")))
    if bind is None:
        errs.append("panel.bind: loopback (the default) or intranet")
    try:
        refresh_s = int(pn.get("refresh_s", 30) or 30)
    except (TypeError, ValueError):
        errs.append("panel.refresh_s: an integer (seconds)")
        refresh_s = 30
    logs = []
    raw_logs = pn.get("logs")
    if raw_logs is None or raw_logs == []:
        pass
    elif not isinstance(raw_logs, list):
        errs.append("panel.logs: a list of {label, glob}")
    else:
        for i, entry in enumerate(raw_logs):
            if not isinstance(entry, dict) or not _is_str(entry.get("label")) or not _is_str(entry.get("glob")):
                errs.append(f"panel.logs[{i}]: label and glob are required")
                continue
            g = str(entry["glob"]).strip()
            if os.path.isabs(g) or g.startswith("~") or ".." in g.split(os.sep):
                errs.append(f"panel.logs[{i}].glob: must be a relative glob inside the project, not {g!r}")
                continue
            logs.append({"label": str(entry["label"]).strip(), "glob": g})
    plans_glob = pn.get("plans_glob")
    if plans_glob in (None, ""):
        plans_glob = None
    elif not _is_str(plans_glob) or os.path.isabs(plans_glob) or str(plans_glob).startswith("~") or ".." in str(plans_glob).split(os.sep):
        errs.append("panel.plans_glob: a relative glob inside the project (e.g. deploy/*.plan), or empty for no plan rule")
        plans_glob = None
    sd = pn.get("state_dir", ".ops/state")
    if str(sd).strip().rstrip("/") != ".ops/state":
        errs.append(f"panel.state_dir: fixed at .ops/state (the panel's pid file and log live there; not configurable), not {sd!r}")
    panel = {"port": port, "hook_port": hook_port, "bind": bind or "loopback", "link_host": str(pn.get("link_host", "auto")),
             "state_dir": ".ops/state", "refresh_s": refresh_s,
             "redact": str(pn.get("redact", "default")), "autostart": bool(pn.get("autostart", True)),
             "logs": logs, "plans_glob": str(plans_glob) if plans_glob else None}

    # the terminal page (T-0181): a separate process that forwards the human's keystrokes into WORKER panes only
    tn = raw.get("terminal") if isinstance(raw.get("terminal"), dict) else {}
    term_bind = BIND_ALIASES.get(str(tn.get("bind", "loopback")))
    if term_bind is None:
        errs.append("terminal.bind: loopback (the default) or intranet")
    try:
        term_port = int(tn.get("port") or ((port + 1) if port else 0))
        if not 1024 <= term_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("terminal.port: an integer between 1024 and 65535 (default panel.port + 1)")
        term_port = 0
    if term_port and term_port == port:
        errs.append("terminal.port: must differ from panel.port")
    term_tls = str(tn.get("tls", "auto")).strip().lower()
    if term_tls not in ("auto", "off"):
        errs.append("terminal.tls: auto (self-signed HTTPS when bound to the intranet) or off")
    term_targets = str(tn.get("targets", "workers")).strip().lower()
    if term_targets != "workers":
        errs.append("terminal.targets: only `workers` exists: the role sessions (desk, manager, debug, chrome_debug) are never "
                    "reachable from the browser (user decision 2026-10-13)")
    term_exclude = _strlist(tn.get("exclude"), "terminal.exclude", errs, [])
    term_times = {}
    for key, default in (("idle_close_s", 300), ("max_open_s", 1800), ("unlock_idle_s", 600), ("unlock_max_s", 28800)):
        try:
            raw_v = tn.get(key, default)
            v = int(default if raw_v is None else raw_v)      # 0 is a value (and refused below), not "unset"
            if v <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"terminal.{key}: a positive integer (seconds)")
            v = default
        term_times[key] = v
    terminal = {"enabled": bool(tn.get("enabled", False)), "port": term_port, "bind": term_bind or "loopback", "tls": term_tls,
                "autostart": bool(tn.get("autostart", False)), "targets": "workers", "exclude": term_exclude}
    terminal.update(term_times)

    aws = raw.get("aws") if isinstance(raw.get("aws"), dict) else None
    if aws is not None:
        try:
            aws = {"profile": str(aws.get("profile", "default")), "sso_cache": str(aws.get("sso_cache", "~/.aws/sso/cache")),
                   "assumed_token_hours": float(aws.get("assumed_token_hours", 2) or 0)}
        except (TypeError, ValueError):
            errs.append("aws.assumed_token_hours: a number (hours)")
            aws = None
    contact = dict(raw.get("contact")) if isinstance(raw.get("contact"), dict) else {}

    if errs:
        raise ConfigError(f"{path}:\n  - " + "\n  - ".join(errs))
    return Config(root=root, name=str(name), short=str(short), repos=clean_repos, trunk=trunk, prod=prod,
                  tickets_dir=tickets_dir, tickets_script=tickets_script, worktrees=worktrees, ask_verbs=ask_verbs,
                  deploy_kinds=deploy_kinds, deploy_plugin=str(deploy_plugin), ticket_types=ticket_types,
                  protected_branches_extra=protected_extra, human_only=human_only, budget=budget,
                  model=str(model) if model else None, model_re=str(model_re), sessions=sessions, panel=panel, terminal=terminal, aws=aws, contact=contact,
                  warnings=warns)


def summary(cfg: Config) -> str:
    roles = " ".join(r for r in cfg.sessions if r != "worker")
    worker = cfg.sessions["worker"]
    return (f"project {cfg.name}/{cfg.short}, root {cfg.root}, trunk {cfg.trunk}"
            f"{', prod ' + cfg.prod if cfg.prod else ''}, tickets {cfg.tickets_dir}/ via {cfg.tickets_script}, "
            f"sessions: {roles}, worker pattern {worker.name}, panel {cfg.panel['port']} {cfg.panel['bind']}")
