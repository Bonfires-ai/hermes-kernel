#!/usr/bin/env bash
# The whole thing from nothing, the way the README says to do it, on a real Hermes and a real stack.
#
#   HERMES_HOME=/some/throwaway/directory E2E_CONFIRM=yes scripts/e2e.sh
#
# THIS SCRIPT DELETES DATA. It starts with `docker compose down -v` on its own project (E2E_PROJECT, default
# hermes-kernel-e2e): that project's Neo4j and Mongo volumes, and with them every memory built under it. It accepts
# only project names that start with `hermes-kernel-e2e` (never the README's `hermes-kernel`), refuses to run while
# something answers on port 8787, and generates a fresh KT_TOKEN for its stack (the shell's value beats .env in
# compose), so a Hermes of yours that points at port 8787 later is refused (401) and keeps its data instead of feeding a
# stack that is about to be wiped. It never reads or writes ~/.hermes: Hermes runs in a fresh temporary home, removed at
# the end, after every process that was started on that home (Hermes leaves a background gateway per home) is stopped.
# It refuses to start unless HERMES_HOME is set in your shell to an absolute path outside ~/.hermes (also through a
# symlink): that value is only checked, the run uses its own temporary home. It needs Hermes v0.23.0 exactly, and 9 GiB of RAM available.
#
# It spends real money (under a cent to build the memory, and a chat model for about a dozen turns; the target is under
# $1) and takes about 15 minutes, most of it waiting for memory to build.
#
# Settings (all optional except E2E_CONFIRM):
#   E2E_PROJECT          compose project name, must start with hermes-kernel-e2e   (hermes-kernel-e2e)
#   E2E_OVERRIDE         one extra compose file                      (none)
#   E2E_PLUGIN_SOURCE    what install.sh gives `hermes plugins install`   (empty: install.sh picks this checkout's HEAD)
#   E2E_HERMES           the hermes command                          (hermes)
#   E2E_PYTHON           a python that has pexpect                   (python3)
#   E2E_MODEL            the chat model (it must be good at calling tools; a small one may reach for Hermes's own
#                        skill tool instead of teaching a phrase)  (anthropic/claude-sonnet-4.5)
#   E2E_OUT              where transcripts and logs go               (a new temporary directory)
#   E2E_BUILD            `no` skips `docker compose build`           (yes)
#   E2E_BUILT_WAIT_S     seconds to wait for each session to be built   (1800)
#   E2E_KEEP_STACK       `yes` leaves the stack running at the end      (no: it is removed with `down -v`)
#   E2E_KEEP_HOME        `yes` keeps the temporary HERMES_HOME; what ran on it is still stopped   (no)
#   E2E_KT_TOKEN         use this bearer token instead of a fresh one    (a fresh one; needed to use a kept stack later)
#
# Needs OPENROUTER_API_KEY, VOYAGE_API_KEY and NEO4J_PASSWORD in .env (copied from .env.example). They are read from
# the file and never printed.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT="${E2E_PROJECT:-hermes-kernel-e2e}"
PLUGIN_SOURCE="${E2E_PLUGIN_SOURCE:-}"
HERMES="${E2E_HERMES:-hermes}"
PY="${E2E_PYTHON:-python3}"
MODEL="${E2E_MODEL:-anthropic/claude-sonnet-4.5}"
BUILD="${E2E_BUILD:-yes}"
OUT="${E2E_OUT:-}"
BUILT_WAIT_S="${E2E_BUILT_WAIT_S:-1800}"
PORT=8787

die() { echo "e2e: $*" >&2; exit 2; }

