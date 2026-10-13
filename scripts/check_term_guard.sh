#!/usr/bin/env bash
# The terminal page (T-0181) may type into WORKER panes and do nothing else. This is an ALLOWLIST check over
# lib/zouinify/term/ and lib/zouinify/termctl.py (the server, its helpers, its process side):
#   - of tmux, only send-keys, capture-pane, list-panes, has-session and display-message may be named (and only through
#     zouinify.tmux): never new-session / kill-* / respawn-* / run-shell / pipe-pane / paste-buffer / load-buffer /
#     set-buffer / send-prefix, nor the launcher's new_session() / kill_session();
#   - no import of the launcher, no cmd_up/launch/down, no ticket_sh(), no mutating ticket.sh verb, no registry write;
#   - no terraform / docker push / git push|merge|commit|... / aws write / deploy verb;
#   - a program may be spawned only by termctl.py (the detached start) and term/tls.py (openssl), and `openssl` only there.
# Usage: scripts/check_term_guard.sh [dir] [ctl]   (default: lib/zouinify/term lib/zouinify/termctl.py)
set -u
dir=${1:-lib/zouinify/term}
ctl=${2:-lib/zouinify/termctl.py}
if [ ! -d "$dir" ]; then
  echo "term guard: $dir does not exist, nothing to check"
  exit 0
fi
inc=(--include='*.py' --include='*.js' --include='*.html' --include='*.css' --include='*.sh')
q="[\"'\`]"
tmux_pat="new-session|kill-session|kill-server|kill-window|kill-pane|respawn-pane|respawn-window|run-shell|pipe-pane|paste-buffer|load-buffer|set-buffer|send-prefix"
tmux_pat="$tmux_pat|\\b(new|kill)_session\\(|${q}(new|kill|resp|run)-${q}|tmux${q}?[[:space:]]*,?[[:space:]]*${q}?(new|kill|resp|run|paste|pipe|load|set|attach|switch|select|neww|killw|splitw)\\b"
launch_pat="(from|import)[[:space:]]+[A-Za-z_.]*\\blaunch\\b|zouinify\\.launch\\b|\\bcmd_(up|launch|down)\\(|\\bticket_sh\\(|Registry\\([^)]*\\)\\.(add|remove|save)\\b"
verb_pat="ticket\\.sh${q}?[[:space:]]*,?[[:space:]]*${q}?(move|claim|ask|new|log|set|put|merge|prune|sync|apply)\\b"
verb_pat="$verb_pat|terraform${q}?[[:space:]]*,?[[:space:]]*${q}?(apply|plan|destroy)\\b|docker${q}?[[:space:]]*,?[[:space:]]*${q}?push\\b"
verb_pat="$verb_pat|git${q}?[[:space:]]*,?[[:space:]]*${q}?(push|merge|commit|reset|checkout|rebase)\\b|deploy_[a-z_]+"
verb_pat="$verb_pat|\\baws${q}?[[:space:]]*,?[[:space:]]*${q}?[a-z0-9-]+${q}?[[:space:]]*,?[[:space:]]*${q}?(put|create|delete|update|start|stop|invoke|run)-?[a-z]*\\b"
spawn_pat="\\bsubprocess\\b|\\bos\\.(system|exec[a-z]*|spawn[a-z]*|popen|fork|forkpty|posix_spawn[a-z]*|startfile)\\b|\\bpty\\b|\\bPopen\\b|\\bcheck_output\\b|\\bcheck_call\\b|\\bgetoutput\\b|\\bmultiprocessing\\b|\\bctypes\\b|__import__|(^|[^.A-Za-z_])eval\\(|(^|[^.A-Za-z_])exec\\("
rc=0
report() { echo "term guard FAIL ($1):"; echo "$2"; rc=1; }
files=("$dir" "$ctl")
h=$(grep -rnE "${inc[@]}" -- "$tmux_pat" "${files[@]}" 2>/dev/null); [ -z "$h" ] || report "a tmux verb other than send-keys/capture-pane/list-panes/has-session/display-message, or the launcher's session calls" "$h"
h=$(grep -rnE "${inc[@]}" -- "$launch_pat" "${files[@]}" 2>/dev/null); [ -z "$h" ] || report "launcher import, cmd_up/launch/down, direct ticket_sh or a registry write" "$h"
h=$(grep -rnE "${inc[@]}" -- "$verb_pat" "${files[@]}" 2>/dev/null); [ -z "$h" ] || report "a ticket, deploy, cloud or VCS verb" "$h"
# process spawning: only termctl.py and term/tls.py
h=$(grep -rnE "${inc[@]}" -- "$spawn_pat" "$dir" 2>/dev/null | grep -v "/tls\\.py:"); [ -z "$h" ] || report "a process-spawning primitive outside term/tls.py (tmux goes through zouinify.tmux)" "$h"
h=$(grep -rnE "${inc[@]}" -- "\\bopenssl\\b" "${files[@]}" 2>/dev/null | grep -v "/tls\\.py:"); [ -z "$h" ] || report "openssl named outside term/tls.py" "$h"
if [ $rc -eq 0 ]; then
  echo "term guard ok: $dir + $ctl name no tmux verb beyond send-keys/capture-pane/list-panes/has-session, import no launcher, write no ticket or registry, spawn only in termctl.py and term/tls.py"
fi
exit $rc
