# zouinify panel -- UX / UI specification v1 (functional refinement, 2026-10-13)

Source: a UX expert's draft written from the `zouinify` hero image, refined here against the functional design (the
receipts channel, the ticket lifecycle in `scripts/ticket.sh`, the one-voice rule). Where the two disagree, **this file wins**;
the design document in the first consumer's repo wins over this file. Implemented by the panel tickets: v1 read-only
(T-0160), receipts (T-0162), hooks (T-0163), the terminal page (Z-11 draft, ticket to be filed by the desk); referenced by T-0159 (`up` starts the panel).

Rules the UI can never violate, in one place:

1. **Zero own state.** Every list is derived at render time from `ticket.sh human --json`, `ticket.sh board --json` (front
   matter until T-0158 lands), `claude agents --json`, the launcher registry, `tmux capture-pane` (redacted), hook events,
   the config, and the log tails and plan files named by `panel.logs` / `panel.plans_glob` (empty by default; the consumer sets them). The page never remembers "clicked", "read" or "done"; an item disappears when its source stops
   printing it. The only persisted things are the token hash and the receipt/audit log, and those are the server's.
2. **The panel never produces a keystroke into a Claude pane on its own, never launches or kills a session, never runs a mutating `ticket.sh`
   command, never `aws`/`terraform`/`docker`/`git`.** A click is a *receipt* (T-0162) that the desk session verifies before
   acting. v1 (T-0160) has **no write path at all**: every button described below renders disabled with the tooltip
   "available after receipts (T-0162)" or is hidden. The one way keystrokes reach a pane from the browser is the **terminal
   page** (2.5a; user decision 2026-10-13 afternoon): a separate process, `zouinify term`, outside the guarded panel package,
   that forwards what the human types after a password. No button, receipt or script ever produces a keystroke.
3. **Tiers.** Tier 1, clickable after T-0162: `ok` after a command the human ran in their terminal, an answer among offered
   options, "send to desk" (file a ticket, ask a question), "launch worker" (on a `ready` card), "kill" (on a session whose
   ticket is `done`). Tier 2, **never clickable**: a paid budget, a `CLAUDE.md`/settings yes, a prod apply/deploy/push, a worker's
   permission prompt. Tier-2 items show the exact text to type in the desk's tmux window, with a copy button. The terminal
   page never reaches the desk's pane (user decision 2026-10-13, "Z-11 yes workers"), so tier 2 stays typed there.
4. **One voice.** Receipts and chat go to the desk only. Nothing on the page addresses a worker, except the terminal page
   (2.5a): it reaches WORKER panes only, because the human is typing there, exactly as from `tmux attach`; the desk,
   manager and debug panes stay out of its reach, enforced in code.
5. **No secrets.** Pane captures are redacted server-side; the UI never renders env, credentials, tfvars or tokens; the
   credential tile shows time left and the identity ARN's role name only.

---

## 1. Design direction (kept from the UX draft)

Premium developer tooling: compact, calm, precise, dark-first, dense without clutter. Near-black navy canvas, layered
surfaces, thin cool-blue borders. Semantic colors: **teal** primary action and success, **amber** needs the human, **blue**
navigation and `ready`, **purple** review, **green** idle/done, **red** failure/P0. Inter or system sans; JetBrains Mono or
system mono for ids, commands, paths, timestamps. 10-13 px card radii, 6-8 px badges, no gradients in content, no glow, no
glassmorphism, no emoji icons, Lucide-style 16-20 px outline icons. 4/8 px spacing scale; 24 px page padding desktop, 16 px
phone. 120-200 ms transitions; honour `prefers-reduced-motion`. Light theme is a follow-up: ship dark with every color as a
token so a light palette is a second `:root` block.

Tokens (targets, adjust after visual comparison with the hero image; the hero's perspective collage, floating terminals and
browser window are marketing, not UI):

```css
:root {
  color-scheme: dark;
  --bg-canvas:#081321; --bg-sidebar:#0a1626; --bg-surface:#0f1c2e; --bg-card:#122238; --bg-card-hover:#182c45; --bg-input:#0b192a;
  --border-subtle:#21344b; --border-strong:#31516a;
  --text-primary:#eef6ff; --text-secondary:#9bb1c9; --text-muted:#68829d;
  --accent-teal:#26e0d0; --accent-teal-hover:#5cf0e3; --accent-blue:#37b9ff; --accent-amber:#f2b544; --accent-purple:#b79bff;
  --accent-green:#35d59b; --accent-red:#ff6e78;
  --radius-sm:7px; --radius-card:11px; --radius-panel:13px; --shadow-panel:0 8px 24px rgb(0 0 0 / .13);
}
```

