#!/usr/bin/env bash
# The panel process must never launch, kill or type into a tmux session, never run a mutating ticket.sh
# command, a deploy, an apply or a push (manager rule, 2026-10-13). CI fails when lib/zouinify/panel/
# contains such a call. Usage: scripts/check_panel_guard.sh [dir]   (default: lib/zouinify/panel)
set -u
dir=${1:-lib/zouinify/panel}
if [ ! -d "$dir" ]; then
  echo "panel guard: $dir does not exist, nothing to check"
  exit 0
fi
pat="new-session|kill-session|send-keys|new_session\\(|kill_session\\(|zouinify\\.launch|zouinify import launch"
pat="$pat|ticket\\.sh[\"' ]+(move|claim|ask|new|log|set|put|merge|prune|sync|apply)"
pat="$pat|terraform[\"', ]+(apply|plan|destroy)|docker[\"', ]+push|git[\"', ]+push|deploy_frontend"
hits=$(grep -rnE --include='*.py' --include='*.js' --include='*.html' --include='*.sh' -- "$pat" "$dir" 2>/dev/null)
if [ -n "$hits" ]; then
  echo "panel guard FAIL: the panel may not launch, kill, type, mutate tickets or deploy:"
  echo "$hits"
  exit 1
fi
echo "panel guard ok: no launcher, kill, send-keys, mutating ticket.sh or deploy call under $dir"
