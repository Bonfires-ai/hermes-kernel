#!/usr/bin/env bash
# The whole thing from nothing, the way the README says to do it, on a real Hermes and a real stack.
#
#   E2E_CONFIRM=yes scripts/e2e.sh
#
# THIS SCRIPT DELETES DATA. It starts with `docker compose down -v` on its own project (E2E_PROJECT, default
# hermes-kernel-e2e): that project's Neo4j and Mongo volumes, and with them every memory built under it. It refuses the
# name `hermes-kernel` (the README's stack) and refuses to run while something answers on port 8787, so it cannot touch
# a stack you use. It never reads or writes ~/.hermes: Hermes runs in a fresh temporary home, removed at the end.
#
# It spends real money (a few cents to build the memory, and a chat model for about a dozen turns; the target is under
# $1) and takes about half an hour, most of it waiting for memory to build.
#
# Settings (all optional except E2E_CONFIRM):
#   E2E_PROJECT          compose project name                        (hermes-kernel-e2e)
#   E2E_OVERRIDE         one extra compose file                      (none)
#   E2E_PLUGIN_SOURCE    what `hermes plugins install` is given      (Bonfires-ai/hermes-kernel/plugin)
#   E2E_HERMES           the hermes command                          (hermes)
#   E2E_PYTHON           a python that has pexpect                   (python3)
#   E2E_MODEL            the chat model (it must be good at calling tools; a small one may reach for Hermes's own
#                        skill tool instead of teaching a phrase)  (anthropic/claude-sonnet-4.5)
#   E2E_OUT              where transcripts and logs go               (a new temporary directory)
#   E2E_BUILD            `no` skips `docker compose build`           (yes)
#   E2E_DOWN             `yes` runs `down -v` again at the end       (no: the stack is left running to look at)
#   E2E_KEEP_HOME        `yes` keeps the temporary HERMES_HOME       (no)
#
# Needs the keys in .env (copied from .env.example). They are read from the file and never printed.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT="${E2E_PROJECT:-hermes-kernel-e2e}"
PLUGIN_SOURCE="${E2E_PLUGIN_SOURCE:-Bonfires-ai/hermes-kernel/plugin}"
HERMES="${E2E_HERMES:-hermes}"
PY="${E2E_PYTHON:-python3}"
MODEL="${E2E_MODEL:-anthropic/claude-sonnet-4.5}"
BUILD="${E2E_BUILD:-yes}"
OUT="${E2E_OUT:-$(mktemp -d)}"
BUILT_WAIT_S="${E2E_BUILT_WAIT_S:-1800}"
PORT=8787

die() { echo "e2e: $*" >&2; exit 2; }

# ---- guards: nothing below runs until all of these pass ----------------------------------------------------------------

[ "$PROJECT" != "hermes-kernel" ] || die "refusing the project name 'hermes-kernel': that is the README's stack. Use another E2E_PROJECT."
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
avail_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
[ "$avail_kb" -ge 8000000 ] || die "less than 8 GB of RAM available ($((avail_kb / 1024)) MB): the stack needs about 8 GiB while it builds memory."

FILES=(-f compose.yaml)
if [ -n "${E2E_OVERRIDE:-}" ]; then
  [ -f "$E2E_OVERRIDE" ] || die "E2E_OVERRIDE '$E2E_OVERRIDE' is not a file."
  FILES+=(-f "$E2E_OVERRIDE")
fi
dc() { docker compose -p "$PROJECT" "${FILES[@]}" "$@"; }

# ---- a fresh Hermes home, and the keys, read without printing ----------------------------------------------------------

