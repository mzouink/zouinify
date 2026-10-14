#!/usr/bin/env bash
# Ticket desk core (T-0186). The project's values are PARSED from tickets/ticket.env (never sourced); the deploy code is
# tickets/plugins/<DEPLOY_PLUGIN>/plugin.sh, sourced once. zouinify vendors this file byte-identical. Protocol: tickets/README.md.
# `scripts/ticket.sh help` prints the commands.
#
# Every mutating command commits ONLY tickets/ from the MAIN worktree on trunk and pushes it.
# It refuses to run when the main worktree is dirty or not on trunk: that is how "nobody develops
# in the main worktree" is enforced, not just stated. Pushes print git's stderr on every failed
# attempt and retry after `pull --rebase` with backoff ($TICKET_PUSH_BACKOFF, default "5 15 30" s:
# a GitHub 5xx lasts minutes, so instant retries are useless). If every attempt fails the commit
# stays local (tree clean), the command exits 1, and the next mutating call pushes it first.
# `new` renumbers only on a real non-fast-forward (another session took the id), never on a 5xx.
# Who you are in log lines: $TICKET_AS = your ListAgents name. Every mutating command refuses to
# run without it (T-0128: the old `else $USER` fallback signed 44 log lines with the shell user,
# so nobody could tell which session did what); `board` and `deploy-check` are read-only and need
# nothing. While tickets/FREEZE exists on trunk, `claim` takes only the ids listed in it.
set -euo pipefail

TICKET_SH_VERSION="1.2.2"   # T-0186 (1.0.0), T-0187 (1.1.0: N repos from ticket.env), T-0189 (1.1.1: two repos sharing a path refused), T-0192 (1.1.2: that check resolves symlinks and slashes; no terraform prefix in the inbox), T-0196 (1.1.3: prune finishes or keeps a failed remove's leftovers, prune --orphans, WORKTREES validated in load_config), T-0217 (1.2.0: `plain` -- the ## In plain words section as one line -- in every board --json ticket and human --json item; backlog|skipped -> ready needs that section), T-0218 (1.2.1: plain = the FIRST ## In plain words section before ## Log, HTML comments stripped whatever they hold, only a whole-line <placeholder> dropped, capped at 1200 bytes on a UTF-8 boundary; blocked -> ready gated like backlog -> ready), T-0220 (1.2.2: the JSON-producing awks run under LC_ALL=C, so every awk counts bytes -- under gawk in a UTF-8 locale the 1200 cap was 1200 characters and %c wrote two bytes; same keys, hooks and JSON schemas): the core's contract -- the hook list, ticket.env's keys and the JSON schemas (board --json carries it first; `ticket.sh version` prints it). Bump on any change to those.
MAIN="$(cd "$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"  # from the script's own repo, not $PWD: the tickets live in the MAIN checkout
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"   # the checkout this copy runs from: its tickets/ticket.env and tickets/plugins/ apply (configuration and code travel with the script)
WHO="${TICKET_AS:-}"
LOCK_WAIT="${TICKET_LOCK_WAIT:-120}"   # seconds to wait for the ticket.sh lock
FREEZE_FILE_NAME="FREEZE"  # tickets/FREEZE: one T-id per line, '#' comments; manager-owned (tickets/README.md rule 12)
STATES="backlog ready in-dev in-review ready-to-merge merged ready-to-deploy deployed done blocked dropped skipped"
# The project's values: filled from $HERE/tickets/ticket.env by read_ticket_env (end of the file). The defaults below are
# the protocol's standard vocabulary; TRUNK and REPOS have none -- the file must name them.
PROJECT=""; TRUNK=""; TICKETS_DIR="tickets"; WORKTREES=".claude/worktrees"; REPOS=""; PROTECTED_BRANCHES_EXTRA=""
ASK_VERBS="APPLY DEPLOY PUSH RUN DECIDE CHECK APPROVE SSO"; TICKET_TYPES="bug feature process chore"; DEPLOY_PLUGIN="none"; DEPLOY_KINDS=""
declare -A REPO_PATH=() REPO_TRUNK=() REPO_PROD=()   # by repo name (REPOS); paths absolute after load_config
TD=""; REPO0=""; KNOWN_REPOS=""   # REPO0 = the default repo (this checkout), the first name in REPOS; every other repo is REPO_PATH/TRUNK by name (T-0187)

die() { echo "ticket.sh: $*" >&2; exit 1; }
usage() { # the commands, on stdout: an unknown or missing subcommand prints them and exits 1
  echo "# Ticket desk for ${PROJECT:-this project} -- scripts/ticket.sh core $TICKET_SH_VERSION, plugin $DEPLOY_PLUGIN. Protocol: tickets/README.md."
  cat <<'EOF'
#
#   scripts/ticket.sh new   <type> <severity> "<title>"          -> backlog, prints the new id
#   scripts/ticket.sh claim T-xxxx <owner> [branch] [worktree]   -> in-dev; creates the worktree under .claude/worktrees/ (an absolute path = an existing worktree of the repo; other repos: always existing, T-0176)
#   scripts/ticket.sh move  T-xxxx <state> "<evidence>"          -> any state, evidence is mandatory
#   scripts/ticket.sh set   T-xxxx <field> "<value>"              -> one front-matter field (closed_by, plan, ...)
#   scripts/ticket.sh log   T-xxxx "<line>"                      -> append a log line, no state change
#   scripts/ticket.sh put   T-xxxx <file> "<what changed>"       -> replace a ticket with a copy you edited ELSEWHERE (preferred)
#   scripts/ticket.sh sync  T-xxxx "<what changed>"              -> commit hand edits to that one ticket file (main gets dirty: avoid)
#   scripts/ticket.sh merge T-xxxx [--allow-trailers T-yyyy,..] -> the ONLY merge path: ff-only, trailers checked, closed_by after the merge
#   scripts/ticket.sh prune [T-xxxx ...|--done] [--orphans] [--dry-run]   -> remove the worktree + branch (local and origin) of done/dropped tickets; refuses anything unmerged;
#                                                                    --orphans: the dirs under .claude/worktrees/ that no worktree registers, when nothing in them is unique (T-0196)
#   scripts/ticket.sh ask   T-xxxx "<VERB> <what> | <cmd> | <expected> | <if nothing>" -> one-line ask for the human (human: field; "" clears; a move clears)
#   scripts/ticket.sh human [--count|--json]                     -> read-only: the human's inbox, derived from the tickets (T-0147); --json items carry "plain" (T-0217)
#   scripts/ticket.sh board [--json [--slow]]                    -> read-only, computed from git; --json: one object for tools, front matter + "plain" per ticket (T-0158, T-0217)
#   scripts/ticket.sh deploy-check <plan> [--tickets T-a,T-b] [--at <sha>] -> read-only pre-apply gate (T-0108)
#   scripts/ticket.sh apply <plan> [--tickets T-a,T-b] [--at <sha>] -> HUMAN only (refused in a session or a tool call): deploy-check, then the saved plan; logged, one log line per ticket (T-0149)
#   scripts/ticket.sh version                                    -> the core's version and the plugin in use (T-0186)
EOF
}
require_who() { [ -n "$WHO" ] || die "export TICKET_AS=<your ListAgents name>   (once per session; every log line is signed with it -- board needs none)"; }
BACKOFF="${TICKET_PUSH_BACKOFF:-5 15 30}"  # one retry per entry, after the first attempt
now() { date '+%Y-%m-%d %H:%M'; }
g() { git -C "$MAIN" "$@"; }

# Several sessions run this in the SAME main worktree: one session's pull --rebase fails on
# another's half-written ticket, and a commit can sweep up another's edit. So every mutating
# command holds one lock for its whole run, commits only its own file, and puts that file back
# if it dies before committing it.
EDITED=""; EDITED_NEW=""; LOCKED=""
restore_edit() {
  # a run killed mid pull --rebase must not leave main mid-rebase -- but only a run holding the lock
  # owns that rebase, and only if main was not already mid-rebase when it took the lock (board takes
  # no lock; a human resolving a rebase by hand must not lose it)
  if [ -n "$LOCKED" ] && rebasing; then
    g rebase --abort 2>/dev/null || true; echo "ticket.sh: aborted an interrupted rebase in $MAIN" >&2
  fi
  [ -n "$EDITED" ] || return 0
  local e left=""
  for e in $EDITED; do  # one or more ticket paths (no spaces in them), space-separated
    # From HEAD, not from the index: commit_push stages the file before it commits, and a commit that dies (git's
    # index.lock message from a concurrent git, T-0218) left the staged edit behind. `checkout` needs that same lock,
    # so when it fails HEAD's bytes are written with `show`, which takes none; the index can only be cleaned once the
    # lock is gone, and then the message says how. "left clean" is printed only when it is true.
    if [ -n "$EDITED_NEW" ]; then g rm -q --cached -- "$e" 2>/dev/null || true; rm -f "$e"
    else g checkout -q HEAD -- "$e" 2>/dev/null || g show "HEAD:${e#"$MAIN"/}" >"$e" 2>/dev/null || true; fi
    [ -z "$(g status --porcelain -- "$e" 2>/dev/null)" ] || left="$left ${e#"$MAIN"/}"
  done
  if [ -z "$left" ]; then
    echo "ticket.sh: aborted -- restored $(for e in $EDITED; do basename "$e"; done | tr '\n' ' ')" | sed 's/ $//' | sed 's/$/; main worktree left clean/' >&2
  else
    echo "ticket.sh: aborted -- NOT clean: a git lock blocked the restore of$left. Once no other git runs in $MAIN:  git -C $MAIN restore --staged --worktree --$left" >&2
  fi
}
trap restore_edit EXIT
trap 'exit 130' INT TERM  # a killed run still goes through the EXIT trap above

require_clean_main() {
  exec 9>"$(g rev-parse --path-format=absolute --git-common-dir)/ticket.sh.lock"
  flock -w "$LOCK_WAIT" 9 || die "another ticket.sh has held the lock for $LOCK_WAIT s"; rebasing || LOCKED=1  # a rebase found already in progress is not ours to abort
  [ "$(g rev-parse --abbrev-ref HEAD)" = "$TRUNK" ] || die "main worktree $MAIN is not on $TRUNK"
  if [ -n "$(g status --porcelain)" ]; then
    g status --short >&2
    die "main worktree is dirty (above). Nobody develops there -- move that work to .claude/worktrees/<ticket> first."
  fi
  pull_or_die
  push_pending
}

# pull --rebase that never leaves main mid-rebase: a conflict (our unpushed commits vs origin) is
# aborted and reported, because every later ticket.sh call would otherwise refuse a dirty main.
# Never `git pull`: it rebases onto whatever the SHARED .git/FETCH_HEAD says, and any fetch in main
# outside our lock rewrites it -> "fatal: Cannot rebase onto multiple branches" (T-0113, 2026-10-08).
# Fetch our trunk into its remote-tracking ref (one retry; not writing FETCH_HEAD, so a bare `git pull`
# someone runs in main is not broken by us either) and rebase onto that ref instead.
fetch_rebase() {
  local err
  err=$(g fetch -q --no-write-fetch-head origin "+refs/heads/$TRUNK:refs/remotes/origin/$TRUNK" 2>&1) \
    || { sleep "${TICKET_FETCH_RETRY_SLEEP:-2}"; err=$(g fetch -q --no-write-fetch-head origin "+refs/heads/$TRUNK:refs/remotes/origin/$TRUNK" 2>&1); } \
    || { echo "$err"; return 1; }
  g rebase -q --fork-point "origin/$TRUNK" 2>&1  # like pull: a commit force-removed from origin stays removed
}
conflict_line() { echo "ticket.sh: $(ahead_count) local commit(s) on main conflict with origin -- resolve by hand (git -C $MAIN log origin/$TRUNK..$TRUNK)"; }
pull_or_die() { # [extra line for the die message]
  local err; err=$(fetch_rebase) && return 0
  if rebasing; then
    g rebase --abort 2>/dev/null || true
    die "fetch + rebase onto origin/$TRUNK failed in $MAIN: $err
$(conflict_line)${1:+
ticket.sh: $1}"
  fi
  die "fetch + rebase onto origin/$TRUNK failed in $MAIN: $err${1:+
ticket.sh: $1}"
}

# Push trunk once, keeping git's stderr in PUSH_ERR. Never 2>/dev/null: on 2026-10-07 a GitHub
# "Internal Server Error" was hidden and `new` misread it as an id race until it gave up.
# It pushes ALL local trunk commits (same as before), e.g. an integrator's unpushed ff-merge.
PUSH_ERR=""
push_once() { # label ("push attempt <k>", "pending push")
  PUSH_ERR=$(g push -q origin "$TRUNK" 2>&1) && return 0
  { echo "ticket.sh: $1 failed:"; sed 's/^/    /' <<<"$PUSH_ERR"; } >&2
  return 1
}
# only a non-fast-forward means "someone pushed first"; `! [remote rejected] (pre-receive hook
# declined)` and 5xx are server trouble, so no [rejected] match here
is_nonff() { grep -qiE '\(fetch first\)|non-fast-forward' <<<"$PUSH_ERR"; }
ahead_count() { g rev-list --count "origin/$TRUNK..$TRUNK" 2>/dev/null || echo 0; }  # local ref: no fetch
ahead_line() { echo "ticket.sh: main is $(ahead_count) ahead of origin -- push will be retried by the next ticket.sh call" >&2; }
rebasing() { [ -d "$(g rev-parse --path-format=absolute --git-path rebase-merge)" ] || [ -d "$(g rev-parse --path-format=absolute --git-path rebase-apply)" ]; }

# rebase onto origin before a retry. A conflict aborts and returns 2 (caller decides); a failed
# fetch (outage) is just printed -- the next push attempt is still worth making.
pull_rebase() {
  local err; err=$(fetch_rebase) && return 0
  if rebasing; then g rebase --abort 2>/dev/null || true; PUSH_ERR=$err; return 2; fi
  { echo "ticket.sh: fetch + rebase failed (retrying the push anyway):"; sed 's/^/    /' <<<"$err"; } >&2
  return 0
}

# an earlier run's push failed (outage): its commit sits on local trunk. Push it before ours so the
# board/others see it; one attempt only -- this command's own commit_push retries and reports.
# Also warns about duplicate ids (a `new` race lost while a push was pending), but still pushes.
dup_ids() { ls "$TD" | sed -n 's/^\(T-[0-9]\{4\}\)-.*\.md$/\1/p' | sort | uniq -d; }
dup_files() { (cd "$TD" && ls "$1"-*.md | tr '\n' ' ' | sed 's/ $//'); }
push_pending() {
  local n id
  for id in $(dup_ids); do echo "ticket.sh: !! DUPLICATE TICKET ID $id: $(dup_files "$id") -- renumber one by hand" >&2; done
  n=$(ahead_count); [ "$n" -gt 0 ] || return 0
  echo "ticket.sh: main was $n ahead of origin (an earlier push failed) -- pushing first" >&2
  if push_once "pending push"; then echo "pushed pending: $n commit(s)"; fi
  return 0
}

# commit ONE ticket file and push; on ANY failed push rebase and retry. Only a server error waits
# out the backoff: an ordinary non-ff race needs no wait, and we hold the lock the whole time.
# If the push never succeeds the commit stays local (tree clean); the next ticket.sh run pushes it.
g_unlocked() { # git in MAIN; only when it fails naming index.lock (a concurrent `git status`/commit holds it for a moment,
  # T-0218) it is retried, at most 5 times 1 s apart, with the same arguments; any other failure is final at once
  local n=0 rc err
  while :; do
    err=$(g "$@" 2>&1) && { [ -z "$err" ] || printf '%s\n' "$err" >&2; return 0; }
    rc=$?
    case $err in *index.lock*) ;; *) printf '%s\n' "$err" >&2; return "$rc";; esac
    if [ "$n" -ge 5 ]; then printf '%s\n' "$err" >&2; echo "ticket.sh: $MAIN/.git/index.lock stayed busy through 5 retries" >&2; return "$rc"; fi
    n=$((n + 1)); echo "ticket.sh: $MAIN/.git/index.lock is busy (another git): retry $n/5 in 1 s" >&2; sleep 1
  done
}
commit_push() { # message file [file...]
  local k=1 s m=$1; shift
  g_unlocked add -- "$@"; g_unlocked commit -q -m "$m" -- "$@"; EDITED=""
  push_once "push attempt $k" && { echo "pushed: $m"; return 0; }
  for s in $BACKOFF; do
    is_nonff || sleep "$s"
    pull_rebase || die "push failed and rebase failed; commit kept locally in $MAIN: $PUSH_ERR
$(conflict_line)"
    k=$((k + 1)); push_once "push attempt $k" && { echo "pushed: $m"; return 0; }
  done
  echo "ticket.sh: push failed after $k attempts; commit kept locally in $MAIN (tree clean)" >&2
  ahead_line; exit 1
}

file_of() {
  local f; f=$(ls "$TD"/"$1"-*.md 2>/dev/null | head -1)
  [ -n "$f" ] || die "no ticket $1 in $TD"
  echo "$f"
}

# field value from the front matter, comments and surrounding space stripped
field() { awk -v k="$2" 'NR==1&&/^---$/{fm=1;next} fm&&/^---$/{exit} fm&&index($0,k":")==1{sub("^"k":[ ]*","");sub(/[ ]*#.*$/,"");print;exit}' "$1"; }

set_field() { # file key value: replace in front matter, or add before its closing ---
  local tmp; tmp=$(mktemp)
  K="$2" V="$3" awk 'BEGIN{k=ENVIRON["K"];v=ENVIRON["V"]} NR==1&&/^---$/{fm=1;print;next}
    fm&&index($0,k":")==1{print k": "v;done=1;next}
    fm&&/^---$/{if(!done)print k": "v;fm=0}
    {print}' "$1" >"$tmp" && mv "$tmp" "$1"   # ENVIRON, not -v: -v would turn a literal \n or \t in the value into a real one
}

append_log() { # file line: append at the end of "## Log" (before the next "## " heading)
  local tmp; tmp=$(mktemp)
  L="$2" awk 'BEGIN{l=ENVIRON["L"]} /^## Log/{inlog=1;print;next}
    inlog&&/^## /{print l;print "";inlog=0;done=1;blank=0;print;next}
    inlog&&/^$/{blank++;next}
    {while(blank>0){print "";blank--} print}
    END{if(inlog&&!done)print l}' "$1" >"$tmp" && mv "$tmp" "$1"
}

# tickets/FREEZE lists the ids `claim` may still take (one per line, '#' comments). Read from main AFTER
# the pull in require_clean_main, so it is trunk's current file, not a stale checkout's.
freeze_on() { [ -f "$TD/$FREEZE_FILE_NAME" ]; }
freeze_ids() { sed 's/#.*//' "$TD/$FREEZE_FILE_NAME" | grep -oE 'T-[0-9]{4}' | sort -u | tr '\n' ' ' | sed 's/ $//'; }
freeze_lists() { sed 's/#.*//' "$TD/$FREEZE_FILE_NAME" | grep -qwF -- "$1"; }

# Evidence a `move` refuses without (tickets/README.md section 5; process review 5.4): a lint against bare claims
# ("done", "SUCCEEDED", "looks fine"), not a proof. What a real apply/deploy or a read-back produces depends on the
# deploy tooling, so the rule is the plugin's (plugin_evidence_check; tickets/plugins/aws-terraform/plugin.sh has the
# calibrated AWS/Terraform/frontend patterns, T-0128). TICKET_FORCE=1 overrides, stamped FORCED like a skipped gate.
weak_evidence() { plugin_evidence_check "$1" "$2" "$3"; } # new-state deploy-field evidence -> prints why it is weak, or nothing

