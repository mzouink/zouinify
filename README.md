<h1 align="center">zouinify</h1>
<p align="center"><b>One dashboard for your Claude Code sessions, tickets and browser checks.</b></p>

<p align="center">
  <img src="img/zouinify_dashboard_command_center.png" alt="zouinify dashboard: ticket board, sessions, inbox and next sitting" width="900">
</p>

You run several Claude Code sessions in tmux: a ticket desk, a manager, a debugger, one worker per ticket. zouinify gives them
one ticket lifecycle, launches them the same way every time, and shows you on one page what needs you, which session is
waiting, and what the tickets are doing. It is a Claude Code skill (`/zouinify ...`) and a plain CLI (`zouinify ...`).

> Status: week 1. The CLI works (`config`, `up`, `launch`, `status`, `down`, `briefs`). The web panel is being built.
> This repository holds the skill and generic templates only; your project's names and config live in your own repo.

## What you get

- **One lifecycle.** `backlog → ready → in-dev → in-review → ready-to-merge → merged → ready-to-deploy → deployed → done`,
  enforced by one shell script, with evidence required at every step.
- **Sessions launched right, every time.** Model, name, permission mode, brief and identity come from one config file; running
  `up` twice never opens a second copy.
- **One page for the human.** The inbox of things only you can do, the board, every session with its state, panes waiting for
  you, the apply and deploy logs, the next sitting.
- **Safe by construction.** The page never types into a Claude session and never deploys anything. A click is a receipt the
  desk session verifies; approvals and production actions stay typed by you.

## Quick start

```bash
git clone https://github.com/mzouink/zouinify ~/src/zouinify
ln -s ~/src/zouinify ~/.claude/skills/zouinify             # /zouinify inside Claude Code
ln -s ~/src/zouinify/bin/zouinify ~/.local/bin/zouinify    # zouinify from a shell
```

Then, inside your project:

```bash
cp ~/src/zouinify/templates/ops/config.yaml.example .ops/config.yaml   # edit: repos, sessions, sittings
zouinify config --check        # validates the file
zouinify briefs render         # writes .ops/briefs/*.md from the templates
zouinify up --dry-run          # shows the exact tmux + claude lines it would run
zouinify up                    # starts the desk, manager and debug sessions
```

Needs Python 3.9+ with PyYAML, tmux 3.2+, and the `claude` CLI on PATH. `zouinify init`, which also scaffolds `tickets/` and
`scripts/ticket.sh`, comes in week 3.

## How it fits together

```mermaid
flowchart LR
    H([You<br/>laptop or phone])
    P[Control panel<br/>read-only view + receipts]
    D[Desk session<br/>the one voice to you]
    T[(tickets/*.md<br/>scripts/ticket.sh)]
    W[Workers<br/>one tmux session per ticket]
    S[Manager / Debug / Chrome debug]

    H -- "click = receipt (tier 1)" --> P
    H == "you type tier 2: budget, CLAUDE.md, prod apply" ==> D
    P -. "zouinify poll" .-> D
    D -- "verify, then move / log" --> T
    D -- "zouinify launch / down" --> W
    W -- "claim, move with evidence" --> T
    T -. "human --json, board --json" .-> P
    S -. "state via hooks, claude agents, tmux" .-> P
    W -. "state via hooks, claude agents, tmux" .-> P
```

Solid arrows are actions, dotted arrows are read-only data. The bold arrow is the only channel with full authority: you,
typing in the desk's window.

## Learn more

| Document | What it covers |
|---|---|
| [`docs/REFERENCE.md`](docs/REFERENCE.md) | Every command and flag, the session-launch rules, the receipts and security model, the repository layout and the CI guards |
| [`docs/UX_SPEC.md`](docs/UX_SPEC.md) | The panel's look and behaviour: screens, tiers of clickable actions, responsive layout, acceptance criteria |
| [`SKILL.md`](SKILL.md) | What `/zouinify` does inside Claude Code and the rules it never bends |
| [`templates/ops/config.yaml.example`](templates/ops/config.yaml.example) | The per-project config, commented |

## Roadmap

1. **Week 1:** CLI core (done), read-only panel, the first project's config.
2. **Week 2:** receipts channel (browser token, audit, rate limit, `zouinify poll`), hooks, optional intranet access.
3. **Week 3:** launch and kill from the panel via the desk, `zouinify init` for a fresh repo, a home page listing your projects.

Inspired by [lavish-axi](https://github.com/kunchenguid/lavish-axi): a local page, a long-polling agent, nothing in the cloud.
