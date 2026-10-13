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
| `zouinify status [--line\|--json] [--no-cache]` | `inbox N \| P0 n \| creds <left> \| d dev r rev m rtd k dep` from `ticket.sh human --count`, the ticket front matter, the credential files' metadata (SSO cache `expiresAt`, else credentials-file mtime + `aws.assumed_token_hours`, marked `est`) Cached 30 s: made for `tmux set -g status-right '#(zouinify --project /path/to/repo status --line)'` (the tmux server's cwd is not the project, so name it). `P0` counts the inbox's P0 items, what the human acts on, not every open P0 ticket. |
| `zouinify down [name...] [--dry-run]` | Kill launcher-registered **worker** sessions whose ticket is `done` or `dropped`; prints the reason for every refusal (a role session, a session zouinify did not launch, a ticket still open). Run `--dry-run` first; a named target that is refused exits 1. |
| `zouinify briefs render [--out DIR] [--force]`, `briefs list` | Render `templates/briefs/*.md.tmpl` (desk, manager, debug, chrome_debug, worker) with the config: `{{desk.name}}`, `{{trunk}}`, `{{prod}}`, `{{human_only}}`, `{{budget}}`, `{{ask_verbs}}`, ... Refuses to overwrite an edited brief without `--force`. |
| `zouinify panel start\|stop\|status\|url` | This project's read-only panel on `http://127.0.0.1:<panel.port>/`, one detached process per project (pid file and log under `.ops/state/`, an entry in `~/.zouinify/projects.json`). `start` probes the port first: this project's panel is reused, another project's or a foreign process is refused, never taken over; `stop` stops only this project's and only a process whose command line is this project's panel. `up` starts it unless `panel.autostart: false` or `--no-panel`. `panel.bind: intranet` listens on 0.0.0.0 and requires the browser token on every LAN request (see LAN access). |
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

## The control panel (v1: read-only)

Read-only for everything a session can see, derived at render time with **zero state of its own**: the human inbox (from
`ticket.sh human --json`, in the command's order, with each item's tier), the board as five lanes (`ready | in-dev | review |
to deploy | deployed`) plus chips for `backlog`, `blocked`, `done`, `dropped`, every session of the project with its tmux window,
model (read from the status bar; red when it is not the configured model, `?` when no status bar is on screen), state
(`waiting for you | working | idle | gone | shell`) and last visible line, the panes waiting at a permission prompt and since when,
the log tails and the newest-plan rule from `panel.logs` / `panel.plans_glob`, credential time left, the
deployed tickets with their latest evidence line ("Live checks"), the parsed config and the hook status ("Settings"). Sources:
`ticket.sh` (read-only modes), `claude agents --json`, `tmux list-panes` / `capture-pane` (redacted server-side), the launcher
registry, the ticket front matter and release records. The page is static HTML/CSS/JS served by a stdlib server: no build step,
no framework, no request to any host but the panel's own. Its API: `GET /health`, `GET /api/snapshot`, `GET /api/tickets/<id>`,
`GET /api/sessions/<name>`, `GET /api/logs?label=<a panel.logs label>`; every other method is 405; a request whose `Host` is not
`127.0.0.1:<port>` / `localhost:<port>`, or whose `Origin` is foreign, is 403 (DNS rebinding). Inbox tiers: `APPROVE`, `APPLY`,
`DEPLOY`, `PUSH` and a `DECIDE` that names `CLAUDE.md`/settings are tier 2 and never get a button, in any version. The look, tiers
and acceptance criteria are in [`UX_SPEC.md`](UX_SPEC.md).

