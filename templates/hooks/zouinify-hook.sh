#!/bin/sh
# zouinify hook (T-0163, hardened by T-0182): forward one Claude Code hook event (Stop, Notification, SessionStart,
# SessionEnd) to the panel of the project the session runs in. The HUMAN installs it in ~/.claude/settings.json
# (`zouinify up` prints the snippet); a session never edits that file. It must never block a session: no project
# under the cwd -> exit 0 in milliseconds; otherwise one curl with a 1 s limit; the exit code is always 0 (an exit
# code 2 would block Stop).
#
# Who gets the payload: only a panel that is OURS and ALIVE. The project is found by walking up from the event's
# cwd, but a `.ops/config.yaml` counts only when this user owns it (another local user could plant one under a
# shared or world-writable directory to harvest replies); the pid file `.ops/state/panel.json` must be ours and not
# a symlink, the pid in it must be a live zouinify process of ours (a stale file after a crash, or a pid reused by an
# unrelated process, would otherwise send replies to whoever bound the port), the port must be LISTENed on by a socket
# of ours right now (/proc/net/tcp), and the per-start nonce in it goes along as a header, which the listener verifies.
payload=$(head -c 1048576)
[ -n "$payload" ] || exit 0
root="${ZOUINIFY_PROJECT:-}"
if [ -z "$root" ]; then
  # the FIRST "cwd" string value in the payload (the top-level field comes first; nested ones never win)
  cwd=$(printf '%s' "$payload" | tr '\n' ' ' | awk 'match($0, /"cwd"[ \t]*:[ \t]*"[^"\\]*"/) { s = substr($0, RSTART, RLENGTH); sub(/^"cwd"[ \t]*:[ \t]*"/, "", s); sub(/"$/, "", s); print s; exit }')
  case "$cwd" in /*) ;; *) exit 0 ;; esac
  d="$cwd"
  depth=0
  while [ -n "$d" ] && [ "$d" != "/" ] && [ "$depth" -lt 64 ]; do
    if [ -f "$d/.ops/config.yaml" ] && [ -O "$d/.ops/config.yaml" ]; then
      root="$d"
      break
    fi
    d=$(dirname "$d")
    depth=$((depth + 1))
  done
fi
[ -n "$root" ] || exit 0
pj="$root/.ops/state/panel.json"
[ -f "$pj" ] && [ ! -L "$pj" ] && [ -O "$pj" ] || exit 0
pid=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$pj" | head -n 1)
[ -n "$pid" ] && [ -d "/proc/$pid" ] && [ -O "/proc/$pid" ] || exit 0
# the process must be the panel itself (`python3 -I .../lib/zouinify/cli.py --project <root> panel start --foreground`):
# a pid reused by any other program of ours, even one living under a path that mentions zouinify, gets nothing
case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in *zouinify/cli.py*panel\ start*) ;; *) exit 0 ;; esac
port=$(sed -n 's/.*"hook_port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$pj" | head -n 1)
nonce=$(sed -n 's/.*"hook_nonce"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]*\)".*/\1/p' "$pj" | head -n 1)
[ -n "$port" ] && [ -n "$nonce" ] || exit 0
# the port must be in LISTEN (state 0A) on 127.0.0.1 by a socket this user owns, right now: a freed or squatted port never
# receives a payload (0100007F is 127.0.0.1 in /proc's byte order)
uid=$(id -u)
awk -v want="0100007F:$(printf '%04X' "$port")" -v uid="$uid" '$2 == want && $4 == "0A" && $8 == uid { found = 1 } END { exit found ? 0 : 1 }' /proc/net/tcp 2>/dev/null || exit 0
printf '%s' "$payload" | curl -s -m 1 -o /dev/null -X POST -H 'Content-Type: application/json' -H "X-Zouinify-Hook: $nonce" \
  --data-binary @- "http://127.0.0.1:$port/api/hook" >/dev/null 2>&1
exit 0
