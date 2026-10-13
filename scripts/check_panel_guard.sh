#!/usr/bin/env bash
# The panel process must never launch, kill or type into a tmux session, never run a mutating ticket.sh
# command, a deploy, an apply or a push (manager rule, 2026-10-13). This is an ALLOWLIST check over
# lib/zouinify/panel/: the panel may read through zouinify.readonly, zouinify.tickets, zouinify.status,
# zouinify.agents and the registry reader; it may not spawn a process by any primitive, import the
# launcher or the tmux wrapper, call ticket_sh() directly, write the registry, or name a tmux/ticket/deploy
# verb or the zouinify CLI. Usage: scripts/check_panel_guard.sh [dir]   (default: lib/zouinify/panel)
set -u
dir=${1:-lib/zouinify/panel}
if [ ! -d "$dir" ]; then
  echo "panel guard: $dir does not exist, nothing to check"
  exit 0
fi
inc=(--include='*.py' --include='*.js' --include='*.mjs' --include='*.cjs' --include='*.ts' --include='*.tsx' --include='*.jsx'
     --include='*.html' --include='*.sh' --include='*.bash')
q="[\"'\`]"
# 1. any way of running a program
exec_pat="\\bsubprocess\\b|\\bos\\.(system|exec[a-z]*|spawn[a-z]*|popen|fork|forkpty|posix_spawn[a-z]*|startfile)\\b|\\bpty\\b|\\bshlex\\b"
exec_pat="$exec_pat|create_subprocess|\\bmultiprocessing\\b|\\bctypes\\b|\\bPopen\\b|\\bcheck_output\\b|\\bcheck_call\\b|\\bgetoutput\\b|\\bgetstatusoutput\\b"
exec_pat="$exec_pat|\\bchild_process\\b|\\bexecSync\\b|\\bspawnSync\\b|\\bexecFile\\b|\\bspawn\\(|(^|[^.A-Za-z_])exec\\(|\\bimportlib\\b|__import__|(^|[^.A-Za-z_])eval\\(|(^|[^.A-Za-z_])compile\\("
# 2. the launcher, the tmux wrapper, direct ticket.sh, registry writes
import_pat="(from|import)[[:space:]]+[A-Za-z_.]*\\b(launch|tmux)\\b|zouinify\\.(launch|tmux)\\b|\\btmux\\.[A-Za-z_]+\\(|\\b(new|kill)_session\\(|\\bcmd_(up|launch|down)\\(|\\bticket_sh\\(|Registry\\([^)]*\\)\\.(add|remove|save)\\b|\\.(add|remove|save)\\([^)]*registry"
# 3. tmux / ticket / deploy verbs and the CLI itself, however spelled
lit_pat="new-session|kill-session|kill-server|kill-window|kill-pane|send-keys|send-prefix|respawn-pane|respawn-window|run-shell|pipe-pane|paste-buffer|load-buffer|set-buffer"
lit_pat="$lit_pat|${q}(new|kill|send|resp|run)-${q}|${q}(kill|send|new|resp)${q}[[:space:]]*\\+|tmux${q}?[[:space:]]*,?[[:space:]]*${q}?(new|kill|send|resp|run|paste|pipe|load|set|attach|switch|select|neww|killw|splitw)\\b"
lit_pat="$lit_pat|ticket\\.sh${q}?[[:space:]]*,?[[:space:]]*${q}?(move|claim|ask|new|log|set|put|merge|prune|sync|apply)\\b|bin/zouinify|zouinify${q}?[[:space:]]*,?[[:space:]]*${q}?(up|launch|down|poll)\\b"
lit_pat="$lit_pat|terraform${q}?[[:space:]]*,?[[:space:]]*${q}?(apply|plan|destroy)\\b|docker${q}?[[:space:]]*,?[[:space:]]*${q}?push\\b|git${q}?[[:space:]]*,?[[:space:]]*${q}?(push|merge|commit|reset|checkout|rebase)\\b"
lit_pat="$lit_pat|deploy_[a-z_]+|\\baws${q}?[[:space:]]*,?[[:space:]]*${q}?[a-z0-9-]+${q}?[[:space:]]*,?[[:space:]]*${q}?(put|create|delete|update|start|stop|invoke|run)-?[a-z]*\\b"
rc=0
report() { echo "panel guard FAIL ($1):"; echo "$2"; rc=1; }
h=$(grep -rnE "${inc[@]}" -- "$exec_pat" "$dir" 2>/dev/null); [ -z "$h" ] || report "a process-spawning primitive; the panel only reads through zouinify.readonly" "$h"
h=$(grep -rnE "${inc[@]}" -- "$import_pat" "$dir" 2>/dev/null); [ -z "$h" ] || report "launcher/tmux import, direct ticket_sh or a registry write" "$h"
h=$(grep -rnE "${inc[@]}" -- "$lit_pat" "$dir" 2>/dev/null); [ -z "$h" ] || report "a tmux, ticket, deploy or zouinify-CLI verb" "$h"
if [ $rc -eq 0 ]; then
  echo "panel guard ok: $dir spawns nothing, imports no launcher/tmux, writes no ticket or registry, names no mutating verb"
fi
exit $rc