# allowed transitions (TICKET_WORKFLOW.md section 3); anything else needs TICKET_FORCE=1 + a reason in the evidence
NEXT_backlog="ready blocked dropped skipped"      # backlog -> ready is the ticket desk's investigation step
NEXT_ready="in-dev blocked backlog dropped skipped"
NEXT_in_dev="in-review blocked ready dropped skipped"
NEXT_in_review="ready-to-merge in-dev skipped"     # ready-to-merge: someone other than the author
NEXT_ready_to_merge="merged in-dev skipped"
NEXT_merged="ready-to-deploy done"         # done straight from merged only when deploy: none
NEXT_ready_to_deploy="deployed merged"
NEXT_deployed="done"
NEXT_blocked="backlog ready in-dev dropped skipped"
NEXT_skipped="backlog ready"               # T-0157: set aside on the user's word, before merge only (merged code ships with the next plan regardless); back through the desk
NEXT_done=""; NEXT_dropped=""
allowed() { local v="NEXT_${1//-/_}"; case " ${!v} " in *" $2 "*) return 0;; esac; return 1; }

valid_state() { case " $STATES " in *" $1 "*) ;; *) die "state '$1' not one of: $STATES";; esac; }

cmd_new() {
  [ $# -eq 3 ] || die "usage: new <${TICKET_TYPES// /|}> <P0|P1|P2|P3> \"<title>\""
  local type=$1 sev=$2 title=$3 n id slug f k=0 s pushed="" rc backed=""
  case " $TICKET_TYPES " in *" $type "*) ;; *) die "type must be ${TICKET_TYPES// /|}";; esac
  case $sev in P0|P1|P2|P3) ;; *) die "severity must be P0..P3";; esac
  require_who; require_clean_main
  slug=$(echo "$title" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]\+/-/g; s/^-//; s/-$//' | cut -c1-48 | sed 's/-$//')
  for _ in 1 2 3 4 5; do  # the id is max+1; if another session pushed the same id first, drop ours and renumber
    n=$(ls "$TD" | sed -n 's/^T-\([0-9]\{4\}\)-.*/\1/p' | sort -n | tail -1)
    id=$(printf 'T-%04d' $((10#${n:-0} + 1)))
    f="$TD/$id-$slug.md"; EDITED=$f; EDITED_NEW=1
    sed -e "s/^id:.*/id: $id/" -e "s/^type:.*/type: $type/" -e "s/^severity:.*/severity: $sev/" \
        -e "s/^status:.*/status: backlog/" -e "s/^created:.*/created: $(now)/" "$TD/_TEMPLATE.md" >"$f"
    set_field "$f" title "$title"
    append_log "$f" "- $(now) $WHO: filed -> backlog."
    g_unlocked add -- "$f"; g_unlocked commit -q -m "tickets: $id new ($type $sev) $title" -- "$f"; EDITED=""
    k=$((k + 1)); if push_once "push attempt $k"; then pushed=1; break; fi
    if ! is_nonff; then  # server error, not an id race: keep the id, back off and retry the same commit
      rc=0
      if [ -z "$backed" ]; then  # the backoff runs once per invocation: we hold the lock throughout
        backed=1
        for s in $BACKOFF; do
          sleep "$s"
          pull_rebase || { rc=2; break; }  # conflict: upstream added this very file name -> renumber
          [ "$(ls "$TD"/"$id"-*.md 2>/dev/null | wc -l)" -le 1 ] || { rc=2; break; }  # upstream took the id meanwhile
          k=$((k + 1)); if push_once "push attempt $k"; then pushed=1; break; fi
        done
        [ -z "$pushed" ] || break
      fi
      if [ $rc = 0 ]; then
        echo "ticket.sh: $id committed locally but NOT pushed: $PUSH_ERR -- do NOT re-run new; the next ticket.sh call pushes it" >&2
        ahead_line; exit 1
      fi
    fi
    g reset -q --keep HEAD~1; rm -f "$f"
    pull_or_die "$id was NOT created (our commit was dropped)"
  done
  [ -n "$pushed" ] || die "could not allocate an id after 5 attempts; last push error: $PUSH_ERR"
  echo "pushed: $id"
  echo "$id  $f  -- now fill Problem/areas/affects; the ticket desk investigates it before 'ready'"
}

cmd_claim() {
  [ $# -ge 2 ] || die "usage: claim T-xxxx <owner-ListAgents-name> [existing-branch] [existing-worktree]"
  local id=$1 owner=$2 f st slug br wt base mine forced=""
  require_who; require_clean_main
  f=$(file_of "$id"); st=$(field "$f" status)
  [ "$st" = ready ] || [ -n "${TICKET_FORCE:-}" ] || die "$id is '$st', not 'ready' (TICKET_FORCE=1 to override, and say why in the log)"
  mine=$(grep -l '^status: in-dev' "$TD"/T-*.md 2>/dev/null | xargs -r grep -l "^owner: $owner\$" || true)
  [ -z "$mine" ] || [ -n "${TICKET_FORCE:-}" ] || die "$owner already has an in-dev ticket: $mine (one in-dev per session)"
  if freeze_on && ! freeze_lists "$id"; then  # README rule 12: during a freeze only the listed ids may be claimed
    [ -n "${TICKET_FORCE:-}" ] || die "$id is not in $TICKETS_DIR/$FREEZE_FILE_NAME -- a freeze is on; claim is limited to: $(freeze_ids). Ask the manager to list it. A hotfix only: TICKET_FORCE=1, then \`ticket.sh log $id\` the reason (claim takes none)."
    forced="FORCED past $TICKETS_DIR/$FREEZE_FILE_NAME (not listed): "
  fi
  slug=$(basename "$f" .md | sed 's/^T-\([0-9]*\)-/t\1-/' | cut -c1-40 | sed 's/-$//')
  br=${3:-}; wt=${4:-}
  # Which repo: the default repo ($REPO0, this checkout) when repos: names it; else the first OTHER configured repo
  # (ticket.env's REPOS, in its order) that repos: names, cut from ITS trunk (board resolves a relative worktree against
  # that repo). A repo not in REPOS (zouinify, auth, ... until the project lists them): ticket.sh creates nothing for it,
  # the caller names an EXISTING worktree there (T-0176: an absolute path was joined onto $MAIN and a worktree appeared
  # INSIDE main, which dirtied it and blocked every session). Multi-repo tickets: the first repo here, the others by
  # hand, all recorded as {<repo>: ..., <repo>: ...} maps (ticket_branches).
  local repo=$MAIN tr=$TRUNK repos other="" wtp cur ohead orepo wre pick
  repos=$(field "$f" repos); pick=$(ticket_repo "$repos")
  if [ -z "$pick" ]; then other=${repos//[][ ]/}; other=${other:-unknown}
  elif [ "$pick" != "$REPO0" ]; then repo=${REPO_PATH[$pick]}; tr=${REPO_TRUNK[$pick]}; fi
  if [ -n "$other" ]; then
    [ -n "$wt" ] || die "$id is a $other ticket: ticket.sh creates nothing in that repo. Claim it with the absolute path of an EXISTING worktree there: claim $id $owner [branch] /path/to/that/worktree"
    case $wt in /*) ;; *) die "$id is a $other ticket: the worktree is the absolute path of an existing worktree of that repo (got '$wt')";; esac
    [ -e "$wt" ] || die "$id: worktree $wt does not exist -- create it in the $other repo first (git worktree add ...), then claim"
    wtp=$(readlink -f "$wt"); wt=$wtp   # stored resolved: symlinks followed, no trailing slash
    [ "$(git -C "$wtp" rev-parse --show-toplevel 2>/dev/null)" = "$wtp" ] || die "$id: $wt is not the top of a git worktree"
    ! is_main_checkout "$wtp" || die "$id: $wt is the main checkout of the $other repo, not a worktree -- nobody develops in a main checkout; claim with a worktree of it"
    orepo=$(dirname "$(git -C "$wtp" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")   # the repo that owns this worktree
    [ -n "$(worktree_entry "$orepo" "$wtp")" ] || die "$id: $wt is not a registered worktree of $orepo (see: git -C $orepo worktree list)"
    cur=$(git -C "$wtp" symbolic-ref -q --short HEAD 2>/dev/null || true)
    [ -n "$cur" ] || die "$id: worktree $wt is on a detached HEAD -- check out the ticket's branch there first"
    ohead=$(git -C "$wtp" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true); ohead=${ohead#origin/}
    [ -z "$ohead" ] || [ "$cur" != "$ohead" ] || die "$id: worktree $wt is on that repo's trunk ($cur) -- check out the ticket's branch there first"
    [ -z "$br" ] || [ "$br" = "$cur" ] || die "$id: worktree $wt is on branch $cur, not $br"
    br=$cur
    base=$(git -C "$wtp" rev-parse --short "$(git -C "$wtp" merge-base HEAD origin/HEAD 2>/dev/null || echo HEAD)")   # where it left that repo's trunk, when known
  else
    git -C "$repo" fetch -q origin "$tr" 2>/dev/null || true
    base=$(git -C "$repo" rev-parse --short "$tr")
    case ${wt:-$WORKTREES/$slug} in
      /*) # used as given, never joined onto the repo path; it must already be one of this repo's worktrees (checked below)
          [ -e "$wt" ] || die "$id: worktree $wt does not exist. An absolute worktree is used as given and must already be a worktree of $repo; leave it out (or give $WORKTREES/<name>) and claim creates one"
          wtp=$(readlink -f "$wt"); wt=$wtp ;;   # stored resolved: symlinks followed, no trailing slash
      *)  wt=${wt:-$WORKTREES/$slug}; wt=${wt%/}   # one level under $WORKTREES/ (git ignores it there): no '..', no nesting
          wre="^$(re_escape "$WORKTREES")/[^/]+\$"
          [[ $wt =~ $wre && ${wt##*/} != . && ${wt##*/} != .. ]] || die "$id: a relative worktree is $WORKTREES/<name> -- one level, no '..' (anywhere else dirties $repo and blocks every session) -- got '$wt'"
          wtp=$repo/$wt ;;
    esac
    if [ -e "$wtp" ]; then   # an existing path: a registered worktree of this repo, not its main checkout, on the ticket's branch
      [ -n "$(worktree_entry "$repo" "$wtp")" ] || die "$id: $wt exists but is not a worktree of $repo (see: git -C $repo worktree list)"
      ! is_main_checkout "$wtp" || die "$id: $wt is the main checkout of $repo, not a worktree -- nobody develops in main; leave the worktree out and claim creates one"
      cur=$(git -C "$wtp" symbolic-ref -q --short HEAD 2>/dev/null || true)
      [ -n "$cur" ] || die "$id: worktree $wt is on a detached HEAD -- check out the ticket's branch there first"
      [ "$cur" != "$tr" ] || die "$id: worktree $wt is on trunk ($tr) -- check out the ticket's branch there first"
      [ -z "$br" ] || [ "$br" = "$cur" ] || die "$id: worktree $wt is on branch $cur, not $br"
      br=$cur
    else
      br=${br:-$slug}
      if git -C "$repo" show-ref -q --verify "refs/heads/$br"; then git -C "$repo" worktree add -q "$wtp" "$br"
      else git -C "$repo" worktree add -q "$wtp" -b "$br" "$tr"; fi
    fi
  fi
  EDITED=$f
  for kv in "status=in-dev" "owner=$owner" "worktree=$wt" "branch=$br" "base=$base" "claimed=$(now)"; do
    set_field "$f" "${kv%%=*}" "${kv#*=}"
  done
  [ -z "$(field "$f" human)" ] || { set_field "$f" human ""; append_log "$f" "- $(now) $WHO: the pending ask for the human was cleared by the claim."; }
  append_log "$f" "- $(now) $WHO: ${forced}claimed -> in-dev. base=$base, worktree=$wt, branch=$br."
  commit_push "tickets: $id -> in-dev ($owner)" "$f"
  echo "work in: $wtp   (commit trailer: 'Ticket: $id')"
}

cmd_move() {
  [ $# -eq 3 ] && [ -n "$3" ] || die 'usage: move T-xxxx <state> "<evidence: command + result, sha, S3 key, ...>"'
  local id=$1 st=$2 ev=$3 f old weak
  valid_state "$st"; require_who; require_clean_main
  f=$(file_of "$id"); old=$(field "$f" status)
  if ! allowed "$old" "$st"; then
    local v="NEXT_${old//-/_}"
    [ -n "${TICKET_FORCE:-}" ] || die "$id: $old -> $st skips a gate (allowed from $old: ${!v:-none}). TICKET_FORCE=1 only with the reason in the evidence."
    ev="FORCED $old -> $st: $ev"
  fi
  if [ "$st" = merged ]; then  # T-0129: the only merge path is `ticket.sh merge`, which writes merged itself
    [ -n "${TICKET_FORCE:-}" ] || die "$id: -> merged is written by 'scripts/ticket.sh merge $id' (ff-only, trailers checked, closed_by read after the merge), not by move. TICKET_FORCE=1 only for a merge that already happened by hand, with its shas in the evidence."
    ev="FORCED (merged by hand, not ticket.sh merge): $ev"
  fi
  if { [ "$old" = backlog ] || [ "$old" = skipped ] || [ "$old" = blocked ]; } && [ "$st" = ready ] && [ -z "$(sed -n '/^## Investigation/,/^## Log/p' "$f" | grep -v '^## \|^<\|^scope\|^$' | head -1)" ]; then   # blocked: T-0218
    die "$id: $old -> ready needs a written ## Investigation (the ticket desk's job; see tickets/README.md)"
  fi
  if { [ "$old" = backlog ] || [ "$old" = skipped ] || [ "$old" = blocked ]; } && [ "$st" = ready ] && [ -z "$(plain_words "$f")" ]; then   # T-0217: T-0215's rule; blocked: T-0218
    [ -n "${TICKET_FORCE:-}" ] || die "$id: $old -> ready needs a written ## In plain words section: right after the front matter, 2-4 sentences the human can understand (what is wrong or wanted, why it matters to them, what they will notice when it is done, what they must do), no paths, shas, session names or acronyms (T-0215). Write it with 'scripts/ticket.sh put $id <your edited copy> \"In plain words\"'. TICKET_FORCE=1 only with the reason in the evidence."
    ev="FORCED (no In plain words section): $ev"
  fi
  if [ "$st" = done ] && [ "$old" = merged ] && [ "$(field "$f" deploy)" != none ]; then
    die "$id: merged -> done only when deploy: none; this ticket deploys via '$(field "$f" deploy)' (ready-to-deploy -> deployed first)"
  fi
  weak=$(weak_evidence "$st" "$(field "$f" deploy)" "$ev")
  if [ -n "$weak" ]; then
    [ -n "${TICKET_FORCE:-}" ] || die "$id: $weak (tickets/README.md section 5) -- not: $ev"
    ev="FORCED (weak evidence): $ev"
  fi
  EDITED=$f
  set_field "$f" status "$st"
  local cleared=""
  [ -z "$(field "$f" human)" ] || { set_field "$f" human ""; cleared=" (the pending ask for the human was cleared by this move)"; }
  append_log "$f" "- $(now) $WHO: $old -> $st. $ev$cleared"
  commit_push "tickets: $id $old -> $st" "$f"
}

cmd_set() {
  [ $# -eq 3 ] || die 'usage: set T-xxxx <field> "<value>"'
  [ "$2" != status ] || die "use 'move' to change status (it requires evidence)"
  [ "$2" != human ] || die "use 'ask' for the human: field (it checks the verb, the fields and the plan)"
  local f; require_who; require_clean_main; f=$(file_of "$1"); EDITED=$f
  set_field "$f" "$2" "$3"
  append_log "$f" "- $(now) $WHO: set $2: $3"
  commit_push "tickets: $1 set $2" "$f"
}

cmd_sync() { # commit hand edits to ticket body/front matter (Problem, Investigation, areas...) -- one file only
  [ $# -eq 2 ] && [ -n "$2" ] || die 'usage: sync T-xxxx "<what changed>"   (after editing the ticket file in the main worktree)'
  local f other err
  require_who
  exec 9>"$(g rev-parse --path-format=absolute --git-common-dir)/ticket.sh.lock"
  flock -w "$LOCK_WAIT" 9 || die "another ticket.sh has held the lock for $LOCK_WAIT s"; rebasing || LOCKED=1  # a rebase found already in progress is not ours to abort
  [ "$(g rev-parse --abbrev-ref HEAD)" = "$TRUNK" ] || die "main worktree $MAIN is not on $TRUNK"
  push_pending  # no require_clean_main here (the tree is dirty by design), so push leftovers ourselves
  f=$(file_of "$1")
  other=$(g status --porcelain | grep -v " $TICKETS_DIR/$(basename "$f")\$" || true)
  [ -z "$other" ] || { echo "$other" >&2; die "main worktree has changes besides $(basename "$f") (above)"; }
  [ -n "$(g status --porcelain -- "$f")" ] || die "no edits to $(basename "$f")"
  [ "$(g diff -- "$f" | grep -c '^-status:')" = 0 ] || die "status changed by hand -- revert that line and use 'move' (it checks the gates)"
  append_log "$f" "- $(now) $WHO: edited: $2"
  # commit first, then rebase onto whatever others pushed (pull --rebase refuses a dirty tree)
  commit_push "tickets: $1 edit ($2)" "$f"
}

cmd_put() { # replace one ticket file with a version written OUTSIDE the main worktree, atomically
  [ $# -eq 3 ] && [ -f "$2" ] && [ -n "$3" ] || die 'usage: put T-xxxx <file written in your own worktree/scratchpad> "<what changed>"'
  local id=$1 src=$2 f old new
  require_who; require_clean_main; f=$(file_of "$id")
  [ "$(field "$src" id)" = "$id" ] || die "$src: front matter id is not $id"
  old=$(field "$f" status); new=$(field "$src" status)
  [ "$old" = "$new" ] || die "$src changes status ($old -> $new); keep '$old' and use 'move' (it checks the gates)"
  EDITED=$f; cp "$src" "$f"
  append_log "$f" "- $(now) $WHO: edited: $3"
  commit_push "tickets: $id edit ($3)" "$f"
}

cmd_log() {
  [ $# -eq 2 ] && [ -n "$2" ] || die 'usage: log T-xxxx "<line>"'
  local f; require_who; require_clean_main; f=$(file_of "$1"); EDITED=$f
  append_log "$f" "- $(now) $WHO: $2"
  commit_push "tickets: $1 log" "$f"
}

# ---- merge: the only sanctioned way a ticket branch reaches trunk (T-0129) -------------------
# By hand this produced a pre-merge closed_by sha twice (T-0011, T-0043) and multi-ticket branches
# (t0031-image-pins). Here: ready-to-merge only; a branch for every repo the ticket names, none of them
# trunk or a protected name; the branch pushed and not behind trunk; every commit carries
# `Ticket: <this id>` (another ticket's trailer only with --allow-trailers, logged in both tickets);
# ff-only under the lock; the merged trunk pushed at once -- a push that fails undoes that repo's local
# merge, so main is never ahead with somebody's branch; closed_by = the shas of trunk..NEW-HEAD read
# AFTER the merge. A two-repo ticket is CHECKED whole before either repo is merged; if the second
# repo's push then fails, the first half stays merged on trunk, the ticket logs the partial merge, and
# a re-run records that half as already merged and finishes the other.
MERGED_SHAS=""; MERGED_RANGE=""; ALREADY_MERGED=""; MERGE_ERR=""; MOVED=""
declare -A AM_SHAS=() AM_RANGE=()   # per repo label, set by merge_check for an already-merged branch
# KNOWN_REPOS (= ticket.env's REPOS, set by load_config): the repos: values ticket.sh can merge and prune.
# Names ticket.sh never merges from and never deletes: main/master and the usual trunk names, release and archive
# namespaces, odd names, plus every trunk/prod branch of REPOS and ticket.env's PROTECTED_BRANCHES_EXTRA (load_config
# builds the regex; this project lists its two old trunks there).
PROTECTED_BRANCH_RE_CORE='main|master|dev|develop|staging|prod|production|HEAD|(archive|archives|release|releases|prod)/.*'
PROTECTED_BRANCH_RE=""
re_escape() { printf '%s' "$1" | sed 's/[][\\.*^$|?+(){}]/\\&/g'; }   # a literal for an ERE
branch_problem() { # name trunk -> why this branch is off-limits, or nothing
  local br=$1 tr=$2
  if [ -z "$br" ]; then echo "empty branch name"
  elif [ "$br" = "$tr" ]; then echo "it is trunk ($tr)"
  elif [[ $br =~ $PROTECTED_BRANCH_RE ]]; then echo "protected name"
  elif ! g check-ref-format --branch "$br" >/dev/null 2>&1; then echo "not a valid branch name"
  else case $br in -*|*' '*|*..*|*/|refs/*|*@\{*) echo "odd name";; esac; fi
}
repo_label() { local r; for r in $REPOS; do [ "${REPO_PATH[$r]}" = "$1" ] && { echo "$r"; return 0; }; done; echo "?"; }   # repo path -> its name in REPOS
repo_trunk() { [ "$1" = "$REPO0" ] && echo "$TRUNK" || echo "${REPO_TRUNK[$1]}"; }   # repo name -> its trunk (the default repo's is TRUNK, which TICKET_TRUNK may override)
map_wt() { # worktree-map repo -> that repo's worktree as branch_stats/prune want it: the other repos' map paths are written relative to their parent dir
  local v; v=$(mapget "$1" "$2"); if [ "$2" = "$REPO0" ]; then echo "$v"; else sed "s|^$(basename "${REPO_PATH[$2]}")/||" <<<"$v"; fi
}
ticket_repo() { # repos-field -> the configured repo a plain branch:/worktree: belongs to: the default repo when named, else the first other configured name repos: lists (REPOS order), else nothing
  local toks r t; toks=$(tr -d '[]' <<<"$1" | tr ',' ' ')
  for t in $toks; do [ "$t" != "$REPO0" ] || { echo "$REPO0"; return 0; }; done
  for r in $REPOS; do for t in $toks; do [ "$t" != "$r" ] || { echo "$r"; return 0; }; done; done
  return 0
}
declare -A T_BR=() T_WT=()   # set by ticket_branches: the ticket's branch and worktree per repo name
ticket_branches() { # ticket-file -> T_BR[repo] / T_WT[repo] for every repo the fields cover; BRANCH_PROBLEMS = why they do not cover repos:
  local f=$1 repos br wt k r keys pick letters=(x y z w v u t s) i=0 maps=""
  repos=$(field "$f" repos | tr -d '[]' | tr ',' ' '); br=$(field "$f" branch); wt=$(field "$f" worktree)
  T_BR=(); T_WT=(); BRANCH_PROBLEMS=""
  # shellcheck disable=SC2086
  set -- $repos
  if [[ $br == "{"* ]]; then
    for r in $REPOS; do T_BR[$r]=$(mapget "$br" "$r"); T_WT[$r]=$(map_wt "$wt" "$r"); done
    keys=$(tr -d '{}' <<<"$br" | tr ',' '\n' | sed -n 's/^[ ]*\([^: ]*\):.*/\1/p')
    for k in $keys; do case " $KNOWN_REPOS " in *" $k "*) ;; *) BRANCH_PROBLEMS="$BRANCH_PROBLEMS the branch map names repo '$k', which ticket.sh cannot merge or prune (only: $KNOWN_REPOS);";; esac; done
  elif [ $# -le 1 ]; then   # one repo (or none named): the plain values are its; an unknown name takes the default repo's slot and is flagged below
    pick=$(ticket_repo "$repos"); pick=${pick:-$REPO0}
    T_BR[$pick]=$br; T_WT[$pick]=$wt
  elif [ -n "$br" ]; then  # merge refuses this (which repo?); prune looks for the name in every repo named, each checked on its own
    for r in "$@"; do T_BR[$r]=$br; T_WT[$r]=$wt; maps="$maps$r: ${letters[i]:-?}, "; i=$((i + 1)); done
    BRANCH_PROBLEMS=" repos: [$*] but branch: is one plain name -- a multi-repo ticket needs maps {${maps%, }} for branch: and worktree:;"
  fi
  [ -n "$br" ] || return 0  # never claimed: nothing to merge or prune, nothing to complain about
  for r in "$@"; do
    case " $KNOWN_REPOS " in
      *" $r "*) [ -n "${T_BR[$r]-}" ] || BRANCH_PROBLEMS="$BRANCH_PROBLEMS no $r branch although repos: names it;" ;;
      *) BRANCH_PROBLEMS="$BRANCH_PROBLEMS repos: names '$r', which ticket.sh cannot merge or prune (only: $KNOWN_REPOS);" ;;
    esac
  done
}
fetch_ref() { git -C "$1" fetch -q --no-write-fetch-head origin "+refs/heads/$2:refs/remotes/origin/$2" 2>/dev/null; }
commit_ticket_ids() { # repo sha -> every T-id on its "Ticket:" trailer lines (one per line, or comma-separated)
  git -C "$1" show -s --format=%B "$2" | sed -n 's/^Ticket:[ ]*//p' | grep -oE 'T-[0-9]{4}' | sort -u | tr '\n' ' ' || true
}
merge_check() { # repo trunk branch id allowed-ids: every refusal, before anything is touched
  local repo=$1 tr=$2 br=$3 id=$4 allow=$5 lbl tip remote n bad="" sha ids t why code
  lbl=$(repo_label "$repo")
  why=$(branch_problem "$br" "$tr"); [ -z "$why" ] || die "$id ($lbl): refusing to merge from '$br': $why"
  if [ "$repo" != "$MAIN" ]; then  # main's trunk was pulled by require_clean_main; the other repo's main must be on its trunk, clean, in sync
    [ -d "$repo" ] || die "$id: $lbl repo $repo not found (tickets/ticket.env REPO_$(repo_key "$lbl")_PATH, or TICKET_FE_REPO)"
    [ "$(git -C "$repo" rev-parse --abbrev-ref HEAD)" = "$tr" ] || die "$id: $repo is not on $tr"
    [ -z "$(git -C "$repo" status --porcelain)" ] || die "$id: $repo is dirty -- nobody develops there; clean it first"
    fetch_ref "$repo" "$tr" || die "$id: fetch of $tr in $repo failed"
    [ "$(git -C "$repo" rev-parse "$tr")" = "$(git -C "$repo" rev-parse "origin/$tr")" ] || die "$id: $repo: local $tr != origin/$tr -- bring it in sync first (git -C $repo pull --ff-only origin $tr)"
  fi
  fetch_ref "$repo" "$br" || die "$id ($lbl): branch $br is not on origin -- push it first"
  remote=$(git -C "$repo" rev-parse "refs/remotes/origin/$br")
  if git -C "$repo" show-ref -q --verify "refs/heads/$br"; then
    tip=$(git -C "$repo" rev-parse "refs/heads/$br")
    [ "$tip" = "$remote" ] || die "$id ($lbl): local $br is $(cut -c1-7 <<<"$tip") but origin/$br is $(cut -c1-7 <<<"$remote") -- push the branch (or pull it) first"
  fi
  if git -C "$repo" merge-base --is-ancestor "$remote" "$tr"; then
    # already on trunk (behind it or not): the other half of a two-repo ticket landed before a failed push, or
    # someone ff-merged by hand. Record it instead of dying "nothing to merge" / "rebase first" (which would strand
    # the ticket) -- but only if something of this ticket is there; decided HERE, before any repo moves.
    already_merged_record "$repo" "$tr" "$remote" "$id" "$lbl" || die "$MERGE_ERR"
    ALREADY_MERGED="${ALREADY_MERGED:+$ALREADY_MERGED }$lbl"; return 0
  fi
  n=$(git -C "$repo" rev-list --count "$remote..$tr")
  if [ "$n" != 0 ]; then  # ff-only needs the branch to contain trunk; until T-0131 every ticket move adds a tickets/ commit there
    code=$(git -C "$repo" log --format= --name-only "$remote..$tr" | grep -c -v "^$TICKETS_DIR/\|^$" || true)
    die "$id ($lbl): $br is $n commit(s) behind $tr ($code of them touch files outside tickets/) -- rebase first:
    git -C <worktree> fetch origin $tr && git -C <worktree> rebase origin/$tr   # then re-run the suites if any of those $code commits is code
    git -C <worktree> push --force-with-lease origin $br
    scripts/ticket.sh merge $id"
  fi
  for sha in $(git -C "$repo" rev-list --reverse "$tr..$remote"); do
    ids=$(commit_ticket_ids "$repo" "$sha")
    if [ -z "$ids" ]; then bad="$bad
    $(git -C "$repo" show -s --format='%h %s' "$sha")   <- no 'Ticket: $id' trailer"; continue; fi
    for t in $ids; do
      [ "$t" = "$id" ] && continue
      case " $allow " in *" $t "*) continue;; esac
      bad="$bad
    $(git -C "$repo" show -s --format='%h %s' "$sha")   <- trailer $t is another ticket: a shared branch needs --allow-trailers $t (agreed by both owners; it is logged in both tickets), otherwise split it"
    done
  done
  [ -z "$bad" ] || die "$id ($lbl): refusing to merge $br:$bad"
}
already_merged_record() { # repo trunk tip id label -> AM_SHAS_<label>, AM_RANGE_<label>; returns 1 with MERGE_ERR when nothing of the ticket is there
  # the branch's own commits: those below the tip that carry this ticket's trailer (the base: field goes stale
  # after a rebase, and tickets/ commits sit between it and the tip), from the base when it is an ancestor
  local repo=$1 tr=$2 new=$3 id=$4 lbl=$5 base range shown shas
  base=$(field "$(file_of "$id")" base); [[ $base != "{"* ]] || base=$(mapget "$base" "$lbl")
  if [ -n "$base" ] && git -C "$repo" merge-base --is-ancestor "$base" "$new" 2>/dev/null; then range="$base..$new"; shown="$(cut -c1-7 <<<"$base")..$(cut -c1-7 <<<"$new")"
  else range="--max-count=50 $new"; shown="the last 50 commits below $(cut -c1-7 <<<"$new")"; fi
  # shellcheck disable=SC2086
  shas=$(git -C "$repo" rev-list --reverse --abbrev-commit --abbrev=7 -E --grep="^Ticket:.*$id([^0-9]|\$)" $range | tr '\n' ' ' | sed 's/ $//; s/ /, /g')
  if [ -z "$shas" ]; then  # recording the tip would name an unrelated trunk commit
    if [ -z "${TICKET_FORCE:-}" ]; then
      MERGE_ERR="$id ($lbl): the branch is already on $tr, but no commit in $shown carries 'Ticket: $id' -- nothing of this ticket to record. If the work landed under another ticket or by hand without the trailer, TICKET_FORCE=1 records the tip $(cut -c1-7 <<<"$new") with a FORCED note; otherwise fix the branch: field."
      return 1
    fi
    shas=$(cut -c1-7 <<<"$new"); AM_RANGE[$lbl]="already on $tr (tip $(cut -c1-7 <<<"$new"); FORCED: no commit carries 'Ticket: $id')"
  else
    AM_RANGE[$lbl]="already on $tr (tip $(cut -c1-7 <<<"$new"))"
  fi
  AM_SHAS[$lbl]=$shas
}
push_trunk_no_rebase() { # repo trunk: like commit_push's retries, but NEVER rebase -- the ff must land byte-identical
  local repo=$1 tr=$2 k=1 s
  PUSH_ERR=$(git -C "$repo" push -q origin "$tr" 2>&1) && return 0
  for s in $BACKOFF; do
    is_nonff && return 1  # trunk moved under us: the caller undoes the merge, the owner rebases
    { echo "ticket.sh: push attempt $k of $tr failed:"; sed 's/^/    /' <<<"$PUSH_ERR"; } >&2
    sleep "$s"; k=$((k + 1))
    PUSH_ERR=$(git -C "$repo" push -q origin "$tr" 2>&1) && return 0
  done
  return 1
}
merge_one() { # repo trunk branch id -> MERGED_SHAS (short, oldest first), MERGED_RANGE "old..new", MOVED=1 when trunk moved; trunk pushed. Returns 1 with MERGE_ERR.
  local repo=$1 tr=$2 br=$3 id=$4 pre new lbl; lbl=$(repo_label "$repo"); MOVED=""; MERGE_ERR=""
  case " $ALREADY_MERGED " in *" $lbl "*)  # see merge_check / already_merged_record: nothing to move, record what is already on trunk
    MERGED_SHAS=${AM_SHAS[$lbl]-}; MERGED_RANGE=${AM_RANGE[$lbl]-}
    echo "merged ($lbl): $br was already on $tr -- recorded, nothing moved"; return 0 ;;
  esac
  pre=$(git -C "$repo" rev-parse HEAD)
  if ! git -C "$repo" merge -q --ff-only "refs/remotes/origin/$br" >/dev/null 2>&1; then
    MERGE_ERR="$id ($lbl): ff-only merge of $br into $tr failed (did $tr move?) -- rebase and retry"; return 1
  fi
  new=$(git -C "$repo" rev-parse HEAD)
  [ "$new" != "$pre" ] || { MERGE_ERR="$id ($lbl): merging $br changed nothing"; return 1; }
  if ! push_trunk_no_rebase "$repo" "$tr"; then
    git -C "$repo" reset -q --keep "$pre"
    MERGE_ERR="$id ($lbl): push of $tr failed after the merge; the local merge was undone ($tr back at $(cut -c1-7 <<<"$pre")): $PUSH_ERR
ticket.sh: if origin/$tr moved, rebase $br onto it, push, and run merge again"; return 1
  fi
  MOVED=1
  MERGED_SHAS=$(git -C "$repo" rev-list --reverse --abbrev-commit --abbrev=7 "$pre..$new" | tr '\n' ' ' | sed 's/ $//; s/ /, /g')
  MERGED_RANGE="$(cut -c1-7 <<<"$pre")..$(cut -c1-7 <<<"$new")"
  echo "merged ($lbl): $br -> $tr $MERGED_RANGE, pushed"
}
cmd_merge() {
  local id="" allow="" a f st deploy closed="" summary="" next of landed="" moved_any="" r any=""
  while [ $# -gt 0 ]; do
    case $1 in
      --allow-trailers) [ -n "${2:-}" ] || die 'usage: merge T-xxxx --allow-trailers T-yyyy[,T-zzzz]   (the option needs the ids)'; allow=$(tr ',' ' ' <<<"$2"); shift 2 ;;
      --allow-trailers=*) allow=$(tr ',' ' ' <<<"${1#*=}"); shift ;;
      T-[0-9][0-9][0-9][0-9]) id=$1; shift ;;
      *) die 'usage: merge T-xxxx [--allow-trailers T-yyyy,T-zzzz]   (ticket ready-to-merge; branch pushed and rebased; every commit trailered "Ticket: T-xxxx")' ;;
    esac
  done
  [ -n "$id" ] || die 'usage: merge T-xxxx [--allow-trailers T-yyyy,T-zzzz]'
  for a in $allow; do
    [[ $a == T-[0-9][0-9][0-9][0-9] ]] || die "--allow-trailers: '$a' is not a T-xxxx id"
    [ "$a" != "$id" ] || die "--allow-trailers: $a is this ticket"
    [ -n "$(ls "$TD"/"$a"-*.md 2>/dev/null)" ] || die "--allow-trailers: no ticket $a"
  done
  require_who; require_clean_main
  f=$(file_of "$id"); st=$(field "$f" status); deploy=$(field "$f" deploy)
  [ "$st" = ready-to-merge ] || die "$id is '$st', not 'ready-to-merge' (a reviewer who did not write it and saw the diff moves it there)"
  ticket_branches "$f"
  [ -z "$BRANCH_PROBLEMS" ] || die "$id: cannot merge:$BRANCH_PROBLEMS fix the ticket's repos:/branch:/worktree: fields (scripts/ticket.sh set) and retry"
  for r in $REPOS; do [ -z "${T_BR[$r]-}" ] || any=1; done
  [ -n "$any" ] || die "$id has no branch: field"
  # check everything before merging anything: a multi-repo ticket is refused whole when any part is not ready
  for r in $REPOS; do [ -z "${T_BR[$r]-}" ] || merge_check "${REPO_PATH[$r]}" "$(repo_trunk "$r")" "${T_BR[$r]}" "$id" "$allow"; done
  for r in $REPOS; do   # REPOS order: the default repo first
    [ -n "${T_BR[$r]-}" ] || continue
    if ! merge_one "${REPO_PATH[$r]}" "$(repo_trunk "$r")" "${T_BR[$r]}" "$id"; then
      if [ -n "$moved_any" ]; then  # an earlier repo landed in this run: on record before dying; the next run records it as already merged
        EDITED=$f
        append_log "$f" "- $(now) $WHO: PARTIAL merge (ticket.sh merge): $landed ff-merged $summary and pushed (shas $closed); $r NOT merged: $(head -n 1 <<<"$MERGE_ERR"). Status unchanged -- fix the $r side and run merge again."
        commit_push "tickets: $id partial merge ($landed landed, $r failed)" "$f"
      fi
      die "$MERGE_ERR"
    fi
    [ -z "$MOVED" ] || moved_any=1
    if [ "$r" = "$REPO0" ]; then closed=$MERGED_SHAS; summary=$MERGED_RANGE
    else closed="${closed:+$closed, }$r $MERGED_SHAS"; summary="${summary:+$summary; }$r $MERGED_RANGE"; fi
    landed="${landed:+$landed+}$r"
  done
  EDITED=$f
  set_field "$f" closed_by "[$closed]"
  set_field "$f" status merged
  local cleared=""
  [ -z "$(field "$f" human)" ] || { set_field "$f" human ""; cleared=" (the pending ask for the human was cleared by the merge)"; }
  append_log "$f" "- $(now) $WHO: ready-to-merge -> merged. ticket.sh merge: ff-merged $summary, pushed; closed_by [$closed]${allow:+; shared branch, trailers also allowed: $allow}.$cleared"
  commit_push "tickets: $id ready-to-merge -> merged" "$f"
  for a in $allow; do  # the agreement is on record in the other ticket too
    of=$(file_of "$a"); EDITED=$of
    append_log "$of" "- $(now) $WHO: $id merged the shared branch that also carries this ticket's trailer: $summary (closed_by of $id: [$closed])."
    commit_push "tickets: $a log (shared branch merged by $id)" "$of"
  done
  if [ "$deploy" = none ]; then next="scripts/ticket.sh move $id done \"<what was checked>\"   (deploy: none)"
  else next="plan + Deploy handoff, then scripts/ticket.sh move $id ready-to-deploy \"handoff written: ...\"   (deploy: $deploy)"; fi
  echo "closed_by: [$closed]"; echo "next: $next"; echo "at done: scripts/ticket.sh prune $id"
}

# ---- prune: a done/dropped ticket's worktree and branch go (README rule 11) -----------------
# Two passes. 1 (no lock; the slow part: fetches): for every candidate, refuse and say why -- a trunk,
# protected or odd branch name; another open ticket naming the same branch or worktree; a worktree that
# is not registered, detached (mid-rebase), on another branch, dirty, or holding ignored files that may
# matter (tfstate, tfvars, plans, backups, keys); a commit with no patch-equivalent in trunk, local AND
# origin's (git cherry: a branch left at its pre-rebase tip after a rebase-and-merge counts as merged)
# -- or list it. 2 (under the lock, trunk pulled; seconds): every planned item is re-checked against what
# may have changed in the window -- the ticket's status and the open tickets naming the branch (re-read from
# the pulled trunk), the worktree (registered, on the branch, clean, no valuable ignored files) and the
# branch sha -- then removed, the remote delete leased on the sha pass 1 saw, and ONE commit logs every
# pruned ticket. --dry-run is pass 1 alone: no lock, no TICKET_AS.
# Known and accepted: an empty commit has no patch-id, so git cherry calls it a copy of any empty commit on
# trunk -- it carries nothing to lose. A squash-merged branch keeps its own commits (no copies) and is left
# alone with them listed: delete it by hand once you have read them.
# T-0196: `git worktree remove` can drop the registration and still leave the directory: "failed to delete '<path>':
# Directory not empty" (builtin/worktree.c goes on to delete_git_dir whatever delete_git_work_tree returned). On this NFS
# mount it hit 34 clean worktrees on 2026-10-13: git unlinks entries while it reads a directory, the readdir continuation
# after the first deletions skipped one run of entries (the same 17 tickets/*.md in every dir), rmdir failed, and the dirs
# stayed behind, registered nowhere, while prune said "failed, left alone". Now what git leaves is judged file by file
# (unique_files): a file whose content is a repo object, or an ignored file the verdict just approved, is nothing to keep,
# and prune removes the directory itself (rm -rf lists a directory in full before it deletes; a second pass catches a skip);
# anything else keeps the directory, named, and the message says what really happened: a dir that cannot be listed in full,
# a .git of its own (a clone) or one pointing at a live gitdir, a path that resolves outside <repo>/WORKTREES (the only
# place prune deletes in). `prune --orphans` applies the same judgement to every unregistered dir under WORKTREES of every
# repo (the dirs of earlier failures).
PRUNE_PLAN=""; PRUNED=""; PRUNE_FILES=""; FETCHED_TRUNKS=""; ORPHAN_PLAN=""; ORPHANS_REMOVED=0; LEFTOVER_NOTE=""
VALUABLE_IGNORED_RE_CORE='\.(backup|bak|orig|pem|key|p12|env|sqlite3?|db)(\.[^/]*)?/?$|(^|/)\.env(\.|/|$)|secret|credential|(^|/)backups?(/|$)'
VALUABLE_IGNORED_RE=""   # the core's pattern | the plugin's (plugin_ignored_valuable_re: tfstate/tfvars/tfplan here); set by load_plugin
is_main_checkout() { # path -> 0 when it is a repository's MAIN working tree (its git dir is the .git itself, not .git/worktrees/<name>)
  local d; d=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  case $d in */.git/worktrees/*) return 1;; *) return 0;; esac
}
worktree_entry() { # repo path -> that worktree's block of `git worktree list --porcelain` (without the path line), or nothing
  local want block="" line path=""
  want=$(readlink -f "$2" 2>/dev/null || echo "$2")
  while IFS= read -r line; do
    case $line in
      "worktree "*) path=${line#worktree }; block="" ;;
      "") [ "$(readlink -f "$path" 2>/dev/null || echo "$path")" = "$want" ] && { printf '%s' "$block"; return 0; } ;;
      *) block="${block:+$block
}$line" ;;
    esac
  done < <(git -C "$1" worktree list --porcelain 2>/dev/null; echo)
  return 0
}
other_open_tickets_naming() { # id branch worktree-basename -> ids of other, not done/dropped tickets whose branch:/worktree: name them exactly
  local id=$1 br=$2 wtb=$3
  awk -v br="$br" -v wtb="$wtb" '
    function names(line, want, base,   v, n, a, i, x) {  # exact token match: a plain value or a {<repo>: x, <repo>: y} map entry
      v = line; sub(/^[a-z]+:[ ]*/, "", v); sub(/[ ]*#.*$/, "", v); n = split(v, a, /[ ,{}]+/)
      for (i = 1; i <= n; i++) { x = a[i]; if (base) sub(/.*\//, "", x); if (x != "" && x == want) return 1 }
      return 0 }
    FNR==1 { if (fn) flush(); fn=FILENAME; st=""; hit=0 }
    /^status:/ { st=$2 }
    /^branch:/ && br!="" && names($0, br, 0) { hit=1 }
    /^worktree:/ && wtb!="" && names($0, wtb, 1) { hit=1 }
    function flush() { if (hit && st!="done" && st!="dropped") { n=split(fn, p, "/"); print substr(p[n], 1, 6) } }
    END { if (fn) flush() }' "$TD"/T-*.md | grep -v "^$id\$" | tr '\n' ' ' || true
}
worktree_verdict() { # repo wpath branch id label -> 0 when the worktree may go; else prints why and returns 1
  local repo=$1 wpath=$2 br=$3 id=$4 lbl=$5 entry val list
  entry=$(worktree_entry "$repo" "$wpath")
  [ -n "$entry" ] || { echo "prune: $id ($lbl): $wpath is not a registered worktree of $repo -- left alone (not git's to remove; look at it, then delete it by hand or run: scripts/ticket.sh prune --orphans)"; return 1; }
  if grep -q '^detached' <<<"$entry"; then echo "prune: $id ($lbl): worktree $wpath is on a detached HEAD (mid-rebase, or loose commits) -- left alone"; return 1; fi
  if ! grep -qx "branch refs/heads/$br" <<<"$entry"; then echo "prune: $id ($lbl): worktree $wpath is on $(sed -n 's|^branch refs/heads/||p' <<<"$entry"), not on $br -- left alone"; return 1; fi
  [ -z "$(git -C "$wpath" status --porcelain 2>/dev/null)" ] || { echo "prune: $id ($lbl): worktree $wpath is dirty -- left alone"; return 1; }
  # ignored FILES one by one (ls-files -oi walks into ignored directories such as terraform.tfstate.d/; status --ignored would collapse them)
  val=$(git -C "$wpath" ls-files -oi --exclude-standard 2>/dev/null | grep -E "$VALUABLE_IGNORED_RE" || true)
  if [ -n "$val" ]; then
    echo "prune: $id ($lbl): worktree $wpath holds ignored files that may matter -- left alone (move or delete them by hand):"
    list=$(sed 's/^/    /' <<<"$val"); head -n 20 <<<"$list"; return 1
  fi
  return 0
}
# ---- what `git worktree remove` may leave behind (T-0196) ------------------------------------------------
unique_files() { # repo dir [approved] -> one line per entry of dir (path relative to dir) that would be lost with it: a regular
  # file or symlink whose blob is not in repo's object store and which the approved list (ignored files already allowed to
  # go, one path per line) does not name; a file that cannot be hashed; anything that is neither (fifo, socket, device);
  # a top-level .git that is a repository of its own or points at a live gitdir; and the whole dir when it cannot be
  # listed in full. Empty output = everything in dir has a copy in the repo. Hashes are of the raw bytes (--no-filters):
  # a file autocrlf or a clean filter would map onto a known blob is not that blob.
  local repo=$1 dir=$2 approved=${3-} listing="" files="" links="" others="" shas="" missing="" l tgt sha gitdir
  [ -d "$dir" ] || return 0
  {
    # the top-level .git: git's stale pointer file (gitdir: <path> that no longer exists) is nothing to keep; a .git DIRECTORY
    # is a repository of its own (a clone left here, history and all) and a pointer at a live gitdir is somebody's worktree
    if [ -d "$dir/.git" ] && [ ! -L "$dir/.git" ]; then echo ".git (a repository of its own -- a clone, not a worktree)"
    elif [ -e "$dir/.git" ] || [ -L "$dir/.git" ]; then
      gitdir=$(sed -n '1s/^gitdir: //p' "$dir/.git" 2>/dev/null || true)
      if [ -z "$gitdir" ]; then echo ".git (not a gitdir pointer)"
      else
        case $gitdir in /*) ;; *) gitdir="$dir/$gitdir";; esac
        [ ! -e "$gitdir" ] || echo ".git (points at a live gitdir: $gitdir)"
      fi
    fi
    # one walk, type and path per entry. A walk that fails (an unreadable subdirectory: mode 000, a root-owned volume) keeps
    # the dir: nothing is deleted on a partial list.
    if ! listing=$(cd "$dir" && find . -mindepth 1 -path ./.git -prune -o -printf '%y %P\n' 2>/dev/null); then
      echo ". (could not be listed in full -- an unreadable subdirectory?)"
    else
      files=$(sed -n 's/^f //p' <<<"$listing" | LC_ALL=C sort); links=$(sed -n 's/^l //p' <<<"$listing" | LC_ALL=C sort)
      others=$(grep -v '^[fld] ' <<<"$listing" | sed 's/^. //' | LC_ALL=C sort || true)
      if [ -n "$files" ]; then
        if ! shas=$(sed "s|^|$dir/|" <<<"$files" | git -C "$repo" hash-object --no-filters --stdin-paths 2>/dev/null) \
           || [ "$(wc -l <<<"$shas")" != "$(wc -l <<<"$files")" ]; then
          sed 's/$/ (could not be hashed)/' <<<"$files"   # unreadable: nothing is deleted on a guess
        else
          missing=$(git -C "$repo" cat-file --batch-check <<<"$shas" | grep -n ' missing$' | cut -d: -f1 || true)   # line numbers = positions in $files
          for l in $missing; do sed -n "${l}p" <<<"$files"; done
        fi
      fi
      while IFS= read -r l; do   # a symlink's blob is its target path
        [ -n "$l" ] || continue
        if ! tgt=$(readlink "$dir/$l"); then echo "$l (unreadable symlink)"; continue; fi
        sha=$(printf '%s' "$tgt" | git -C "$repo" hash-object --stdin) || { echo "$l (could not be hashed)"; continue; }
        git -C "$repo" cat-file -e "$sha" 2>/dev/null || echo "$l"
      done <<<"$links"
      [ -z "$others" ] || sed 's/$/ (not a regular file)/' <<<"$others"
    fi
  } | { if [ -n "$approved" ]; then grep -vxF -f <(printf '%s\n' "$approved") || true; else cat; fi; }
}
managed_path() { # repo path -> the resolved path when it lies strictly under <repo>/$WORKTREES, and that dir itself (resolved: it may
  # be a symlink) lies strictly inside the resolved repo and outside its .git -- the only place prune deletes in; nothing otherwise
  local repo_real root real
  repo_real=$(readlink -f "$1" 2>/dev/null) || return 0; [ -n "$repo_real" ] || return 0
  root=$(readlink -f "$1/$WORKTREES" 2>/dev/null) || return 0; [ -n "$root" ] || return 0
  case $root in "$repo_real"/.git|"$repo_real"/.git/*) return 0;; "$repo_real"/?*) ;; *) return 0;; esac
  real=$(readlink -f "$2" 2>/dev/null) || return 0
  case $real in "$root"/?*) echo "$real";; esac
}
count_entries() { local n; n=$(cd "$1" 2>/dev/null && find . -mindepth 1 -path ./.git -prune -o ! -type d -print 2>/dev/null | wc -l | tr -d ' ') || n="?"; echo "${n:-?}"; }   # dir -> files, links and other non-dirs in it (the top-level .git file aside)
finish_worktree_removal() { # repo wpath id label git-error approved -> after a failed `git worktree remove`: 0 = the worktree is gone
  # (LEFTOVER_NOTE says what git left and that prune removed it); 1 = git refused and the worktree is still registered (left
  # alone, as before); 2 = git dropped the registration but the directory stays, named (something in it has no copy in the
  # repo, or rm failed) -- the branch may still go. Every message says what really happened.
  local repo=$1 wpath=$2 id=$3 lbl=$4 err=$5 approved=$6 first uniq n nu real
  LEFTOVER_NOTE=""; first=$(head -n 1 <<<"$err")
  if [ -n "$(worktree_entry "$repo" "$wpath")" ]; then
    echo "prune: $id ($lbl): worktree remove refused, worktree left alone (still registered): $first"; return 1
  fi
  if [ ! -e "$wpath" ] && [ ! -L "$wpath" ]; then LEFTOVER_NOTE=" (git worktree remove said '$first', yet the worktree is gone)"; return 0; fi
  # the ticket's worktree: field is a path somebody wrote: resolve it (a symlink would make rm drop the link and the message
  # lie) and delete only strictly under <repo>/WORKTREES, the one place prune owns
  real=$(managed_path "$repo" "$wpath")
  if [ -z "$real" ]; then
    echo "prune: $id ($lbl): git worktree remove dropped the registration but left $wpath ($first); it resolves to '$(readlink -f "$wpath" 2>/dev/null || echo "?")', outside $repo/$WORKTREES/, the only place prune deletes in -- directory KEPT (look at it, then delete it by hand)"
    LEFTOVER_NOTE=" (worktree dir KEPT: outside $WORKTREES/, not prune's to delete -- '$first')"; return 2
  fi
  n=$(count_entries "$real")
  uniq=$(unique_files "$repo" "$real" "$approved")
  if [ -n "$uniq" ]; then
    nu=$(wc -l <<<"$uniq" | tr -d ' ')
    echo "prune: $id ($lbl): git worktree remove dropped the registration but left $wpath ($first); $nu of $n file(s) in it have no copy in the repo's objects -- directory KEPT (it is no worktree any more: look at these, then delete it by hand or run: scripts/ticket.sh prune --orphans):"
    head -n 20 <<<"$uniq" | sed 's/^/    /'
    LEFTOVER_NOTE=" (worktree dir KEPT: git left $n file(s), $nu with no copy in the repo's objects -- '$first')"; return 2
  fi
  rm -rf -- "$real" 2>/dev/null || true; [ ! -e "$real" ] || rm -rf -- "$real" 2>/dev/null || true   # a second pass for what a readdir skipped
  if [ -e "$real" ]; then
    echo "prune: $id ($lbl): git worktree remove dropped the registration and left $wpath ($first); prune could not remove it either -- look at it by hand"
    LEFTOVER_NOTE=" (worktree dir KEPT: neither git nor rm -rf could remove it -- '$first')"; return 2
  fi
  echo "prune: $id ($lbl): git worktree remove dropped the registration but left $n file(s) in $wpath ($first); every one is a copy of a repo object or an ignored file already approved, so prune removed the directory"
  LEFTOVER_NOTE=" (git left $n file(s) behind -- '$first' -- all copies of repo objects or approved ignored files -- prune removed the directory)"
  return 0
}
orphan_dirs() { # repo -> every dir under <repo>/$WORKTREES that no worktree of repo registers (absolute, one per line); nothing when the list cannot be read
  local repo=$1 d reg real
  [ -d "$repo/$WORKTREES" ] || return 0
  reg=$(git -C "$repo" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | while IFS= read -r d; do readlink -f "$d" 2>/dev/null || echo "$d"; done) || return 0
  for d in "$repo/$WORKTREES"/*/; do
    d=${d%/}; { [ -d "$d" ] && [ ! -L "$d" ]; } || continue
    real=$(readlink -f "$d" 2>/dev/null || echo "$d")
    grep -qxF "$real" <<<"$reg" || echo "$d"
  done
}
orphan_check() { # repo -> the verdict for every orphan dir of repo (printed); a removable one is appended to ORPHAN_PLAN
  local repo=$1 lbl d rel uniq n open
  lbl=$(repo_label "$repo")
  while IFS= read -r d; do
    [ -n "$d" ] || continue; rel=${d#"$repo"/}
    open=$(other_open_tickets_naming "" "" "$(basename "$d")")
    [ -z "$open" ] || { echo "prune: orphan dir $rel ($lbl): not a registered worktree, but open ticket(s) ${open}name it -- left alone"; continue; }
    [ -n "$(managed_path "$repo" "$d")" ] || { echo "prune: orphan dir $rel ($lbl): $repo/$WORKTREES does not resolve to a directory strictly inside the repo and outside its .git, or $rel is not strictly inside it -- left alone (prune deletes nowhere else)"; continue; }
    n=$(count_entries "$d")
    uniq=$(unique_files "$repo" "$d")
    if [ -n "$uniq" ]; then
      echo "prune: orphan dir $rel ($lbl): not a registered worktree; $(wc -l <<<"$uniq" | tr -d ' ') of $n file(s) have no copy in the repo's objects -- left alone (look at these, then delete it by hand):"
      head -n 20 <<<"$uniq" | sed 's/^/    /'; continue
    fi
    ORPHAN_PLAN="$ORPHAN_PLAN$lbl|$repo|$d|$n
"
    echo "prune: orphan dir $rel ($lbl): not a registered worktree, $n file(s), every one a copy of a repo object -- would remove"
  done < <(orphan_dirs "$repo")
}
orphan_apply() { # ORPHAN_PLAN -> the removals, each re-checked under the lock (still unregistered, no open ticket naming it, still nothing unique); a tNNNN-* dir is logged on its ticket through PRUNED
  local lbl repo d n rel uniq open id real
  while IFS='|' read -r lbl repo d n; do
    [ -n "$d" ] || continue; rel=${d#"$repo"/}
    [ -d "$d" ] || { echo "prune: orphan dir $rel ($lbl): gone already"; continue; }
    real=$(managed_path "$repo" "$d"); [ -n "$real" ] || { echo "prune: orphan dir $rel ($lbl): $repo/$WORKTREES does not resolve to a directory strictly inside the repo and outside its .git, or $rel is not strictly inside it -- left alone (prune deletes nowhere else)"; continue; }
    [ -z "$(worktree_entry "$repo" "$d")" ] || { echo "prune: orphan dir $rel ($lbl): is a registered worktree now -- left alone"; continue; }
    open=$(other_open_tickets_naming "" "" "$(basename "$d")")
    [ -z "$open" ] || { echo "prune: orphan dir $rel ($lbl): open ticket(s) ${open}name it now -- left alone"; continue; }
    uniq=$(unique_files "$repo" "$d")
    [ -z "$uniq" ] || { echo "prune: orphan dir $rel ($lbl): changed since the check ($(head -n 1 <<<"$uniq") has no copy in the repo's objects) -- left alone"; continue; }
    rm -rf -- "$real" 2>/dev/null || true; [ ! -e "$real" ] || rm -rf -- "$real" 2>/dev/null || true
    [ ! -e "$real" ] || { echo "prune: orphan dir $rel ($lbl): rm -rf could not remove it -- look at it by hand"; continue; }
    echo "prune: orphan dir $rel ($lbl): removed ($n file(s), every one a copy of a repo object)"
    ORPHANS_REMOVED=$((ORPHANS_REMOVED + 1))
    id=$(sed -n 's/^t\([0-9][0-9][0-9][0-9]\)-.*/T-\1/p' <<<"$(basename "$d")")
    if [ -n "$id" ] && ls "$TD"/"$id"-*.md >/dev/null 2>&1; then
      PRUNED="$PRUNED$id|($lbl): orphan dir $rel removed ($n file(s), every one a copy of a repo object, left behind by an earlier worktree remove)
"
    fi
  done <<<"$ORPHAN_PLAN"
}
prune_check() { # repo trunk branch worktree id -> prints the verdict; a removable item is appended to PRUNE_PLAN
  local repo=$1 tr=$2 br=$3 wt=$4 id=$5 lbl wpath="" ref base sha uniq eq ch list note="" have_wt="" lsha="" rsha="" why others ign bases
  lbl=$(repo_label "$repo")
  [ -n "$br" ] || return 0
  why=$(branch_problem "$br" "$tr"); [ -z "$why" ] || { echo "prune: $id ($lbl): branch '$br' -- refused: $why"; return 0; }
  [ -d "$repo" ] || { echo "prune: $id ($lbl): repo $repo not found -- left alone"; return 0; }
  case $wt in /*) wpath=$wt;; "") ;; *) wpath="$repo/$wt";; esac
  others=$(other_open_tickets_naming "$id" "$br" "${wpath:+$(basename "$wpath")}")
  [ -z "$others" ] || { echo "prune: $id ($lbl): branch or worktree also named by open ticket(s) $others-- left alone"; return 0; }
  if git -C "$repo" show-ref -q --verify "refs/heads/$br"; then lsha=$(git -C "$repo" rev-parse "refs/heads/$br"); fi
  if fetch_ref "$repo" "$br"; then rsha=$(git -C "$repo" rev-parse "refs/remotes/origin/$br"); fi
  case " $FETCHED_TRUNKS " in *" $repo "*) ;; *) fetch_ref "$repo" "$tr" || true; FETCHED_TRUNKS="$FETCHED_TRUNKS $repo";; esac  # origin's trunk is compared too
  if [ -n "$wpath" ] && [ -e "$wpath" ]; then
    worktree_verdict "$repo" "$wpath" "$br" "$id" "$lbl" || return 0
    have_wt=1
  fi
  [ -n "$lsha$rsha$have_wt" ] || { echo "prune: $id ($lbl): nothing left for $br"; return 0; }
  git -C "$repo" rev-parse -q --verify "$tr^{commit}" >/dev/null 2>&1 \
    || { echo "prune: $id ($lbl): trunk '$tr' is not a branch of $repo (checked out elsewhere and deleted, or the trunk in tickets/ticket.env / TICKET_TRUNK / TICKET_FE_TRUNK wrong) -- left alone"; return 0; }
  bases=$tr  # origin's trunk as well, when it is not the same commit (an unpushed local merge is not on GitHub yet)
  if git -C "$repo" show-ref -q --verify "refs/remotes/origin/$tr" && [ "$(git -C "$repo" rev-parse "refs/remotes/origin/$tr")" != "$(git -C "$repo" rev-parse "$tr")" ]; then bases="$tr refs/remotes/origin/$tr"; fi
  for ref in ${lsha:+refs/heads/$br} ${rsha:+refs/remotes/origin/$br}; do
    eq=0
    for base in $bases; do
      if ! ch=$(git -C "$repo" cherry "$base" "$ref" 2>&1); then echo "prune: $id ($lbl): git cherry $base $ref failed -- left alone: $(head -n 1 <<<"$ch")"; return 0; fi
      # '+' = no patch-equivalent in base, '-' = a rebased copy of it is there
      uniq=$(sed -n 's/^+ //p' <<<"$ch")
      if [ -n "$uniq" ]; then
        echo "prune: $id ($lbl): $ref has $(wc -w <<<"$uniq" | tr -d ' ') commit(s) not in $base -- left alone (no patch-equivalent in $base either):"
        list=$(for sha in $uniq; do git -C "$repo" show -s --format='    %h %s' "$sha"; done); head -n 20 <<<"$list"
        return 0
      fi
      [ "$base" != "$tr" ] || eq=$(grep -c '^-' <<<"$ch" || true)
    done
    [ "$eq" = 0 ] || note="$note (${ref#refs/}: $eq commit(s) already in $tr as rebased copies)"
  done
  if [ -n "$have_wt" ]; then
    ign=$(git -C "$wpath" status --porcelain --ignored 2>/dev/null | sed -n 's/^!! //p' || true)
    [ -z "$ign" ] || note="$note (also deletes $(wc -l <<<"$ign" | tr -d ' ') ignored path(s): $(head -n 3 <<<"$ign" | tr '\n' ' '))"
  fi
  PRUNE_PLAN="$PRUNE_PLAN$id|$lbl|$repo|$tr|$br|${have_wt:+$wpath}|$lsha|$rsha|$note
"
  echo "prune: $id ($lbl): would remove${have_wt:+ worktree $wpath}${lsha:+ branch $br}${rsha:+ origin/$br}$note"
}
prune_apply() { # PRUNE_PLAN -> the removals; PRUNED collects "id|(repo): removed ..." per item. Runs under the lock, after the pull.
  local id lbl repo tr br wpath lsha rsha note did err cur st others approved rc
  while IFS='|' read -r id lbl repo tr br wpath lsha rsha note; do
    [ -n "$id" ] || continue; did=""
    # the window between pass 1 and the lock: the ticket may have been reopened or its branch claimed by another
    # ticket (both on the trunk just pulled), the worktree touched, the branch moved -- re-check all of it
    st=$(field "$(file_of "$id")" status)
    case $st in done|dropped) ;; *) echo "prune: $id ($lbl): is '$st' now (changed since the check) -- left alone"; continue;; esac
    others=$(other_open_tickets_naming "$id" "$br" "${wpath:+$(basename "$wpath")}")
    [ -z "$others" ] || { echo "prune: $id ($lbl): branch or worktree now named by open ticket(s) $others(changed since the check) -- left alone"; continue; }
    cur=$(git -C "$repo" rev-parse -q --verify "refs/heads/$br" 2>/dev/null || true)
    [ "$cur" = "$lsha" ] || { echo "prune: $id ($lbl): $br moved since the check ($(cut -c1-7 <<<"$lsha") -> $(cut -c1-7 <<<"$cur")) -- left alone"; continue; }
    if [ -n "$wpath" ]; then
      worktree_verdict "$repo" "$wpath" "$br" "$id" "$lbl" || { echo "prune: $id ($lbl): the worktree changed since the check (above) -- left alone"; continue; }
      approved=$(git -C "$wpath" ls-files -oi --exclude-standard 2>/dev/null || true)   # the ignored files the verdict just let go: git may leave them too
      if err=$(git -C "$repo" worktree remove "$wpath" 2>&1); then did="worktree ${wpath#"$repo"/}"
      else   # T-0196: git may have dropped the registration and left the directory; finish, keep, or leave alone -- truthfully
        rc=0; finish_worktree_removal "$repo" "$wpath" "$id" "$lbl" "$err" "$approved" || rc=$?
        case $rc in 0) did="worktree ${wpath#"$repo"/}"; note="$note$LEFTOVER_NOTE";; 1) continue;; *) note="$note$LEFTOVER_NOTE";; esac
      fi
    fi
    if [ -n "$lsha" ]; then
      # compare-and-swap: the ref goes only if it still points at the sha pass 1 and the check above saw -- a commit
      # that lands in between (the worktree was still there a moment ago) keeps the branch. Never `branch -D`.
      if git -C "$repo" worktree list --porcelain | grep -qx "branch refs/heads/$br"; then echo "prune: $id ($lbl): $br is checked out in another worktree -- branch kept"
      elif err=$(git -C "$repo" update-ref -d "refs/heads/$br" "$lsha" 2>&1); then did="${did:+$did, }branch $br"
      else echo "prune: $id ($lbl): $br moved since the check -- branch kept: $(head -n 1 <<<"$err")"; fi
    fi
    if [ -n "$rsha" ]; then
      if err=$(git -C "$repo" push --force-with-lease="refs/heads/$br:$rsha" origin ":refs/heads/$br" 2>&1); then did="${did:+$did, }origin/$br"
      else echo "prune: $id ($lbl): delete of origin/$br refused (moved since the check, or protected on origin): $(grep -v '^$' <<<"$err" | tail -n 2 | tr '\n' ' ')"; fi
    fi
    [ -n "$did" ] || { echo "prune: $id ($lbl): nothing removed"; continue; }
    echo "prune: $id ($lbl): removed $did$note"
    PRUNED="$PRUNED$id|($lbl): removed $did$note
"
  done <<<"$PRUNE_PLAN"
}
cmd_prune() {
  local dry="" all="" ids="" orph="" a id f st seen="" entries r any
  for a in "$@"; do
    case $a in --dry-run) dry=1;; --done) all=1;; --orphans) orph=1;; T-[0-9][0-9][0-9][0-9]) ids="$ids $a";; *) die 'usage: prune [T-xxxx ...|--done] [--orphans] [--dry-run]';; esac
  done
  [ -n "$ids$all$orph" ] || die "usage: prune [T-xxxx ...|--done] [--orphans] [--dry-run]   (done/dropped tickets; --orphans: the dirs under $WORKTREES/ that no worktree registers)"
  [ -n "$dry" ] || require_who
  [ -z "$all" ] || ids="$ids $(grep -l '^status: \(done\|dropped\)$' "$TD"/T-*.md 2>/dev/null | xargs -r -n1 basename | cut -c1-6 | tr '\n' ' ')"
  # pass 1, no lock: the verdict for every candidate (fetches are the slow part)
  for id in $ids; do
    case " $seen " in *" $id "*) continue;; esac; seen="$seen $id"
    f=$(file_of "$id"); st=$(field "$f" status)
    case $st in done|dropped) ;; *) echo "prune: $id is '$st' -- only done/dropped tickets are pruned (at done the worktree goes, not before)"; continue;; esac
    ticket_branches "$f"
    case $BRANCH_PROBLEMS in *"cannot merge or prune"*) echo "prune: $id:$BRANCH_PROBLEMS left alone -- fix the ticket's fields first"; continue;; esac
    any=""; for r in $REPOS; do [ -z "${T_BR[$r]-}" ] || any=1; done
    [ -n "$any" ] || continue  # never had a branch
    for r in $REPOS; do [ -z "${T_BR[$r]-}" ] || prune_check "${REPO_PATH[$r]}" "$(repo_trunk "$r")" "${T_BR[$r]}" "${T_WT[$r]-}" "$id"; done
  done
  if [ -n "$orph" ]; then for r in $REPOS; do orphan_check "${REPO_PATH[$r]}"; done; fi   # the dirs an earlier worktree remove left behind (T-0196)
  [ -n "$PRUNE_PLAN$ORPHAN_PLAN" ] || { echo "prune: nothing to remove"; return 0; }
  [ -z "$dry" ] || return 0
  # pass 2, under the lock with trunk synced: the removals and one commit -- seconds, however long pass 1 took
  require_clean_main
  [ -z "$PRUNE_PLAN" ] || prune_apply
  [ -z "$ORPHAN_PLAN" ] || orphan_apply
  [ -n "$PRUNED" ] || { [ "$ORPHANS_REMOVED" -gt 0 ] || echo "prune: nothing removed"; return 0; }
  for id in $(cut -d'|' -f1 <<<"$PRUNED" | sort -u); do
    f=$(file_of "$id"); EDITED="${EDITED:+$EDITED }$f"; PRUNE_FILES="${PRUNE_FILES:+$PRUNE_FILES }$f"
    entries=$(grep "^$id|" <<<"$PRUNED" | cut -d'|' -f2- | tr '\n' ';' | sed 's/;$//; s/;/; /g')
    append_log "$f" "- $(now) $WHO: worktree removed (ticket.sh prune): $id $entries"
  done
  # shellcheck disable=SC2086
  commit_push "tickets: prune (worktrees removed): $(for f in $PRUNE_FILES; do basename "$f" | cut -c1-6; done | tr '\n' ' ' | sed 's/ $//')" $PRUNE_FILES
}

# ---- human: the human's inbox, derived from the tickets (T-0147) --------------------------------
# Read-only, any directory, no lock, no TICKET_AS -- like board. One screen: what the human must do,
# top item first. Sources, in priority order: (1) ready-to-deploy tickets, one item per shared plan (the
# plugin's plan key: plugin_plan_key reads plan: as ONE value -- a *.tfplan path for aws-terraform; a batch
# ticket points plan: at the owner's plan) or per ticket when there is no plan key (frontend deploys, manual
# steps), with the command from the handoff block that names that plan -- any `## Deploy handoff ...`
# heading -- and the plugin's void rule (plugin_plan_void / plugin_command_void_reason: for aws-terraform a
# plan with a newer *.tfplan in its directory is VOID, T-0081): a void plan goes under "do NOT run", never
# as a command; (2) any open ticket whose blocked_on is a question, verbatim; (3) `human:` asks (set by
# `ask`, cleared by `ask T-xxxx ""` or by the next status change), checked against void plans too; (4)
# deployed tickets whose handoff Verify names the human. P0 first. --count prints the number (T-0151's
# status line); --json the items (a JSON array -- its schema is frozen; the version is in board --json).
# ASK_VERBS: the default list at the top of the file, or ticket.env's.
US=$'\x1f'; RSEP=$'\x1e'   # field separator inside an item record; block separator (commands and asks contain '|')
HUMAN_ITEMS=""; HUMAN_VOID=""; HUMAN_LATER=""
sev_rank() { case $1 in P0) echo 0;; P1) echo 1;; P2) echo 2;; P3) echo 3;; *) echo 4;; esac; }
log_stamp() { # file "pattern" -> the "YYYY-MM-DD HH:MM" of the LAST log line matching the pattern (or nothing)
  sed -n '/^## Log/,/^## /p' "$1" | grep -E -- "$2" | tail -n 1 | sed -n 's/^- \([0-9-]* [0-9:]*\) .*/\1/p' || true
}
raw_ask() { # file -> "text<US>who<US>stamp" from the human: line (field() hides the stamp, a comment)
  sed -n 's/^human:[ ]*//p' "$1" | head -n 1 | sed "s/[ ]*# \([^ ]*\) \([0-9-]* [0-9:]*\)[ ]*\$/${US}\1${US}\2/"
}
handoff_blocks() { # file -> every non-empty block under a "## Deploy handoff..." heading (any suffix; the template's commented example dropped), RSEP-separated
  sed '/^<!--/,/^-->/d' "$1" | awk -v rs="$RSEP" '
    function flush() { if (on && body != "") printf "%s%s%s\n", head, body, rs; head=""; body=""; on=0 }
    /^## Deploy handoff/ { flush(); on=1; head=$0 "\n"; next }
    /^## / { flush(); next }
    on { if ($0 !~ /^[ \t]*$/) body=body $0 "\n" }
    END { flush() }'
}
plan_key() { # ticket-file -> the plugin's "<kind>:<plan as written>" (aws-terraform: "tfplan:terraform/x.tfplan"), else "ticket:<id>"
  local k; k=$(plugin_plan_key "$1"); [ -n "$k" ] && echo "$k" || echo "ticket:$(field "$1" id)"
}
apply_lines() { # block -> the human-run command lines of a handoff block (key stripped, trailing notes cut)
  grep -E '^(APPLY|Apply|DEPLOY|Deploy|RUN|Run|STEP [0-9]+)[^:]*:' <<<"$1" | grep -v 'BLOCKER' | sed -E 's/^[^:]*:[ ]*//; s/   .*//; s/[ ]+#.*//' || true
}
pick_block() { # basename-or-empty file... -> the block that names the plan (Apply line > heading > any line; latest wins on ties), or the last block
  local want=$1 blk best="" bestscore=-1 score f; shift
  for f in "$@"; do
    while IFS= read -r -d "$RSEP" blk; do
      blk=${blk#$'\n'}; [ -n "$blk" ] || continue
      if [ -z "$want" ]; then best=$blk; continue; fi
      score=0
      if apply_lines "$blk" | grep -qF -- "$want"; then score=3
      elif head -n 1 <<<"$blk" | grep -qF -- "$want"; then score=2
      elif grep -qF -- "$want" <<<"$blk"; then score=1; fi
      [ "$score" -lt "$bestscore" ] || { [ "$score" = 0 ] && [ -n "$best" ] || { best=$blk; bestscore=$score; }; }
    done < <(handoff_blocks "$f")
  done
  printf '%s' "$best"
}
expand_placeholders() { # command -> with the known <...> expanded (<main>, <main worktree>, <NAME main>, <NAME repo>, <NAME> for the other repos); anything else in <> stays for the caller to refuse
  local cmd=$1 r p
  for r in $REPOS; do
    p=${REPO_PATH[$r]}; cmd=${cmd//<$r main>/$p}; cmd=${cmd//<$r repo>/$p}
    [ "$r" = "$REPO0" ] || cmd=${cmd//<$r>/$p}
  done
  cmd=${cmd//<main worktree>/$MAIN}; cmd=${cmd//<main>/$MAIN}
  printf '%s\n' "$cmd"
}
command_void_reason() { plugin_command_void_reason "$1"; } # command -> the first reason a plan it names must not run (plugin rule), or nothing
batch_for_ask() { # ticket-file ask-text -> "T-a + T-b<US>P1" when the ask stands in for a ready-to-deploy batch (every ticket on the same plan key, its highest severity), else nothing
  # mirrors the replacement rule of source (1): an APPLY/DEPLOY ask on a batch ticket replaces the derived item when it names the plan (T-0151)
  local f=$1 ask=$2 key ids="" best=P9 g sev
  [ "$(field "$f" status)" = ready-to-deploy ] || return 0
  key=$(plan_key "$f")
  case $key in ticket:*) ;; *) grep -qF -- "$(basename "${key#*:}")" <<<"$ask" || return 0;; esac
  for g in $(grep -l '^status: ready-to-deploy$' "$TD"/T-*.md 2>/dev/null | sort); do
    [ "$(plan_key "$g")" = "$key" ] || continue
    ids="${ids:+$ids + }$(field "$g" id)"; sev=$(field "$g" severity)
    [ "$(sev_rank "$sev")" -ge "$(sev_rank "$best")" ] || best=$sev
  done
  printf '%s%s%s' "$ids" "$US" "$best"
}
add_item() { # sort-rank source stamp verb ids prio title command expect nothing note -> HUMAN_ITEMS (one record per line)
  HUMAN_ITEMS="$HUMAN_ITEMS$1$US$2$US$3$US$4$US$5$US$6$US$7$US$8$US$9$US${10}$US${11}
"
}
human_collect() {
  local f id sev title st key keys="" k ids best bestf blk cmd raw expect nothing stamp q rec names ask who verb what rest asked files n why owner steps note
  # (1) ready-to-deploy: one item per shared plan (the plugin's key), or per ticket when it has none
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    key=$(plan_key "$f"); grep -qxF -- "$key" <<<"$keys" || keys="$keys$key
"
  done < <(grep -l '^status: ready-to-deploy$' "$TD"/T-*.md 2>/dev/null | sort)
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    ids=""; best=P9; bestf=""; stamp=""; files=""; asked=""
    while IFS= read -r f; do
      [ -n "$f" ] || continue; [ "$(plan_key "$f")" = "$key" ] || continue
      id=$(field "$f" id); sev=$(field "$f" severity); ids="${ids:+$ids + }$id"; files="$files$f
"
      [ "$(sev_rank "$sev")" -ge "$(sev_rank "$best")" ] || { best=$sev; bestf=$f; }
      [ -n "$stamp" ] || stamp=$(log_stamp "$f" '-> ready-to-deploy')
      ask=$(field "$f" human)
      case $ask in APPLY*|DEPLOY*) case $key in ticket:*) asked=1;; *) grep -qF -- "$(basename "${key#*:}")" <<<"$ask" && asked=1;; esac;; esac
    done < <(grep -l '^status: ready-to-deploy$' "$TD"/T-*.md 2>/dev/null | sort)
    [ -z "$asked" ] || continue   # a curated APPLY/DEPLOY ask naming this plan replaces the derived item (asks are void-checked below)
    title=$(field "$bestf" title); owner=$(field "$bestf" owner); note=""
    case $key in
      ticket:*)
        blk=$(pick_block "" "$bestf"); raw=$(apply_lines "$blk" | head -n 1)
        verb=$(plugin_verb_for "$raw"); verb=${verb:-RUN}
        [ -z "$(field "$bestf" plan)" ] || note="plan: says: $(field "$bestf" plan | cut -c1-120)" ;;
      *)
        why=$(plugin_plan_void "${key#*:}")
        if [ -n "$why" ]; then HUMAN_VOID="$HUMAN_VOID  $ids  $why
"; continue; fi
        # shellcheck disable=SC2086
        blk=$(pick_block "$(basename "${key#*:}")" $(printf '%s' "$files" | tr '\n' ' '))
        raw=$(apply_lines "$blk" | { grep -F -- "$(basename "${key#*:}")" || true; } | head -n 1); verb=APPLY ;;
    esac
    steps=$(apply_lines "$blk" | grep -c . || true); [ "$steps" -le 1 ] || note="${note:+$note; }step 1 of $steps in the handoff: the ticket lists the rest"
    why=$(command_void_reason "$raw")   # the line may name another, older plan too (F1/F2): then it is not a command for the human
    if [ -n "$why" ]; then HUMAN_VOID="$HUMAN_VOID  $ids  the handoff's command names a plan that must not run: $why
"; continue; fi
    if [ -z "$raw" ]; then cmd="(no human-run line -- APPLY/Deploy/STEP n -- in a handoff block of $ids$( [ "${key%%:*}" = ticket ] || echo " naming $(basename "${key#*:}")" ); ask its owner, $owner)"
    else
      cmd=$(expand_placeholders "$raw")
      if grep -qE '<[^>]+>' <<<"$cmd"; then cmd="(not copy-paste: the handoff's command has a placeholder -- $raw -- ask its owner, $owner)"
      else case $cmd in cd\ *) ;; *) cmd="cd $MAIN && $cmd";; esac; fi
    fi
    expect=$(plugin_expect_line "$verb" "$blk"); expect=${expect:-"the handoff's Verify"}
    [ "$steps" -le 1 ] || [ "$verb" = APPLY ] || expect="the handoff's expected output for this step"
    add_item "$(sev_rank "$best")" 1 "${stamp:-?}" "$verb" "$ids" "$best" "$title" "$cmd" "$expect" "the merged fix stays unreleased ($ids)" "$note"
  done <<<"$keys"
  # (2) a question in blocked_on, on any open ticket (not only blocked ones)
  for f in $(grep -l '^blocked_on:[ ]*[^ #]' "$TD"/T-*.md 2>/dev/null | sort); do
    q=$(field "$f" blocked_on); [ -n "$q" ] || continue
    [[ $q == T-[0-9][0-9][0-9][0-9]* ]] && continue
    st=$(field "$f" status); case $st in done|dropped|skipped) continue;; esac   # skipped (T-0157): kept, never asked
    id=$(field "$f" id); sev=$(field "$f" severity); title=$(field "$f" title)
    rec=$(sed -n '/^## Investigation/,/^## /p' "$f" | sed -n 's/^Recommendation:[ ]*//p' | head -n 1)
    names=$(grep -l "^depends_on:.*$id" "$TD"/T-*.md 2>/dev/null | xargs -r -n1 basename | cut -c1-6 | grep -v "^$id\$" | tr '\n' ' ' || true)
    if [ "$sev" = P3 ]; then HUMAN_LATER="$HUMAN_LATER$id "; continue; fi
    add_item "$(sev_rank "$sev")" 2 "$(log_stamp "$f" "-> $st")" DECIDE "$id" "$sev" "$title" \
      "question: $q" "your answer, one line, in the tickets window: \"$id <answer>\"${rec:+   -- desk recommends: $rec}" "$id stays $st${names:+; $names wait on it}" "$( [ "$st" = blocked ] || echo "ticket is $st, the question is in its blocked_on:")"
  done
  # (3) asks: a human: line on any open ticket -- a command naming a void or missing plan is never printed as a command
  for f in $(grep -l '^human:[ ]*[^ #]' "$TD"/T-*.md 2>/dev/null | sort); do
    st=$(field "$f" status); case $st in done|dropped|skipped) continue;; esac   # skipped (T-0157): kept, never asked
    ask=$(raw_ask "$f"); IFS=$US read -r what who stamp <<<"$ask"
    verb=${what%% *}; case " $ASK_VERBS " in *" $verb "*) what=${what#* };; *) verb=RUN;; esac
    id=$(field "$f" id); sev=$(field "$f" severity); ids=$id
    case $verb in APPLY|DEPLOY)   # standing in for a batch: the batch's ids and its highest severity, as the derived item shows them
      rec=$(batch_for_ask "$f" "$(field "$f" human)"); [ -z "$rec" ] || { ids=${rec%%$US*}; sev=${rec##*$US}; } ;;
    esac
    IFS='|' read -r what cmd expect nothing <<<"$what"
    cmd=$(sed 's/^ *//; s/ *$//' <<<"${cmd:-}"); expect=$(sed 's/^ *//; s/ *$//' <<<"${expect:-}"); nothing=$(sed 's/^ *//; s/ *$//' <<<"${nothing:-}")
    why=$(command_void_reason "$cmd")
    if [ -n "$why" ]; then HUMAN_VOID="$HUMAN_VOID  $id  the ask by ${who:-?} names a plan that must not run: $why (ask $id \"\" clears it)
"; continue; fi
    why=$(plugin_ask_check "$verb" "$cmd" inbox)
    if grep -qE '<[^>]+>' <<<"$cmd"; then cmd="(not copy-paste: the ask has a placeholder -- $cmd -- ask ${who:-its author})"
    elif [ -n "$why" ]; then cmd="(not copy-paste: $why -- $cmd -- ask ${who:-its author})"
    else case $cmd in scripts/*|./*|python3\ *|bash\ *|make\ *|[A-Z_]*=*) cmd="cd $MAIN && $cmd";; esac; fi   # a repo-relative command gets the cd; a bare tool name is the ask's own business (terraform dropped, T-0192: scripts/* covers tf.sh)
    add_item "$(sev_rank "$sev")" 3 "${stamp:-?}" "$verb" "$ids" "$sev" "$(sed 's/^ *//; s/ *$//' <<<"$what")" "${cmd:-reply in the tickets window: \"$id ok\"}" "${expect:-(the ask names no expected output)}" "${nothing:-$ids waits}" "asked by ${who:-?}"
  done
  # (4) deployed, with a Verify line that names the human and no done yet
  for f in $(grep -l '^status: deployed$' "$TD"/T-*.md 2>/dev/null | sort); do
    [ -z "$(field "$f" human)" ] || continue   # the ask already covers it
    blk=$(pick_block "" "$f"); [ -n "$blk" ] || continue
    q=$(grep -m1 -iE '^Verify.*(\(human\)|the human|by hand|browser)' <<<"$blk") || true
    [ -n "$q" ] || continue
    id=$(field "$f" id); sev=$(field "$f" severity); title=$(field "$f" title)
    add_item "$(sev_rank "$sev")" 4 "$(log_stamp "$f" '-> deployed')" CHECK "$id" "$sev" "$title" \
      "$(sed 's/^Verify[^:]*:[ ]*//' <<<"$q" | cut -c1-200)" "the real output named in the ticket's Verify lines" "$id stays at deployed, unverified" ""
  done
}
# T-0217 (1.2.0), T-0218 (1.2.1): a ticket's `## In plain words` section (T-0215: right after the front matter, 2-4
# sentences the human can understand) reaches tools as ONE line. The rule, in PLAIN_AWK and nowhere else: the FIRST
# `## In plain words` heading before `## Log` opens the section, the next `## ` heading closes it (plain_feed, one body
# line at a time); HTML comments are stripped whatever they hold, a line that is only a `<placeholder>` is dropped, the
# rest is joined by one space with the whitespace squeezed, capped at 1200 bytes on a UTF-8 boundary (plain_line); ""
# when there is nothing. Shared by the -> ready gate (cmd_move), `board --json` (per ticket, inside board_json_tickets)
# and `human --json` (per item, item_plain). Every awk that runs it runs under LC_ALL=C (T-0220): gawk in a UTF-8 locale
# counts characters in length()/substr() and writes %c above 127 as two bytes, so the cap and the UTF-8 trim were wrong
# there; in the C locale mawk, gawk, nawk and busybox all count bytes, and bytes above 127 pass through untouched.
PLAIN_AWK='function plain_reset() { pw = ""; pon = 0; pseen = 0; pdone = 0 }
function plain_feed(l) {
  if (pdone) return
  if (l ~ /^## Log[ \t]*$/) { pdone = 1; pon = 0; return }
  if (l ~ /^## /) { pon = 0; if (!pseen && l ~ /^## In plain words[ \t]*$/) { pon = 1; pseen = 1 }; return }
  if (pon) pw = pw l "\n" }
function plain_line(s,   n, i, j, l, L, out, rest) {
  while ((i = index(s, "<!--")) > 0) { rest = substr(s, i + 4); j = index(rest, "-->"); s = substr(s, 1, i - 1) (j ? substr(rest, j + 3) : "") }
  n = split(s, L, "\n"); out = ""
  for (i = 1; i <= n; i++) { l = L[i]; gsub(/^[ \t]+|[ \t]+$/, "", l); if (l == "" || l ~ /^<[^>]*>$/) continue; out = out (out == "" ? "" : " ") l }
  gsub(/[ \t]+/, " ", out)
  if (length(out) > 1200) out = utf8_whole(substr(out, 1, 1200))
  return out }
function utf8_whole(s,   n, k, c, need, i) {
  if (!hiinit) { for (i = 128; i < 256; i++) HI[sprintf("%c", i)] = i; hiinit = 1 }
  n = length(s); k = n
  while (k > 0 && (c = HI[substr(s, k, 1)] + 0) >= 128 && c < 192) k--
  if (k == 0) return s
  c = HI[substr(s, k, 1)] + 0; if (c < 192) return s
  need = (c >= 240) ? 4 : (c >= 224) ? 3 : 2
  return (n - k + 1 < need) ? substr(s, 1, k - 1) : s }
'
plain_words() { # file -> the one line (or nothing)
  LC_ALL=C awk "$PLAIN_AWK"'BEGIN { plain_reset() } { plain_feed($0) } END { printf "%s", plain_line(pw) }' "$1"
}
item_plain() { # "T-a + T-b" (an inbox item'"'"'s tickets) -> their plain words on one line; several tickets: each prefixed by its id
  local id f n=0 out="" p
  for id in $1; do case $id in T-[0-9]*) n=$((n + 1));; esac; done
  for id in $1; do
    case $id in T-[0-9]*) ;; *) continue;; esac
    f=$(file_of "$id" 2>/dev/null) || continue; p=$(plain_words "$f"); [ -n "$p" ] || continue
    [ "$n" -le 1 ] || p="$id: $p"
    out="${out:+$out  }$p"
  done
  printf '%s' "$out"
}
# JSON string escaping shared by `human --json` and `board --json` (T-0147/T-0158): \ and " as usual, every control
# character by name (\t \n \r) or as \u00XX -- lossless, and the ops panel parses this, so no byte may break it.
JSON_ESC_AWK='function esc(s,   i, c, n, out) {
  if (!ordinit) { for (i = 1; i < 32; i++) ORD[sprintf("%c", i)] = i; ordinit = 1 }
  gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s)
  if (s !~ /[\001-\037]/) return s
  n = length(s); out = ""
  for (i = 1; i <= n; i++) { c = substr(s, i, 1)
    if (c in ORD) out = out (c == "\t" ? "\\t" : c == "\n" ? "\\n" : c == "\r" ? "\\r" : sprintf("\\u%04x", ORD[c])); else out = out c }
  return out
}
'
human_json_str() { LC_ALL=C awk "$JSON_ESC_AWK"'{ printf "%s", esc($0) }' <<<"$1"; }   # lines are joined (an item is one line); bytes, whatever the awk (T-0220)
human_wrap() { # indent text -> the text verbatim, wrapped at 120 columns, continuation lines indented
  fold -s -w 120 <<<"$2" | sed "1s/^/$1/; 2,\$s/^/$1   /"
}
# No sittings (T-0167): the inbox is continuous -- an ask is listed the moment it is made, by priority, then kind, then
# age. T-0155's next-sitting header, its freeze window, the --next-sitting mode and the sittings env override were
# withdrawn on the user's word (2026-10-13).
cmd_human() {
  local mode=${1:-} n=0 rank src stamp verb ids prio title cmd expect nothing note line counts st first=1
  case $mode in ""|--count|--json) ;; *) die 'usage: human [--count|--json]   (read-only: the human'"'"'s list, derived from the tickets)';; esac
  human_collect
  n=$(printf '%s' "$HUMAN_ITEMS" | grep -c . || true)
  if [ "$mode" = --count ]; then echo "$n"; return 0; fi
  if [ "$mode" = --json ]; then
    printf '['
    while IFS=$US read -r rank src stamp verb ids prio title cmd expect nothing note; do
      [ -n "$verb" ] || continue
      [ $first = 1 ] || printf ','; first=0
      # id (T-0158): stable across re-renders, changes when the ask text changes -- what a click on a page binds to
      printf '{"id":"%s","verb":"%s","tickets":"%s","priority":"%s","title":"%s","plain":"%s","since":"%s","command":"%s","expect":"%s","if_nothing":"%s","note":"%s"}' \
        "$(printf '%s' "$verb|$ids|$cmd|$expect" | sha1sum | cut -c1-10)" \
        "$verb" "$ids" "$prio" "$(human_json_str "$title")" "$(human_json_str "$(item_plain "$ids")")" "$stamp" "$(human_json_str "$cmd")" "$(human_json_str "$expect")" "$(human_json_str "$nothing")" "$(human_json_str "$note")"
    done < <(printf '%s' "$HUMAN_ITEMS" | sort -t"$US" -k1,1n -k2,2n -k3,3)
    printf ']\n'; return 0
  fi
  echo "HUMAN INBOX  $(now)   trunk $TRUNK @ $(g rev-parse --short HEAD 2>/dev/null)   (scripts/ticket.sh human; read-only)"
  if [ "$n" = 0 ]; then echo "Nothing is waiting for you."
  else echo "$n item(s). Do them top to bottom. Every command is copy-paste; after each one type the ticket id and \"ok\" (or \"<ticket> <answer>\"; paste only the LAST line, and only if it differs from expect)."; fi
  echo
  n=0
  while IFS=$US read -r rank src stamp verb ids prio title cmd expect nothing note; do
    [ -n "$verb" ] || continue; n=$((n + 1))
    printf '%-2d %-7s %-7s %-3s %-58.58s  since %s%s\n' "$n" "$verb" "$ids" "$prio" "$title" "${stamp:-?}" "$( [ -z "$stamp" ] || [ "$stamp" = "?" ] || echo " ($(age "$stamp"))")"
    case $cmd in "question: "*) human_wrap "   " "$cmd";; *) echo "   $cmd";; esac   # a question is wrapped; a command never
    echo "   expect: $expect"
    echo "   if you do nothing: $nothing${note:+   [$note]}"
  done < <(printf '%s' "$HUMAN_ITEMS" | sort -t"$US" -k1,1n -k2,2n -k3,3)
  [ -z "$HUMAN_VOID" ] || { echo; echo "do NOT run (void or missing plans; nothing for you until the owner re-plans):"; printf '%s' "$HUMAN_VOID"; }
  [ -z "$HUMAN_LATER" ] || { echo; echo "Blocked on you but not urgent (P3): $HUMAN_LATER-- scripts/ticket.sh board shows their questions."; }
  counts=""
  for st in in-dev in-review ready-to-merge merged ready-to-deploy deployed; do counts="$counts $(grep -l "^status: $st\$" "$TD"/T-*.md 2>/dev/null | wc -l || true) $st,"; done
  echo; echo "Nothing else is waiting for you.${counts% ,} $(grep -l '^status: done$' "$TD"/T-*.md 2>/dev/null | wc -l || true) done (deployed = sessions are verifying)."
}
cmd_ask() { # T-xxxx "<VERB> <what> | <command or how to reply> | <expected last line> | <if you do nothing>"   ("" clears)
  [ $# -eq 2 ] || die 'usage: ask T-xxxx "<VERB> <what> | <command> | <expected> | <if you do nothing>"   or   ask T-xxxx ""  (clear)'
  local id=$1 text=$2 f verb old replaced="" why
  require_who; require_clean_main; f=$(file_of "$id"); EDITED=$f
  [ -z "$text" ] || [ "$(field "$f" status)" != skipped ] || die "$id is skipped: set aside on the user's word, nothing is asked for it (T-0157). Move it back first: move $id backlog|ready"
  if [ -z "$text" ]; then
    [ -n "$(field "$f" human)" ] || die "$id has no pending ask"
    set_field "$f" human ""; append_log "$f" "- $(now) $WHO: ask cleared."
    commit_push "tickets: $id ask cleared" "$f"; return 0
  fi
  verb=${text%% *}
  case " $ASK_VERBS " in *" $verb "*) ;; *) die "an ask starts with a verb -- one of: $ASK_VERBS -- then: <what> | <command or how to reply> | <expected last line> | <if you do nothing>";; esac
  [[ $text == *"|"* ]] || die 'an ask needs at least "<VERB> <what> | <command or how to reply>"; add "| <expected> | <if you do nothing>" so the human knows when it worked and what happens otherwise'
  [ "$(tr -cd '|' <<<"$text" | wc -c)" -le 3 ] || die "an ask has at most four fields: <what> | <command> | <expected> | <if you do nothing>. A '|' inside the command is not supported (it would split the fields): put the pipeline in a script, or in the ticket, and point at it"
  [[ $text != *"#"* ]] || die "no '#' in an ask (the front matter treats it as a comment)"
  [[ $text != *[[:cntrl:]]* ]] || die "an ask is one line of printable text (no newline, tab or other control character: the inbox record and its JSON would break)"
  ! grep -qE '<[^>]+>' <<<"$text" || die "no <placeholders> in an ask: the human copies the command as written -- fill in the real value"
  why=$(plugin_ask_check "$verb" "$text" ask); [ -z "$why" ] || die "$why"
  old=$(field "$f" human)
  [ -z "$old" ] || { echo "note: this replaces the pending ask: $old"; replaced=" (replaced: $old)"; }
  set_field "$f" human "$text   # $WHO $(now)"
  append_log "$f" "- $(now) $WHO: ask (human:): $text$replaced"
  commit_push "tickets: $id ask" "$f"
  echo "asked: it is in scripts/ticket.sh human until the next status change of $id, or 'ask $id \"\"', clears it"
}

