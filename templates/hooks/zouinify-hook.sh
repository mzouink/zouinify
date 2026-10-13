#!/bin/sh
# zouinify hook (T-0163): forward one Claude Code hook event (Stop, Notification, SessionStart, SessionEnd) to the
# panel of the project the session runs in. The HUMAN installs it in ~/.claude/settings.json (`zouinify up` prints
# the snippet); a session never edits that file. It must never block a session: no project under the cwd -> exit 0
# in milliseconds; otherwise one curl with a 1 s limit; the exit code is always 0 (an exit code 2 would block Stop).
payload=$(head -c 65536)
[ -n "$payload" ] || exit 0
root="${ZOUINIFY_PROJECT:-}"
if [ -z "$root" ]; then
  cwd=$(printf '%s' "$payload" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$cwd" ] || exit 0
  d="$cwd"
  depth=0
  while [ -n "$d" ] && [ "$d" != "/" ] && [ "$d" != "." ] && [ "$depth" -lt 64 ]; do
    if [ -f "$d/.ops/config.yaml" ]; then
      root="$d"
      break
    fi
    d=$(dirname "$d")
    depth=$((depth + 1))
  done
fi
[ -n "$root" ] || exit 0
[ -f "$root/.ops/state/panel.json" ] || exit 0
port=$(sed -n 's/.*"hook_port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$root/.ops/state/panel.json" | head -n 1)
[ -n "$port" ] || exit 0
printf '%s' "$payload" | curl -s -m 1 -o /dev/null -X POST -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:$port/api/hook" >/dev/null 2>&1
exit 0