The panel package is held to an **allowlist**, checked in CI and by the tests: `scripts/check_panel_ast.py` parses every file
under `lib/zouinify/panel/` and refuses any import outside its list, any attribute of an allowed module that is not on that
module's list (so `http.client`, `http.server.shutil` or `re.functools` are out), any module used as a value (no aliasing), any
reference to `getattr`/`__import__`/`exec`/`eval`/`compile`, `open`/`print` as values, `*`/`**` call arguments, any file opened
for writing, any attribute starting with `_` or named `os`/`subprocess`/`sys`/`ticket_sh`/`kill`/`replace`/`remove`/`write`
(except the response's `wfile`)/...; `scripts/check_panel_guard.sh` adds a regex denylist on every file type. The panel reads
only through `lib/zouinify/readonly.py` and writes only through `lib/zouinify/panelstate.py`, whose two files sit at
`<project root>/.ops/state/{panel.json,server.log}`, fixed in code, never following a symlink. With no way to spawn, import
dynamically or write, a click cannot become a launch, a kill, a keystroke, a ticket move or a deploy.

### Waiting for me (T-0175)

`#/waiting` lists every Claude session of yours that `claude agents --json` reports as waiting on you, across all your
projects, this project first. Each row names the tmux window; answer from your own terminal with
`tmux attach -t <window>` (the page copies the window name; the panel itself never names or runs a tmux command). Rows
of other projects show the project name from `~/.zouinify/projects.json`, or the directory name (`~` for your home
directory), never a path. Their drawer shows an allowlist, not a redaction: the question and option lines of the
dialog, and only when every line is plain printable text (no path, address, host name, key- or id-shaped string, long
number, cookie or credential text); otherwise the row says "waiting (screen withheld)" with the reason, and you attach
to the window to read it. The capture is
the last visible screen only, never scrollback.

## UX specification

The panel's look, screen anatomy, tiers of clickable actions and acceptance criteria are in
[`docs/UX_SPEC.md`](UX_SPEC.md). It is the UI contract for the panel tickets; the design document in the first
consumer's repo wins over it for behaviour.

## LAN access: `panel.bind: intranet` and the browser token

The default is loopback: the panel listens on 127.0.0.1 and asks nothing of a loopback client (every process on the
workstation runs as the same user and can read the same data directly). `panel.bind: intranet` in `.ops/config.yaml` makes it
listen on 0.0.0.0 for the lab network (a phone, a laptop), and then **every request from a non-loopback address needs the
browser token**, the page and its static files included:

1. In your own terminal, never inside a Claude session (the command refuses when `CLAUDECODE` is set or stdin is not a TTY;
   that refusal keeps the token out of a session transcript by accident, it is no boundary against a deliberate same-user
   process, which can read `.ops/state/` anyway): `zouinify panel token rotate`. It generates 32 random bytes, keeps only `sha256(token)` and a 4-hex token id in
   `.ops/state/panel.json` (mode 0600), prints `http://<link_host>:<port>/#t=<token>` once and clears the screen after Enter.
2. Open that address on the device. The `#t=` fragment never reaches the server; a token-less navigation gets a 401 unlock
   page whose only script (pinned by a CSP hash) sends the token once as `Authorization: Bearer` to `GET /api/unlock`; the
   server answers with the `zouinify_token` cookie, `HttpOnly` and `SameSite=Strict`, and the page reloads. From then on
   every request carries it (the cookie, or `Authorization: Bearer <token>`); the server compares it in constant time.
3. Wrong or missing token: 401 and an `AUDIT` line in `server.log` with the source address (never the token). Refusals from
   one address are budgeted per minute (a wrong Bearer counts 1, a missing token or a wrong cookie 0.2, budget 10): beyond
   it, 429 for the rest of the minute. Idle or half-open connections are dropped after 30 s. A `Host` other than 127.0.0.1, localhost, the
   configured `link_host`, the machine's names or its IPv4 addresses, or a foreign `Origin`: 403 (DNS rebinding). Rotate any
   time with the same command; running servers pick the new hash up without a restart, old tokens stop at once.

Two facts to know before you rely on it. **Loopback is token-free in both modes**, and that means any other *local user* of
the workstation (or anyone with an ssh -L to it) reaches the panel without a token: the read-only panel shows what every
same-user process could read anyway, but on a shared machine other accounts can read it too (true since the first panel). And
**cookies ignore ports**: a browser scopes cookies by host name, not by port, so every web server reachable under the
panel's host name, on any port, RECEIVES the `zouinify_token` cookie with every request that browser makes to it. `HttpOnly`
only keeps their page scripts from reading it; a server process under that name that logs its request headers has the
token. Such a server can also set a junk `zouinify_token`, which the panel then refuses on every request (weighed like a
missing token, so the unlock address still works). The real fixes are a dedicated host name for the panel that nothing
else serves under (a DNS alias or a hosts entry), or HTTPS with a `__Host-` cookie; until then keep the LAN name of the
panel for the panel and run no other service under it. A loopback token or a peer-uid check is a follow-up ticket.

`zouinify panel start` and `zouinify up` refuse to start an intranet panel without a token and point at the command; neither
ever prints a token. `zouinify panel url` prints the address without it. Writes stay 405 in both modes; tier-2 items never get
a button. **HTTP on the LAN is clear text**: use it on the lab network only; from anywhere else, `ssh -L 4790:127.0.0.1:4790
<workstation>` to the loopback panel. Worst case of a stolen token: reading the board, the inbox and the redacted pane
captures; never a launch, a kill, a keystroke, a ticket move or a deploy, because no endpoint does those.

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
  panelctl.py                 panel start/stop/status/url and the user-level projects list (the process side, outside panel/)
  panelstate.py               the server's own pid file and log (the only files the panel package may write)
  webauth.py                  the request-side checks (Host/Origin, bearer/cookie, weighted rate limiter, the decision) shared by the panel and the terminal page
  panel/                      the read-only server: sources.py (cached sources), server.py (GET-only HTTP), redact.py, static/
templates/
  ops/config.yaml.example     the per-project config
  briefs/*.md.tmpl            desk, manager, debug, chrome_debug, worker
  tickets/, scripts/, claude/, hooks/   (week 3: the vendored lifecycle core, the CLAUDE.md section, the hook)
readonly.py                 the read-only facade the panel may import (captures, pane list, human/board --json)
                            (with panelstate.py and webauth.py the only zouinify modules the panel package may import)
scripts/check_panel_ast.py    CI merge gate: the panel package uses only allowlisted imports and attributes, no dynamic import, no exec, no write
scripts/check_panel_guard.sh  CI regex denylist on every file type under the panel package
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
