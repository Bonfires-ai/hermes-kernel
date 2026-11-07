#!/usr/bin/env bash
# Fails if a tracked or new file holds a path of someone's machine, or something shaped like a key, or a private name.
#
#   scripts/name-scan.sh
#
# Generic checks always run: a home-directory path, an `sk-` key, a `bf_` token. Private names (a company's internal
# project names, people) are not listed here, because a tracked list would publish what it hunts. They are read from
# .dev/private-names.txt, which is git-ignored: one name per line, optionally `name|text` to exempt matches that hold
# `text`. The scan FAILS CLOSED: without a name to look for it fails, unless NAME_SCAN_GENERIC_ONLY=1 says that only the generic
# half is wanted (CI sets it, since CI does not have the file; run the full scan on your own machine before a release).
# It also fails when git cannot list the files, when there are no files, and when grep itself errors. Every file git
# knows is scanned, tests and data included.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

NAMES_FILE="${NAME_SCAN_NAMES:-.dev/private-names.txt}"
GENERIC_ONLY="${NAME_SCAN_GENERIC_ONLY:-0}"
hits=0
files=()
listing="$(git ls-files -z --cached --others --exclude-standard | tr '\0' '\n')" \
  || { echo "name-scan: git could not list the files: the scan cannot say anything." >&2; exit 2; }
while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] && [ ! -L "$f" ] && files+=("$f")
done <<<"$listing"
[ "${#files[@]}" -gt 0 ] || { echo "name-scan: no files to scan: the scan cannot say anything." >&2; exit 2; }

# run_grep <grep args...>: prints the matches; status 0 for matches or none, status 2 when grep itself fails. It runs in
# a command substitution, so its status is the only way out: a caller must assign first and stop on it (`scan` does).
run_grep() {
  local out status=0
  out="$(grep -InH "$@" -- "${files[@]}" 2>&1)" || status=$?
  if [ "$status" -gt 1 ]; then echo "name-scan: grep failed (status $status): $out" >&2; return 2; fi
  [ "$status" -eq 1 ] || printf '%s\n' "$out"
}

report() { # report <label> <matches>
  if [ -n "$2" ]; then
    echo "name-scan: $1:"
    printf '%s\n' "$2" | sed 's/^/  /'
    hits=$((hits + $(printf '%s\n' "$2" | wc -l)))
  fi
}

scan() { # scan <label> <grep args...>
  local label="$1" out
  shift
  out="$(run_grep "$@")" || exit 2
  report "$label" "$out"
}

# Generic. The brackets keep this file from matching its own patterns.
scan "a home-directory path" -E '[/]home[/]'
scan "something shaped like an sk- key" -E '(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}'
scan "something shaped like a bf_ token" -E '(^|[^A-Za-z0-9])bf_[A-Za-z0-9]{16,}'

# Private.
mode="generic and private names"
names=0
if [ -f "$NAMES_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    names=$((names + 1))
    name="${line%%|*}"
    exempt=""
    [ "$name" != "$line" ] && exempt="${line#*|}"
    # -o prints only the matched text, so an exemption is matched against that, not against file:line:text.
    out="$(run_grep -oiF "$name")" || exit 2
    if [ -n "$exempt" ] && [ -n "$out" ]; then
      # Exempt a hit when its line holds the exempting text: look the line up again by file and number.
      kept=""
      while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        file="${hit%%:*}"; rest="${hit#*:}"; num="${rest%%:*}"
        if ! sed -n "${num}p" "$file" | grep -qiF -- "$exempt"; then kept+="$hit"$'\n'; fi
      done <<<"$out"
      out="${kept%$'\n'}"
    fi
    report "a private name from $NAMES_FILE" "$out"
  done <"$NAMES_FILE"
fi
if [ "$names" -eq 0 ]; then
  if [ "$GENERIC_ONLY" != "1" ]; then
    echo "name-scan: $NAMES_FILE is absent or names nothing, so the private-name half cannot run. Fill it in, or set NAME_SCAN_GENERIC_ONLY=1 to scan only for paths and keys." >&2
    exit 2
  fi
  mode="generic only"
fi

if [ "$hits" -gt 0 ]; then
  echo "name-scan: $hits hit(s)."
  exit 1
fi
echo "name-scan: no hits in ${#files[@]} files ($mode)."
