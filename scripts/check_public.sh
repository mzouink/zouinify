#!/usr/bin/env bash
# The public skill repo holds templates only. This greps every tracked and staged file for strings a
# project must never publish. Two generic tiers: credential-shaped strings (cloud access key ids,
# secret assignments, API keys) are refused everywhere, tests included -- GitHub push protection scans
# every pushed blob, so a "fake" literal shaped like a key blocks the push; build such test strings
# from fragments at runtime. Looser patterns (12-digit account ids, e-mail addresses, absolute home
# paths) exempt tests/, which plant such values on purpose. A project's own names are NOT written here
# (that would publish them): put them, one case-insensitive regex per line, in
# $ZOUINIFY_PRIVATE_PATTERNS (default ~/.config/zouinify/private-patterns), outside the repo; those are
# checked in every file.
# Usage: scripts/check_public.sh   (from the repo root; run it before every push; CI runs it too)
set -u
strict='AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9_-]{8,}|aws_secret_access_key *= *[A-Za-z0-9/+]{20,}|aws_session_token *= *[A-Za-z0-9/+=]{40,}|gh[pousr]_[A-Za-z0-9]{20,}'
loose='\b[0-9]{12}\b|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|/(home|Users|groups)/[A-Za-z0-9._@-]+'
self='^scripts/check_public\.sh$'
exempt_loose='^(tests/|scripts/check_public\.sh$)'
pats=${ZOUINIFY_PRIVATE_PATTERNS:-$HOME/.config/zouinify/private-patterns}
files=$( { git ls-files; git diff --name-only --cached; } 2>/dev/null | sort -u | while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done)
if [ -z "$files" ]; then
  echo "public check: no tracked files"
  exit 0
fi
rc=0
hits=$(printf '%s\n' "$files" | grep -vE "$self" | tr '\n' '\0' | xargs -0 grep -nHE -- "$strict" 2>/dev/null)
if [ -n "$hits" ]; then
  echo "public check FAIL (credential-shaped strings; also in tests: assemble fakes from fragments at runtime):"
  echo "$hits"
  rc=1
fi
hits=$(printf '%s\n' "$files" | grep -vE "$exempt_loose" | tr '\n' '\0' | xargs -0 grep -nHE -- "$loose" 2>/dev/null)
if [ -n "$hits" ]; then
  echo "public check FAIL (account id, e-mail address or home path outside tests/):"
  echo "$hits"
  rc=1
fi
if [ -f "$pats" ]; then
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    case $p in \#*) continue ;; esac
    h=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nHiE -- "$p" 2>/dev/null)
    if [ -n "$h" ]; then
      echo "public check FAIL (private pattern '$p'):"
      echo "$h"
      rc=1
    fi
  done <"$pats"
else
  echo "public check: no private pattern file at $pats (generic patterns only)"
fi
if [ $rc -eq 0 ]; then
  echo "public check ok: $(printf '%s\n' "$files" | wc -l | tr -d ' ') files"
fi
exit $rc