# ---- apply: the HUMAN's apply (T-0149): deploy-check, then the saved plan, everything logged ------
# Sessions never apply (CLAUDE.md rule 4). This refuses whenever the shell is not the human's terminal:
# TICKET_AS set (a session's signature), CLAUDECODE / CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_ENTRYPOINT set
# (Claude Code sets them in every tool call, and a session's per-call TICKET_AS= prefix does not clear
# them), or stdin/stdout not a terminal (a tool call) -- the tty test runs on the real fds, never inside
# a command substitution, where stdout is a pipe. It runs deploy-check with the same
# arguments and stops on a FAIL; applies the saved plan (the plugin: for aws-terraform through
# scripts/tf.sh prod apply (T-0119, workspace default) when tf.sh is on trunk, else `TF_WORKSPACE=default
# terraform -chdir=terraform apply -input=false -no-color <plan>` with TF_CLI_ARGS*/TF_DATA_DIR dropped
# like tf.sh does); tees every line to terraform/<plan>.apply.log (gitignored) through a tee that ignores
# Ctrl-C, so an interrupt reaches terraform -- which shuts down gracefully and persists state -- while the pipe stays
# open; and on success writes one log line per ticket of the batch (state serial before -> after, the
# "Apply complete!" line of THIS run, the log path), signed `human`, in one commit. The lock is taken
# only for those lines, after the apply, and a failure there never turns a successful apply into a
# non-zero exit: it says "APPLY SUCCEEDED; ticket line NOT written" and the desk logs it by hand.
APPLY_TTY=""   # set by cmd_apply BEFORE any command substitution: inside $(...) stdout is a pipe and `[ -t 1 ]` is always false
apply_not_human() { # -> NOT_HUMAN_WHY = why this shell is not the human's terminal, or empty (sets a variable: no substitution involved)
  NOT_HUMAN_WHY=""
  if [ -n "$WHO" ]; then NOT_HUMAN_WHY="TICKET_AS is set ($WHO): a session's signature. A human who exported it for a test runs: env -u TICKET_AS scripts/ticket.sh apply ..."
  elif [ -n "${CLAUDECODE:-}${CLAUDE_CODE_SESSION_ID:-}${CLAUDE_CODE_ENTRYPOINT:-}" ] || [[ ${AI_AGENT:-} == claude-code* ]]; then
    NOT_HUMAN_WHY="CLAUDECODE / CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_ENTRYPOINT is set: this shell is a Claude Code session's tool call. A human in a tmux window that a session started (it inherits them) runs: env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRYPOINT scripts/ticket.sh apply ..."
  elif [ -z "$APPLY_TTY" ] && [ -z "${TICKET_APPLY_NO_TTY_OK:-}" ]; then NOT_HUMAN_WHY="stdin/stdout is not a terminal: a tool call or a pipe, not the human's shell -- run it from your own terminal"
  fi
}
batch_ticket_ids() { # plan-basename -> the ids whose plan: first word IS that plan (exact, like deploy_check.batch_tickets), space-separated
  local f p first
  for f in "$TD"/T-*.md; do
    p=$(field "$f" plan); first=${p%% *}; [ -n "$first" ] || continue
    if [ "$first" = "$1" ] || [[ $first == */$1 ]]; then basename "$f" | cut -c1-6; fi
  done | tr '\n' ' '
}
APPLY_PLAN=""; APPLY_LOG=""; APPLY_SUMMARY=""; APPLY_SHORT=""   # set by the plugin: the plan's basename, the log path, "state serial a -> b; Apply complete! ...; deploy-check 0 FAIL", "serial a -> b"
cmd_apply() {
  local tickets="" at="" id f out rc=0 rest=()
  APPLY_TTY=""; ! { [ -t 0 ] && [ -t 1 ]; } || APPLY_TTY=1   # tested here, on the real fds -- never inside $(...)
  apply_not_human; [ -z "$NOT_HUMAN_WHY" ] || die "apply is the human's command -- $NOT_HUMAN_WHY. Sessions never apply (CLAUDE.md rule 4): write the Deploy handoff and hand the command to the desk."
  trap 'exit 130' INT   # a Ctrl-C outside the apply itself still runs the EXIT trap (restore_edit)
  while [ $# -gt 0 ]; do
    case $1 in
      --tickets) [ -n "${2:-}" ] || die "$(plugin_apply_usage)"; tickets=$(tr ',' ' ' <<<"$2"); shift 2 ;;
      --tickets=*) tickets=$(tr ',' ' ' <<<"${1#*=}"); shift ;;
      --at) [ -n "${2:-}" ] || die "$(plugin_apply_usage)"; at=$2; shift 2 ;;
      --at=*) at=${1#*=}; shift ;;
      *) rest+=("$1"); shift ;;   # the plan and the plugin's own options: plugin_apply_prepare parses and refuses them
    esac
  done
  plugin_apply_prepare ${rest[@]+"${rest[@]}"}
  [ -n "$APPLY_PLAN" ] && [ -n "$APPLY_LOG" ] || die "plugin $DEPLOY_PLUGIN: plugin_apply_prepare named no plan or no log"
  for id in $tickets; do [[ $id == T-[0-9][0-9][0-9][0-9] ]] || die "--tickets: '$id' is not a T-xxxx id"; file_of "$id" >/dev/null; done
  [ -n "$tickets" ] || tickets=$(batch_ticket_ids "$APPLY_PLAN")
  { echo; echo "=== ticket.sh apply $APPLY_PLAN  $(now)  by ${USER:-?}  tickets: ${tickets:-none}  at: ${at:-?}"; } >>"$APPLY_LOG"
  echo "ticket.sh apply: $APPLY_PLAN  (log: ${APPLY_LOG#"$MAIN"/}; the desk reads it -- paste nothing)"
  # 1 + 2. the plugin: its pre-apply check (a FAIL dies: nothing applied), then the saved plan, every line logged
  set +e; plugin_apply "$tickets" "$at"; rc=$?; set -e
  [ "$rc" = 0 ] || exit "$rc"
  # 3. one log line per ticket of the batch, signed human, one commit -- the lock only now, and never a
  #    non-zero exit for the apply because of it
  [ -n "$tickets" ] || { echo "ticket.sh apply: no ticket points plan: at $APPLY_PLAN and no --tickets given: nothing logged; the desk logs it by hand"; return 0; }
  out=$(mktemp)
  if ( trap restore_edit EXIT; WHO=human; require_clean_main
       for id in $tickets; do
         f=$(file_of "$id"); EDITED="${EDITED:+$EDITED }$f"
         append_log "$f" "- $(now) human: applied by the human (ticket.sh apply): $APPLY_PLAN; $APPLY_SUMMARY; log ${APPLY_LOG#"$MAIN"/}"
       done
       # shellcheck disable=SC2086
       commit_push "tickets: applied $APPLY_PLAN ($(tr ' ' ',' <<<"$tickets" | sed 's/,$//'))" $EDITED ) >"$out" 2>&1; then
    cat "$out"; echo "logged on: $tickets"
  else
    cat "$out"
    echo "ticket.sh apply: APPLY SUCCEEDED; ticket line NOT written: $(grep -v '^$' "$out" | tail -n 1 | cut -c1-200) -- tell the desk; the apply log is ${APPLY_LOG#"$MAIN"/} ($APPLY_SHORT)"
  fi
  rm -f "$out"
  echo "type: $(for id in $tickets; do printf '%s ok   ' "$id"; done)-- the desk moves the ticket(s) from the log"
  return 0
}

