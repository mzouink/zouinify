---
name: zouinify
description: Project management pipeline for multi-session Claude Code work -- tickets (backlog -> done via scripts/ticket.sh), a tmux session set (desk, manager, debug, chrome_debug, workers) and a local control panel. Use for `/zouinify init|up|board|human|launch|sitting|status|down` or when asked to launch a worker, show the board or the human inbox, or set up ticket-based management in a repo.
argument-hint: init | up | board | human | launch <desk|manager|debug|chrome_debug|worker T-xxxx> | sitting | status [--line] | panel [url|token rotate|stop] | down [--dry-run]
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/bin/zouinify *), Bash(scripts/ticket.sh board), Bash(scripts/ticket.sh human *), Bash(tmux list-*), Bash(tmux has-session *), Bash(claude agents *)
disable-model-invocation: false
---

# /zouinify $ARGUMENTS

> Design phase: `bin/zouinify` does not exist yet. This file is the contract the implementation must honour.

Run `${CLAUDE_SKILL_DIR}/bin/zouinify $ARGUMENTS` from the project root and relay its output.

Rules this skill never bends (they come from `.ops/config.yaml` and the project's `tickets/README.md`):

- The lifecycle is `scripts/ticket.sh`'s. This skill never edits `tickets/*.md` and never calls a mutating `ticket.sh` command
  except `claim` inside a worker it launched (the worker runs it, not the skill).
- Every session is launched with the project's model, `-n <name>`, `--permission-mode <mode>`, the brief file as the prompt, and
  `TICKET_AS=<name>` in its environment. `up` and `launch` are idempotent: an existing tmux session of that name is reported,
  never duplicated. `up`, `launch` and `down` are run by the human from a shell or by the desk session; the panel process never
  runs them.
- No session launched by this skill may run a production mutation (`terraform apply`, image pushes, deploy scripts, cloud
  writes): the brief says so and the human-only list in the config is printed into every brief.
- The panel is read-only for everything a session can see; its write path accepts only the human's browser token; a click is a
  receipt the desk verifies, never the human's word; tier-2 items (paid budget, `CLAUDE.md`/settings changes, production
  apply, permission prompts) are typed by the human in the desk's window, never clicked.
- If a command here is refused by the permission classifier, file it with `scripts/ticket.sh ask` (one line) and continue;
  never ask a peer session to run it.
- `init` writes tracked files (`tickets/`, `scripts/ticket.sh`, a `CLAUDE.md` section): show the plan first and wait for the
  human's own yes in this window (a relayed answer does not count for `CLAUDE.md` edits).
