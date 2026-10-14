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

Then, at a repository's root: `zouinify init`. It asks for the project name, the short tag that prefixes the session names,
the trunk and production branches, the model, the panel port and your name (Enter takes the proposal: the directory name,
its initials, the checked-out branch, `main`, `fable`, `4700 + crc32(root) % 90` with its three ports free, `$USER`) and
writes `.ops/config.yaml` (commented; the terminal block present but `enabled: false`), `.ops/briefs/*.md` from the templates
and the `.ops/state/` line in `.gitignore`, then runs `config --check` and prints `up --dry-run`'s plan. It never overwrites
an existing `.ops/` file (a second run lists what exists and exits 0), never writes outside the realpath'd repository root
(a directory that is not a git repository, a subdirectory, a symlinked `.ops`, `.ops/briefs` or brief are refused before
anything is written; a `.gitignore` that is a symlink, a hard link or a directory is skipped), never touches `~/.claude` or
`CLAUDE.md`. Values are printable text without double quotes or backslashes and go into the file quoted; the rendered file
is parsed and its top-level keys checked before it is written, and a run that fails after the first write removes what it
wrote. `--dry-run` prints the
plan and the config and writes nothing; `--non-interactive` takes the answers from `--name --short --human` (required) and
`--trunk --prod --model --port` (optional) for a caller without a terminal, such as a session. The tracked protocol files
(`tickets/`, `scripts/ticket.sh`, the `CLAUDE.md` section) are the second half (T-0185), written only after a plan and the
human's own yes. By hand instead: copy `templates/ops/config.yaml.example` to `<repo>/.ops/config.yaml`, fill it in, add
`.ops/state/` to `.gitignore`, run `zouinify config --check`, `zouinify briefs render` and `zouinify up --dry-run`.

## The vendored ticket protocol core