# ---- board (read-only: never commits, works from any worktree, dirty or not) --------------
age() { # "YYYY-MM-DD HH:MM" -> "Nd"/"Nh"
  local s; s=$(date -d "$1" +%s 2>/dev/null) || { echo "?"; return; }
  s=$(( ($(date +%s) - s) / 3600 )); [ $s -ge 48 ] && echo "$((s / 24))d" || echo "${s}h"
}

branch_stats() { # repo trunk branch worktree -> "behind N  ahead N  dirty N"
  local repo=$1 tr=$2 br=$3 wt=$4 out="" d
  if git -C "$repo" show-ref -q --verify "refs/heads/$br"; then
    out="behind $(git -C "$repo" rev-list --count "$br..$tr")  ahead $(git -C "$repo" rev-list --count "$tr..$br")"
  else out="branch '$br' missing!"; fi
  case $wt in /*) ;; *) wt="$repo/$wt";; esac
  if [ -d "$wt" ]; then d=$(git -C "$wt" status --porcelain | wc -l); out="$out  dirty $d$([ "$d" -gt 0 ] && echo ' !')"
  elif [ -n "$4" ]; then out="$out  worktree missing!"; fi
  echo "$out"
}

mapget() { echo "$1" | tr -d '{}' | tr ',' '\n' | sed -n "s/^[ ]*$2:[ ]*//p" | sed 's/[ ]*$//'; }

orphans() { # -> "<branch>\t<commits not in trunk>\t<behind>" for every local branch with commits outside trunk that no ticket names
  local named ref n
  named=$(cat "$TD"/T-*.md 2>/dev/null | sed -n 's/^branch:[ ]*//p' | tr '{},:\n' '     ')
  for ref in $(g for-each-ref --format='%(refname:short)' refs/heads); do
    [ "$ref" = "$TRUNK" ] && continue
    n=$(g rev-list --count "$TRUNK..$ref")
    [ "$n" -gt 0 ] || continue
    case " $named " in *" $ref "*) continue;; esac
    printf '%s\t%s\t%s\n' "$ref" "$n" "$(g rev-list --count "$ref..$TRUNK")"
  done
}
# ---- board --json (T-0158): the board as one JSON object for tools (the ops panel, the status line) ----------------
# Front matter only, in ONE awk pass over every ticket: no git per branch (29 s on this repo), so a page may call it every
# 30 s. --slow adds what the text board computes with git: behind/ahead/dirty per in-progress ticket and the ORPHANS.
BOARD_JSON_KEYS="id title type severity status repos areas affects deploy owner worktree branch base claimed created depends_on siblings blocked_on human closed_by plan resolution"
BOARD_JSON_LIST_KEYS="repos areas affects depends_on siblings closed_by"   # always arrays; every other key is always a string
board_json_tickets() { # files... -> one line per ticket, US-separated (empty columns survive `read`): id, status, repos, branch, worktree, has ask (1|0), json object
  # The object: every front-matter key (below), then "plain" (T-0217/T-0218: the ## In plain words section as one line
  # by PLAIN_AWK's rule, "" when missing -- the only key read from the body); the caller appends "stats".
  # values follow field(): the first "key:" at column 1 of the front matter, a trailing "#..." comment stripped (so a
  # value with a '#' in it is cut there, as on the text board). Types are per KEY, never per value, so a consumer can rely
  # on them: the six list keys are always arrays ("[a, b]" split on commas; free text = one element; "" = []), every
  # other key is always a string ("" when empty; a map {<repo>: x, <repo>: y} stays the text as written). A file
  # without front matter is skipped.
  LC_ALL=C awk -v keys="$BOARD_JSON_KEYS" -v listkeys="$BOARD_JSON_LIST_KEYS" "$JSON_ESC_AWK$PLAIN_AWK"'
    function jval(k, s,   n, i, parts, out) {
      if (!(k in L)) return "\"" esc(s) "\""
      if (s ~ /^\[.*\]$/) { s = substr(s, 2, length(s) - 2); gsub(/^[ ]+/, "", s); gsub(/[ ]+$/, "", s) }
      if (s == "") return "[]"
      n = split(s, parts, /[ ]*,[ ]*/); out = "["
      for (i = 1; i <= n; i++) out = out (i > 1 ? "," : "") "\"" esc(parts[i]) "\""
      return out "]"
    }
    function flush(   i, out) {
      if (!started || V["id"] == "") return
      out = "{"
      for (i = 1; i <= nk; i++) out = out (i > 1 ? "," : "") "\"" K[i] "\":" jval(K[i], V[K[i]])
      out = out ",\"plain\":\"" esc(plain_line(pw)) "\""
      printf "%s\037%s\037%s\037%s\037%s\037%d\037%s}\n", V["id"], V["status"], V["repos"], V["branch"], V["worktree"], (V["human"] != ""), out
    }
    BEGIN { nk = split(keys, K, " "); nl = split(listkeys, LK, " "); for (i = 1; i <= nl; i++) L[LK[i]] = 1 }
    FNR == 1 { flush(); started = 1; fm = 0; plain_reset(); for (i = 1; i <= nk; i++) V[K[i]] = ""; delete set; if ($0 == "---") { fm = 1; next } }
    fm && $0 == "---" { fm = 0; next }
    fm { k = $0; sub(/:.*/, "", k)
         if (k ~ /^[a-z_]+$/ && (k in V) && !(k in set)) { v = $0; sub("^" k ":[ ]*", "", v); sub(/[ ]*#.*$/, "", v); V[k] = v; set[k] = 1 } }
    !fm { plain_feed($0) }
    END { flush() }
  ' "$@"
}
branch_stats_json() { # repo trunk branch worktree -> {"behind":N|null,"ahead":N|null,"dirty":N|null,"branch_missing":b,"worktree_missing":b}
  local repo=$1 tr=$2 br=$3 wt=$4 behind=null ahead=null dirty=null bm=true wm=false
  if git -C "$repo" show-ref -q --verify "refs/heads/$br" 2>/dev/null; then   # a missing repo dir is "branch missing", quietly
    bm=false; behind=$(git -C "$repo" rev-list --count "$br..$tr"); ahead=$(git -C "$repo" rev-list --count "$tr..$br")
  fi
  case $wt in /*) ;; *) wt="$repo/$wt";; esac
  if [ -d "$wt" ]; then dirty=$(git -C "$wt" status --porcelain | wc -l); elif [ -n "$4" ]; then wm=true; fi
  printf '{"behind":%s,"ahead":%s,"dirty":%s,"branch_missing":%s,"worktree_missing":%s}' "$behind" "$ahead" "$dirty" "$bm" "$wm"
}
cmd_board_json() { # [--slow] -> one JSON object (see the usage line); the text board is untouched
  local slow=${1:-} lines n id st repos br wt ask obj stats first=1 warn="" freeze behind r rb sep pick
  lines=$(board_json_tickets "$TD"/T-*.md 2>/dev/null || true)
  printf '{"version":"%s","generated":"%s","trunk":"%s","trunk_sha":"%s","slow":%s,' "$TICKET_SH_VERSION" "$(now)" "$TRUNK" "$(g rev-parse --short HEAD 2>/dev/null)" "$( [ -n "$slow" ] && echo true || echo false )"
  if freeze_on; then freeze=$(freeze_ids | tr ' ' '\n' | grep . | sed 's/.*/"&"/' | paste -sd, || true); printf '"freeze":[%s],' "$freeze"
  else printf '"freeze":null,'; fi
  printf '"counts":%s,' "$(printf '%s\n' "$lines" | awk -F'\037' -v states="$STATES" '
    BEGIN { n = split(states, S, " ") } NF { c[$2]++ }
    END { printf "{"; for (i = 1; i <= n; i++) printf "%s\"%s\":%d", (i > 1 ? "," : ""), S[i], c[S[i]] + 0; printf "}" }')"
  n=$(printf '%s\n' "$lines" | awk -F'\037' 'NF && $6 == 1 && $2 != "done" && $2 != "dropped" && $2 != "skipped"' | wc -l)   # as the text board counts asks
  printf '"asks_pending":%d,' "$n"
  n=$(g status --porcelain | wc -l); [ "$n" = 0 ] || warn="$warn,\"MAIN WORKTREE IS DIRTY: $n entries -- ticket.sh will refuse to run\""
  n=$(ahead_count); [ "$n" = 0 ] || warn="$warn,\"MAIN IS $n AHEAD OF origin/$TRUNK: an earlier push failed -- the next mutating ticket.sh call pushes it\""
  for id in $(dup_ids); do warn="$warn,\"DUPLICATE TICKET ID $id: $(human_json_str "$(dup_files "$id")")\""; done
  printf '"warnings":[%s],"tickets":[' "${warn#,}"
  while IFS=$US read -r id st repos br wt ask obj; do
    [ -n "$id" ] || continue
    [ $first = 1 ] || printf ','; first=0
    stats=null
    if [ -n "$slow" ] && [ -n "$br" ]; then
      case $st in
        in-dev|in-review|ready-to-merge|skipped)   # the same branch choice as the text board
          if [[ $br == "{"* ]]; then
            stats="{"; sep=""
            for r in $REPOS; do   # every repo the map names, in REPOS order
              rb=$(mapget "$br" "$r"); [ -n "$rb" ] || continue
              stats="$stats$sep\"$r\":$(branch_stats_json "${REPO_PATH[$r]}" "$(repo_trunk "$r")" "$rb" "$(map_wt "$wt" "$r")")"; sep=","
            done; stats="$stats}"
          else pick=$(ticket_repo "$repos"); pick=${pick:-$REPO0}; stats=$(branch_stats_json "${REPO_PATH[$pick]}" "$(repo_trunk "$pick")" "$br" "$wt"); fi ;;
      esac
    fi
    printf '%s,"stats":%s}' "${obj%\}}" "$stats"
  done <<<"$lines"
  printf '],'
  if [ -n "$slow" ]; then
    printf '"orphans":['; first=1
    while IFS=$'\t' read -r br n behind; do
      [ -n "$br" ] || continue
      [ $first = 1 ] || printf ','; first=0
      printf '{"branch":"%s","ahead":%d,"behind":%d}' "$(human_json_str "$br")" "$n" "$behind"
    done < <(orphans)
    printf ']}\n'
  else printf '"orphans":null}\n'; fi
}
cmd_board() {
  local st f id sev title owner br wt repos hdr line ref named n bo dep r rb sep pick
  if [ "${1:-}" = --json ]; then
    case "$#:${2:-}" in 1:) cmd_board_json; return 0;; 2:--slow) cmd_board_json --slow; return 0;; esac
  fi
  [ $# -eq 0 ] || die 'usage: board [--json [--slow]]   (read-only: the text board, or one JSON object for tools; --slow adds branch stats and orphans)'
  ! freeze_on || echo "== FREEZE is on ($TICKETS_DIR/$FREEZE_FILE_NAME): claim is limited to: $(freeze_ids)"
  for st in ready-to-deploy blocked in-review ready-to-merge in-dev ready backlog skipped merged deployed; do
    hdr=0
    for f in $(grep -l "^status: $st\$" "$TD"/T-*.md 2>/dev/null | sort); do
      if [ $hdr != 1 ]; then
        echo; hdr=1
        if [ "$st" = skipped ]; then echo "== SKIPPED ($(grep -l '^status: skipped$' "$TD"/T-*.md 2>/dev/null | wc -l)): set aside on the user's word (T-0157); nothing is asked; back with: move T-xxxx backlog|ready"
        else echo "== $(echo "$st" | tr a-z A-Z)"; fi
      fi
      id=$(field "$f" id); sev=$(field "$f" severity); title=$(field "$f" title)
      line=$(printf '  %-7s %-3s %-62.62s' "$id" "$sev" "$title")
      case $st in
        in-dev|in-review|ready-to-merge|skipped)   # skipped (T-0157): parked work keeps its branch and worktree, so its stats stay visible
          owner=$(field "$f" owner); br=$(field "$f" branch); wt=$(field "$f" worktree); repos=$(field "$f" repos)
          if [ "$st" = skipped ] && [ -z "$br" ]; then
            [ -z "$(field "$f" blocked_on)" ] || line="$line  blocked_on: $(field "$f" blocked_on)"
            echo "$line"; continue
          fi
          line="$line  $owner"
          if [ -z "$br" ]; then line="$line  (no branch)"
          elif [[ $br == "{"* ]]; then  # {<repo>: x, <repo>: y}; worktree map paths are relative to each repo's parent dir
            sep=""
            for r in $REPOS; do   # every repo the map names, in REPOS order
              rb=$(mapget "$br" "$r"); [ -n "$rb" ] || continue
              line="$line  $sep$r: $(branch_stats "${REPO_PATH[$r]}" "$(repo_trunk "$r")" "$rb" "$(map_wt "$wt" "$r")")"; sep="| "
            done
          else pick=$(ticket_repo "$repos"); pick=${pick:-$REPO0}; line="$line  $(branch_stats "${REPO_PATH[$pick]}" "$(repo_trunk "$pick")" "$br" "$wt")"; fi
          [ -z "$(field "$f" claimed)" ] || line="$line  claimed $(age "$(field "$f" claimed)") ago"
          [ "$st" != skipped ] || [ -z "$(field "$f" blocked_on)" ] || line="$line  blocked_on: $(field "$f" blocked_on)" ;;
        blocked|backlog)
          bo=$(field "$f" blocked_on)
          if [ -n "$bo" ]; then
            line="$line  blocked_on: $bo"
            if [[ $bo =~ ^T-[0-9]{4}$ ]]; then  # waiting on a ticket that is parked = waiting for nothing
              dep=$(grep -l "^id: $bo\$" "$TD"/T-*.md 2>/dev/null | head -1 || true)
              [ -z "$dep" ] || [ "$(field "$dep" status)" != skipped ] || line="$line  !! $bo is SKIPPED (parked on the user's word): nothing will unblock this"
            fi
          fi ;;
        ready-to-deploy) line="$line  plan: $(field "$f" plan)" ;;
        merged|deployed) line="$line  closed_by: $(field "$f" closed_by)" ;;
      esac
      echo "$line"
    done
  done
  n=$(grep -l '^human:[ ]*[^ #]' "$TD"/T-*.md 2>/dev/null | xargs -r grep -L '^status: \(done\|dropped\|skipped\)$' | wc -l || true)
  [ "$n" = 0 ] || { echo; echo "== $n ask(s) pending for the human: scripts/ticket.sh human"; }
  echo; echo "== DONE/DROPPED: $(grep -l '^status: done$' "$TD"/T-*.md 2>/dev/null | wc -l) done, $(grep -l '^status: dropped$' "$TD"/T-*.md 2>/dev/null | wc -l) dropped"
  echo; echo "== ORPHANS (local branches with commits outside trunk that no ticket names)"
  while IFS=$'\t' read -r ref n bo; do [ -z "$ref" ] || echo "  $ref: $n commits not in trunk, $bo behind"; done < <(orphans)
  [ -z "$(g status --porcelain)" ] || echo "  !! MAIN WORKTREE IS DIRTY: $(g status --porcelain | wc -l) entries -- ticket.sh will refuse to run"
  n=$(ahead_count)  # vs the local origin ref: board never fetches
  [ "$n" = 0 ] || echo "  !! MAIN IS $n AHEAD OF origin/$TRUNK: an earlier push failed -- the next mutating ticket.sh call pushes it"
  for id in $(dup_ids); do echo "  !! DUPLICATE TICKET ID $id: $(dup_files "$id")"; done  # a race `new` lost silently
}

