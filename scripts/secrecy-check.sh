#!/usr/bin/env bash
# Fails if a forbidden literal appears in the tree or in committed history.
# Literals come from the MAIN checkout's .secrecy-literals (gitignored, one
# per line) and from its .env's DATABASE_URL_AUTH. A linked worktree does not
# check either file out, and worktree-local copies are ignored — the main
# checkout is the single source, resolved through --git-common-dir below.
# Findings name commits and files, never the value.
#
#   scripts/secrecy-check.sh          tree + history
#   scripts/secrecy-check.sh --tree   tree only

set -uo pipefail

# Scan the calling checkout's tree (a linked worktree's, not the main one's),
# but read the literals from the main checkout: --git-common-dir is
# <main>/.git from a worktree and ./.git from the main checkout, so its
# dirname is the main checkout in both.
TREE_ROOT="$(git rev-parse --show-toplevel)" || exit 1
MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)" || exit 1
cd "$TREE_ROOT" || exit 1

MODE="${1:-full}"
FAIL=0
declare -a NEEDLES=()
declare -a LABELS=()

if [ -f "$MAIN_ROOT/.secrecy-literals" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -n "$line" ] || continue
    NEEDLES+=("$line")
    LABELS+=("a listed literal (${#line} chars)")
  done < "$MAIN_ROOT/.secrecy-literals"
fi

if [ -f "$MAIN_ROOT/.env" ]; then
  # -E, not a BRE \?: BSD sed (macOS) reads \? as a literal and matches
  # nothing, so the auth-DB name silently vanished there.
  AUTH_DB="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?DATABASE_URL_AUTH[[:space:]]*=[[:space:]]*//p' "$MAIN_ROOT/.env" \
    | head -n1 | tr -d '"'"'"'' | sed 's/?.*$//' | sed 's#.*/##' | tr -d '[:space:]')"
  if [ -n "${AUTH_DB:-}" ] && [ "${#AUTH_DB}" -ge 4 ]; then
    NEEDLES+=("$AUTH_DB")
    LABELS+=("the auth-DB name")
  fi
fi

# Nothing to check must not look like nothing found.
if [ "${#NEEDLES[@]}" -eq 0 ]; then
  echo "secrecy-check: ERROR — no literals available; add .secrecy-literals or .env" >&2
  exit 1
fi

for i in "${!NEEDLES[@]}"; do
  hits="$(git grep -I -l -i -e "${NEEDLES[$i]}" -- . 2>/dev/null)"
  if [ -n "$hits" ]; then
    echo "secrecy-check: FAIL — ${LABELS[$i]} is in the working tree:" >&2
    echo "$hits" | sed 's/^/    /' >&2
    FAIL=1
  fi
done

if [ "$MODE" = "--tree" ]; then
  [ "$FAIL" -eq 0 ] && echo "secrecy-check: tree clean (${#NEEDLES[@]} literal(s))"
  exit "$FAIL"
fi

for i in "${!NEEDLES[@]}"; do
  # --regexp-ignore-case is load-bearing: without it this misses what the
  # -i tree check above catches, and reports clean.
  hits="$(git log --branches --tags --oneline --regexp-ignore-case \
            -S"${NEEDLES[$i]}" --format='%h' 2>/dev/null)"
  if [ -n "$hits" ]; then
    echo "secrecy-check: FAIL — ${LABELS[$i]} is in committed history:" >&2
    echo "$hits" | sed 's/^/    /' >&2
    echo "    Deleting it from the tree does not unpublish it: rewrite history," >&2
    echo "    and rotate the value if it was ever pushed." >&2
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "secrecy-check: clean (tree + history, ${#NEEDLES[@]} literal(s))"
fi
exit "$FAIL"
