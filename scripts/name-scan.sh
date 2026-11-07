#!/usr/bin/env bash
# Fails if a tracked or new file holds a path of someone's machine, or something shaped like a key.
#
#   scripts/name-scan.sh
#
# Generic checks always run: a home-directory path, an `sk-` key, a `bf_` token. Private names (a company's internal
# project names, people) are not listed here, because a tracked list would publish what it hunts. They are read from
# .dev/private-names.txt, which is git-ignored: one name per line, optionally `name|text` to exempt lines that hold
# `text`. Without that file the private half is skipped with a note (CI does not have it, so run the scan on your own
# machine before a release). Every file git knows is scanned, tests and data included.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

NAMES_FILE="${NAME_SCAN_NAMES:-.dev/private-names.txt}"
hits=0
files=()
while IFS= read -r -d '' f; do
  [ -f "$f" ] && [ ! -L "$f" ] && files+=("$f")
done < <(git ls-files -z --cached --others --exclude-standard)

scan() { # scan <label> <grep args...>: print every hit, count it
  local label="$1"; shift
  local out
  out="$(grep -InH "$@" -- "${files[@]}" 2>/dev/null || true)"
  if [ -n "$out" ]; then
    echo "name-scan: $label:"
    echo "$out" | sed 's/^/  /'
    hits=$((hits + $(echo "$out" | wc -l)))
  fi
}

# Generic. The brackets keep this file from matching its own patterns.
scan "a home-directory path" -E '[/]home[/]'
scan "something shaped like an sk- key" -E '(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}'
scan "something shaped like a bf_ token" -E '(^|[^A-Za-z0-9])bf_[A-Za-z0-9]{16,}'

# Private.
if [ -f "$NAMES_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    name="${line%%|*}"
    exempt=""
    [ "$name" != "$line" ] && exempt="${line#*|}"
    out="$(grep -InHiF "$name" -- "${files[@]}" 2>/dev/null || true)"
    [ -n "$exempt" ] && out="$(echo "$out" | grep -viF "$exempt" || true)"
    if [ -n "$out" ]; then
      echo "name-scan: a private name from $NAMES_FILE:"
      echo "$out" | sed 's/^/  /'
      hits=$((hits + $(echo "$out" | wc -l)))
    fi
  done <"$NAMES_FILE"
else
  echo "name-scan: $NAMES_FILE is absent: the private-name half was skipped (only the generic checks ran)."
fi

if [ "$hits" -gt 0 ]; then
  echo "name-scan: $hits hit(s)."
  exit 1
fi
echo "name-scan: no hits in ${#files[@]} files."
