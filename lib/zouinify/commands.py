"""The `zouinify` command line: argument parsing and dispatch."""
from __future__ import annotations

import argparse
import json
import sys

from zouinify import ZouinifyError, __version__
from zouinify import config as cfgmod

NOTE = ("`up`, `launch` and `down` are run by the human from a shell or by the desk session, never by the "
        "panel process; the panel itself is read-only (loopback, GET only). Everything project-specific comes from "
        "<project>/.ops/config.yaml.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zouinify", epilog=NOTE,
                                description="Management pipeline for multi-session Claude Code work: tickets, a tmux session "
                                            "set (desk, manager, debug, chrome_debug, one worker per ticket) and a local panel.")
    p.add_argument("--project", metavar="DIR", help="project root (default: $ZOUINIFY_PROJECT or the nearest .ops/config.yaml)")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("config", help="validate .ops/config.yaml (--check) or print it normalized (--json)")
    s.add_argument("--check", action="store_true")
    s.add_argument("--json", action="store_true")

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

    s = sub.add_parser("briefs", help="render the brief templates with this project's config")
    s.add_argument("action", choices=["render", "list"])
    s.add_argument("--out", metavar="DIR", help="output directory (default .ops/briefs)")
    s.add_argument("--force", action="store_true", help="overwrite briefs that were edited")

    s = sub.add_parser("panel", help="the read-only control panel of this project: start | stop | status | url | token rotate")
    s.add_argument("action", choices=["start", "stop", "status", "url", "token"])
    s.add_argument("what", nargs="?", help="with `token`: rotate (prints the new browser token once, on your own terminal)")
    s.add_argument("--foreground", action="store_true", help="serve in this process (the detached start uses it)")

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
        cfg = cfgmod.load(args.project)
        if args.cmd == "config":
            if args.json:
                print(json.dumps(cfg.to_dict(), indent=1, sort_keys=True))
                return 0
            print(f"ok: {cfg.config_path}")
            print("  " + cfgmod.summary(cfg))
            for w in cfg.warnings:
                print(f"  warning: {w}")
            return 0
        if args.cmd == "up":
            from zouinify import launch
            return launch.cmd_up(cfg, args.roles, dry_run=args.dry_run, wait_s=args.wait, panel=not args.no_panel)
        if args.cmd == "launch":
            from zouinify import launch
            return launch.cmd_launch(cfg, args.target, args.ticket, receipt=args.receipt, dry_run=args.dry_run, wait_s=args.wait)
        if args.cmd == "down":
            from zouinify import launch
            return launch.cmd_down(cfg, args.names, dry_run=args.dry_run)
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