# Refuse, before anything is spawned, unless HERMES_HOME is an absolute path outside the real Hermes home. The rule of
# e2e_guard.py: Hermes strips the value, expands variables in it and takes a profile under the real home as a home, and
# the real home comes from the password database as well as from HOME, which can be unset or point elsewhere.
require_own_hermes_home() {
  local raw="${HERMES_HOME:-}" target home real checked=0
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  [ -n "$raw" ] || die "refusing to run: HERMES_HOME is not set. Hermes migrates the home it starts on, so point it at a throwaway directory first."
  case "$raw" in
    *'$'*) die "refusing to run: HERMES_HOME must be a plain absolute path, got '$raw'." ;;
    /*) ;;
    *) die "refusing to run: HERMES_HOME must be a plain absolute path, got '$raw'." ;;
  esac
  target="$(realpath -m -- "$raw")"
  for home in "$(getent passwd "$(id -u)" | cut -d: -f6)" "${HOME:-}"; do
    [ -n "$home" ] || continue
    checked=1
    real="$(realpath -m -- "$home/.hermes")"
    case "$target/" in
      "$real/"*) die "refusing to run: HERMES_HOME is in the real Hermes home ($real). Use a throwaway directory." ;;
    esac
  done
  [ "$checked" -eq 1 ] || die "refusing to run: cannot tell where the real Hermes home is, so HERMES_HOME cannot be checked."
}
require_own_hermes_home

# The line "HERMES_HOME=<value>" from one process's environ (nothing else of it is read or shown), or nothing.
home_of() { { tr '\0' '\n' <"/proc/$1/environ" | grep -m1 '^HERMES_HOME=' || true; } 2>/dev/null; }

# Stop every process whose HERMES_HOME is "$1" (children included), reading only that one variable from each environ.
stop_home_processes() {
  local home="$1" round pid value found
  for round in 1 2 3 4 5 6; do
    found=0
    for pid in $(ls /proc | grep -E '^[0-9]+$'); do
      [ "$pid" != "$$" ] && [ "$pid" != "$BASHPID" ] || continue
      value="$(home_of "$pid")"
      if [ "$value" = "HERMES_HOME=$home" ]; then
        found=1
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done
    [ $found -eq 1 ] || return 0
    sleep 3
    for pid in $(ls /proc | grep -E '^[0-9]+$'); do
      [ "$pid" != "$$" ] && [ "$pid" != "$BASHPID" ] || continue
      value="$(home_of "$pid")"
      [ "$value" != "HERMES_HOME=$home" ] || kill -KILL "$pid" 2>/dev/null || true
    done
    sleep 1
  done
}
# The pids (one per line) of processes still holding "$1" as their HERMES_HOME.
home_processes() {
  local home="$1" pid value
  for pid in $(ls /proc | grep -E '^[0-9]+$'); do
    [ "$pid" != "$$" ] && [ "$pid" != "$BASHPID" ] || continue
    value="$(home_of "$pid")"
    [ "$value" != "HERMES_HOME=$home" ] || echo "$pid"
  done
}

# ---- guards: nothing below runs until all of these pass ----------------------------------------------------------------

case "$PROJECT" in
  hermes-kernel-e2e*) ;;
  *) die "refusing the project name '$PROJECT': this script deletes a project's volumes, so only names that start with 'hermes-kernel-e2e' are accepted." ;;
esac
[ -f .env ] || die ".env is missing: copy .env.example to .env and fill it in."
if [ "${E2E_CONFIRM:-}" != "yes" ]; then
  cat >&2 <<EOF
e2e: this script deletes data. With E2E_CONFIRM=yes it will:
  - run 'docker compose -p $PROJECT down -v': delete the containers and the neo4j-data, mongo-data and writer-scratch
    volumes of the compose project '$PROJECT', and every memory built in them;
  - create a new temporary Hermes home (it does not touch ~/.hermes or any existing home);
  - start the stack, bound to 127.0.0.1:$PORT, and spend real money on OpenRouter and Voyage.
Nothing has been changed. Run it again with E2E_CONFIRM=yes to go on.
EOF
  exit 2
fi
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  die "something already answers on 127.0.0.1:$PORT (a running stack of yours?). Stop it first; this script will not run beside it."
fi
command -v "$HERMES" >/dev/null || die "no '$HERMES' on PATH (set E2E_HERMES)."
"$PY" -c 'import pexpect' 2>/dev/null || die "'$PY' has no pexpect (pip install pexpect, or set E2E_PYTHON)."
command -v jq >/dev/null || die "jq is needed."
command -v openssl >/dev/null || die "openssl is needed."
avail_mb="$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)"
[ "$avail_mb" -ge 9216 ] || die "less than 9 GiB of RAM available (${avail_mb} MiB): the stack peaked at 8 GiB while it built memory."

FILES=(-f compose.yaml)
if [ -n "${E2E_OVERRIDE:-}" ]; then
  [ -f "$E2E_OVERRIDE" ] || die "E2E_OVERRIDE '$E2E_OVERRIDE' is not a file."
  FILES+=(-f "$E2E_OVERRIDE")
fi
dc() { docker compose -p "$PROJECT" "${FILES[@]}" "$@"; }

# ---- a fresh Hermes home, and the keys, read without printing ----------------------------------------------------------

# Resolved: Hermes gives its gateway the resolved path, and the stop below matches a process by that exact value.
HERMES_HOME="$(realpath -- "$(mktemp -d)")"
export HERMES_HOME
# Until the real trap below: any exit from here on still removes this home.
trap 'stop_home_processes "$HERMES_HOME"; rm -rf "$HERMES_HOME"' EXIT
require_own_hermes_home
[ -n "$OUT" ] || OUT="$(mktemp -d)"
mkdir -p "$OUT"
hermes_said="$("$HERMES" --version 2>&1)" || die "'$HERMES --version' failed: $(head -1 <<<"$hermes_said")"
hermes_version="$(head -1 <<<"$hermes_said")"
case "$hermes_version" in
  "Hermes Agent v0.23.0 "*) ;;
  *) die "this needs Hermes v0.23.0, and '$HERMES' says: $hermes_version" ;;
esac
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
OPENROUTER_VALUE="$(env_value OPENROUTER_API_KEY)"
[ -n "$OPENROUTER_VALUE" ] || die "OPENROUTER_API_KEY must be set in .env."
# A fresh token for this stack only. The shell's value beats .env in compose, and .env itself is left alone.
KT_TOKEN_VALUE="${E2E_KT_TOKEN:-$(openssl rand -hex 24)}"
export KT_TOKEN="$KT_TOKEN_VALUE"
(umask 077 && printf 'OPENROUTER_API_KEY=%s\n' "$OPENROUTER_VALUE" >"$HERMES_HOME/.env")
(umask 077 && printf 'Authorization: Bearer %s\n' "$KT_TOKEN_VALUE" >"$OUT/.auth-header")
unset OPENROUTER_VALUE

START="$(date +%s)"
STAGE=""
RESULTS=()
cleanup() {
  code=$?
  trap - EXIT INT TERM HUP
  CHAT_COST="chat model cost: not available"
  if [ -f "$HERMES_HOME/state.db" ] && command -v sqlite3 >/dev/null; then
    CHAT_COST="chat model cost as Hermes estimated it: $(sqlite3 "$HERMES_HOME/state.db" 'select printf("%.4f", coalesce(sum(estimated_cost_usd), 0)) from sessions' 2>/dev/null || echo unknown) USD"
  fi
  if [ $code -ne 0 ]; then dc logs --no-color --tail 300 >"$OUT/stack-logs.txt" 2>&1 || true; fi
  # Every run, passed or failed: what the turn service refused, and what Hermes itself logged about each model call.
  dc logs --no-color turn-service >"$OUT/turn-service.log" 2>&1 || true
  RESULTS+=("INFO  calls the turn service refused: $(grep -c 'kernel_turns.call_refused' "$OUT/turn-service.log" || true)")
  cp -r "$HERMES_HOME/logs" "$OUT/hermes-logs" 2>/dev/null || true
  rm -f "$OUT/.auth-header"
  # Hermes starts a background gateway for the home it runs on, and it outlives the chat: stop everything on this home
  # first, and only then remove the home (a gateway still running would recreate part of it). E2E_KEEP_HOME keeps the
  # files and nothing that runs.
  stop_home_processes "$HERMES_HOME"
  [ "${E2E_KEEP_HOME:-no}" = "yes" ] || rm -rf "$HERMES_HOME"
  leftover="$(home_processes "$HERMES_HOME")"
  leftover="${leftover//$'\n'/ }"
  if [ -n "${leftover// /}" ]; then
    RESULTS+=("FAIL  cleanup: processes still hold the temporary Hermes home after the stop: $leftover")
    [ $code -ne 0 ] || code=1
    STAGE="${STAGE:-cleanup}"
  else
    RESULTS+=("PASS  cleanup: no process holds the temporary Hermes home")
  fi
  [ "${E2E_KEEP_HOME:-no}" = "yes" ] || rm -rf "$HERMES_HOME"
  echo
  echo "== summary ($(( $(date +%s) - START )) s wall) =="
  for line in "${RESULTS[@]}"; do echo "$line"; done
  if [ $code -ne 0 ]; then
    echo "FAILED at: ${STAGE:-before the first stage}"
    echo "(the stack's last log lines are in $OUT/stack-logs.txt; rerun with E2E_KEEP_STACK=yes E2E_KEEP_HOME=yes to look at the stack)"
  fi
  echo "$CHAT_COST"
  echo "transcripts and logs: $OUT"
  [ "${E2E_KEEP_HOME:-no}" != "yes" ] || echo "the temporary Hermes home is kept: $HERMES_HOME"
  if [ "${E2E_KEEP_STACK:-no}" = "yes" ]; then
    echo "the stack '$PROJECT' is still running (E2E_KEEP_STACK=yes); remove it with: docker compose -p $PROJECT ${FILES[*]} down -v"
  else
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    echo "the stack '$PROJECT' was removed (E2E_KEEP_STACK=yes keeps it)"
  fi
  exit $code
}
trap cleanup EXIT
# Without these the EXIT trap would see status 0 on a signal, and report nothing.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stage() { STAGE="$1"; echo; echo "== $1 =="; }
pass() { RESULTS+=("PASS  $1"); echo "PASS  $1"; }
fail() { RESULTS+=("FAIL  $1"); echo "FAIL  $1"; exit 1; }

now() { date +%s; }
status() { curl -fsS -m 10 -H "@$OUT/.auth-header" "http://127.0.0.1:$PORT/v1/status"; }
spend() { status | jq -r '.spend.charged_usd // "unknown"' 2>/dev/null || echo unknown; }
mongo() { dc exec -T mongo mongosh --quiet kernel_turns --eval "$1"; }

# Wait until every closed session is built (nothing waiting, nothing failed) and the last build is newer than $1.
wait_built() {
  local since="$1" deadline=$(( $(now) + BUILT_WAIT_S )) s
  while [ "$(now)" -lt "$deadline" ]; do
    s="$(status 2>/dev/null || true)"
    if [ -n "$s" ]; then
      if [ "$(jq '.failed | length' <<<"$s")" != "0" ]; then echo "a session failed: $(jq -c .failed <<<"$s")" >&2; return 1; fi
      built="$(jq -r '.last_built_at // empty' <<<"$s")"
      if [ -n "$built" ] && [ "$(jq '.sessions_waiting' <<<"$s")" = "0" ] \
         && [ "$(jq '.founded' <<<"$s")" = "true" ] && [ "$(date -d "$built" +%s)" -ge "$since" ]; then
        return 0
      fi
    fi
    sleep 10
  done
  echo "memory was not built within ${BUILT_WAIT_S} s: $(status 2>&1 | head -c 600)" >&2
  return 1
}

# Poll the turn service's recall until it returns text holding $2 for query $1.
wait_recall() {
  local query="$1" needle="$2" deadline=$(( $(now) + 180 ))
  while [ "$(now)" -lt "$deadline" ]; do
    if curl -fsS -m 20 -H "@$OUT/.auth-header" -H 'Content-Type: application/json' \
         -d "$(jq -n --arg q "$query" '{query: $q, speaker: null}')" "http://127.0.0.1:$PORT/v1/context" 2>/dev/null \
       | jq -r '.context' | grep -qi "$needle"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

chat() { # chat <name> <prompt-json-file> [driver options]: one Hermes conversation, transcript in $OUT/<name>.txt
  local name="$1" prompts="$2"; shift 2
  "$PY" "$ROOT/scripts/e2e_drive.py" "$HERMES" "$OUT/$name.txt" "$prompts" "$@"
}
prompts() { jq -n '$ARGS.positional' --args "$@"; }

# ---- 1. install from nothing ---------------------------------------------------------------------------------------------

stage "1. install from nothing"
echo "hermes: $hermes_version"
dc down -v --remove-orphans
t0="$(now)"
# The way a person does it: install.sh builds and starts the stack, installs the plugin, stores the token and the URL,
# selects the memory provider and enables the plugin last. KT_TOKEN is exported above (the shell's value beats .env).
INSTALL=(./install.sh --mode local --hermes "$HERMES" --project "$PROJECT")
[ -z "${E2E_OVERRIDE:-}" ] || INSTALL+=(--compose-file "$E2E_OVERRIDE")
[ "$BUILD" != "no" ] || INSTALL+=(--no-build)
[ -z "$PLUGIN_SOURCE" ] || INSTALL+=(--plugin-source "$PLUGIN_SOURCE")
"${INSTALL[@]}" || fail "install.sh failed"
echo "install.sh done in $(( $(now) - t0 )) s; the kernel has spent $(spend) USD"
"$HERMES" config set model.provider openrouter >/dev/null
"$HERMES" config set model.default "$MODEL" >/dev/null
# Hermes's own memory and chat search are switched off so that anything recalled later can only have come from here.
"$HERMES" config set memory.memory_enabled false >/dev/null
"$HERMES" config set memory.user_profile_enabled false >/dev/null
"$HERMES" tools disable session_search >/dev/null
# A new home asks once whether to share usage metrics; answer it in the config so the chat starts clean.
"$HERMES" config set telemetry.shared_metrics.enabled false >/dev/null
"$HERMES" config set telemetry.shared_metrics.send false >/dev/null
"$HERMES" config set telemetry.shared_metrics.offer_version 2 --force >/dev/null
"$HERMES" plugins list >"$OUT/plugins-list.txt"
grep -Eq '^│ hermes-kernel +│ enabled +│' "$OUT/plugins-list.txt" || fail "the plugin is not enabled after install (see $OUT/plugins-list.txt)"
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || fail "the turn service does not answer"
[ "$(status | jq -r '.founded')" = "false" ] || fail "a fresh stack says it already has memory"
pass "1. install from nothing (stack and plugin up, no memory yet)"

# ---- 2. a first conversation; end it; wait for founding -----------------------------------------------------------------

stage "2. a first conversation, then founding"
prompts \
  "Hi, I'm Marta. I keep a sourdough starter called Bramble, and I'm allergic to walnuts. Please just greet me." \
  "I'm flying in March with my sister Priya to the capital of Portugal. Which city is that? Reply with only the city." >"$OUT/p2.json"
chat s1 "$OUT/p2.json"
S1_END="$(now)"
# The reply only: the answer is not in the prompt, and the prompt's own line is left out.
awk 'BEGIN{k=0} /^=== PROMPT /{k++; next} k==2' "$OUT/s1.txt" | grep -qi "lisbon" \
  || fail "2. Hermes did not answer the first conversation (see $OUT/s1.txt)"
echo "session 1 closed at $(date -d "@$S1_END" +%T); waiting for the first build"
wait_built "$S1_END" || fail "2. memory was not founded"
MEM_BUILT_FOUND=$(( $(now) - S1_END ))
wait_recall "What is the sourdough starter called?" "bramble" || fail "2. built, but recall does not return the first session (Bramble)"
MEM_DELAY_FOUND=$(( $(now) - S1_END ))
echo "memory delay (session end to recall returning it, founding): ${MEM_DELAY_FOUND} s (built after ${MEM_BUILT_FOUND} s)"
# What was stored: the agent is hermes whatever the Hermes profile is called, and the person is someone else.
handles="$(mongo 'print(JSON.stringify(db.sessions.find().toArray().map(s => s.agent_handles)))' | tr -d '\r')"
replies="$(mongo 'db.messages.countDocuments({speaker: "hermes"})' | tr -d '[:space:]')"
person="$(mongo 'print(JSON.stringify(db.messages.distinct("speaker", {addressees: "hermes"})))' | tr -d '\r')"
echo "stored: agent_handles $handles, replies by hermes $replies, spoken to hermes by $person"
[ "$handles" = '[["hermes"]]' ] || fail "2. the session's agent_handles are $handles, not hermes"
[ "$replies" = "2" ] || fail "2. expected the agent's two replies stored under the speaker hermes, found $replies"
jq -e 'length == 1 and .[0] != "hermes"' <<<"$person" >/dev/null || fail "2. the person's speaker is $person"
pass "2. a first conversation, then founding (${MEM_DELAY_FOUND} s from session end to recall returning it; kernel spend $(spend) USD)"

# ---- 3. a new session recalls it; /kernel against the live stack --------------------------------------------------------
# ---- 4. a phrase with a think step: teach, keep it on its card, use ---------------------------------------------------
# One conversation does both. The chat is asked first, so the recall in stage 3 comes from the first session alone.

stage "3 and 4. recall, /kernel, then teach a phrase, keep it on its card, and use it"
# No "yes" is ever sent here: the phrase is kept by picking Keep it on the card. A plain "yes" is proved by the unit
# tests and the contract test. A model that does not call clarify fails this stage; it does not fall back.
jq -n '[
  "/kernel",
  "What is my sourdough starter called, and what am I allergic to?",
  {prompt: "When I say '"'"'research X'"'"', I want you to look X up on the web and give me a one-line summary. Can you set that up as a phrase? Let me decide whether to keep it.", card: true},
  "research sourdough hydration"
]' >"$OUT/p3.json"
S2_START="$(now)"
chat s2 "$OUT/p3.json" --quiet-s 25 --max-s 300 || fail "4. the card never came up: the model did not call clarify (see $OUT/s2.txt)"
S2_END="$(now)"

# /kernel (stage 2's check, run live) and recall (stage 3)
# The nth prompt's segment of a transcript, without the "=== PROMPT" line (which holds the prompt's own words).
# RUN steps count as segments too (their line is the whole segment).
seg() { awk -v n="$2" 'BEGIN{k=0} /^=== (PROMPT|RUN) /{k++; if ($0 ~ /^=== PROMPT /) next} k==n' "$OUT/$1.txt"; }
seg s2 1 >"$OUT/s2-kernel.txt"
seg s2 2 >"$OUT/s2-recall.txt"
# "Saving." with its capital and full stop: the command's own description ("Is memory saving?") must not count.
grep -q "Saving\." "$OUT/s2-kernel.txt" && ! grep -qi "not saving" "$OUT/s2-kernel.txt" || fail "/kernel did not say Saving (see $OUT/s2-kernel.txt)"
grep -qi "last built" "$OUT/s2-kernel.txt" || fail "/kernel did not show a built session (see $OUT/s2-kernel.txt)"
grep -qi "away" "$OUT/s2-kernel.txt" && fail "/kernel says the service is away (see $OUT/s2-kernel.txt)"
pass "/kernel on the live stack shows Saving and the last build"
grep -qi "bramble" "$OUT/s2-recall.txt" && grep -qi "walnut" "$OUT/s2-recall.txt" \
  || fail "3. the new session did not recall Bramble and walnuts (see $OUT/s2-recall.txt)"
pass "3. a new session recalls the first one (Bramble, walnuts)"

# 4: the card showed the read-back, the service's own log has the one confirm the click made, and the phrase reached
# done, with the think step done by the model. The hook takes only an exact choice, so that 200 also shows that the
# "(Recommended)" label on the screen did not come back in the answer.
grep -Eq 'Keep *this *phrase\?' "$OUT/s2.txt" || fail "4. the card did not show the read-back (see $OUT/s2.txt)"
dc logs --no-color turn-service >"$OUT/turn-service-stage4.log" 2>&1 || true
confirms="$(grep -Ec 'POST /v1/teaching/[^ /]+/confirm HTTP/[0-9.]+" 200' "$OUT/turn-service-stage4.log" || true)"
echo "confirms the turn service answered 200: $confirms"
[ "$confirms" = "1" ] || fail "4. expected one confirm from the card in the service's log, found $confirms (see $OUT/turn-service-stage4.log)"
# The 200 must have come from the card: Hermes's own log says the plugin's hook saw the answer. (The logs are read
# here from the throwaway home; a Hermes that has not flushed them yet is waited for a moment.)
card_logged=no
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if grep -rqsF 'hermes-kernel: a card was answered: chosen' "$HERMES_HOME/logs"; then card_logged=yes; break; fi
  sleep 1
done
[ "$card_logged" = "yes" ] || fail "4. the Hermes log has no 'a card was answered: chosen', so the 200 may not have come from the card (see $OUT/hermes-logs)"
pass "4. the phrase was kept on its card, with no yes sent (one confirm, 200, in the service's log, and the card's own log line)"
uses="$(mongo 'db.uses.countDocuments({outcome: "ok"})' | tr -d '[:space:]')"
think_len="$(mongo 'const r = db.ledger.find({tool: "think", verdict: "committed"}).toArray(); print(r.length ? JSON.stringify(r[r.length-1].value_json || r[r.length-1].value).length : 0)' | tr -d '[:space:]')"
search_ok="$(mongo 'db.ledger.countDocuments({tool: "web_search", verdict: "committed"})' | tr -d '[:space:]')"
echo "phrase uses ok: $uses; committed web_search steps: $search_ok; length of the last committed think value: $think_len"
mongo 'db.ledger.find({}, {_id: 0, turn_id: 1, step_id: 1, tool: 1, verdict: 1, detail: 1, at: 1}).toArray()' >"$OUT/ledger.txt" || true
[ "${search_ok:-0}" -ge 1 ] || fail "4. the phrase's web_search step never committed (see $OUT/ledger.txt)"
[ "${uses:-0}" -ge 1 ] || fail "4. no taught phrase reached done (see $OUT/s2.txt and $OUT/ledger.txt)"
[ "${think_len:-0}" -gt 20 ] || fail "4. the think step has no result from the model (see $OUT/ledger.txt)"
pass "4. a taught phrase with a think step reached done (web_search steps committed: $search_ok)"

# Memory delay again, on an append: from the end of this session to being built.
wait_built "$S2_END" || fail "3/4. the second session was not built"
MEM_BUILT_APPEND=$(( $(now) - S2_END ))
echo "append of the second session built after ${MEM_BUILT_APPEND} s"

# ---- 5. the stack stops mid-conversation; Hermes still answers; it comes back; nothing is lost --------------------------

stage "5. stop the stack mid-conversation, then restart it"
STOP_CMD="docker compose -p $PROJECT ${FILES[*]} stop"
UP_CMD="docker compose -p $PROJECT ${FILES[*]} up -d --wait"
# Steps between the prompts are shell commands the driver runs mid-conversation; each must exit 0 or the run fails.
# After the stop the service must really be unreachable; before the restart the spool on disk must hold the turn.
DOWN_CHECK="if curl -s -m 3 -o /dev/null http://127.0.0.1:$PORT/healthz; then echo 'the service still answers after the stop'; exit 1; fi"
SPOOL_CHECK='test -s "$HERMES_HOME/hermes-kernel-spool.jsonl" || { echo "nothing in the spool while the stack is stopped"; exit 1; }'
jq -n --arg stop "$STOP_CMD" --arg up "$UP_CMD" --arg down "$DOWN_CHECK" --arg spool "$SPOOL_CHECK" '[
  "What is the capital of France? Reply with only the city.",
  {run: $stop},
  {run: $down},
  "My neighbour has a cat called Pumpernickel. Also: what is the capital of Italy? Reply with only the city, first.",
  {run: $spool},
  {run: $up},
  "What is the capital of Spain? Reply with only the city."
]' >"$OUT/p5.json"
S3_START="$(now)"
chat s3 "$OUT/p5.json" --quiet-s 20 || fail "5. a step between the prompts failed: the stack was not really stopped, or nothing was spooled (see $OUT/s3.txt)"
S3_END="$(now)"
seg s3 1 | grep -qi "paris" || fail "5. no answer before the stop (see $OUT/s3.txt)"
seg s3 2 | grep -q "exit 0" || fail "5. the stop failed (see $OUT/s3.txt)"
seg s3 4 | grep -qi "rome" || fail "5. Hermes did not answer while the stack was stopped (see $OUT/s3.txt)"
seg s3 7 | grep -qi "madrid" || fail "5. Hermes did not answer after the restart (see $OUT/s3.txt)"
pass "5a. Hermes answered with the stack stopped, and after the restart"
wait_built "$S3_END" || fail "5. the session with the outage was not built"
MEM_BUILT_S3=$(( $(now) - S3_END ))
# A built session's messages hold no text in the store, so "nothing lost" is counted by messages: three user messages and
# three replies, the middle pair sent while the stack was stopped; and the recall below must return what was said then.
session_row="$(mongo 'const s = db.sessions.find().sort({closed_at: -1}).limit(1).toArray()[0]; print(s._id + " " + s.message_count + " " + db.messages.countDocuments({session_id: s._id}))' | tr -d '\r')"
echo "newest session (id, message_count, messages stored): $session_row"
[ "$(awk '{print $2 " " $3}' <<<"$session_row")" = "6 6" ] || fail "5. messages are missing after the replay: expected 6 of 6 (see $OUT/s3.txt)"
wait_recall "What is the neighbour's cat called?" "pumpernickel" || fail "5. built, but recall does not return the session that had the outage (Pumpernickel)"
MEM_DELAY_APPEND=$(( $(now) - S3_END ))
echo "memory delay (session end to recall returning it, append): ${MEM_DELAY_APPEND} s (built after ${MEM_BUILT_S3} s)"
RESULTS+=("INFO  memory delay, session end to recall returning it: founding ${MEM_DELAY_FOUND} s, append ${MEM_DELAY_APPEND} s; to built: founding ${MEM_BUILT_FOUND} s, append of session 2 ${MEM_BUILT_APPEND} s, append of session 3 ${MEM_BUILT_S3} s")
pass "5b. the spool replayed: all six messages of the session with the outage arrived, and recall returns what was said while the stack was stopped"

echo
echo "kernel spend at the end: $(spend) USD"
RESULTS+=("INFO  kernel spend at the end: $(spend) USD (the chat model's cost is reported separately below)")
RESULTS+=("INFO  wall time: $(( $(now) - START )) s")