# ---- tickets/ticket.env: the project's values, PARSED -- never sourced (T-0186) ---------------------------------
# The file sits in every worktree, and a sourced file is executed code. Grammar (desk decision 2026-10-13): one bare
# `KEY=value` per line, KEY = [A-Z_][A-Z0-9_]*, value = [A-Za-z0-9._/@:+ -]* (no quotes, no escapes, nothing is
# evaluated; a space only separates list items); blank and '#' lines are skipped; any other line, an unknown key or a
# value with another character is reported on stderr and ignored (a required key then dies as missing). Keys: ENV_KEYS below plus
# REPO_<NAME>_PATH|TRUNK|PROD for every name in REPOS (<NAME> = the name upper-cased, '-' -> '_'). Exported
# TICKET_<KEY> values override the file after the parse (TICKET_TRUNK, TICKET_REPOS, TICKET_REPO_FRONTEND_PATH, ...;
# TICKET_FE_REPO and TICKET_FE_TRUNK are legacy names for the SECOND repo in REPOS) and meet the same rules, or the run dies: an
# override is deliberate, so a slip is loud. A repo path is relative to this repo's root, joined by text (../x -> the
# parent's x), never resolved through symlinks. A branch name (TRUNK, a repo's trunk/prod, a protected name) never
# starts with '-'. The header `# generated-from: .ops/config.yaml sha256 <hex>` is compared with the file it names: a
# mismatch is a warning, never a death (regenerate: zouinify config --env --write).
# The character classes are explicit lists, not ranges: under en_US.UTF-8 a [a-z] range also matches é, ß or ｓ.
LOWER=abcdefghijklmnopqrstuvwxyz; UPPER=ABCDEFGHIJKLMNOPQRSTUVWXYZ; DIGIT=0123456789
ENV_KEYS="PROJECT TRUNK TICKETS_DIR WORKTREES REPOS PROTECTED_BRANCHES_EXTRA ASK_VERBS TICKET_TYPES DEPLOY_PLUGIN DEPLOY_KINDS"
ENV_KEY_RE="^([${UPPER}_][${UPPER}${DIGIT}_]*)=(.*)\$"
ENV_VALUE_RE="^[${UPPER}${LOWER}${DIGIT}._/@:+ -]*\$"; ENV_VALUE_DESC='[A-Za-z0-9._/@:+ -]'
REPO_NAME_RE="^[${LOWER}][${LOWER}${DIGIT}_-]*\$"
PLUGIN_NAME_RE="^[${LOWER}${DIGIT}-]+\$"
ENV_SHA_RE="^# generated-from: \.ops/config\.yaml sha256 ([${DIGIT}abcdef]{64})[[:space:]]*\$"
ENV_FILE=""; ENV_SHA=""
declare -A ENV_RAW=() ENV_SEEN=() ENV_BAD=()   # the REPO_<NAME>_* lines as read (resolved against REPOS in resolve_repos); line of every key; keys whose line was unusable
ENV_REQUIRED="TRUNK REPOS DEPLOY_PLUGIN"   # an unusable line for one of these is a death, never a silent default (DEPLOY_PLUGIN=none would drop every deploy rule)
env_note() { echo "ticket.sh: ticket.env:$1: ignored: $2" >&2; }
env_value_ok() { [[ $1 =~ $ENV_VALUE_RE ]]; }
ref_ok() { case $1 in ""|-*) return 1;; esac; return 0; }   # a branch name: never empty, never a leading '-' (git would read an option)
repo_key() { tr "${LOWER}-" "${UPPER}_" <<<"$1"; }   # a repo name as it appears in its REPO_<NAME>_* keys (names are validated ASCII first)
join_path() { # base relative -> the path as the project wrote it, ./ and ../ folded by text (no readlink: no symlink surprises in messages)
  local b=$1 r=$2
  while :; do case $r in ./*) r=${r#./};; ../*) b=$(dirname "$b"); r=${r#../};; *) break;; esac; done
  case $r in ""|.) echo "$b";; ..) dirname "$b";; /*) echo "$r";; *) echo "$b/$r";; esac
}
read_ticket_env() { # -> 0 with the plain keys set and ENV_RAW/ENV_SEEN filled, 1 when there is no file; grammar slips are reported, never fatal
  local n=0 line key val
  ENV_FILE="$HERE/tickets/ticket.env"
  [ -f "$ENV_FILE" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1)); line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*$ ]] && continue
    if [[ $line =~ ^[[:space:]]*# ]]; then
      [[ $line =~ $ENV_SHA_RE ]] && ENV_SHA=${BASH_REMATCH[1]}
      continue
    fi
    [[ $line =~ $ENV_KEY_RE ]] || { env_note "$n" "not KEY=value (an upper-case key, '=', the value -- no 'export', no indentation, no ': \${X:=y}')"; continue; }
    key=${BASH_REMATCH[1]}; val=${BASH_REMATCH[2]}
    env_value_ok "$val" || { env_note "$n" "$key has a character outside $ENV_VALUE_DESC in its value (no quotes, no escapes, nothing is evaluated)"; ENV_BAD[$key]=$n; continue; }
    [ -z "${ENV_SEEN[$key]-}" ] || env_note "$n" "$key repeats line ${ENV_SEEN[$key]} (the last one wins)"
    ENV_SEEN[$key]=$n
    case $key in
      PROJECT|TRUNK|TICKETS_DIR|WORKTREES|REPOS|PROTECTED_BRANCHES_EXTRA|ASK_VERBS|TICKET_TYPES|DEPLOY_PLUGIN|DEPLOY_KINDS) printf -v "$key" '%s' "$val" ;;
      REPO_*_PATH|REPO_*_TRUNK|REPO_*_PROD) ENV_RAW[$key]=$val ;;
      *) env_note "$n" "unknown key $key (keys: $ENV_KEYS, REPO_<NAME>_PATH|TRUNK|PROD)" ;;
    esac
  done <"$ENV_FILE"
  return 0
}
override_ok() { # VAR -> dies unless the exported override VAR is unset or meets the file's own value rule
  [ -z "${!1-}" ] || env_value_ok "${!1}" || die "$1 (an exported override) has a character outside $ENV_VALUE_DESC -- the same rule as tickets/ticket.env"
}
apply_env_overrides() { # exported TICKET_<KEY> wins over the file, validated BEFORE anything is derived from it
  local key v
  for key in $ENV_KEYS; do v="TICKET_$key"; override_ok "$v"; [ -z "${!v-}" ] || printf -v "$key" '%s' "${!v}"; done
}
resolve_repos() { # REPOS (file or override) -> REPO0 and REPO_PATH/TRUNK/PROD per name from ENV_RAW, then the exported per-repo overrides
  local r u key v second=""
  [ -n "$REPOS" ] || die "ticket.env: REPOS is empty -- its first name is this repo (REPO_<NAME>_PATH=.)"
  for r in $REPOS; do [[ $r =~ $REPO_NAME_RE ]] || die "ticket.env: the repo name '$r' in REPOS is not [a-z][a-z0-9_-]*"; done
  # shellcheck disable=SC2086
  set -- $REPOS; REPO0=$1; second=${2:-}
  for r in $REPOS; do [ "$(tr ' ' '\n' <<<"$REPOS" | grep -cx -- "$r")" = 1 ] || die "ticket.env: REPOS names '$r' twice"; done
  declare -A used=()
  for r in $REPOS; do
    u=$(repo_key "$r")
    REPO_PATH[$r]=${ENV_RAW[REPO_${u}_PATH]-}; REPO_TRUNK[$r]=${ENV_RAW[REPO_${u}_TRUNK]-}; REPO_PROD[$r]=${ENV_RAW[REPO_${u}_PROD]-}
    used[REPO_${u}_PATH]=1; used[REPO_${u}_TRUNK]=1; used[REPO_${u}_PROD]=1
  done
  for key in "${!ENV_RAW[@]}"; do [ -n "${used[$key]-}" ] || env_note "${ENV_SEEN[$key]}" "$key names a repo that REPOS does not list ($REPOS)"; done
  # the old aliases (the second repo in REPOS) first, a specific TICKET_REPO_<NAME>_* over them; each meets the file's rule or the run dies
  override_ok TICKET_FE_REPO; override_ok TICKET_FE_TRUNK
  [ -z "$second" ] || { [ -z "${TICKET_FE_REPO-}" ] || REPO_PATH[$second]=$TICKET_FE_REPO; [ -z "${TICKET_FE_TRUNK-}" ] || REPO_TRUNK[$second]=$TICKET_FE_TRUNK; }
  for r in $REPOS; do
    u=$(repo_key "$r")
    v="TICKET_REPO_${u}_PATH"; override_ok "$v"; [ -z "${!v-}" ] || REPO_PATH[$r]=${!v}
    v="TICKET_REPO_${u}_TRUNK"; override_ok "$v"; [ -z "${!v-}" ] || REPO_TRUNK[$r]=${!v}
  done
}
worktrees_ok() { # value -> 0 when it is a relative path of plain segments: non-empty, no leading or trailing '/', no empty, '.' or '..' segment, not .git or under it
  local v=$1   # patterns, not `read -a`: read drops a trailing empty field, so "a/" would pass as one plain segment
  [ -n "$v" ] || return 1
  case $v in /*|*/|*//*|.|./*|*/.|*/./*|..|../*|*/..|*/../*|.git|.git/*) return 1;; esac
  return 0
}
load_config() { # everything derived from ticket.env, or a clear death
  local r p nm extra=""
  read_ticket_env || die "no $HERE/tickets/ticket.env -- the project's values (TRUNK, REPOS, ...). Write it (bare KEY=value lines; keys: $ENV_KEYS, REPO_<NAME>_PATH|TRUNK|PROD) or run: zouinify config --env --write"
  for nm in $ENV_REQUIRED; do [ -z "${ENV_BAD[$nm]-}" ] || die "ticket.env:${ENV_BAD[$nm]}: $nm has an unusable value (reported above) -- a required key is never skipped or defaulted"; done
  apply_env_overrides
  resolve_repos
  [ -n "$TRUNK" ] || die "ticket.env: TRUNK is empty"
  [ -n "$TICKETS_DIR" ] && [ -n "$WORKTREES" ] && [ -n "$ASK_VERBS" ] && [ -n "$TICKET_TYPES" ] || die "ticket.env: TICKETS_DIR, WORKTREES, ASK_VERBS and TICKET_TYPES must not be empty (leave a key out to keep the default)"
  # T-0196 review: prune --orphans deletes under <repo>/WORKTREES and nowhere else, so that value is checked here, before any
  # command runs: '.' listed the repo's own dirs as orphans, '/' or '..' would reach anywhere, .git is git's
  worktrees_ok "$WORKTREES" || die "${TICKET_WORKTREES:+TICKET_WORKTREES (an exported override): }ticket.env: WORKTREES='$WORKTREES' is not usable -- a relative path inside the repo, plain segments only (no leading '/', no '.', '..' or empty segment, not .git or under it): prune deletes only there"
  TD="$MAIN/$TICKETS_DIR"; KNOWN_REPOS=$REPOS
  for r in $REPOS; do
    p=${REPO_PATH[$r]-}; [ -n "$p" ] || die "ticket.env: REPO_$(repo_key "$r")_PATH is missing (REPOS names $r)"
    REPO_PATH[$r]=$(join_path "$MAIN" "$p")
  done
  [ "${REPO_PATH[$REPO0]}" = "$MAIN" ] || die "ticket.env: the default repo $REPO0 resolves to ${REPO_PATH[$REPO0]}, but this script's main checkout is $MAIN (REPO_$(repo_key "$REPO0")_PATH is '.')"
  for r in $REPOS; do for nm in $REPOS; do   # two names, one directory: merge would land a ticket twice and prune would look in the wrong repo
    [ "$r" = "$nm" ] && break
    # compared resolved (symlinks followed, trailing slashes dropped; a missing path canonicalised as text), reported as written (T-0192)
    [ "$(readlink -m "${REPO_PATH[$r]}")" != "$(readlink -m "${REPO_PATH[$nm]}")" ] || die "ticket.env: REPO_$(repo_key "$nm")_PATH and REPO_$(repo_key "$r")_PATH resolve to the same directory (${REPO_PATH[$nm]} = ${REPO_PATH[$r]})"
  done; done
  for r in $REPOS; do [ "$r" = "$REPO0" ] || [ -n "${REPO_TRUNK[$r]-}" ] || die "ticket.env: REPO_$(repo_key "$r")_TRUNK is missing (REPOS names $r)"; done
  # every branch name the core protects: TRUNK, each repo's trunk and prod, the project's extra names -- none starts with '-'
  for nm in $TRUNK $(for r in $REPOS; do echo "${REPO_TRUNK[$r]-} ${REPO_PROD[$r]-}"; done) $PROTECTED_BRANCHES_EXTRA; do
    ref_ok "$nm" || die "ticket.env: '$nm' is not a branch name (a leading '-' would read as an option to git)"
    extra="$extra|$(re_escape "$nm")"
  done
  PROTECTED_BRANCH_RE="^($PROTECTED_BRANCH_RE_CORE$extra)\$"
  if [ -n "$ENV_SHA" ] && [ -f "$HERE/.ops/config.yaml" ] && [ "$(sha256sum "$HERE/.ops/config.yaml" | cut -c1-64)" != "$ENV_SHA" ]; then
    echo "ticket.sh: tickets/ticket.env was generated from another .ops/config.yaml (its sha256 differs): regenerate it in a ticket (zouinify config --env --write)" >&2
  fi
  return 0
}
# ---- the deploy plugin: sourced once, from this checkout, after every core definition --------------------------------
# tickets/plugins/<DEPLOY_PLUGIN>/plugin.sh defines every hook below (tickets/plugins/none/plugin.sh has the no-ops;
# tickets/plugins/aws-terraform/plugin.sh is this project's). The name comes from the parsed ticket.env and resolves only
# to that file by its literal path under <this checkout>/tickets/plugins/: no path, and no symlink at tickets/plugins,
# at the plugin's directory or at plugin.sh (a link could point anywhere). A plugin missing a hook is refused.
PLUGIN_HOOKS="plugin_evidence_check plugin_plan_key plugin_plan_void plugin_command_void_reason plugin_ask_check plugin_verb_for plugin_expect_line plugin_ignored_valuable_re plugin_deploy_kinds plugin_deploy_check plugin_apply_usage plugin_apply_prepare plugin_apply"
PLUGIN_DIR=""
load_plugin() {
  local dir file h missing="" x
  [[ $DEPLOY_PLUGIN =~ $PLUGIN_NAME_RE ]] || die "ticket.env: DEPLOY_PLUGIN='$DEPLOY_PLUGIN' is not a plugin name ([a-z0-9-]+: one directory under tickets/plugins/)"
  dir="$HERE/tickets/plugins"; file="$dir/$DEPLOY_PLUGIN/plugin.sh"
  for x in "$dir" "$dir/$DEPLOY_PLUGIN" "$file"; do
    ! [ -L "$x" ] || die "no plugin '$DEPLOY_PLUGIN': $x is a symlink -- a plugin is read from this checkout's own tickets/plugins/, never through a link"
  done
  [ -f "$file" ] || die "no plugin '$DEPLOY_PLUGIN': $file is not a regular file"
  PLUGIN_DIR="$dir/$DEPLOY_PLUGIN"
  # shellcheck disable=SC1090
  . "$file"
  for h in $PLUGIN_HOOKS; do declare -F "$h" >/dev/null || missing="$missing $h"; done
  [ -z "$missing" ] || die "plugin $DEPLOY_PLUGIN ($file) does not define:$missing -- a plugin defines every hook (tickets/plugins/none/plugin.sh has the no-ops)"
  x=$(plugin_ignored_valuable_re); VALUABLE_IGNORED_RE="$VALUABLE_IGNORED_RE_CORE${x:+|$x}"
}

c=${1:-board}; shift || true
if [ "$c" = version ]; then   # works without ticket.env: zouinify asks it before anything else
  if read_ticket_env 2>/dev/null; then echo "ticket.sh $TICKET_SH_VERSION  plugin $DEPLOY_PLUGIN  ($ENV_FILE)"; else echo "ticket.sh $TICKET_SH_VERSION  (no tickets/ticket.env in $HERE)"; fi
  exit 0
fi
load_config; load_plugin
case $c in
  new) cmd_new "$@" ;; claim) cmd_claim "$@" ;; move) cmd_move "$@" ;;
  set) cmd_set "$@" ;; log) cmd_log "$@" ;; sync) cmd_sync "$@" ;; put) cmd_put "$@" ;; board) cmd_board "$@" ;;
  merge) cmd_merge "$@" ;; prune) cmd_prune "$@" ;; human) cmd_human "$@" ;; ask) cmd_ask "$@" ;; apply) cmd_apply "$@" ;;
  deploy-check) trap - EXIT; plugin_deploy_check "$@" ;;
  *) usage; exit 1 ;;
esac