HERMES_HOME="$(mktemp -d)"
export HERMES_HOME
mkdir -p "$OUT"
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
KT_TOKEN_VALUE="$(env_value KT_TOKEN)"
OPENROUTER_VALUE="$(env_value OPENROUTER_API_KEY)"
[ -n "$KT_TOKEN_VALUE" ] && [ -n "$OPENROUTER_VALUE" ] || die "KT_TOKEN and OPENROUTER_API_KEY must be set in .env."
(umask 077 && printf 'OPENROUTER_API_KEY=%s\nHERMES_KERNEL_TOKEN=%s\n' "$OPENROUTER_VALUE" "$KT_TOKEN_VALUE" >"$HERMES_HOME/.env")
(umask 077 && printf 'Authorization: Bearer %s\n' "$KT_TOKEN_VALUE" >"$OUT/.auth-header")
unset OPENROUTER_VALUE

START="$(date +%s)"
STAGE=""
RESULTS=()
cleanup() {
  code=$?
  CHAT_COST="chat model cost: not available"
  if [ -f "$HERMES_HOME/state.db" ] && command -v sqlite3 >/dev/null; then
    CHAT_COST="chat model cost as Hermes estimated it: $(sqlite3 "$HERMES_HOME/state.db" 'select printf("%.4f", coalesce(sum(estimated_cost_usd), 0)) from sessions' 2>/dev/null || echo unknown) USD"
  fi
  rm -f "$OUT/.auth-header"
  [ "${E2E_KEEP_HOME:-no}" = "yes" ] || rm -rf "$HERMES_HOME"
  echo
  echo "== summary ($(( $(date +%s) - START )) s wall) =="
  for line in "${RESULTS[@]}"; do echo "$line"; done
  [ $code -eq 0 ] || echo "FAILED at: ${STAGE:-before the first stage}"
  echo "$CHAT_COST"
  echo "transcripts and logs: $OUT"
  if [ "${E2E_DOWN:-no}" = "yes" ]; then dc down -v --remove-orphans >/dev/null 2>&1 || true; else
    echo "the stack '$PROJECT' is still running; remove it with: docker compose -p $PROJECT ${FILES[*]} down -v"
  fi
}
trap cleanup EXIT

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
echo "hermes: $("$HERMES" --version 2>&1 | head -1)"
dc down -v --remove-orphans
[ "$BUILD" = "no" ] || dc build
t0="$(now)"
dc up -d --wait
echo "stack up in $(( $(now) - t0 )) s; the kernel has spent $(spend) USD"
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
"$HERMES" plugins install "$PLUGIN_SOURCE" --enable
"$HERMES" plugins enable hermes-kernel
"$HERMES" plugins list | tee "$OUT/plugins-list.txt" | grep -i "hermes-kernel" | grep -qi "enabled" \
  || fail "the plugin is not enabled after install"
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || fail "the turn service does not answer"
[ "$(status | jq -r '.founded')" = "false" ] || fail "a fresh stack says it already has memory"
pass "1. install from nothing (stack and plugin up, no memory yet)"

# ---- 2. a first conversation; end it; wait for founding -----------------------------------------------------------------

stage "2. a first conversation, then founding"
prompts \
  "Hi, I'm Marta. I keep a sourdough starter called Bramble, and I'm allergic to walnuts. Just say hello back." \
  "I'm flying to Lisbon in March with my sister Priya. Please just say ok." >"$OUT/p2.json"
chat s1 "$OUT/p2.json"
S1_END="$(now)"
grep -qi "hello\|hi \|hi,\|marta" "$OUT/s1.txt" || fail "2. Hermes did not answer the first conversation (see $OUT/s1.txt)"
echo "session 1 closed at $(date -d "@$S1_END" +%T); waiting for the first build"
wait_built "$S1_END" || fail "2. memory was not founded"
MEM_BUILT_FOUND=$(( $(now) - S1_END ))
wait_recall "What is the sourdough starter called?" "bramble" || fail "2. built, but recall does not return the first session (Bramble)"
MEM_DELAY_FOUND=$(( $(now) - S1_END ))
echo "memory delay (session end to recall returning it, founding): ${MEM_DELAY_FOUND} s (built after ${MEM_BUILT_FOUND} s)"
pass "2. a first conversation, then founding (${MEM_DELAY_FOUND} s from session end to recall returning it; kernel spend $(spend) USD)"