`templates/protocol/scripts/ticket.sh` is a byte-identical copy of the ticket lifecycle script of the project that owns it,
at the commit named in `templates/protocol/MANIFEST` (with the core's `TICKET_SH_VERSION`, its `PLUGIN_HOOKS` and every file's
sha256). The core is project-free: it parses `tickets/ticket.env` (never sources it) for the project's values -- `zouinify
config --env` writes that file -- and sources exactly one deploy plugin, `tickets/plugins/<DEPLOY_PLUGIN>/plugin.sh`, which must
define every hook of `PLUGIN_HOOKS`; only the no-op plugin `none` is public, a project's own deploy plugin stays in its
repository. `tests/test_vendored.py` checks the manifest, runs the vendored `test_ticket_env.py` against the core with
`plugins/none`, and, when `ZOUINIFY_UPSTREAM_REPO` names a checkout of the upstream repository, diffs every vendored file
against `git show <pin>:<path>` there (skipped in public CI). Re-pinning is one ticket: copy the files from the new commit,
update the manifest, run the tests.

## Commands

| Command | Effect |
|---|---|
| `zouinify init [--dry-run] [--non-interactive] [--name N --short S --human H] [--trunk B --prod B --model M --port P]` | Bootstrap the repository at the root (or `--project DIR`): `.ops/config.yaml` from questions on the terminal or from the flags, `.ops/briefs/*.md` from the templates, the `.ops/state/` line in `.gitignore`; then `config --check` and `up --dry-run`'s plan. Never overwrites an `.ops/` file, never writes outside the realpath'd root, never touches `~/.claude` or `CLAUDE.md`; a second run reports and exits 0. `init --protocol [--dry-run]` is the second step, once the config exists: it prints the plan -- `scripts/ticket.sh` (the vendored core, byte-identical to the pinned upstream commit, mode 755), `tickets/ticket.env` (this project's values), `tickets/plugins/none/` (the no-op deploy plugin and its docs), the rendered `tickets/README.md`, `tickets/_TEMPLATE.md` and `tickets/WORKER_BRIEF.md`, and the worktrees line in `.gitignore` -- with the full `ticket.env` and the marked `CLAUDE.md` block, then asks for your typed `yes` on that terminal and writes only then. `CLAUDE.md` itself is never written by init: the block is printed for you to paste (the yes gate stops accidents -- a session, a pipe, a relayed answer -- not a deliberate process of the same user, and `CLAUDE.md` is the one file that changes what every future session is told; the plan reports whether your `CLAUDE.md` already carries the markers, or only one of them). It refuses to run inside a Claude session or without a terminal (a session may show the plan with `--dry-run`), never overwrites an existing file (each is reported `exists`), never writes outside the realpath'd root (a symlinked `scripts/`, `tickets/` or target is refused before anything is written), runs the installed core's `version` only when this run wrote it, requires `tickets.dir: tickets`, `tickets.script: scripts/ticket.sh`, the default repo at the root, no two repos resolving to one directory, and a `deploy_plugin` zouinify ships (`none`; a project's own plugin lives in its repository and is named after the install), ends with the installed core's `version` line, and commits nothing: you commit. |
| `zouinify config [--check\|--json]` | Load and validate `.ops/config.yaml` (every problem listed at once); `--json` prints the normalized config. The project root is `$ZOUINIFY_PROJECT`, `--project DIR`, or the nearest ancestor holding `.ops/config.yaml`. |
| `zouinify config --env [--write [PATH]]` | Print `tickets/ticket.env`, the flat bare `KEY=value` file the vendored `ticket.sh` core PARSES (never sources) for this project's values: PROJECT, TRUNK, TICKETS_DIR, WORKTREES, REPOS (the configured names, space-separated, the default repo first: the core takes the first name as the repo the tickets live in) with `REPO_<NAME>_PATH\|_TRUNK\|_PROD` per repo (NAME = the name uppercased, `-` -> `_`), PROTECTED_BRANCHES_EXTRA, ASK_VERBS, TICKET_TYPES, DEPLOY_PLUGIN, DEPLOY_KINDS; the first line records the sha256 of `.ops/config.yaml` it came from (the core warns when they drift). Stdout by default; `--write` writes it to `<root>/<tickets dir>/ticket.env` or PATH, refuses a path inside a repository's **main** worktree (a ticket worktree or a directory outside git is fine), and never commits: the file is tracked, so a ticket commits it. Every value is made of the alphabet `[A-Za-z0-9._/@:+ -]` (the space only between list items); anything else is refused at generation and by the parser, so no quoting exists (the file is for the parser: a shell that sourced it would read a list value's second word as a command). Branch fields of the config (`trunk`, `prod`, `protected_branches_extra`) must be valid git branch names made of letters, digits and `. _ / @ + -`. |
| `zouinify protocol render --out DIR [--plugin-docs DIR] [--project-readme FILE] [--force]`, `protocol list` | Render `templates/protocol/tickets/{README,_TEMPLATE,WORKER_BRIEF}.md.tmpl` into DIR and nowhere else (`init --protocol`, T-0185, is what installs them): the config fills names, repos, trunk/prod, verbs, types, deploy kinds, the desk and debug sessions; the deploy plugin's doc fragments (`templates/protocol/tickets/plugins/<tickets.deploy_plugin>/docs/*.md`, vendored from the upstream core; only `none` ships publicly, so another plugin falls back to it with a warning unless `--plugin-docs` names the project's own) fill the deploy-specific prose (README rules 2, 3, 4 and 8, the `deployed`/`done` evidence rows, brief points 5 and 6, the `## Deploy handoff` block; `verbs.md` optionally replaces the verbs sentence); `<root>/.ops/README.project.md` supplies `## 0. Environments` and `## 7. Deploying` verbatim (headings in exactly that form at the margin, outside fenced code; trailing `---` rules dropped; a stub says where to write them when the file is missing). A configured plugin without shipped docs renders with the `none` prose and a warning. Refuses to overwrite a file under DIR that differs without `--force`. |
| `zouinify up [role...] [--dry-run] [--wait S]` | Start the standard session set (`always: true`) or the named roles, **idempotently**: an existing tmux session of that name is reported (`attached`, with its status from `claude agents --json` and the model from its status bar), never duplicated. A new one is `tmux new-session -d -s <tmux> -c <root> -e TICKET_AS=<name> -e ZOUINIFY_PROJECT=<root> claude --model <model> -n <name> [--permission-mode <mode>] [flags] "<brief>"`; `up` waits up to 30 s for the status bar to show the model and fails loudly on a wrong one. `--dry-run` prints the exact lines. Launched sessions are recorded in `.ops/state/sessions.json`. |
| `zouinify launch worker T-xxxx [--receipt ID] [--dry-run]` | One worker per ticket, named `t<num>-<slug>` (the ticket's slug, like `ticket.sh claim`), prompt `Read tickets/WORKER_BRIEF.md, then claim T-xxxx.`, `TICKET_AS=<name>`. Refuses unless the ticket is `ready` with an empty `owner:`, refuses an id that `tickets/FREEZE` does not list while a freeze is on, refuses a duplicate tmux name. The worker claims the ticket itself. With `--receipt ID` (the desk acting on a panel click) it appends `ticket.sh log T-xxxx "launched <name> (panel receipt ID)"`, signed by the caller's `TICKET_AS`. `launch <role>` starts one role session (`chrome_debug` gets `--chrome`). |
| `zouinify status [--line\|--json] [--no-cache]` | `inbox N \| P0 n \| creds <left> \| d dev r rev m rtd k dep` (rev = in-review + ready-to-merge; rtd = ready-to-deploy, the human's apply queue; creds = `ok` / `LOGIN` / `ERR` / `?` from a read-only `aws sts get-caller-identity --profile <aws.profile>` with a 2 s timeout, cached 60 s in `.ops/state/creds.json`; `LOGIN` means an SSO token error, so the human runs `aws sso login`; the long form adds the SSO access token's minutes left as information only, since the CLI renews that token silently) from `ticket.sh human --count`, the ticket front matter, the credential files' metadata (SSO cache `expiresAt`, else credentials-file mtime + `aws.assumed_token_hours`, marked `est`) Cached 30 s: made for `tmux set -g status-right '#(zouinify --project /path/to/repo status --line)'` (the tmux server's cwd is not the project, so name it). `P0` counts the inbox's P0 items, what the human acts on, not every open P0 ticket. |
| `zouinify down [name...] [--dry-run]` | Kill launcher-registered **worker** sessions whose ticket is `done` or `dropped`; prints the reason for every refusal (a role session, a session zouinify did not launch, a ticket still open). Run `--dry-run` first; a named target that is refused exits 1. |
| `zouinify briefs render [--out DIR] [--force]`, `briefs list` | Render `templates/briefs/*.md.tmpl` (desk, manager, debug, chrome_debug, worker) with the config: `{{desk.name}}`, `{{trunk}}`, `{{prod}}`, `{{human_only}}`, `{{budget}}`, `{{ask_verbs}}`, ... Refuses to overwrite an edited brief without `--force`. |
| `zouinify panel start\|stop\|status\|url` | This project's read-only panel on `http://127.0.0.1:<panel.port>/`, one detached process per project (pid file and log under `.ops/state/`, an entry in `~/.zouinify/projects.json`). `start` probes the port first: this project's panel is reused, another project's or a foreign process is refused, never taken over; `stop` stops only this project's and only a process whose command line is this project's panel. `up` starts it unless `panel.autostart: false` or `--no-panel`. `panel.bind: intranet` listens on 0.0.0.0 and requires the browser token on every LAN request (see LAN access). `panel start` runs `claude agents --json` for the cross-project waiting rows and the agents-says-waiting signal; the Claude CLI keeps its own state in `~/.claude.json` (it exists on every Claude Code machine; a fresh HOME gets it on that first call). zouinify itself writes nothing in HOME but `~/.zouinify/projects.json`, the list of running panels. |
| `zouinify down [name...] [--dry-run] [--receipt ID]` | Kill launcher-registered worker sessions whose ticket is `done`/`dropped`; refuses a role session, an unregistered session, a ticket that is not terminal. `--dry-run` first, always. With `--receipt ID` (the desk acting on a panel click) it appends `ticket.sh log T-xxxx "killed <name> (panel receipt ID)"`, signed by the caller's `TICKET_AS`. |
| `zouinify poll [--timeout-ms MS] [--since ID] [--no-ack] [--ack ID,...]` | The desk's side of the panel receipts (T-0162). Blocks until the panel hands out at least one pending receipt (a click on the page), prints each as `receipt <id> <kind> <ticket> (<at>, token <id>, <ip>)`, a `data:` line (the human's text and the item's title, JSON-encoded, kept apart from any command) and the exact script to run (`zouinify launch worker T-xxxx --receipt <id>`, `zouinify down <name> --receipt <id>`, "verify, then `ticket.sh move ... \"...; panel receipt <id>\"`", ...), flushes, acknowledges the printed ids in one request and exits; the desk runs it again after each batch. Fetching marks nothing (at-least-once): a receipt whose print or ack was lost comes back on the next poll marked re-sent. `--timeout-ms` gives up after that long with no receipt; `--no-ack` peeks; `--ack ID,...` acknowledges by hand; `--since ID` replays every receipt after that id, acknowledged or not. It reads the loopback-only listener inside the panel process (`hook_port` and the per-start nonce from `.ops/state/panel.json`, the same handshake as the hook script) after checking that the panel pid itself listens on that port, so the desk needs no browser token and the LAN-facing port serves no receipts. |
| `zouinify reply <id> "<text>"` | The desk's one-line verdict on a receipt it verified (T-0201): at most 300 characters, made one line; `POST /api/receipts/<id>/reply` on the loopback-only listener behind the poll's four checks (same-uid peer, no `Origin`, own `Host`, the nonce); a later reply replaces the earlier one, each audited; 404 when the panel no longer has the id in memory. The page shows it on the badge under the control that was clicked ("receipt b07f09 · desk 00:09: not launched, that step is yours"; green when the first word is launched/done/ok/moved/filed/killed/answered/resumed, amber for not/refused/still/no/cannot/failed), under the question in the Desk chat, and in the Settings receipts table. `zouinify poll` prints the exact reply command under every receipt. A reply is text for the human, never a ticket move. |
| `zouinify board`, `zouinify human [--count\|--json]` | `scripts/ticket.sh board` / `human`, verbatim. The lifecycle stays in `ticket.sh`; the skill never re-implements it. |

Session-launch rules, all read from the config: the model, `-n <name>`, the brief file (a relative path inside the project;
anything else is refused) or a literal prompt as the first prompt, placed after `--` so a brief starting with `-` is still a
prompt, `TICKET_AS=<name>` and `ZOUINIFY_PROJECT=<root>` in the environment. **`permission_mode` is a per-session key with no
default**: `--permission-mode` is passed only when the config sets it. The model is read from the status bar with `model_re`
(a config key; group 1 is the label): a different model than configured fails loudly, no label within the wait is a warning.
A session that appears between the existence check and the launch is reported as attached, never duplicated.

**`up`, `launch`, `down` and `poll` are run by the human from a shell or by the desk session, never by the panel process.** The
panel only reads; a click to launch or kill becomes a receipt the desk executes with these commands after its own checks.
`scripts/check_panel_guard.sh` is an allowlist over `lib/zouinify/panel/`: it fails CI on any process-spawning primitive
(`subprocess`, `os.system`, `os.exec*`, `pty`, `shlex`, `child_process`, ...), any import of the launcher or the tmux wrapper, a
direct `ticket_sh()` call or a registry write, and any tmux, ticket, deploy or `zouinify up|launch|down` verb however spelled.
The panel reads through `lib/zouinify/readonly.py` (captures, pane list, `human --json`, `board --json`) and nothing else.

## The control panel (read-only, plus receipts)

Read-only for everything a session can see, derived at render time with **zero state of its own**: the human inbox (from
`ticket.sh human --json`, in the command's order, with each item's tier), the board as five lanes (`ready | in-dev | review |
to deploy | deployed`) plus chips for `backlog`, `blocked`, `done`, `dropped`, every session of the project with its tmux window,
model (read from the status bar; red when it is not the configured model, `?` when no status bar is on screen), state
(`waiting for you | working | idle | gone | shell`) and last visible line, the panes waiting at a permission prompt and since when,
the log tails and the newest-plan rule from `panel.logs` / `panel.plans_glob`, credential time left, the
deployed tickets with their latest evidence line ("Live checks"), the parsed config and the hook status ("Settings"). Sources:
`ticket.sh` (read-only modes), `claude agents --json`, `tmux list-panes` / `capture-pane` (redacted server-side), the launcher
registry, the ticket front matter and release records. The page is static HTML/CSS/JS served by a stdlib server: no build step,
no framework, no request to any host but the panel's own. Its API: `GET /health`, `GET /api/snapshot` (`?fresh=1` re-reads every
source now -- the refresh button sends it; a fresh snapshot runs `ticket.sh board`/`human` and the pane captures, so the server
serves at most one per 5 s per client address and answers the cached snapshot otherwise, saying so in its `fresh` block with the
wait; the periodic refresh of the page takes the cached path), `GET /api/tickets/<id>`,
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
dialog, and only when every line is plain printable text (no path, network address, host name, key- or id-shaped string, long
number, cookie text, credential/login/flag word); otherwise the row says "waiting (screen withheld)" with the reason, and you attach
to the window to read it. The capture is
the last visible screen only, never scrollback.

## The hook: how the panel learns that a session answered or waits

Claude Code hooks (user-level, `~/.claude/settings.json`) run `templates/hooks/zouinify-hook.sh` on `Stop`,
`Notification` (`permission_prompt|idle_prompt`), `SessionStart` and `SessionEnd`. The script reads the event JSON on stdin,
finds the project (`$ZOUINIFY_PROJECT`, else the nearest ancestor of the event's `cwd` with `.ops/config.yaml`; none: exit 0
in milliseconds), reads `hook_port` from `.ops/state/panel.json` and posts the JSON to `http://127.0.0.1:<hook_port>/api/hook`
with a 1 s limit; it always exits 0, so it can never block a session. `zouinify up` prints the settings snippet when the
file lacks it: **you add it yourself; a session never edits your settings.**

The listener is a second server inside the panel process, bound to 127.0.0.1 only (`panel.hook_port`, default `panel.port
+ 2`); the LAN-facing server keeps answering 405 to every POST. It accepts `POST /api/hook` from a loopback peer, at most
64 KiB, JSON, and keeps only allowlisted fields: event name, session id, permission mode, notification type, SessionStart
source and SessionEnd reason, plus the reply or notification text for THIS project's sessions (truncated to 2000 characters,
passed through the redactor). An event whose `cwd` lies outside the project keeps no text at all, in memory, in
`.ops/state/events.jsonl` (0600, rotated daily, 14 days kept) or over the feed. Events are data: the sessions table shows a
session's last reply and last event, a `permission_prompt`/`idle_prompt` marks it "waiting for you" with the exact time until
a later `Stop`, `UserPromptSubmit`, `SessionStart` or `SessionEnd`; the screen heuristic stands for sessions without events.
`GET /api/events` is the page's server-sent-events feed (same Host and token rules as every other request); the page
re-reads the snapshot within a second of an event and keeps its 30 s poll as the fallback.

Who may post: only a process of the panel's own user. The listener resolves the peer socket of every loopback connection
in `/proc/net/tcp` and `/proc/net/tcp6` to its owner uid and refuses any other uid with 403 (audited); when the lookup
fails it fails closed. A browser page cannot post either: any request carrying an `Origin` header is refused, the body must
be `application/json` (a page would have to preflight, and `OPTIONS` is 405), `Host` must be the listener's own loopback
name, and the per-start nonce that the server writes to `panel.json` (mode 0600) must come back as `X-Zouinify-Hook`.
Events never trigger an action in any case.

Where the script sends: only to a panel that is yours and alive. On its way up from the event's `cwd` it accepts a
`.ops/config.yaml` only when you own it (another local user could plant one under a shared or world-writable directory);
`.ops/state/panel.json` must be yours and not a symlink; the `pid` in it must be a live process of yours (a stale file
after a crash would otherwise send replies to whoever bound the port); and the nonce travels along. Each check that fails
ends the script silently with exit 0.

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

Two facts to know before you rely on it. **Loopback is token-free for the panel's own user only.** Every 127.0.0.1 (or
::1) connection is resolved to the uid that owns the peer socket (`/proc/net/tcp`, `/proc/net/tcp6`; Linux only) and only
the panel's uid is trusted without a token: another local account of the workstation, or anyone with an `ssh -L` to it as
another user, is treated like a LAN peer (token required on an intranet bind, 403 on a loopback bind, audited as
`other-local-user`); when the lookup fails, the panel fails closed the same way. The check sees the socket's owner, not where
the bytes came from: a proxy or an `ssh -L` that the panel's OWN user runs and that forwards traffic from elsewhere to
127.0.0.1 is trusted without a token, so do not run one. Other local users share the loopback address's refusal budget
among themselves; your own requests are exempt from it. And **cookies ignore ports**: a browser scopes cookies by host name, not by port, so every web server reachable under the
panel's host name, on any port, RECEIVES the `zouinify_token` cookie with every request that browser makes to it. `HttpOnly`
only keeps their page scripts from reading it; a server process under that name that logs its request headers has the
token. Such a server can also set a junk `zouinify_token`, which the panel then refuses on every request (weighed like a
missing token, so the unlock address still works). The real fixes are a dedicated host name for the panel that nothing
else serves under (a DNS alias or a hosts entry), or HTTPS with a `__Host-` cookie; until then keep the LAN name of the
panel for the panel and run no other service under it.

`zouinify panel start` and `zouinify up` refuse to start an intranet panel without a token and point at the command; neither
ever prints a token. `zouinify panel url` prints the address without it. The one write, `POST /api/receipts`, needs the token
in both modes (next section); every other write is 405; tier-2 items never get a button. **HTTP on the LAN is clear text**: use
it on the lab network only; from anywhere else, `ssh -L 4790:127.0.0.1:4790 <workstation>` to the loopback panel. Worst case
of a stolen token: a false tier-1 claim that the desk re-verifies before acting (a `launch` receipt makes the desk launch a
worker only for a `ready`, unclaimed ticket; an `ok` makes it verify the command's effect read-only before any move), or reading
the board, the inbox and the redacted pane captures; never a production action, a merge, a push, a grant, a keystroke or a
message to a worker, because no endpoint does those and the desk acts only on what it verified.

## Receipts: the panel's one write (T-0162)

A click on a tier-1 control is a **receipt**: a claim the page records and the desk fetches and verifies, never an action. The
server runs nothing for it: no `tmux`, no `ticket.sh` mutating command, no `aws`/`terraform`/`git` (the gates above still
apply to the panel package; `tests/test_receipts.py` pins the write path).

- `POST /api/receipts {item_id | kind, ticket, text}` on the panel port, authenticated like every other panel request (the
  HttpOnly cookie set at unlock or `Authorization: Bearer`, Host/Origin checked) **but with no loopback exemption**: every
  Claude session of this user is a same-uid loopback peer, and the token, whose hash alone is on disk, is what tells the
  human's browser from a session. On a loopback bind the token exists once you ran `zouinify panel token rotate` and opened
  the address it printed; until then the page's controls are disabled with that reason. An `item_id` names an inbox item
  (`ticket.sh human --json`'s stable id): the item's tier decides (`ok`, or `decision` for a DECIDE item with the answer as
  `text`); a `kind` without an item is one of `ok`, `decision`, `question`, `new`, `launch`, `kill`, `sso`. The answer is
  201 `{id, kind, ticket, at, presence}`; a repeat click of the same item or kind while its receipt is still open (not fetched
  by a poll yet, or fetched less than two minutes ago) answers 200 with the same id (a nervous thumb makes no flood).
- Refused with 403, no endpoint behaviour: a tier-2 item (APPROVE, APPLY, DEPLOY, PUSH, or any item whose text names
  `CLAUDE.md` or settings), an `ok`/`decision` for a ticket that has any tier-2 item in the inbox (the item's tier cannot be
  dodged by naming the ticket), a kind that names an approval, apply, deploy, push, budget, permission, merge or grant, and a
  text of ANY kind that names `CLAUDE.md` or `settings`/`setting`/`settings.json` as a word (checked after NFKC normalisation
  with Unicode format characters removed and the common Cyrillic look-alikes folded: a zero-width space inside the word,
  fullwidth letters, `setting.json` or a Cyrillic `с` do not get past it; other look-alike alphabets may, and the desk verifies
  anyway; "resetting the cache" keeps its button). A tier-1 item of a ticket that also has a tier-2 item is accepted and says
  so (`tier2_also`): the poll line tells the desk to verify that step only and never move the ticket past the typed tier-2
  ok. With `item_id` the ticket is
  the item's own (another `ticket` in the body is 400). 401 without the token (or before one is set), 404 for an item the inbox
  no longer prints, 409 for a void item, 411/413/415/400 for a body that is not one small JSON object. Every refusal is an
  `AUDIT` line in `server.log` and a `refused` record in `audit.jsonl` (at most 30 lines a minute; the rest are counted,
  and the record names the credential kind presented, never a token), and the usual per-address refusal budget applies (429
  beyond).
- Rate limit per token: 10 accepted receipts a minute and 60 an hour; beyond, 429 with a `banner` flag the page shows as a red
  banner ("something other than you is knocking"); the page also shows it when the server counted five or more refused writes
  in the last minute.
- Kept in memory (the last 500). `.ops/state/audit.jsonl` (0600) is a **write-only** trail: `{event: receipt, id, at, kind,
  ticket, text, item_id, verb, title, token_id, ip, user_agent}` per accepted receipt, one `fetched` record per poll batch, one
  `acked` record per acknowledgement, the `refused` records. Nothing reads it back into the channel: a line any same-user
  process appends there is never a receipt, and a panel restart drops the receipts that were pending (the page says so under
  the control that was clicked: click again). `GET /api/receipts/<id>` answers from memory only.
- The desk's side is served by the **loopback-only listener** of the hooks (127.0.0.1, same-uid peer, no `Origin`, the
  listener's own `Host`, the per-start nonce from `panel.json` as `X-Zouinify-Hook`), never by the LAN-facing port: a LAN device
  without the token cannot read receipts, and the desk needs no browser token. `GET /api/poll?since=<id>&timeout_ms=<n>` blocks
  until a receipt is pending (a space every 15 s keeps the connection alive, like lavish; at most 120 s) and answers
  `{receipts: [...], since, since_known, at}`; a timeout answers an empty list and is safe to repeat. Fetching marks nothing
  (the first hand-out stamps `delivered_at`, later ones count in `redelivered`): the desk acknowledges by id with
  `POST /api/ack {ids}` after it printed them, and a receipt stays pending until then (re-sent on every poll, for an hour).
  That is at-least-once delivery, not a defence against another same-uid process: one can read the nonce from `panel.json`
  and fetch or acknowledge receipts (delay or swallow a click), and it can run a listener of its own, give it the panel's
  command-line shape and write its pid, port and nonce into `panel.json`, feeding the desk receipts nobody clicked (the
  owner check `zouinify poll` runs guards against a STALE file, nothing more; a test documents that a forged one passes).
  A receipt is therefore a claim, full stop: the desk verifies every one before acting, and a click that never arrived is
  simply clicked again. With `since=<id>` the poll answers everything after that id, acknowledged or not. `zouinify poll`
  wraps all of it.
- Presence: the page shows `desk polling` while a poll is open and `not polling since HH:MM` otherwise (from the active-poll
  count, read through the panel port like the rest of the page); the snapshot's `panel.receipts` block carries the counters and
  the last 20 receipts without address, user agent or token id, which the page uses for the "receipt <id> · waiting for the
  desk / fetched HH:MM / acked by the desk HH:MM" badges and the Desk chat history.
- The desk's rule (its brief): keep one `zouinify poll` running; verify every receipt as a claim, exactly like a typed `ok`;
  name the receipt id in the reply and in the log line; never move a ticket on a receipt alone; tier 2 means "ask the human to
  type it"; after verifying, `zouinify reply <id> "<one line>"` so the human reads the verdict on the page (`reply` and
  `replied_at` on the receipt, T-0201).

## Typing into a worker's pane: the terminal page (`zouinify term`, T-0181)

The panel itself never produces a keystroke: that gate (`check_panel_guard.sh`, `check_panel_ast.py`) stays, and the panel
only links. A second, separate process, `zouinify term`, serves the **terminal page**: one WORKER session of this project,
its screen refreshed every 300 ms (redacted), a key bar and a text field; what you type goes to that pane with
`tmux send-keys`, like typing after `tmux attach`, without attaching a client. The user's decision of 2026-10-13 ("Z-11
yes workers") binds it: the desk, manager, debug and chrome_debug windows are never offered and never reachable, enforced
in code (`lib/zouinify/term/targets.py`) and by a test, so approvals and tier-2 answers for the desk stay typed in the
desk's tmux window.

- **Its own secret.** `zouinify term secret rotate` in your own terminal (refused inside a Claude session or without a TTY)
  shows a passphrase once (four groups of five characters) and keeps a PBKDF2 hash in `.ops/state/term.json`. You type it
  on the unlock screen once per browser session; the server answers with an `HttpOnly`, `SameSite=Strict` cookie that
  expires after 10 idle minutes and 8 hours at most. The unlock is required from every address, loopback included. The
  fifth wrong passphrase within a minute locks that address out for 15 minutes.
- **Loopback by default.** `terminal.bind: intranet` is its own explicit flag; on the LAN the page serves HTTPS with a
  self-signed certificate made once with `openssl` (`terminal.tls: auto`; the browser warns once per device). Without
  `openssl` it refuses to bind the LAN unless you set `terminal.tls: off` knowingly (the passphrase then crosses the LAN in
  clear once per browser session, the cookie on every request).
- **Workers only, checked live.** A target is a worker session the launcher registered, or a live Claude session named by
  the worker pattern under the project; before every batch its tmux session must exist and its pane must run `claude`
  (a worker that dropped to a shell is closed). The check runs before EVERY keystroke op against one concrete pane id:
  exactly one pane across the session's windows, its real executable not tmux for any process on the pane's tty or under
  its shell (a nested tmux client, even one renamed `claude`, is refused: tmux is recognised by name, by inode, a hard
  link under any name, and by content, a byte-identical copy under any name), no client that your tmux servers
  themselves list (`list-clients` on the default server and on every server in tmux's socket directory,
  `$TMUX_TMPDIR`/`/tmp` + `tmux-<uid>/`) attached from the pane's tty or from under its shell, whatever that client's
  binary is called, and the tty's foreground process group the worker agent's own; a split, a respawned or swapped pane
  and a nested client all close the attachment with nothing sent. A tmux server that does not answer within 2 s (3 s for
  all of them: a hung or a planted socket) fails the request closed with 503 and nothing sent; the attachment stays and
  the page retries.
  Keys are an allowlist: printable text and Enter, Esc, arrows, Tab, Backspace, Home, End, PageUp, PageDown, Space, Ctrl-C,
  Ctrl-U; never Ctrl-D; at most 8 ops per batch.
- **The residual, stated plainly.** Every Claude session on the workstation runs as your user. A worker can already run
  `tmux send-keys` into any of your windows, or read your `/dev/pts` devices, on its own, without this page; the checks
  above keep the terminal page from becoming an easier route, they do not isolate the sessions. What they still miss: a
  client of a tmux server on a socket outside tmux's directory (`-S /elsewhere`) that runs a modified tmux binary (not
  the one on PATH, not a hard link to it, not a copy of it). Full isolation needs a separate uid per session, which is
  not v1. That is exactly why approvals stay typed in the desk's window and why the role panes stay unreachable from the
  browser.
- **Audit and kill switch.** Every unlock, open, batch of keys, close and refusal is one JSON line in
  `.ops/state/term_audit.jsonl` with the unlock id, the attachment id, the address, the target and the time; the page
  shows the attachment id and `zouinify term audit [--since 10m] [--target NAME]` prints the recent lines, so a session's
  log line can cite `term <tid8>`. An open pane closes after 5 idle minutes and 30 minutes at most; `zouinify term stop`
  ends the process, every unlock and every attachment at once (the passphrase hash stays).
- **Commands.** `zouinify term start|stop|status|url`, `zouinify term secret rotate`, `zouinify term audit`; `up` starts it
  when `terminal.enabled` and `terminal.autostart` are on. Config block: `terminal: {enabled, port (default panel.port + 1),
  bind, tls, autostart, targets: workers, exclude, idle_close_s, max_open_s, unlock_idle_s, unlock_max_s}`.
- **What it cannot prevent.** Any Claude session on this host could already run `tmux send-keys` itself, so this adds LAN
  and browser exposure, not a new capability for sessions; a Claude session that drives your Chrome can type into a
  terminal tab you left open and unlocked in that same browser profile (use the phone or a profile without the extension
  for anything sensitive; the idle times bound it). Specification: `docs/UX_SPEC.md` 2.5a; its own CI allowlist is
  `scripts/check_term_guard.sh`.

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
  panel/hooks.py              the loopback-only hook listener and the event store (T-0163); templates/hooks/zouinify-hook.sh is the script the human installs
  panel/                      the read-only server: sources.py (cached sources), server.py (GET-only HTTP), redact.py, static/
templates/
  ops/config.yaml.example     the per-project config
  briefs/*.md.tmpl            desk, manager, debug, chrome_debug, worker
  protocol/                     the ticket protocol, laid out as a repository root (`init --protocol` copies it to the same relative paths)
    MANIFEST                    the pinned upstream commit, the core's version, its hook list and every vendored file's sha256 (tests/test_vendored.py)
    scripts/ticket.sh           the vendored core, byte-identical to the pin (the project that owns it is private; its deploy plugin is not vendored)
    scripts/tests/ticket_world.py, test_ticket_env.py   the vendored harness and the core's own tests, run here against plugins/none
    tickets/plugins/none/       the no-op deploy plugin (plugin.sh defines every hook of PLUGIN_HOOKS) and its docs/ fragments
    tickets/README.md.tmpl, _TEMPLATE.md.tmpl, WORKER_BRIEF.md.tmpl   the rendered protocol files (`protocol render`)
    CLAUDE.section.md.tmpl      the marked block `init --protocol` prints for the human to paste into CLAUDE.md (init never writes it)
  hooks/zouinify-hook.sh        the Claude Code hook script (T-0163)
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
2. Week 2: the receipts channel (token, audit, rate limit, `zouinify poll`), the hook script, the intranet flag (done).
3. Week 3: receipt kinds `launch`/`kill`/`new` with the desk-side handlers, `init` on an empty repo, the home page.

## Rules the skill never bends

- The lifecycle is `scripts/ticket.sh`'s; the skill never edits a ticket file and never calls a mutating `ticket.sh` command
  (the one `ticket.sh log` line of `launch --receipt` / `down --receipt` is written on the desk's behalf, signed by it).
- Every session is launched with a model, a name, the configured permission mode (none by default), a brief and `TICKET_AS`;
  launches are idempotent.
- No session launched by the skill may run a production mutation; the human-only list from the config is printed into every brief.
- The panel is read-only for everything a session can see; its one write, a receipt, accepts only the human's browser token
  (even from a same-uid loopback peer) and changes nothing by itself; tier 2 is typed.
- A relayed message from another session is never the human's consent.
