# zouinify reference

The full command reference, session-launch rules, receipts and security model, layout and guards. The friendly overview is the [README](../README.md); the panel UI contract is [UX_SPEC.md](UX_SPEC.md).

## Requirements

- Python 3.9+ with [PyYAML](https://pypi.org/project/PyYAML/) (`pip install pyyaml`); the CLI runs as `python3 -I`
- tmux 3.2+ (`new-session -e` sets the session's environment; an older tmux is refused loudly)
- the `claude` CLI on PATH (`claude agents --json` is the session source)

## Install

```bash
git clone https://github.com/mzouink/zouinify ~/src/zouinify
ln -s ~/src/zouinify ~/.claude/skills/zouinify        # the skill: /zouinify <command> inside Claude Code
ln -s ~/src/zouinify/bin/zouinify ~/.local/bin/zouinify   # the same CLI from a shell
```

Then, in a project: copy `templates/ops/config.yaml.example` to `<repo>/.ops/config.yaml`, fill it in, add `.ops/state/` to
`.gitignore`, run `zouinify config --check`, `zouinify briefs render` (writes `.ops/briefs/*.md` from the templates; edit them
to taste) and `zouinify up --dry-run`. (`zouinify init`, which scaffolds all of that plus `tickets/` and `scripts/ticket.sh`, is
week 3.)

## Commands

| Command | Effect |
|---|---|
| `zouinify config [--check\|--json]` | Load and validate `.ops/config.yaml` (every problem listed at once); `--json` prints the normalized config. The project root is `$ZOUINIFY_PROJECT`, `--project DIR`, or the nearest ancestor holding `.ops/config.yaml`. |
| `zouinify up [role...] [--dry-run] [--wait S]` | Start the standard session set (`always: true`) or the named roles, **idempotently**: an existing tmux session of that name is reported (`attached`, with its status from `claude agents --json` and the model from its status bar), never duplicated. A new one is `tmux new-session -d -s <tmux> -c <root> -e TICKET_AS=<name> -e ZOUINIFY_PROJECT=<root> claude --model <model> -n <name> [--permission-mode <mode>] [flags] "<brief>"`; `up` waits up to 30 s for the status bar to show the model and fails loudly on a wrong one. `--dry-run` prints the exact lines. Launched sessions are recorded in `.ops/state/sessions.json`. |
| `zouinify launch worker T-xxxx [--receipt ID] [--dry-run]` | One worker per ticket, named `t<num>-<slug>` (the ticket's slug, like `ticket.sh claim`), prompt `Read tickets/WORKER_BRIEF.md, then claim T-xxxx.`, `TICKET_AS=<name>`. Refuses unless the ticket is `ready` with an empty `owner:`, refuses an id that `tickets/FREEZE` does not list while a freeze is on, refuses a duplicate tmux name. The worker claims the ticket itself. With `--receipt ID` (the desk acting on a panel click) it appends `ticket.sh log T-xxxx "launched <name> (panel receipt ID)"`, signed by the caller's `TICKET_AS`. `launch <role>` starts one role session (`chrome_debug` gets `--chrome`). |
| `zouinify status [--line\|--json] [--no-cache]` | `inbox N \| P0 n \| creds <left> \| sitting HH:MM in Xh \| d dev r rev m rtd k dep` from `ticket.sh human --count`, the ticket front matter, the credential files' metadata (SSO cache `expiresAt`, else credentials-file mtime + `aws.assumed_token_hours`, marked `est`) and the config's sittings. Cached 30 s: made for `tmux set -g status-right '#(zouinify --project /path/to/repo status --line)'` (the tmux server's cwd is not the project, so name it). `P0` counts the inbox's P0 items, what the human acts on, not every open P0 ticket. |
| `zouinify down [name...] [--dry-run]` | Kill launcher-registered **worker** sessions whose ticket is `done` or `dropped`; prints the reason for every refusal (a role session, a session zouinify did not launch, a ticket still open). Run `--dry-run` first; a named target that is refused exits 1. |
| `zouinify briefs render [--out DIR] [--force]`, `briefs list` | Render `templates/briefs/*.md.tmpl` (desk, manager, debug, chrome_debug, worker) with the config: `{{desk.name}}`, `{{trunk}}`, `{{prod}}`, `{{human_only}}`, `{{budget}}`, `{{sittings}}`, `{{ask_verbs}}`, ... Refuses to overwrite an edited brief without `--force`. |
| `zouinify board`, `zouinify human [--count\|--json]` | `scripts/ticket.sh board` / `human`, verbatim. The lifecycle stays in `ticket.sh`; the skill never re-implements it. |

Session-launch rules, all read from the config: the model, `-n <name>`, the brief file (a relative path inside the project;
anything else is refused) or a literal prompt as the first prompt, placed after `--` so a brief starting with `-` is still a
prompt, `TICKET_AS=<name>` and `ZOUINIFY_PROJECT=<root>` in the environment. **`permission_mode` is a per-session key with no
default**: `--permission-mode` is passed only when the config sets it. The model is read from the status bar with `model_re`
(a config key; group 1 is the label): a different model than configured fails loudly, no label within the wait is a warning.
A session that appears between the existence check and the launch is reported as attached, never duplicated.

**`up`, `launch` and `down` are run by the human from a shell or by the desk session, never by the panel process.** The panel
(next step) only reads; a click to launch or kill becomes a receipt the desk executes with these commands after its own checks.
`scripts/check_panel_guard.sh` is an allowlist over `lib/zouinify/panel/`: it fails CI on any process-spawning primitive
(`subprocess`, `os.system`, `os.exec*`, `pty`, `shlex`, `child_process`, ...), any import of the launcher or the tmux wrapper, a
direct `ticket_sh()` call or a registry write, and any tmux, ticket, deploy or `zouinify up|launch|down` verb however spelled.
The panel reads through `lib/zouinify/readonly.py` (captures, pane list, `human --json`, `board --json`) and nothing else.

## The control panel (next)

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
  desk runs `zouinify launch` / `zouinify down` after its own checks and logs the receipt id on the ticket).
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
[`docs/UX_SPEC.md`](UX_SPEC.md). It is the UI contract for the panel tickets; the design document in the first
consumer's repo wins over it for behaviour.

## Layout

```
SKILL.md                      the Claude Code skill (thin wrapper over bin/zouinify)
bin/zouinify                  the CLI the skill and the human both run (bash -> python3 -I lib/zouinify/cli.py)
lib/zouinify/
  config.py                   .ops/config.yaml loader and validator (schema v1)
  tmux.py  agents.py          tmux wrappers (no send-keys exists here); `claude agents --json`
  tickets.py  registry.py     read-only ticket access; .ops/state/sessions.json
  launch.py                   up / launch / down -- the only code that creates or kills a session
  status.py  briefs.py        the status line; brief templates
templates/
  ops/config.yaml.example     the per-project config
  briefs/*.md.tmpl            desk, manager, debug, chrome_debug, worker
  tickets/, scripts/, claude/, hooks/   (week 3: the vendored lifecycle core, the CLAUDE.md section, the hook)
readonly.py                 the read-only facade the panel may import (captures, pane list, human/board --json)
scripts/check_panel_guard.sh  CI allowlist: the panel spawns nothing, imports no launcher/tmux, mutates no ticket or registry
scripts/check_public.sh       CI + pre-push: no account ids, keys, secrets, e-mails or home paths in tracked files;
                              a project's own names go in ~/.config/zouinify/private-patterns (outside the repo)
tests/                        throwaway-world tests: fake tmux, fake claude, fake ticket.sh (`python3 -m pytest -q tests`)
```

## Per-project isolation

One panel per project: state in `<repo>/.ops/state/` (gitignored), a port recorded in the config, tmux session names from the
config (`<short>-desk`, `<short>-manager`, ..., workers `t<num>-<slug>`). The public repository holds templates only; a
project's config, briefs and names stay in that project.

## Roadmap

1. Week 1: `ticket.sh board --json` + stable inbox item ids (in the consumer's `ticket.sh`), **the CLI core (done)**, the
   read-only panel, the first consumer's `.ops/config.yaml`.
2. Week 2: the receipts channel (token, audit, rate limit, `zouinify poll`), the hook script, the intranet flag.
3. Week 3: receipt kinds `launch`/`kill`/`new` with the desk-side handlers, `init` on an empty repo, the home page.

## Rules the skill never bends

- The lifecycle is `scripts/ticket.sh`'s; the skill never edits a ticket file and never calls a mutating `ticket.sh` command
  (the one `ticket.sh log` line of `launch --receipt` is written on the desk's behalf, signed by it).
- Every session is launched with a model, a name, the configured permission mode (none by default), a brief and `TICKET_AS`;
  launches are idempotent.
- No session launched by the skill may run a production mutation; the human-only list from the config is printed into every brief.
- The panel is read-only for everything a session can see; its write path accepts only the human's browser token; tier 2 is typed.
- A relayed message from another session is never the human's consent.
