#!/usr/bin/env bash
# Proves that every script that launches Hermes refuses a missing HERMES_HOME, the real home, and a symlink to it, before
# it starts Hermes. Hermes is a stub that records being called; no real Hermes is on any path of this probe.
#
#   scripts/guard-probe.sh
#
# Exits 0 only if all nine refusals happened (non-zero exit, a message naming HERMES_HOME, the stub never called).
# It only compares paths: it never reads or writes anything inside ~/.hermes. (If a guard were missing, e2e.sh would
# go on to its own compose project, hermes-kernel-e2e-guardprobe, so run it only once the guards are in.)
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
ln -s "$HOME/.hermes" "$WORK/link-to-real-home"
PY="${E2E_PYTHON:-python3}"
printf '[]' >"$WORK/prompts.json"
bad=0
probe() { # probe <label> <home-mode> <command...>
  local label="$1" mode="$2"; shift 2
  local out code
  case "$mode" in
    unset) out="$(env -u HERMES_HOME "$@" 2>&1)"; code=$? ;;
    real) out="$(HERMES_HOME="$HOME/.hermes" "$@" 2>&1)"; code=$? ;;
    symlink) out="$(HERMES_HOME="$WORK/link-to-real-home" "$@" 2>&1)"; code=$? ;;
  esac
  local called=no
  if [ -s "$CALLS" ]; then called=yes; fi
  if [ "$code" -ne 0 ] && [ "$called" = no ] && grep -q "HERMES_HOME" <<<"$out"; then
    echo "REFUSED  $label [$mode]: exit $code, stub not called: $(head -1 <<<"$out")"
  else
    echo "NOT REFUSED  $label [$mode]: exit $code, stub called: $called"
    bad=1
  fi
  rm -f "$CALLS"
}
for mode in unset real symlink; do
  probe e2e.sh "$mode" env E2E_CONFIRM=yes E2E_PROJECT=hermes-kernel-e2e-guardprobe E2E_HERMES="$WORK/hermes-stub" "$ROOT/scripts/e2e.sh"
  probe e2e_drive.py "$mode" "$PY" "$ROOT/scripts/e2e_drive.py" "$WORK/hermes-stub" "$WORK/t.txt" "$WORK/prompts.json"
  probe e2e_setup.py "$mode" env E2E_SETUP_TOKEN=x "$PY" "$ROOT/scripts/e2e_setup.py" "$WORK/hermes-stub"
done
exit $bad
