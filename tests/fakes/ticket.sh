#!/usr/bin/env bash
# Fake scripts/ticket.sh for the zouinify test world (copied into <project>/scripts/ticket.sh).
# Records every call in $FAKE_TICKET_LOG; read-only commands answer from env; `log` appends to the
# ticket file (it needs TICKET_AS like the real one); every other mutating command is refused, so a
# test can prove the CLI never ran one.
set -u
printf '%s\n' "$*" >>"${FAKE_TICKET_LOG:?FAKE_TICKET_LOG unset}"
here=$(cd "$(dirname "$0")/.." && pwd)
case "$1 ${2:-}" in
  "human --count") echo "${FAKE_HUMAN_COUNT:-3}" ;;
  "human --json") if [ -n "${FAKE_HUMAN_JSON:-}" ] && [ -f "$FAKE_HUMAN_JSON" ]; then cat "$FAKE_HUMAN_JSON"; else echo "[]"; fi ;;
  "board --json") if [ -n "${FAKE_BOARD_JSON:-}" ] && [ -f "$FAKE_BOARD_JSON" ]; then cat "$FAKE_BOARD_JSON"; else echo "usage: board" >&2; exit 1; fi ;;
  "board ") echo "== READY (fake board)" ;;
  "human ") echo "HUMAN INBOX (fake)" ;;
  "log "*) [ -n "${TICKET_AS:-}" ] || { echo "ticket.sh: TICKET_AS unset" >&2; exit 1; }
    f=$(ls "$here/tickets/$2-"*.md 2>/dev/null | head -1); [ -n "$f" ] || { echo "no ticket $2" >&2; exit 1; }
    printf -- '- 2026-01-01 00:00 %s: %s\n' "$TICKET_AS" "$3" >>"$f" ;;
  *) echo "fake ticket.sh: refused (mutating or unknown): $*" >&2; exit 3 ;;
esac
