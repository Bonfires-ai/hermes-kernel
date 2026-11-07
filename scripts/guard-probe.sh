#!/usr/bin/env bash
# Proves that every script that launches Hermes refuses a home that is not its own, before it starts Hermes: HERMES_HOME
# missing, blank, the real home, a symlink to it, the same with a trailing space (Hermes strips it), a profile under it
# (Hermes accepts one), a value Hermes would expand, and the real home with HOME pointing elsewhere or unset. And that a
# throwaway home passes. Hermes is a stub that records being called; no real Hermes is on any path of this probe.
#
#   scripts/guard-probe.sh
#
# Exits 0 only if all the refusals happened (non-zero exit, a message naming HERMES_HOME, the stub never called) and the
# throwaway home got through each guard. It only compares paths: it never reads or writes anything inside ~/.hermes. (If a
# guard were missing, e2e.sh would go on to its own compose project, hermes-kernel-e2e-guardprobe, so run it only once
# the guards are in.)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CALLS="$WORK/calls"
cat >"$WORK/hermes-stub" <<STUB
#!/usr/bin/env bash
echo "called: \$*" >>"$CALLS"
echo "Hermes Agent v0.23.0 (stub)"
exit 1
STUB
chmod +x "$WORK/hermes-stub"
REAL="$(getent passwd "$(id -u)" | cut -d: -f6)/.hermes"
ln -s "$REAL" "$WORK/link-to-real-home"
mkdir "$WORK/own" "$WORK/elsewhere"
PY="${E2E_PYTHON:-python3}"
printf '[]' >"$WORK/prompts.json"
bad=0
run() { # run <mode> <command...>: the output and exit status of one script under one kind of HERMES_HOME
  local mode="$1"; shift
  case "$mode" in
    unset) env -u HERMES_HOME "$@" ;;
    blank) HERMES_HOME="   " "$@" ;;
    real) HERMES_HOME="$REAL" "$@" ;;
    symlink) HERMES_HOME="$WORK/link-to-real-home" "$@" ;;
    trailing-space) HERMES_HOME="$REAL " "$@" ;;
    profile) HERMES_HOME="$REAL/profiles/work" "$@" ;;
    expanded) REAL_FOR_PROBE="$REAL" HERMES_HOME='/$REAL_FOR_PROBE' "$@" ;;
    home-elsewhere) HOME="$WORK/elsewhere" HERMES_HOME="$REAL" "$@" ;;
    home-unset) env -u HOME HERMES_HOME="$REAL" "$@" ;;
    own) HERMES_HOME="$WORK/own" "$@" ;;
  esac
}
probe() { # probe <label> <mode> <command...>
  local label="$1" mode="$2"; shift 2
  local out code called=no
  out="$(run "$mode" "$@" 2>&1)"; code=$?
  if [ -s "$CALLS" ]; then called=yes; fi
  if [ "$code" -ne 0 ] && [ "$called" = no ] && grep -q "HERMES_HOME" <<<"$out"; then
    echo "REFUSED  $label [$mode]: exit $code, stub not called: $(head -1 <<<"$out")"
  else
    echo "NOT REFUSED  $label [$mode]: exit $code, stub called: $called"
    bad=1
  fi
  rm -f "$CALLS"
}
passes() { # passes <label> <what shows the guard let it through> <command...>
  local label="$1" sign="$2"; shift 2
  local out called=no
  out="$(run own "$@" 2>&1)"
  if [ -s "$CALLS" ]; then called=yes; fi
  if [ "$called" = yes ] || grep -q "$sign" <<<"$out"; then
    echo "PASSES   $label [own]"
  else
    echo "WRONGLY REFUSED  $label [own]: $(head -1 <<<"$out")"
    bad=1
  fi
  rm -f "$CALLS"
}
E2E=(env E2E_CONFIRM=yes E2E_PROJECT=hermes-kernel-e2e-guardprobe E2E_HERMES="$WORK/hermes-stub" "$ROOT/scripts/e2e.sh")
DRIVE=("$PY" "$ROOT/scripts/e2e_drive.py" "$WORK/hermes-stub" "$WORK/t.txt" "$WORK/prompts.json")
SETUP=(env E2E_SETUP_TOKEN=x "$PY" "$ROOT/scripts/e2e_setup.py" "$WORK/hermes-stub")
for mode in unset blank real symlink trailing-space profile expanded home-elsewhere home-unset; do
  probe e2e.sh "$mode" "${E2E[@]}"
  probe e2e_drive.py "$mode" "${DRIVE[@]}"
  probe e2e_setup.py "$mode" "${SETUP[@]}"
done
# Without E2E_CONFIRM, e2e.sh stops at its warning, which is past the guard and before anything is changed.
passes e2e.sh "Nothing has been changed" env -u E2E_CONFIRM E2E_PROJECT=hermes-kernel-e2e-guardprobe E2E_HERMES="$WORK/hermes-stub" "$ROOT/scripts/e2e.sh"
passes e2e_drive.py "called" "${DRIVE[@]}"
passes e2e_setup.py "called" "${SETUP[@]}"
exit $bad
