"""The `zouinify` command line: argument parsing and dispatch."""
from __future__ import annotations

import argparse
import os
import json
import sys

from zouinify import ZouinifyError, __version__
from zouinify import config as cfgmod

NOTE = ("`up`, `launch`, `down`, `poll` and `reply` are run by the human from a shell or by the desk session, never by the "
        "panel process; the panel's one write is a receipt (a click the desk verifies). Everything project-specific comes from "
        "<project>/.ops/config.yaml.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zouinify", epilog=NOTE,
                                description="Management pipeline for multi-session Claude Code work: tickets, a tmux session "
                                            "set (desk, manager, debug, chrome_debug, one worker per ticket) and a local panel.")
    p.add_argument("--project", metavar="DIR", help="project root (default: $ZOUINIFY_PROJECT or the nearest .ops/config.yaml)")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("init", help="bootstrap this repository: .ops/config.yaml (asked for on a terminal), .ops/briefs/*.md, the "
                                    ".ops/state/ line in .gitignore; never overwrites, never writes outside the repository")
    s.add_argument("--dry-run", action="store_true", help="print the plan and the config it would write; write nothing")
    s.add_argument("--non-interactive", action="store_true", help="no questions: --name, --short and --human are required, the rest "
                                                                   "default to the proposals")
    s.add_argument("--protocol", action="store_true", help="second step, after the config exists: install the ticket protocol "
                   "(scripts/ticket.sh, tickets/ticket.env, tickets/plugins/none, the rendered README/_TEMPLATE/WORKER_BRIEF) after a printed "
                   "plan and your typed yes on this terminal, and prints the CLAUDE.md block for you to paste; never non-interactive, never from a session")
    for f, h in (("name", "project name"), ("short", "short tag prefixing the session names (1-8 lowercase letters or digits)"),
                 ("trunk", "trunk branch (default: the checked-out branch)"), ("prod", "production branch ('' for none)"),
                 ("model", "model for every session (default fable)"), ("port", "panel port (default 4700 + crc32(root) %% 90, free)"),
                 ("human", "your name, the human the briefs address")):
        s.add_argument(f"--{f}", metavar=f.upper(), help=h)

    s = sub.add_parser("config", help="validate .ops/config.yaml (--check), print it normalized (--json), or print tickets/ticket.env "
                                      "for the ticket.sh core (--env; --write puts it in a ticket worktree, never a main one)")
    s.add_argument("--check", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--env", action="store_true", help="print tickets/ticket.env (bare KEY=value lines the core parses) to stdout")
    s.add_argument("--write", nargs="?", const="", metavar="PATH", help="with --env: write it to PATH (default <root>/<tickets dir>/"
                   "ticket.env); refused inside a repository's main worktree; never commits")

    s = sub.add_parser("protocol", help="the ticket protocol files (README, _TEMPLATE, WORKER_BRIEF) rendered from templates/protocol/tickets: "
                                        "render --out DIR | list")
    s.add_argument("action", choices=["render", "list"])
    s.add_argument("--out", metavar="DIR", help="render: the directory to write into (required; nothing is written anywhere else)")
    s.add_argument("--plugin-docs", metavar="DIR", help="render: the deploy plugin's docs directory (default templates/protocol/tickets/plugins/"
                   "<tickets.deploy_plugin>/docs, falling back to none)")
    s.add_argument("--project-readme", metavar="FILE", help="render: the project-owned README sections (default <root>/.ops/README.project.md)")
    s.add_argument("--force", action="store_true", help="render: overwrite files under --out that differ")
    s.add_argument("--main-root", metavar="DIR", help="render: the main checkout whose absolute path and directory name the files embed "
                   "(rule 1, the README title, the brief's opening, every repo path); default --project. From a ticket worktree name the "
                   "main checkout here, so the render matches one made from it. Env: ZOUINIFY_MAIN_ROOT")

    s = sub.add_parser("up", help="start the standard session set (always: true) or the named roles, idempotently")
    s.add_argument("roles", nargs="*", metavar="role")
    s.add_argument("--dry-run", action="store_true", help="print the exact launch lines, start nothing")
    s.add_argument("--wait", type=float, default=30.0, metavar="S", help="seconds to wait for the status bar's model (30)")
    s.add_argument("--no-panel", action="store_true", help="do not start this project's panel (panel.autostart in the config)")

    s = sub.add_parser("launch", help="launch worker T-xxxx (ready, unclaimed, not frozen out) or one role session")
    s.add_argument("target", metavar="worker|<role>")
    s.add_argument("ticket", nargs="?", metavar="T-xxxx")
    s.add_argument("--receipt", metavar="ID", help="log `launched <name> (panel receipt ID)` on the ticket, signed by $TICKET_AS")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--wait", type=float, default=30.0, metavar="S")

    s = sub.add_parser("status", help="inbox, P0, creds, board counts (cached 30 s)")
    s.add_argument("--line", action="store_true", help="one line for the tmux status bar")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true")

    s = sub.add_parser("down", help="kill launcher-registered worker sessions whose ticket is done/dropped")
    s.add_argument("names", nargs="*", metavar="name")
    s.add_argument("--dry-run", action="store_true", help="list what would be killed (always run this first)")
    s.add_argument("--receipt", metavar="ID", help="log `killed <name> (panel receipt ID)` on each killed session's ticket, signed by $TICKET_AS")

    s = sub.add_parser("poll", help="the desk's side of the panel receipts: block until a click arrives, print each receipt with the "
                                    "exact script to run, exit (run it again after each batch)")
    s.add_argument("--timeout-ms", type=int, default=0, metavar="MS", help="give up after MS milliseconds with no receipt (default: wait)")
    s.add_argument("--since", default="", metavar="ID", help="replay every receipt after this id, acknowledged or not (a lost batch)")
    s.add_argument("--no-ack", action="store_true", help="print without acknowledging: the receipts come back on the next poll")
    s.add_argument("--ack", default="", metavar="ID[,ID]", help="acknowledge these receipt ids by hand and exit")

    s = sub.add_parser("reply", help="the desk's one-line verdict on a panel receipt, shown to the human on the page (never a ticket move)")
    s.add_argument("id", metavar="ID", help="the receipt id from zouinify poll")
    s.add_argument("text", metavar="TEXT", help="one line, at most 300 characters: what you verified, or why not")

    s = sub.add_parser("briefs", help="render the brief templates with this project's config")
    s.add_argument("action", choices=["render", "list"])
    s.add_argument("--out", metavar="DIR", help="output directory (default .ops/briefs)")
    s.add_argument("--force", action="store_true", help="overwrite briefs that were edited")

    s = sub.add_parser("panel", help="the read-only control panel of this project: start | stop | status | url | token rotate")
    s.add_argument("action", choices=["start", "stop", "status", "url", "token"])
    s.add_argument("what", nargs="?", help="with `token`: rotate (prints the new browser token once, on your own terminal)")
    s.add_argument("--foreground", action="store_true", help="serve in this process (the detached start uses it)")

    s = sub.add_parser("term", help="the terminal page of this project (types into WORKER panes only): start | stop | status | url | secret rotate | audit")
    s.add_argument("action", choices=["start", "stop", "status", "url", "secret", "audit"])
    s.add_argument("what", nargs="?", help="with `secret`: rotate (shows the new passphrase once, on your own terminal)")
    s.add_argument("--foreground", action="store_true", help="serve in this process (the detached start uses it)")
    s.add_argument("--since", default="10m", help="with `audit`: how far back, e.g. 10m, 2h, 1d (default 10m)")
    s.add_argument("--target", metavar="NAME", help="with `audit`: only this worker session")

    sub.add_parser("board", help="scripts/ticket.sh board, verbatim (extra arguments pass through)")
    sub.add_parser("human", help="scripts/ticket.sh human, verbatim (extra arguments pass through)")
    sub.add_parser("version", help="print the version")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args, extra = p.parse_known_args(argv)
    if args.cmd is None:
        p.print_help(sys.stderr)
        return 2
    if args.cmd == "version":
        print(__version__)
        return 0
    if extra and args.cmd not in ("board", "human"):
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        if args.cmd == "briefs" and args.action == "list":
            from zouinify import briefs
            return briefs.cmd_list()
        if args.cmd == "init":                                # before the config load: there is no config yet
            from zouinify import initcmd
            flags = {f: getattr(args, f) for f in initcmd.FIELDS if getattr(args, f) is not None}
            if args.protocol:
                if args.non_interactive or flags:
                    p.error("--protocol takes no --non-interactive and no field flags: it reads .ops/config.yaml and asks for your yes")
                from zouinify import protoinit
                return protoinit.cmd_protocol(args.project, dry_run=args.dry_run)
            return initcmd.cmd_init(args.project, flags, dry_run=args.dry_run, non_interactive=args.non_interactive)
        cfg = cfgmod.load(args.project)
        if args.cmd == "config":
            if args.env:
                if args.json or args.check:
                    p.error("--env goes alone (not with --json or --check)")
                from zouinify import ticketenv
                return ticketenv.cmd_env(cfg, write=args.write)
            if args.write is not None:
                p.error("--write needs --env")
            if args.json:
                print(json.dumps(cfg.to_dict(), indent=1, sort_keys=True))
                return 0
            print(f"ok: {cfg.config_path}")
            print("  " + cfgmod.summary(cfg))
            for w in cfg.warnings:
                print(f"  warning: {w}")
            return 0
        if args.cmd == "protocol":
            from zouinify import protocol
            if args.action == "list":
                return protocol.cmd_list(cfg)
            if not args.out:
                p.error("protocol render needs --out DIR (it writes nowhere else)")
            return protocol.cmd_render(cfg, args.out, plugin_docs=args.plugin_docs, project_readme=args.project_readme, force=args.force,
                                       main_root=args.main_root or os.environ.get("ZOUINIFY_MAIN_ROOT") or None)
        if args.cmd == "up":
            from zouinify import launch
            return launch.cmd_up(cfg, args.roles, dry_run=args.dry_run, wait_s=args.wait, panel=not args.no_panel)
        if args.cmd == "launch":
            from zouinify import launch
            return launch.cmd_launch(cfg, args.target, args.ticket, receipt=args.receipt, dry_run=args.dry_run, wait_s=args.wait)
        if args.cmd == "down":
            from zouinify import launch
            return launch.cmd_down(cfg, args.names, dry_run=args.dry_run, receipt=args.receipt)
        if args.cmd == "poll":
            from zouinify import poll
            return poll.cmd_poll(cfg, timeout_ms=args.timeout_ms, since=args.since, no_ack=args.no_ack, ack_ids=args.ack)
        if args.cmd == "reply":
            from zouinify import poll
            return poll.cmd_reply(cfg, args.id, args.text)
        if args.cmd == "status":
            from zouinify import status
            data = status.collect(cfg, use_cache=not args.no_cache)
            if args.json:
                print(json.dumps(data, indent=1, sort_keys=True))
            elif args.line:
                print(status.line(data))
            else:
                print(status.long(data))
            return 0
        if args.cmd == "briefs":
            from zouinify import briefs
            return briefs.cmd_render(cfg, args.out, force=args.force)
        if args.cmd == "panel":
            from zouinify import panelctl
            if args.action != "token" and args.what:
                p.error(f"panel {args.action} takes no argument (got {args.what!r})")
            if args.action == "start":
                return panelctl.cmd_start(cfg, foreground=args.foreground)
            if args.action == "stop":
                return panelctl.cmd_stop(cfg)
            if args.action == "status":
                return panelctl.cmd_status(cfg)
            if args.action == "token":
                if args.what != "rotate":
                    p.error("usage: zouinify panel token rotate")
                return panelctl.cmd_token_rotate(cfg)
            return panelctl.cmd_url(cfg)
        if args.cmd == "term":
            from zouinify import termctl
            if args.action != "secret" and args.what:
                p.error(f"term {args.action} takes no argument (got {args.what!r})")
            if args.action == "start":
                return termctl.cmd_start(cfg, foreground=args.foreground)
            if args.action == "stop":
                return termctl.cmd_stop(cfg)
            if args.action == "status":
                return termctl.cmd_status(cfg)
            if args.action == "secret":
                if args.what != "rotate":
                    p.error("usage: zouinify term secret rotate")
                return termctl.cmd_secret_rotate(cfg)
            if args.action == "audit":
                return termctl.cmd_audit(cfg, since=args.since, target=args.target)
            return termctl.cmd_url(cfg)
        if args.cmd in ("board", "human"):
            from zouinify import tickets
            r = tickets.ticket_sh(cfg, [args.cmd, *extra], capture=False)
            return r.returncode
        p.error(f"unknown command {args.cmd}")
    except ZouinifyError as e:
        print(f"zouinify: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
