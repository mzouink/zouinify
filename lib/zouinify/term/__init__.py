"""The terminal page (T-0181): a SEPARATE process, `zouinify term`, that shows one WORKER pane of this project in the
browser and forwards the human's own keystrokes into it with `tmux send-keys`, after a passphrase unlock. It lives
outside lib/zouinify/panel/ on purpose: the panel's gates (no spawn, no tmux, no write) stay exactly as merged, and the
panel only links here. scripts/check_term_guard.sh is this package's own allowlist: send-keys, capture-pane,
list-panes, has-session and nothing else of tmux; no launcher, no ticket.sh verb, no cloud or deploy verb.

User decision 2026-10-13 ("Z-11 yes workers"): the role sessions (desk, manager, debug, chrome_debug) are never offered
and never reachable, enforced in code (zouinify.term.targets), not only by config."""
