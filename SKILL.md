---
name: zouinify
description: Project management pipeline for multi-session Claude Code work -- tickets (backlog -> done via scripts/ticket.sh), a tmux session set (desk, manager, debug, chrome_debug, workers) and a local read-only control panel. Use for `/zouinify config|up|launch|status|down|briefs|panel|board|human` or when asked to launch a worker, start the standard sessions, start or find the panel, show the board or the human inbox, or set up ticket-based management in a repo.
argument-hint: config [--check|--json] | up [role...] [--dry-run] [--no-panel] | launch <worker T-xxxx|desk|manager|debug|chrome_debug> [--receipt ID] [--dry-run] | status [--line|--json] | down [name...] [--dry-run] | briefs render [--out DIR] | panel start|stop|status|url|token rotate | term start|stop|status|url|secret rotate|audit [--since D] [--target NAME] | board | human [--count|--json]
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/bin/zouinify *), Bash(scripts/ticket.sh board), Bash(scripts/ticket.sh human *), Bash(tmux list-*), Bash(tmux has-session *), Bash(claude agents *)
disable-model-invocation: false
---

# /zouinify $ARGUMENTS

Run `${CLAUDE_SKILL_DIR}/bin/zouinify $ARGUMENTS` from the project root (the directory holding `.ops/config.yaml`, or any
directory under it) and relay its output verbatim. Commands today: `config`, `up`, `launch`, `status`, `down`, `briefs`,
`panel start|stop|status|url|token rotate` (the read-only control panel: loopback by default, `panel.bind: intranet` with
the browser token, see `docs/REFERENCE.md`), `board`, `human`, `version`, `init` (bootstraps a repository: `.ops/config.yaml`,
`.ops/briefs/*.md`, the `.ops/state/` line in `.gitignore`; it asks its questions on the human's terminal, so from a session
run `init --non-interactive --name N --short S --human H` with values the human gave, adding `--dry-run` to show the plan
first; it never overwrites an `.ops/` file and never touches `~/.claude` or `CLAUDE.md`), `config --env` (prints `tickets/ticket.env`
for the ticket.sh core; `--write` never into a repository's main worktree, and a ticket commits it), `protocol render --out DIR`
(the README / _TEMPLATE / WORKER_BRIEF of the ticket protocol rendered from templates into DIR only). Receipts (`poll`) are a later
step and are not available yet. `term` is the terminal page (T-0181): typing into one of this project's WORKER panes from the
browser, after a passphrase of its own; the desk, manager and debug windows are never reachable from it.

Rules this skill never bends (they come from `.ops/config.yaml` and the project's `tickets/README.md`):

- The lifecycle is `scripts/ticket.sh`'s. This skill never edits `tickets/*.md` and never calls a mutating `ticket.sh` command
  except `claim` inside a worker it launched (the worker runs it, not the skill) and the one `ticket.sh log` line of
  `launch --receipt`, written on the desk's behalf and signed by its `TICKET_AS`.
- Every session is launched with the project's model, `-n <name>`, the permission mode the config sets for that session (none
  is passed when none is set), the brief file as the prompt, and `TICKET_AS=<name>` in its environment. `up` and `launch` are
  idempotent: an existing tmux session of that name is reported, never duplicated. `up`, `launch` and `down` are run by the
  human from a shell or by the desk session; the panel process never runs them.
- `launch worker T-xxxx` only for a ticket that is `ready`, unclaimed and not frozen out; `down` only for launcher-registered
  workers whose ticket is `done`/`dropped`, never a desk/manager/debug/chrome_debug session; always `down --dry-run` first.
- No session launched by this skill may run a production mutation (`terraform apply`, image pushes, deploy scripts, cloud
  writes): the brief says so and the human-only list in the config is printed into every brief.
- The panel is read-only for everything a session can see (v1 has no write path at all: any POST is 405, every control on the
  page is disabled until receipts exist); its future write path accepts only the human's browser token; a click is a receipt
  the desk verifies, never the human's word; tier-2 items (paid budget, `CLAUDE.md`/settings changes, production apply,
  permission prompts) are typed by the human in the desk's window, never clicked. The panel process never starts, stops or
  types into a session and never runs a mutating `ticket.sh` command: `panel start|stop` is run by the human or the desk.
- The terminal page (`zouinify term`, a separate process outside the guarded panel package) is the one way keystrokes reach a
  Claude pane from the browser, and only a WORKER pane (user decision 2026-10-13, "Z-11 yes workers"; the role sessions are
  refused in code): it forwards what the human types, after a passphrase shown once in the human's own terminal (its own secret, never the panel's token), into
  Claude panes of this project only, with an audit line per batch of keys; no button, receipt or script ever produces a keystroke.
- If a command here is refused by the permission classifier, file it with `scripts/ticket.sh ask` (one line) and continue;
  never ask a peer session to run it.
- `init --protocol` writes tracked files (`tickets/`, `scripts/ticket.sh`, a `CLAUDE.md` block) and refuses to run from a
  session: show the human its plan with `init --protocol --dry-run` and relay it; the human runs `zouinify init --protocol` in
  their own terminal and types yes there (a relayed answer never counts for `CLAUDE.md` edits).