Semantic token names for every status (`--status-waiting`, `--status-idle`, `--status-working`, `--status-gone`,
`--status-p0`), never a raw hue in a component. WCAG AA contrast; visible `:focus-visible` ring in teal.

Stack: the panel is a Python stdlib server serving static HTML/CSS/JS from `lib/zouinify/panel/static/`. **No build step, no
framework, no CDN** (the page must work on a LAN with no internet and must not leak requests to third parties). One
`app.js` with small modules, CSS variables for tokens, SSE for live updates once T-0163 exists, else a 30 s refresh of
`GET /api/snapshot`.

## 2. Screen anatomy (desktop)

```
┌──────────────┬──────────────────────────────────────────────────────────────────────────────────────────────┐
│ zouin|ify    │ mp  · trunk dev @a1b2c3d · FREEZE off · creds ~1h51 (estimate) · desk: polling · data 12 s ago · 🔒 │
│              ├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ ▣ Dashboard  │ !! P0 T-0146 live regression     · DEPLOY waiting since 19:50 (10h)                            │
│ ◫ Tickets    │ ┌ For you ──────────┐ ┌ Sessions ─────────────┐ ┌ Deploy & verify ───────────────┐           │
│ >_ Sessions  │ │ 12 items · 1 P0   │ │ 1 waiting for you     │ │ 3 to deploy · 19 deployed      │           │
│ ✓ Live checks│ │ nothing else      │ │ 2 working · 4 idle    │ │ awaiting a live check          │           │
│ ✉ Inbox      │ └───────────────────┘ └───────────────────────┘ └────────────────────────────────┘           │
│ ◔ Desk chat  │ Tickets   backlog 26 · blocked 1 · done 90 · dropped 3                              [Send to desk]│
│ ⚙ Settings   │ ┌ ready 4 ─┬ in-dev 1 ─┬ review 1 ──┬ to deploy 5 ─┬ deployed 19 ─┐                            │
│              │ │ T-0155 P1│ T-0125 P2 │ T-0119 P1  │ T-0145 P1    │ T-0139 P0    │  (horizontal scroll < 1440)│
│              │ │ T-0156 P2│ t0119-wo… │ t0119-wo…  │ plan 10-12…  │ T-0141 P2    │                            │
│              │ └──────────┴───────────┴────────────┴──────────────┴──────────────┘                            │
│              │ ┌ Inbox (ticket.sh human) ───────────────────────────┐ ┌ Panes waiting for you ─┐                │
│              │ │ 1 DEPLOY T-0146 P0 … [copy cmd] expect … [OK]       │ │ t0119-worker    perm   │                 │
│              │ │ 2 APPROVE T-0119 P1 … type in tickets window: … [copy]│ │  since 14:40 (16h)     │                 │
│              │ │ 3 APPLY T-0145 P1 … [copy cmd] expect … [OK]        │ │ t0117-worker   2d      │                 │
│              │ └─────────────────────────────────────────────────────┘ └────────────────────────┘                 │
│              │ ┌ Sessions ─────────────────────────────────────────────┐ ┌ Desk chat ─────────────────────────┐ │
│              │ │ desk  mp-desk                 mp-desk  Opus  working  │ │ you: is T-0145 safe to apply?       │ │
│              │ │ worker t0119-worker    Fable  WAITING  T-0119  14:40  │ │ desk (a1b2c3): yes, plan is newest…│ │
│              │ │ debug mp-debug         Fable  idle  T-0011            │ │ [ ask the desk … ]          [send]  │ │
│              │ └───────────────────────────────────────────────────────┘ └────────────────────────────────────┘ │
└──────────────┴──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Shell and navigation

Sidebar 220-240 px, wordmark `zouin` white + `ify` teal. Items, in this order: **Dashboard, Tickets, Sessions, Live checks,
Inbox, Desk chat, Settings**. Collapses to icons under 1024 px; a top bar with a menu under 768 px. Routes are hash routes
(`#/tickets?state=blocked`, `#/tickets/T-0145`, `#/sessions/t0119-worker`) so links are shareable without a router.

