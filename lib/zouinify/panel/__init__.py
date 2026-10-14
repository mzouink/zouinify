"""The local control panel: an HTTP server over the project's state (read-only, T-0160) with one write, the
receipts (T-0162): a click is a claim the desk fetches and verifies before acting.

Rules this package never bends (manager 2026-10-13; design section 3.5; docs/UX_SPEC.md):
- it never starts, stops or types into a tmux session, never runs a mutating ticket.sh command, never
  aws/terraform/docker/git, never a deploy script -- scripts/check_panel_guard.sh greps this directory
  in CI and scripts/check_panel_ast.py checks it structurally; a receipt changes nothing by itself;
- loopback by default; `panel.bind: intranet` with the browser token on every LAN request (T-0165); the one
  write, POST /api/receipts, needs the token on every bind, even from a same-uid loopback peer (a Claude
  session is one); every other POST is 405; the desk reads receipts on the loopback-only listener (T-0163);
- every list is derived at render time from ticket.sh's read-only output, the ticket files,
  `claude agents --json`, tmux captures (redacted), the launcher registry and the config: the server
  owns no state beyond its pid file, its log, the receipts in memory and their audit (audit.jsonl).
"""
