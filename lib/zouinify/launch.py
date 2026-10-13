"""`up`, `launch` and `down`: the only code in zouinify that creates or kills tmux sessions.

These commands are run by the human from a shell or by the desk in its own session. The panel process
never calls them (manager rule, 2026-10-13); scripts/check_panel_guard.sh fails CI if lib/zouinify/panel/
ever imports this module or names new-session / kill-session / send-keys.

Every session is launched as
    tmux new-session -d -s <tmux> -c <root> -e TICKET_AS=<name> -e ZOUINIFY_PROJECT=<root>
        claude --model <model> -n <name> [--permission-mode <mode>] [flags] "<brief or prompt>"
and `--permission-mode` is passed only when the session's config sets it: there is no default mode.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from zouinify import ZouinifyError
from zouinify import agents as agentsmod
from zouinify import tickets as tk
from zouinify import tmux
from zouinify.config import ROLE_SESSIONS
from zouinify.registry import Registry

CLAUDE_BIN = os.environ.get("ZOUINIFY_CLAUDE", "claude")


class LaunchError(ZouinifyError):
    pass


def _by() -> str:
    return os.environ.get("TICKET_AS") or os.environ.get("USER") or "unknown"


def _now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def session_env(cfg, name: str) -> dict:
    return {"TICKET_AS": name, "ZOUINIFY_PROJECT": cfg.root}


def claude_argv(sess, name: str, prompt: str) -> list:
    argv = [CLAUDE_BIN, "--model", sess.model, "-n", name]
    if sess.permission_mode:
        argv += ["--permission-mode", sess.permission_mode]
    argv += list(sess.flags)
    argv.append(prompt)
    return argv


def brief_text(cfg, sess) -> str:
    """The prompt of a role session: its brief file's contents, else its literal prompt."""
    if sess.brief:
        p = cfg.path(sess.brief)
        if not os.path.isfile(p):
            raise LaunchError(f"{sess.role}: brief {sess.brief} not found under {cfg.root} "
                              "(write it, or `zouinify briefs render --out .ops/briefs`)")
        with open(p, encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text:
            raise LaunchError(f"{sess.role}: brief {sess.brief} is empty")
        return text
    return sess.prompt or ""


class Launcher:
    def __init__(self, cfg, out=None, wait_s: float = 30.0, dry_run: bool = False):
        self.cfg = cfg
        self.out = out or sys.stdout
        self.wait_s = wait_s
        self.dry_run = dry_run
        self.reg = Registry(cfg)
        self._agents = None

    def say(self, text: str) -> None:
        print(text, file=self.out)

    def agents(self) -> list:
        if self._agents is None:
            self._agents = agentsmod.list_agents()
        return self._agents

    @staticmethod
    def _row(role, name, tmux_name, verdict, note=""):
        return f"{role:<13} {name:<32} tmux {tmux_name:<28} {verdict:<10} {note}".rstrip()

    def report_attached(self, role: str, sess, tmux_name: str) -> None:
        a = agentsmod.find(self.agents(), sess.name, self.cfg.root)
        status = (a or {}).get("status") or "unknown"
        label = tmux.model_on_screen(tmux.capture_pane(tmux_name))
        note = f"status {status}, model {label or '?'}"
        want = tmux.alias_of(sess.model)
        if label and want and tmux.model_word(label) != want:
            note += f"  WARNING: the config says {sess.model}"
        self.say(self._row(role, sess.name, tmux_name, "attached", note))

    def start(self, role: str, sess, name: str, tmux_name: str, prompt: str, ticket: str | None = None,
              brief_rel: str | None = None) -> bool:
        """Create the session, wait for the status bar to show the model, record it. False = check it."""
        env = session_env(self.cfg, name)
        argv = claude_argv(sess, name, prompt)
        line = tmux.launch_line(tmux_name, self.cfg.root, env, argv, brief_rel)
        if self.dry_run:
            self.say(self._row(role, name, tmux_name, "would launch:"))
            self.say("  " + line)
            return True
        tmux.new_session(tmux_name, self.cfg.root, env, argv)
        t0 = time.monotonic()
        label = tmux.wait_for_model(tmux_name, self.wait_s)
        elapsed = int(round(time.monotonic() - t0))
        self.reg.add({"name": name, "tmux": tmux_name, "role": role, "ticket": ticket, "launched_at": _now_iso(),
                      "by": _by(), "model": sess.model, "permission_mode": sess.permission_mode, "cwd": self.cfg.root,
                      "line": line})
        want = tmux.alias_of(sess.model)
        if label is None:
            self.say(self._row(role, name, tmux_name, "launched",
                               f"model not seen in {int(self.wait_s)} s: attach to tmux {tmux_name} and check the status bar"))
            return False
        if want and tmux.model_word(label) != want:
            self.say(self._row(role, name, tmux_name, "launched",
                               f"WRONG MODEL: the status bar shows {label}, the config says {sess.model}; "
                               f"kill it (tmux kill-session -t ={tmux_name}) and check `claude --model`"))
            return False
        self.say(self._row(role, name, tmux_name, "launched", f"model {label} ({elapsed} s)"))
        return True


def cmd_up(cfg, roles=(), dry_run: bool = False, wait_s: float = 30.0, out=None) -> int:
    """Idempotent: an existing tmux session of the role's name is reported, never duplicated."""
    roles = list(roles)
    if roles:
        bad = [r for r in roles if r not in cfg.sessions]
        if bad:
            raise LaunchError(f"unknown role(s): {' '.join(bad)} (sessions in .ops/config.yaml: {' '.join(cfg.sessions)})")
        if "worker" in roles:
            raise LaunchError("a worker is launched per ticket: zouinify launch worker T-xxxx")
        chosen = [(r, cfg.sessions[r]) for r in roles]
    else:
        chosen = [(r, s) for r, s in cfg.role_sessions().items() if s.always]
        if not chosen:
            raise LaunchError("nothing to start: no session has `always: true`; name a role (zouinify up desk)")
    L = Launcher(cfg, out, wait_s, dry_run)
    ok = True
    for role, sess in chosen:
        if sess.kind != "claude":
            L.say(L._row(role, sess.name or "-", sess.tmux, "skipped", f"kind {sess.kind}: not started by zouinify"))
            continue
        if tmux.has_session(sess.tmux):
            L.report_attached(role, sess, sess.tmux)
            continue
        ok = L.start(role, sess, sess.name, sess.tmux, brief_text(cfg, sess), brief_rel=sess.brief) and ok
    if not roles:
        optional = [r for r, s in cfg.role_sessions().items() if not s.always and s.kind == "claude"]
        if optional:
            L.say("optional roles, started only when named: " + ", ".join(f"{r} (zouinify up {r})" for r in optional))
    return 0 if ok or dry_run else 1


def cmd_launch(cfg, target: str, ticket: str | None = None, receipt: str | None = None, dry_run: bool = False,
               wait_s: float = 30.0, out=None) -> int:
    if target == "worker":
        if not ticket:
            raise LaunchError("usage: zouinify launch worker T-xxxx [--receipt <id>] [--dry-run]")
        return _launch_worker(cfg, ticket, receipt, dry_run, wait_s, out)
    if ticket:
        raise LaunchError(f"launch {target} takes no ticket (only `launch worker T-xxxx` does)")
    if target not in cfg.sessions:
        raise LaunchError(f"unknown role {target} (sessions in .ops/config.yaml: {' '.join(cfg.sessions)})")
    sess = cfg.sessions[target]
    if sess.kind != "claude":
        raise LaunchError(f"{target} is kind {sess.kind}: not started by zouinify")
    L = Launcher(cfg, out, wait_s, dry_run)
    if tmux.has_session(sess.tmux):
        L.report_attached(target, sess, sess.tmux)
        return 0
    return 0 if L.start(target, sess, sess.name, sess.tmux, brief_text(cfg, sess), brief_rel=sess.brief) or dry_run else 1


def _launch_worker(cfg, tid: str, receipt, dry_run, wait_s, out) -> int:
    tk.check_id(tid)
    if receipt and not os.environ.get("TICKET_AS"):
        raise LaunchError("--receipt needs TICKET_AS in the environment: the log line is signed by the caller (the desk)")
    path = tk.file_of(cfg, tid)
    if not path:
        raise LaunchError(f"no ticket {tid} in {cfg.tickets_dir}/")
    fm = tk.front_matter(path)
    st = fm.get("status", "")
    if st != "ready":
        raise LaunchError(f"{tid} is '{st}', not 'ready': a worker is launched for a ready, unclaimed ticket only")
    if fm.get("owner"):
        raise LaunchError(f"{tid} already has an owner: {fm['owner']} (status {st})")
    if tk.freeze_on(cfg) and not tk.freeze_lists(cfg, tid):
        allowed = " ".join(tk.freeze_ids(cfg)) or "none"
        raise LaunchError(f"{cfg.tickets_dir}/FREEZE is on and does not list {tid} (allowed: {allowed}); ask the manager to list it")
    name, tmux_name = tk.worker_names(cfg, path)
    if tmux.has_session(tmux_name):
        raise LaunchError(f"tmux session {tmux_name} already exists: {tid} already has a worker window")
    sess = cfg.sessions["worker"]
    prompt = (sess.prompt or "").replace("{id}", tid)
    L = Launcher(cfg, out, wait_s, dry_run)
    ok = L.start("worker", sess, name, tmux_name, prompt, ticket=tid)
    if dry_run:
        return 0
    if receipt:
        line = f"launched {name} (panel receipt {receipt})"
        r = tk.ticket_sh(cfg, ["log", tid, line])
        if r.returncode != 0:
            L.say(f"WARNING: {cfg.tickets_script} log {tid} failed: {(r.stderr or r.stdout).strip()}")
            ok = False
        else:
            L.say(f"logged on {tid}: {line}")
    return 0 if ok else 1


def cmd_down(cfg, names=(), dry_run: bool = False, out=None) -> int:
    """Kill launcher-registered worker sessions whose ticket is done or dropped; refuse everything else,
    with the reason. Never a role session, never a session zouinify did not launch."""
    out = out or sys.stdout
    say = lambda t: print(t, file=out)  # noqa: E731
    reg = Registry(cfg)
    entries = reg.load()
    by_name = {e["name"]: e for e in entries}
    by_tmux = {e.get("tmux"): e for e in entries}
    role_of: dict = {}
    for r, s in cfg.role_sessions().items():
        role_of[s.name] = r
        role_of[s.tmux] = r
    explicit = bool(names)
    if explicit:
        targets = list(names)
    else:
        targets = [e["name"] for e in entries if e.get("role") not in ROLE_SESSIONS and e["name"] not in role_of]
        skipped = [e["name"] for e in entries if e["name"] not in targets]
        if skipped:
            say("role sessions are never killed by zouinify: " + ", ".join(skipped))
    killed = refused = 0
    for n in targets:
        e = by_name.get(n) or by_tmux.get(n)
        role = role_of.get(n) or (e or {}).get("role")
        if role in ROLE_SESSIONS or n in role_of:
            say(f"{n:<36} refused: role session ({role}); zouinify never kills desk/manager/debug/chrome_debug")
            refused += 1
            continue
        if not e:
            say(f"{n:<36} refused: not launcher-registered (only sessions zouinify launched, see {reg.path})")
            refused += 1
            continue
        tid = e.get("ticket")
        if not tid:
            say(f"{n:<36} refused: no ticket recorded for it")
            refused += 1
            continue
        path = tk.file_of(cfg, tid)
        st = tk.front_matter(path).get("status", "") if path else ""
        if st not in tk.TERMINAL:
            say(f"{n:<36} refused: ticket {tid} is '{st or 'missing'}', not done/dropped")
            refused += 1
            continue
        if not tmux.has_session(e["tmux"]):
            say(f"{n:<36} ticket {tid} {st}; already gone" + ("" if dry_run else ": removed from the registry"))
            if not dry_run:
                reg.remove(e["name"])
            continue
        if dry_run:
            say(f"{n:<36} ticket {tid} {st}; would kill (dry-run)")
            killed += 1
            continue
        tmux.kill_session(e["tmux"])
        reg.remove(e["name"])
        say(f"{n:<36} ticket {tid} {st}; killed")
        killed += 1
    if killed == 0:
        say("nothing to kill" + (" (dry-run)" if dry_run else ""))
    elif dry_run:
        say(f"{killed} session(s) would be killed: run `zouinify down` without --dry-run")
    return 1 if explicit and refused else 0
