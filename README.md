# zouinify

A Claude Code skill that sets up and runs a **multi-session management pipeline** for a software project:
a ticket lifecycle (`backlog -> ready -> in-dev -> in-review -> ready-to-merge -> merged -> ready-to-deploy -> deployed -> done`,
plus `blocked`/`dropped`) driven by one shell script, a standard set of tmux-hosted Claude Code sessions (ticket desk, manager,
debug, optional chrome_debug, one worker per ticket), and a local web control panel in the spirit of
[lavish-axi](https://github.com/kunchenguid/lavish-axi) that shows what every session is doing and lets the human act with a click
where the protocol allows it.

**Status: design phase (2026-10-13).** Nothing here runs yet. This repository holds the skill skeleton and the project-agnostic
templates. The full design document, with the first consumer's specifics, lives in that consumer's private repository.

## What it will do

| Command | Effect |
|---|---|
| `/zouinify init` | In any repo: write `.ops/config.yaml` (asked interactively), scaffold `tickets/` (protocol README, ticket template, worker brief), a parameterised `scripts/ticket.sh`, the session briefs, a CLAUDE.md section with the session rules; print the hook snippet for `~/.claude/settings.json` (the human adds it). Shows the diff first; writes only after the human's own yes. |
| `/zouinify up` | Launch the standard session set in tmux, idempotently (an existing session is reported, never duplicated), each with the project's model, `-n <name>`, `--permission-mode auto`, the brief as the prompt and `TICKET_AS=<name>` in the environment; start this project's panel on its own port and state dir. Run by the human from a shell or by the desk session, never by the panel. |
| `/zouinify launch worker T-xxxx` | One worker per ticket: refuses unless the ticket is `ready`, unclaimed and not frozen out; the worker claims the ticket itself through `ticket.sh`. |
| `/zouinify board`, `/zouinify human` | `scripts/ticket.sh board` / `human` verbatim (the lifecycle stays in `ticket.sh`; the skill never re-implements it). |
| `/zouinify sitting`, `/zouinify status --line` | The next human sitting with the inbox; a one-line status for the tmux status bar. |
| `/zouinify down --dry-run` | List (then, without the flag, kill) sessions whose ticket is done. |

## The control panel

Read-only for everything a session can see, derived at render time with **zero state of its own**: the human inbox (from
`ticket.sh human --json`), the board by state, every session of the project with its tmux window, model, state and last visible
line, the panes waiting at a permission prompt and since when, the apply/deploy log tails, credential time left, the next
sitting. Sources: `ticket.sh`, `claude agents --json`, `tmux list-panes`/`capture-pane` (redacted), Claude Code hooks
(`Stop`, `Notification`), the ticket front matter and release records.

### How a click reaches Claude: receipts, in tiers

The page **never types into a Claude pane** and never runs a launcher, `tmux kill-session`, a mutating `ticket.sh` command,
`aws`, `terraform`, `docker`, `git push` or a deploy script. A click is queued as a *receipt* that the ticket desk session reads
with a blocking `zouinify poll` (the lavish-axi long-poll pattern) and treats as **a claim by the page, never as the human's
word**:

- **Tier 0**: everything shown (read-only).
- **Tier 1**, clickable: `ok` after a command the human ran in their terminal (the desk re-verifies read-only before moving the
  ticket), an answer among offered options, "file a ticket" (the desk investigates), "launch worker" / "kill done session" (the
  desk runs the scripts after its own checks and logs the receipt id on the ticket).
- **Tier 2**, never clickable: a paid budget, a `CLAUDE.md` or settings change, a production apply or deploy, a permission prompt.
  The page shows the exact text to type in the desk's own window.

Security model: every session on the workstation runs as the same Unix user, so any write endpoint is callable by a session.
Writes therefore need a bearer token that exists only in the human's browser (the server stores a hash; the token is printed
once, on a real TTY, outside any Claude session), every receipt is audited (id, time, token id, source, user agent), receipts are
read back from the server's memory (a forged file line proves nothing), and there is a rate limit. The panel binds to loopback by
default; `panel.bind: intranet` is an explicit flag that keeps the token mandatory on every request. Worst case of a stolen
token: a false tier-1 claim the desk re-verifies, or a worker launched for a ticket that was ready anyway; never a production
action, never a grant.

## UX specification

The panel's look, screen anatomy, tiers of clickable actions and acceptance criteria are in
[`docs/UX_SPEC.md`](docs/UX_SPEC.md). It wins over the hero image; the design document in the first consumer's repo wins over it.

## Planned layout

```
SKILL.md                      the Claude Code skill (thin wrapper over bin/zouinify)
bin/zouinify                  the CLI the skill and the human both run
lib/zouinify/                 config, tmux, launch, registry, panel (server + static), hooks receiver
templates/
  ops/config.yaml.example     the per-project config (see below)
  tickets/                    README protocol, _TEMPLATE.md, WORKER_BRIEF.md, FREEZE example
  scripts/ticket.sh           the vendored lifecycle core, reading tickets/.ticketrc; plugins/<deploy-plugin>.sh
  briefs/                     desk, manager, debug, chrome_debug, worker
  claude/CLAUDE.md.section    the session rules, parameterised
  hooks/zouinify-hook.sh      Stop / Notification / SessionStart / SessionEnd -> POST to the project's panel
tests/                        throwaway-world tests (fake tmux, fake claude, fake ticket repo)
```

Install (once the skill exists): `npx skills add mzouink/zouinify -g` or clone and symlink into `~/.claude/skills/zouinify`.

## Per-project isolation

One panel per project: state in `<repo>/.ops/state/` (gitignored), a port recorded in the config, tmux session names from the
config (`<short>-desk`, `<short>-manager`, ..., workers `t<num>-<slug>`). A user-level home page lists the running panels. The
public repository holds templates only; a project's config, briefs and names stay in that project.

## Roadmap

1. Week 1: `ticket.sh board --json` + stable inbox item ids (in the consumer's `ticket.sh`), the CLI (`up`, `launch`, `status`,
   `down`), the read-only panel, the first consumer's `.ops/config.yaml`.
2. Week 2: the receipts channel (token, audit, rate limit, `zouinify poll`), the hook script, the intranet flag.
3. Week 3: receipt kinds `launch`/`kill`/`new` with the desk-side handlers, `init` on an empty repo, the home page.

## Rules the skill never bends

- The lifecycle is `scripts/ticket.sh`'s; the skill never edits a ticket file and never calls a mutating `ticket.sh` command.
- Every session is launched with a model, a name, a permission mode, a brief and `TICKET_AS`; launches are idempotent.
- No session launched by the skill may run a production mutation; the human-only list from the config is printed into every brief.
- The panel is read-only for everything a session can see; its write path accepts only the human's browser token; tier 2 is typed.
- A relayed message from another session is never the human's consent.