Header (every page): project short name and name, trunk sha, FREEZE badge (red, with the allowed ids on hover), credential
tile ("~1h51 left (estimate)" from the key file's age until SSO lands, then "SSO 9h12"), desk presence ("polling" green,
"not polling since HH:MM" amber, "desk gone" red, from the server's active-poll count and `claude agents`), data age per
source on hover, a padlock showing `loopback` or `intranet`. No user menu: there is one human.

### 2.2 KPI cards (replace the draft's Tickets / Sessions / Browser Tests)

| Card | Big number | Secondary | Click |
|---|---|---|---|
| **For you** | inbox count (`ticket.sh human --count`) | `P0 n` in red when > 0 | `#/inbox` |
| **Sessions** | sessions waiting for you (amber when > 0) | `w working · i idle · g gone` | `#/sessions` |
| **Deploy & verify** | tickets in `ready-to-deploy` (+ `merged` with a deploy) | `deployed awaiting a live check: n` | `#/tickets?state=ready-to-deploy` |

Counts are scoped to this project (sessions: `cwd == project root`; tickets: this `tickets/` dir). Skeletons while loading;
`0` with a one-line explanation when empty; never an invented metric.

### 2.3 Ticket board (the lifecycle has 12 states, the dashboard shows 5 lanes)

`scripts/ticket.sh` states: `backlog ready in-dev in-review ready-to-merge merged ready-to-deploy deployed done blocked dropped skipped`.

| Lane | States shown | Accent | Card extras |
|---|---|---|---|
| `ready` | ready | blue | severity; **[launch worker]** after T-0162 (disabled in v1), hidden when FREEZE excludes the id |
| `in-dev` | in-dev | amber | owner session name, `claimed Xh ago`, dirty/behind badges from `board --json --slow` when cached |
| `review` | in-review + ready-to-merge | purple | owner; `ready-to-merge` sub-badge |
| `to deploy` | merged (deploy != none) + ready-to-deploy | teal | the `plan:` basename; VOID badge when a newer file matching `panel.plans_glob` exists (the same newest-plan rule `ticket.sh human` applies); no badge when the glob is empty |
| `deployed` | deployed | green outline | `closed_by` short shas; "awaiting live check" |

Above the lanes, chips with counts: `backlog n` (grey), **`blocked n`** (amber; the questions are inbox DECIDE items),
`done n`, `dropped n`. Clicking a chip opens `#/tickets?state=…`. `done` is never a lane on the dashboard. The Tickets page
shows all 12 states as filters plus text search, sorted by severity then age.

Card: `T-0145` in mono, severity badge `P0`..`P3` (P0 red), 2-line title, extras per lane, a bell when the ticket has a pending
`human:` ask. The whole card opens the **ticket drawer**: front matter as a key/value table, the last 10 `## Log` lines
(newest first, mono), the newest `## Deploy handoff` block with copy buttons on command lines, links to the owner session and
to the inbox item. Nothing in the drawer is editable.

**Not in any version:** drag-and-drop, "+ Add ticket" per lane, inline stage changes. A state change needs evidence and is
made by the owning session with `ticket.sh move`. "**Send to desk**" (top right of the board; receipt `new`, T-0162) opens a
small form (type bug/feature/chore/process, severity, title, two lines of text) and shows "sent to the desk, receipt a1b2c3";
the card appears when the desk files it. In v1 the button is disabled with the T-0162 tooltip.

Under 1440 px the board scrolls horizontally with `min-width` per lane 180 px; never five crushed lanes.

### 2.4 Inbox panel and Inbox page (this is `ticket.sh human --json`, not a notification feed)

Each item, in the command's own order (never re-sorted client-side; P0 items print first already):

```
1  DEPLOY  T-0146  P0  live regression after the last deploy: opening a finished job fails …   since 19:50 (10h)
   cd … && scripts/deploy_site.sh                                                      [copy]
   expect: LIVE CHECK PASSED        if you do nothing: the merged fix stays unreleased         asked by desk
   ─ run it in your terminal, then ─────────────────────────────────────────────────────  [ OK ]  (tier 1, T-0162)
```

Verb badge colors: `APPLY`/`DEPLOY`/`PUSH` teal outline, `RUN` blue, `DECIDE` purple, `CHECK` green, `APPROVE` amber,
`SSO` grey. Fields: verb, ticket id(s), priority, title, `since` + age, command (mono, copy button; a `question:` wraps, a
command never), `expect`, `if you do nothing`, `note`/asked-by. The item's `id` from `human --json` (T-0158) is the receipt key.

Per-verb action, after T-0162 (all disabled in v1):

| Verb | Action on the page | Tier |
|---|---|---|
| APPLY, DEPLOY, PUSH, RUN, SSO | copy the command; after the human ran it in their terminal, **[OK]** queues `{kind: ok}`; the desk verifies read-only and moves the ticket | 1 (the command itself is always typed by the human) |
| CHECK | **[OK]** after the human did the browser/email check | 1 |
| DECIDE | the offered replies (`reply "T-0140 delete" or "T-0140 keep"`) parsed into radios + **[Send answer]**; a free-text answer box when none are offered | 1, except when the question names `CLAUDE.md`/settings: then tier 2 |
| APPROVE | no button; "type in the tickets window: `T-0115 budget ok`" with copy | 2, always |

Never "Acknowledge all", never a generic OK, never an OK that executes anything. The "do NOT run (void plans)" group renders
in grey with the newer plan named; "Blocked on you but not urgent" is a collapsed footer. The Inbox page is the same list
full-width with the drawer on the right.

### 2.5 Panes waiting for you (the page's main win)

Sessions of this project whose state is `waiting` (`claude agents --json` status, or a `Notification(permission_prompt |
idle_prompt)` hook without a later `Stop`/`UserPromptSubmit`, or the prompt heuristics on the captured screen). Row: name,
role, what it waits for (first line of the prompt, redacted), **since HH:MM (age)**, ticket, **[show pane]** (the redacted
visible screen in a drawer) and, when `terminal.enabled` is true, **[type]**: a link to the terminal page for that pane when
it is a WORKER session (2.5a). No answer *button*, in any version: a permission prompt or an option dialog is answered by the human's own keystrokes,
in the terminal page or in tmux, never by a click the page turns into keys. (Decision 2026-10-13 morning: "never from the page";
revised by the user the same afternoon: "enable the option to answer sessions from the website directly, e.g. a terminal
session". The revision adds the terminal page; it does not add buttons.)

### 2.5a The terminal page (`zouinify term`; user decision 2026-10-13, "Z-11 yes workers"; T-0181)

**What it is.** A browser page attached to one tmux pane of this project: the pane's visible screen (refreshed every 300 ms,
redacted like every capture), a text field and a key bar (`1`-`5`, `y`, `n`, Enter, Esc, arrows, Tab, Backspace, Ctrl-C).
What the human types goes to that pane with `tmux send-keys`; the screen is `tmux capture-pane`. No tmux client is attached, so
the human's own attached terminal is never resized or disturbed. It is the browser equivalent of `ssh` + `tmux attach`: the
keystrokes are the human's, and every session treats them as such, the desk included.

**Where it lives.** A separate process and package, `lib/zouinify/term/`, started with `zouinify term start|stop|status` (plus `secret rotate` and `audit`), on
`terminal.port` (default `panel.port + 1`) with the panel's bind mode. The guarded panel package (`lib/zouinify/panel/`) keeps
its gates exactly as merged (no spawn, no tmux, no write); its only change is the `[type]` link, a plain `<a href>` to the
terminal page. The request-side checks the two share (Host/Origin, bearer and cookie parsing, the per-address rate limiter) are lifted
into `lib/zouinify/webauth.py` (Z-12, zouinify-dev) and imported by both; the panel's AST gate allowlists that module and the
panel's behaviour stays byte-identical. The terminal package gets its own CI allowlist (`scripts/check_term_guard.sh`): it may run `tmux send-keys`,
`capture-pane`, `list-panes`, `has-session` and nothing else (no `new-session`/`kill-session`, no `ticket.sh`, no
`aws`/`terraform`/`git`/deploy verbs). No xterm.js, no websocket, no CDN: plain polling of the screen endpoint.

**Who may type (the same-user problem; manager conditions 2026-10-13).** Every Claude session on the workstation runs as the
human's user and can reach loopback ports; a `--chrome` session can drive the human's own browser. Hence:

- **its own secret, never the panel's token.** `zouinify term secret rotate`, in the human's own terminal (refused with
  `CLAUDECODE` set or without a TTY, like the token), generates a passphrase of four five-character groups (about 99 bits, readable from a screen, typeable on a phone),
  shows it once, clears the screen after Enter and keeps only a PBKDF2-HMAC-SHA256 hash (600k rounds, 16-byte salt) in `.ops/state/term.json` (0600,
  under flock, next to the token code in `panelstate.py`). The browser never stores the passphrase: the unlock screen sends it
  once per browser session to `POST /term/unlock`; the server answers with an `HttpOnly`, `SameSite=Strict` cookie bound to a
  server-side unlock record (`terminal.unlock_idle_s`, 600, without a keystroke; `terminal.unlock_max_s`, 28800, at most; gone
  when the process stops). The fifth wrong passphrase within a minute from one address answers 429 and locks that address out for 15
  minutes; every attempt is audited. The unlock is required from every address, loopback included (other local users exist);
- **loopback by default; the LAN is an explicit flag.** `terminal.bind: loopback | intranet`, its own key (default `loopback`,
  independent of `panel.bind`). On `intranet` the passphrase travels once per browser session and the cookie afterwards; with
  `terminal.tls: auto` (the default) the process serves HTTPS on `intranet` with a self-signed certificate it generates once with
  `openssl` into `.ops/state/term-cert.pem` + `term-key.pem` (0600), so nothing travels in clear (the browser warns once per
  device). Without `openssl` the process refuses to bind `intranet` unless `terminal.tls: off`, and REFERENCE.md says plainly
  what then travels in clear. The panel's Host/Origin check applies through the shared helpers (`lib/zouinify/webauth.py`, Z-12);
- an open pane is a server-side attachment id (`tid`, 32 random bytes) under the unlock, bound to one target, closed after
  `terminal.idle_close_s` (300) without a keystroke and after `terminal.max_open_s` (1800) regardless; **`zouinify term stop` is
  the kill switch**: it ends the process, every unlock and every attachment at once, and the cookie is then worthless;
- **targets are WORKER sessions of this project only** (the user's decision, "Z-11 yes workers"): a session the launcher
  registered with role `worker`, or a live Claude session named by the configured worker pattern (`t{num}-{slug}`) with its
  cwd under the project; each is checked live before EVERY keystroke op against the one %pane_id pinned at open: its tmux
  session has exactly one pane across its windows, that pane runs `claude`, no process on the pane's tty or under its shell
  has tmux as its real executable (a nested client renamed `claude` is caught by /proc/<pid>/exe: by the name, by the
  inode of the tmux on PATH -- a hard link -- and by its content -- a byte-identical copy, T-0183), no client the user's
  tmux servers list themselves (`list-clients`, the default server and every server in tmux's socket directory) is
  attached from the pane's tty or from under its shell, the tty's foreground process group is the agent's own, and the
  agent's pid descends from the pane's. Same-uid residual accepted for v1 (a worker can already `send-keys` or read
  /dev/pts on its own; a client of a `-S /elsewhere` server running a modified binary is not seen): stated in REFERENCE.md.
  A tmux server that does not answer within 2 s (3 s for the walk over all sockets) fails the request closed: 503 +
  `Retry-After`, nothing sent, the attachment kept, the listing row marked "not checked".
  The configured role sessions (desk, manager, debug, chrome_debug) are refused by name AND by tmux window whatever the
  registry says, enforced in code (`zouinify.term.targets`) and pinned by a test; so is a shell pane (no human-only command
  can be typed through it), a session of another project, an arbitrary tmux target, a name in `terminal.exclude`, and
  another user's session (tmux servers are per Unix user; none is reachable). `terminal.targets` accepts `workers` only;
- keys are an allowlist: printable text (at most 2000 characters per batch, no control characters) and the named keys
  `Enter Escape Up Down Left Right Tab BSpace Home End PageUp PageDown Space C-c C-u`; never `C-d`, `C-z`, `C-\`, never a tmux
  prefix, never a raw escape sequence; a batch is an ordered list of `{t: "text"}` and `{k: "Name"}` items;
- **every unlock, open, batch of keys, close and refusal is one line** in `.ops/state/term_audit.jsonl`
  (`at, event, unlock8, tid8, target, ip, user_agent, text, keys, reason`), 0600, never the passphrase. The page shows the
  attachment id next to the target's name, and `zouinify term audit [--since 10m] [--target <name>]` prints the recent lines
  (read-only), so the desk's log line for an answer the human typed this way can cite `term <tid8>`. The page shows "typing
  goes straight into `<name>`" in red above the field.

**Residual risks the human accepts** (stated in REFERENCE.md). Any Claude session on this host could already run `tmux
send-keys` itself (the classifier may or may not stop it), so the terminal page adds LAN and browser exposure, not a new
capability for sessions. The exposures: a Claude session that drives the human's Chrome can type into a terminal tab that is
open and unlocked in that same browser profile (open the terminal page from the phone or from a browser profile without the
Claude extension for anything sensitive; the unlock and attachment idle times bound the window); with `terminal.tls: off` on
`intranet` the passphrase crosses the LAN in clear once per browser session and the cookie on every request (`ssh -L` from
off-LAN, or keep `tls: auto`); and keystrokes typed here are indistinguishable from the human's at the keyboard, which is the
point.

**UI.** A full page on its own origin, opened in a new tab from `[type]` (on the phone it is the whole screen): a header with
the target's name, role, ticket and state; the screen in mono (`pre`, redacted; the pane has no scrollback, so none is shown);
the key bar with 44 px targets; the text field (Enter sends the text and Enter, Shift+Enter sends the text only, empty field
and Enter sends Enter alone); a close button; the red banner. `?text=` pre-fills the field; nothing is sent until the human presses Enter. The unlock screen (the passphrase) is the first screen of a browser session, not of every open; it names the project and the
bind mode.

A later refinement, not in the first version: a `PermissionRequest` hook that lets the human allow or deny a worker's tool call
from the panel without any keystroke (documented Claude Code hook; it would wait only while a dashboard tab is connected and
never for a command on the human-only list).

### 2.5b Waiting for me (every session of the user; T-0175)

A page, `#/waiting`, second in the nav, listing every Claude session of the user that `claude agents --json` reports as
waiting, whatever its project: this project's rows first, then the others grouped by project (the registered project's name
from `~/.zouinify/projects.json`, else the directory name of that session, never a path). Row: name, project badge, tmux
window, the prompt's first line (redacted capture), since (first seen waiting by this panel), and "answer from your terminal:
attach to tmux window `<name>`" with a copy button for the window name (the panel names no tmux command: its guard forbids
the verb; the command is in REFERENCE.md). **[show pane]** opens `GET /api/waiting/<name>`: this project's sessions show
their whole redacted screen as on the Sessions page; another project's session shows **an allowlist, not a redaction**
(desk + security review, 2026-10-13): only the question block right above the first option line and the option lines
with their continuation lines, each NFKC-normalised, zero-width characters stripped, checked whole (withheld over 400
characters, shown to 160) and accepted only as printable text with no `/`, `\`, `~` or `@`, no alphanumeric run of 8+ mixing letters and digits, no token-shaped
run of 16+, no 9+ digit number, no dotted host name, no cookie text, none of the review's denylist words (pass, pwd,
secret, token, credential, auth, login, key, `sudo -S`, a -p/-P/-k/-W/-b flag not followed by a digit), no IPv4 or IPv6
address and nothing the redactor would change; with no option
list, the first prompt line alone. One failing line withholds the whole dialog: the row says "waiting (screen withheld)"
and the drawer shows name, project, since and the reason. The capture is the last visible screen, never scrollback.
A session started in the home directory, or under a directory named like a login, is labelled `~`. The dashboard's "Panes waiting for you" keeps this project's rows and links "+ n elsewhere"; the
Sessions KPI shows "n elsewhere". No `[type]` until Z-11 is decided; whether the terminal page may reach sessions outside
this project's config is a separate decision. Zero own state; the panel's gates unchanged; a foreign capture is redacted
exactly like this project's (merge gate: the panel may serve the LAN).

### 2.6 Sessions panel and page

State mapping, with text labels (never color alone):

| Shown | From | Color |
|---|---|---|
| `waiting for you` | status `waiting` or hook as above | amber, row highlighted |
| `working` | status `busy` | blue |
| `idle` | status `idle` | green |
| `gone` | in the launcher registry or the tmux list but not in `claude agents` | grey; offers "re-launch via desk" after T-0162 |
| `shell` | pane runs a non-claude command (the human is in it) | grey outline |

Columns: role (desk/manager/debug/chrome_debug/worker, from the registry or the config's name map), name, tmux window,
**model** from the status bar (red `Sonnet?` badge when it is not the configured model: a real failure mode), state, ticket,
last visible line or last `Stop` reply (hook), age. The desk row is pinned first with its poll presence. The model is read from the status bar on the visible screen: `?` when the bar is not on screen (a dialog or a prompt covers it); the red badge fires only when a model was read and differs from the configured one. Expect it to fire on day one for sessions started before the launcher existed; that is the point, not a bug. Session detail
(`#/sessions/<name>`): the redacted screen, last reply, hook event timeline, ticket link, registry entry. Controls: none in
v1; after T-0162 only **[kill]** on a session whose ticket is `done`/`dropped` (receipt; the desk runs `zouinify down`) and
**[launch]** for a configured role that is `gone`.

### 2.7 Live checks (the draft's "Browser Tests"; v2, after a chrome_debug session exists)

There is no test runner. Evidence is what the debug/chrome_debug sessions write: `ticket.sh log` lines on `deployed` tickets
(`LIVE CHECK PASSED`, reload markers, job ids, execution ARNs: the same patterns `ticket.sh move done` accepts) and files under
`.ops/state/evidence/<ticket>/` (screenshots, GIFs, console excerpts). v1 shows the `deployed` tickets with their latest
evidence line and age; v2 adds thumbnails and a per-ticket evidence page. The KPI is "awaiting a live check: n · checked in
7 d: m", never a pass rate.

### 2.8 Next sitting (removed)

The sitting protocol was removed by the user on 2026-10-13: the inbox is continuous, there is no countdown, no freeze time and
no `sittings` key in the config (an old key is accepted and ignored). A P0 is the only thing that interrupts the human.

### 2.9 Desk chat (the lavish-style chat box; after T-0162 + T-0163)

A message list and a one-line composer, like the lavish-axi chrome. Sending queues a receipt `{kind: question, text}`; the
desk's reply arrives through its `Stop` hook and is shown with the receipt id it answers ("desk (a1b2c3): …"). Presence
exactly as lavish: `listening` (a poll is open), `working` (a receipt was delivered, no Stop yet), `away` (no poll for >
60 s), shown next to the heading. History = this project's receipts of kind `question` and the replies, from the audit and
event logs (nothing else is stored). Fixed note under the composer: "The desk treats this as a question or a claim, never as
an approval. Approvals are typed in the tickets window." Desk only in v1 (one voice); a session picker is a later decision.
Enter sends, Shift+Enter newline. In v1 (read-only) the panel shows the desk's last visible line and the composer is disabled.

### 2.10 Settings (read-only)

The parsed `.ops/config.yaml` (sessions table, repos, panel bind mode, port, state dir), the hook status ("hook
installed in ~/.claude/settings.json: yes/no", detected by reading the file; the snippet with a copy button when missing),
token instructions ("rotate with `zouinify panel token rotate` in your terminal"), the version of the skill and of the
vendored `ticket.sh` core. Nothing editable from the page.

## 3. Responsive behaviour

| Width | Layout |
|---|---|
| ≥ 1440 | sidebar, 3 KPIs, 5 lanes visible, lower rows 2 + 1 |
| 1024-1439 | sidebar, board scrolls horizontally, lower rows 2 columns |
| 768-1023 | collapsed sidebar, KPIs 3 then 2 + 1, board scrolls, panels stack |
| < 768 (phone over the LAN) | top bar + menu; order: P0 banner, **Inbox**, **Panes waiting**, KPIs, board (horizontal scroll), Sessions, Desk chat; 44 px touch targets; safe-area padding; no hover-only actions |

## 4. States, security UX, accessibility

- Per-panel skeletons; inline error with **Retry** per source; a source's age shown on hover; a stale source (> 3 × its
  refresh interval) gets an amber "stale" chip, never silent.
- `401` on a write: toast "token missing or wrong: open the address printed by `zouinify panel token rotate` in your own
  terminal" (never show the token). `429`: a persistent red banner "receipts rate-limited: something other than you is knocking"
  until the next successful receipt (also shown when the server counts five or more refused writes in the last minute). Every
  accepted write shows its receipt id and whether the desk's poll fetched it yet.
- Writes are disabled (not hidden) when the panel is bound to the intranet and no token is stored, with the reason.
- Keyboard: every control reachable, logical order, visible focus, `aria-label` on icon buttons, headings per panel, a polite
  live region for "waiting for you" count changes only. 200 % zoom and reduced motion tested.

## 5. API the UI consumes

v1 (T-0160, read-only): `GET /health` (project root, version, bind), `GET /api/snapshot` (header, kpis, inbox, board,
sessions, waiting, creds, logs, releases; each with `age_s`), `GET /api/tickets/<id>` (drawer), `GET /api/sessions/<name>`
(detail with the redacted screen), `GET /api/logs?label=<a panel.logs label>` (404 for an unknown label; the dashboard shows one tail per configured entry, none when the list is empty). Any `POST` -> 405.
v2 (T-0162/T-0163): `POST /api/receipts` on the panel port (the cookie or the bearer, on every bind; 201 `{id}`, 200 with the same
id for a repeat click while the receipt is open, 401/403/429 otherwise), `GET /api/events` (SSE); on the loopback-only listener,
for the desk: `GET /api/poll?since=<id>&timeout_ms=<n>`, `GET /api/receipts/<id>`, `POST /api/hook`.
Terminal page (separate service, `zouinify term`, Z-11): `GET /term/health`, `GET /term/?target=<name>[&text=...]` (the page;
the unlock screen when no valid cookie), `POST /term/unlock {passphrase}` -> `Set-Cookie zouinify_term` (401 wrong, 429 over
budget or locked out), `GET /term/targets` (cookie required), `POST /term/open {target}` -> `{tid}` (404 target not offered),
`GET /term/<tid>/screen` -> `{text, at, closes_in_s}`, `POST /term/<tid>/keys {ops: [{t: "text"} | {k: "Enter"}]}` (400 for a key
outside the allowlist or control characters), `POST /term/<tid>/close`, `POST /term/lock` (ends the unlock). The unlock is
required from every address, loopback included. Host/Origin as the panel; an unknown or expired `tid` is 410; no CORS.

## 6. Acceptance (UI part; the ticket's functional acceptance applies too)

1. Desktop first view: dark navy shell, sidebar, three KPIs as in 2.2, five lanes as in 2.3 with the chips, Inbox / Panes
   waiting row, Sessions / Desk chat row; visually in the family of the hero's laptop screen, flat, no collage.
2. The Inbox panel lists exactly the items `scripts/ticket.sh human` prints, in the same order, with command, expect and age;
   a tier-2 item has no button and shows the text to type.
3. A session at a permission prompt appears in "Panes waiting for you" with its since-time within one refresh.
4. The board's lanes and chips sum to the ticket count; a ticket in `ready-to-merge` is in `review`; a void plan is badged.
5. On a 390 px phone the Inbox is the first thing on screen and the board scrolls horizontally.
6. v1 has no enabled write control; `POST /api/anything` returns 405; no request leaves the host (no CDN, no fonts fetched).
7. Keyboard-only navigation reaches every control; focus visible; contrast AA on badges.
8. A pane capture containing `aws_secret_access_key = x` renders as `<redacted>`.
9. Terminal page (Z-11): a waiting pane's **[type]** opens the page for that pane; after the password, typing `2` and Enter
   answers the dialog in the real pane and the audit log has the open and the keys; a wrong password sends nothing; after
   five idle minutes the next key is refused and the field is disabled; a shell pane and another project's session are not
   offered; `Ctrl-D` is not in the key bar and is refused by the server; the panel's CI gates are unchanged and green.

## 7. What to avoid (the draft's list, plus the functional ones)

No neon/cyberpunk, no permanent fake terminals, no placeholder data once wired, no five crushed lanes on mobile, no
hover-only actions, no excessive shadows or decorative charts. **No drag-and-drop, no per-lane add, no stage buttons, no
"Acknowledge all", no generic OK, no OK that executes, no test pass-rates, no button that turns a click into a keystroke (the terminal page forwards typed keys only), no chat to
workers, no CDN or third-party request, no token or credential ever rendered, no client-side re-sorting of the inbox.**
