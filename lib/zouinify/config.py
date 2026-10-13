"""Load and validate the per-project configuration: <root>/.ops/config.yaml (schema v1).

Everything project-specific (names, paths, window names, sitting times, a budget number) lives in that
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
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_WORKER_PROMPT = "Read tickets/WORKER_BRIEF.md, then claim {id}."
BIND_ALIASES = {"loopback": "loopback", "127.0.0.1": "loopback", "localhost": "loopback",
                "intranet": "intranet", "0.0.0.0": "intranet"}
DEFAULT_ASK_VERBS = ["APPLY", "DEPLOY", "PUSH", "RUN", "DECIDE", "CHECK", "APPROVE", "SSO"]


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
    human_only: list
    sittings: dict
    budget: dict
    model: str | None
    sessions: dict
    panel: dict
    aws: dict | None
    contact: dict
    warnings: list = field(default_factory=list)

    @property
    def config_path(self) -> str:
        return os.path.join(self.root, CONFIG_REL)

    @property
    def state_dir(self) -> str:
        return self.path(self.panel.get("state_dir") or ".ops/state")

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
                            "ask_verbs": self.ask_verbs, "deploy_kinds": self.deploy_kinds},
                "human_only": self.human_only, "sittings": self.sittings, "budget": self.budget,
                "model": self.model, "sessions": {r: s.to_dict() for r, s in self.sessions.items()},
                "panel": self.panel, "aws": self.aws, "contact": self.contact, "warnings": self.warnings}


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
    tickets_script = str(tk.get("script") or "scripts/ticket.sh")
    worktrees = str(tk.get("worktrees") or ".claude/worktrees")
    ask_verbs = _strlist(tk.get("ask_verbs"), "tickets.ask_verbs", errs, DEFAULT_ASK_VERBS)
    deploy_kinds = _strlist(tk.get("deploy_kinds"), "tickets.deploy_kinds", errs, ["none"])

    human_only = _strlist(raw.get("human_only"), "human_only", errs, [])
    if not human_only:
        warns.append("human_only is empty: the briefs will say nothing is reserved for the human")

    st = raw.get("sittings") if isinstance(raw.get("sittings"), dict) else {}
    times = st.get("times") if st.get("times") is not None else []
    if not isinstance(times, list) or any(not (isinstance(t, str) and TIME_RE.match(t)) for t in times):
        errs.append('sittings.times: a list of "HH:MM" strings')
        times = []
    try:
        sittings = {"tz": str(st.get("tz") or "UTC"), "times": sorted(str(t) for t in times),
                    "freeze_before_min": int(st.get("freeze_before_min", 10) or 0),
                    "push_before_min": int(st.get("push_before_min", 10) or 0)}
    except (TypeError, ValueError):
        errs.append("sittings.freeze_before_min / push_before_min: integers (minutes)")
        sittings = {"tz": "UTC", "times": [], "freeze_before_min": 10, "push_before_min": 10}
    if not times:
        warns.append("sittings.times is empty: the status line shows no next sitting")

    budget = dict(raw.get("budget")) if isinstance(raw.get("budget"), dict) else {}
    model = raw.get("model")
    if model is not None and not _is_str(model):
        errs.append("model: a string (e.g. fable)")
        model = None

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
        if pm is not None and not _is_str(pm):
            errs.append(f"sessions.{role}.permission_mode: a string when set (it is passed to claude only when set)")
            pm = None
        brief, prompt = s.get("brief"), s.get("prompt")
        if brief is not None and not _is_str(brief):
            errs.append(f"sessions.{role}.brief: a file path relative to the project")
            brief = None
        if prompt is not None and not _is_str(prompt):
            errs.append(f"sessions.{role}.prompt: a string")
            prompt = None
        flags = _strlist(s.get("flags"), f"sessions.{role}.flags", errs, [])
        if role == "worker":
            prompt = prompt or DEFAULT_WORKER_PROMPT
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
                                     DEFAULT_WORKER_PROMPT, [], False, False, "claude")
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
    bind = BIND_ALIASES.get(str(pn.get("bind", "loopback")))
    if bind is None:
        errs.append("panel.bind: loopback (the default) or intranet")
    try:
        refresh_s = int(pn.get("refresh_s", 30) or 30)
    except (TypeError, ValueError):
        errs.append("panel.refresh_s: an integer (seconds)")
        refresh_s = 30
    panel = {"port": port, "bind": bind or "loopback", "link_host": str(pn.get("link_host", "auto")),
             "state_dir": str(pn.get("state_dir") or ".ops/state"), "refresh_s": refresh_s,
             "redact": str(pn.get("redact", "default"))}

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
                  deploy_kinds=deploy_kinds, human_only=human_only, sittings=sittings, budget=budget,
                  model=str(model) if model else None, sessions=sessions, panel=panel, aws=aws, contact=contact,
                  warnings=warns)


def summary(cfg: Config) -> str:
    roles = " ".join(r for r in cfg.sessions if r != "worker")
    worker = cfg.sessions["worker"]
    return (f"project {cfg.name}/{cfg.short}, root {cfg.root}, trunk {cfg.trunk}"
            f"{', prod ' + cfg.prod if cfg.prod else ''}, tickets {cfg.tickets_dir}/ via {cfg.tickets_script}, "
            f"sessions: {roles}, worker pattern {worker.name}, panel {cfg.panel['port']} {cfg.panel['bind']}, "
            f"sittings {', '.join(cfg.sittings['times']) or 'none'} ({cfg.sittings['tz']})")