# ---- 3. a new session recalls it; /kernel against the live stack --------------------------------------------------------
# ---- 4. a phrase with a think step: teach, confirm, use -----------------------------------------------------------------
# One conversation does both. The chat is asked first, so the recall in stage 3 comes from the first session alone.

stage "3 and 4. recall, /kernel, then teach a phrase and use it"
prompts \
  "/kernel" \
  "What is my sourdough starter called, and what am I allergic to?" \
  "When I say 'research X', I want you to look X up on the web and give me a one-line summary. Can you set that up as a phrase? Show me what you drafted and wait for my yes." \
  "yes" \
  "research sourdough hydration" >"$OUT/p3.json"
S2_START="$(now)"
chat s2 "$OUT/p3.json" --quiet-s 25 --max-s 300
S2_END="$(now)"

# /kernel (stage 2's check, run live) and recall (stage 3)
seg() { awk -v n="$1" 'BEGIN{k=0} /^=== PROMPT /{k++} k==n' "$OUT/s2.txt"; }
seg 1 >"$OUT/s2-kernel.txt"
seg 2 >"$OUT/s2-recall.txt"
# "Saving." with its capital and full stop: the command's own description ("Is memory saving?") must not count.
grep -q "Saving\." "$OUT/s2-kernel.txt" && ! grep -qi "not saving" "$OUT/s2-kernel.txt" || fail "/kernel did not say Saving (see $OUT/s2-kernel.txt)"
grep -qi "last built" "$OUT/s2-kernel.txt" || fail "/kernel did not show a built session (see $OUT/s2-kernel.txt)"
grep -qi "away" "$OUT/s2-kernel.txt" && fail "/kernel says the service is away (see $OUT/s2-kernel.txt)"
pass "/kernel on the live stack shows Saving and the last build"
grep -qi "bramble" "$OUT/s2-recall.txt" && grep -qi "walnut" "$OUT/s2-recall.txt" \
  || fail "3. the new session did not recall Bramble and walnuts (see $OUT/s2-recall.txt)"
pass "3. a new session recalls the first one (Bramble, walnuts)"

# 4: the phrase must have reached done, with the think step done by the model.
uses="$(mongo 'db.uses.countDocuments({outcome: "ok"})' | tr -d '[:space:]')"
think_len="$(mongo 'const r = db.ledger.find({tool: "think", verdict: "committed"}).toArray(); print(r.length ? JSON.stringify(r[r.length-1].value_json || r[r.length-1].value).length : 0)' | tr -d '[:space:]')"
search_ok="$(mongo 'db.ledger.countDocuments({tool: "web_search", verdict: "committed"})' | tr -d '[:space:]')"
echo "phrase uses ok: $uses; committed web_search steps: $search_ok; length of the last committed think value: $think_len"
mongo 'db.ledger.find({}, {_id: 0, turn_id: 1, step_id: 1, tool: 1, verdict: 1, detail: 1, at: 1}).toArray()' >"$OUT/ledger.txt" || true
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
jq -n --arg stop "$STOP_CMD" --arg up "$UP_CMD" '[
  "What is 20 plus 22? Reply with only the number.",
  {run: $stop},
  "My neighbour has a cat called Pumpernickel. Also: what is 10 plus 8? Reply with the number first.",
  {run: $up},
  "What is 30 plus 12? Reply with only the number."
]' >"$OUT/p5.json"
S3_START="$(now)"
chat s3 "$OUT/p5.json" --quiet-s 20
S3_END="$(now)"
seg3() { awk -v n="$1" 'BEGIN{k=0} /^=== (PROMPT|RUN) /{k++} k==n' "$OUT/s3.txt"; }
seg3 1 | grep -q "42" || fail "5. no answer before the stop (see $OUT/s3.txt)"
seg3 3 | grep -q "18" || fail "5. Hermes did not answer while the stack was stopped (see $OUT/s3.txt)"
seg3 5 | grep -q "42" || fail "5. Hermes did not answer after the restart (see $OUT/s3.txt)"
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
