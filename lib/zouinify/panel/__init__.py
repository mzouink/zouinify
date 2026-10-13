"""The local control panel: a read-only HTTP server over the project's state (v1, T-0160).

Rules this package never bends (manager 2026-10-13; design section 3.5; docs/UX_SPEC.md):
- it never starts, stops or types into a tmux session, never runs a mutating ticket.sh command, never
  aws/terraform/docker/git, never a deploy script -- scripts/check_panel_guard.sh greps this directory
  in CI and fails on any such call;
- it binds to loopback only until the token and receipts exist (T-0162); any POST is 405;
- every list is derived at render time from ticket.sh's read-only output, the ticket files,
  `claude agents --json`, tmux captures (redacted), the launcher registry and the config: the server
  owns no state beyond its pid file and its log.
"""
