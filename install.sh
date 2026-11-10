#!/usr/bin/env bash
# One script that sets up hermes-kernel for a person's Hermes: the local stack, the plugin, and the token that joins them.
#
#   ./install.sh [--mode local|hosted] [--hermes PATH] [--project NAME] [--compose-file FILE] [--no-build]
#                [--plugin-source SOURCE] [--url URL]
#
# Without --mode it asks: local (a Docker stack on this machine) or hosted (a Bonfires key, no Docker).
# Each option has an environment equivalent: HERMES_KERNEL_MODE, HERMES_BIN, HERMES_KERNEL_PROJECT,
# HERMES_KERNEL_COMPOSE_FILE, HERMES_KERNEL_NO_BUILD, HERMES_KERNEL_PLUGIN_SOURCE, HERMES_KERNEL_URL.
# Keys come from the environment only (local: OPENROUTER_API_KEY, VOYAGE_API_KEY, NEO4J_PASSWORD, KT_TOKEN;
# hosted: HERMES_KERNEL_TOKEN) or a hidden prompt, never from the command line.
#
# This writes to your real Hermes home ($HERMES_HOME, default ~/.hermes). It never installs or upgrades Hermes.
# Bash 3.2 and POSIX tools only: the same file runs on a Mac.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)

WANT_HERMES="v0.23.0"
LOCAL_URL="http://127.0.0.1:8787"
HOSTED_URL="https://api.staging-bonfires.dev/kernel"

MODE=${HERMES_KERNEL_MODE:-}
HERMES_BIN=${HERMES_BIN:-hermes}
PROJECT=${HERMES_KERNEL_PROJECT:-hermes-kernel}
COMPOSE_FILE=${HERMES_KERNEL_COMPOSE_FILE:-$ROOT/compose.yaml}
NO_BUILD=${HERMES_KERNEL_NO_BUILD:-}
PLUGIN_SOURCE=${HERMES_KERNEL_PLUGIN_SOURCE:-}
URL=${HERMES_KERNEL_URL:-}
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
export HERMES_HOME
UPDATED=""

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- small file helpers

# Temp files live next to their target; one is tracked here so an interrupt or a failure never leaves a copy of a
# secrets file behind.
HK_TMP=""
HK_CFG=""
HK_BODY=""
HK_ERR=""
cleanup() {
  if [ -n "$HK_TMP" ]; then rm -f "$HK_TMP"; fi
  if [ -n "$HK_CFG" ]; then rm -f "$HK_CFG"; fi
  if [ -n "$HK_BODY" ]; then rm -f "$HK_BODY"; fi
  if [ -n "$HK_ERR" ]; then rm -f "$HK_ERR"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# new_tmp DIR: a private temp file in DIR, tracked for cleanup.
new_tmp() {
  HK_TMP=$(mktemp "$1/$2.XXXXXX") || die "could not create a temporary file in $1"
  chmod 600 "$HK_TMP"
}

# env_get FILE KEY: the value compose reads for KEY: the last matching line (an "export " prefix and leading spaces
# are fine), a quoted value up to its closing quote, an unquoted one up to " #". Nothing if absent.
env_get() {
  [ -f "$1" ] || return 0
  HK_K=$2 awk '
    BEGIN { k = ENVIRON["HK_K"]; found = 0 }
    {
      if (match($0, "^[ \t]*(export[ \t]+)?" k "=")) { line = substr($0, RLENGTH + 1); found = 1 }
    }
    END {
      if (!found) exit
      v = line
      sub(/\r$/, "", v)
      sub(/^[ \t]+/, "", v)
      c = substr(v, 1, 1)
      if (c == "\"" || c == "\047") {
        rest = substr(v, 2); i = index(rest, c)
        v = (i ? substr(rest, 1, i - 1) : rest)
      } else {
        if (v ~ /^#/) v = ""
        sub(/[ \t]+#.*$/, "", v)
        sub(/[ \t]+$/, "", v)
      }
      print v
    }
  ' "$1"
}

# env_fill FILE KEY VALUE: set KEY only when its value is blank or the key is absent, replacing the last matching
# line in place. Returns 0 when it wrote, 1 when it kept the value. The value travels in the environment of awk.
env_fill() {
  _file=$1
  [ -z "$(env_get "$_file" "$2")" ] || return 1
  new_tmp "$(dirname "$_file")" .hk
  if HK_K=$2 HK_V=$3 awk '
      BEGIN { k = ENVIRON["HK_K"]; v = ENVIRON["HK_V"]; last = 0 }
      {
        L[NR] = $0
        if (match($0, "^[ \t]*(export[ \t]+)?" k "=")) last = NR
      }
      END {
        for (i = 1; i <= NR; i++) { if (i == last) print k "=" v; else print L[i] }
        if (!last) print k "=" v
      }
    ' "$_file" >"$HK_TMP"; then
    mv "$HK_TMP" "$_file" || die "could not update $_file"
    HK_TMP=""
  else
    die "could not update $_file"
  fi
}

gen_secret() {
  od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
}

# ---------------------------------------------------------------- the checks

check_hermes() {
  command -v "$HERMES_BIN" >/dev/null 2>&1 ||
    die "hermes was not found. This script never installs Hermes: install Hermes $WANT_HERMES first, then run it again."
  _ver=$("$HERMES_BIN" --version 2>&1 | head -n 1) || true
  if ! printf '%s\n' "$_ver" | grep -Eq "v0\\.23\\.0([[:space:]]|\$)"; then
    die "this needs Hermes $WANT_HERMES, and hermes says \"$_ver\". This script never installs or upgrades Hermes."
  fi
}

stack_running() {
  [ -n "$(docker compose -p "$PROJECT" -f "$COMPOSE_FILE" ps -q 2>/dev/null || true)" ]
}

check_local() {
  [ "$(uname -sm)" = "Linux x86_64" ] ||
    die "the local stack runs on Linux x86_64 only, and this machine is \"$(uname -sm)\"."
  command -v docker >/dev/null 2>&1 || die "docker was not found."
  docker compose version >/dev/null 2>&1 || die "'docker compose' is not available: install Docker with Compose v2."
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"

  _running=""
  if stack_running; then _running=1; fi

  if [ -z "$_running" ]; then
    _kb=$(awk '/^MemAvailable:/ { print $2 }' "${HK_PROC_MEMINFO:-/proc/meminfo}" 2>/dev/null || true)
    case "$_kb" in
      '' | *[!0-9]*) die "could not read available memory from ${HK_PROC_MEMINFO:-/proc/meminfo}." ;;
    esac
    [ "$_kb" -ge 9437184 ] ||
      die "building memory needs 9 GiB of RAM available, and $((_kb / 1048576)) GiB is. Close something, or stop other containers."
  fi

  _mkver=$(env_get "$ENV_FILE" MK_VERSION)
  _mkver=${_mkver:-2.0.1}
  if ! docker image inspect "hermes-kernel-mk:$_mkver" >/dev/null 2>&1; then
    _root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    [ -n "$_root" ] || die "could not find Docker's data directory."
    _free=$(df -Pk "$_root" | awk 'NR == 2 { print $4 }')
    case "$_free" in
      '' | *[!0-9]*) die "could not read free disk space on $_root." ;;
    esac
    [ "$_free" -ge 8388608 ] ||
      die "the images need 8 GiB free where Docker keeps its data ($_root), and $((_free / 1048576)) GiB is free."
  fi
}

# ---------------------------------------------------------------- keys and .env

# resolve_secrets: every key the stack needs must come from the shell, the existing .env, or a hidden prompt.
# Nothing is written until all are in hand.
resolve_secrets() {
  _missing=""
  for _name in OPENROUTER_API_KEY VOYAGE_API_KEY; do
    eval "_have=\${$_name:-}"
    [ -z "$_have" ] || continue
    [ -z "$(env_get "$ENV_FILE" "$_name")" ] || continue
    if [ -t 0 ]; then
      printf '%s (input hidden): ' "$_name" >&2
      read -r -s _have || true
      printf '\n' >&2
      if [ -n "$_have" ]; then
        eval "$_name=\$_have"
        continue
      fi
    fi
    _missing="$_missing $_name"
  done
  [ -z "$_missing" ] ||
    die "no value for:$_missing. Set them in the environment (they are never taken from the command line) and run again."
  if [ -n "${NEO4J_PASSWORD:-}" ]; then
    case "$NEO4J_PASSWORD" in
      */*) die "NEO4J_PASSWORD cannot contain \"/\" (Neo4j reads NEO4J_AUTH as user/password)." ;;
    esac
  fi
}

write_env() {
  if [ -f "$ENV_FILE" ]; then
    say "kept $ENV_FILE (values already there are never overwritten; blanks are filled)"
  else
    [ -f "$ROOT/.env.example" ] || die "$ROOT/.env.example not found."
    (umask 077 && cp "$ROOT/.env.example" "$ENV_FILE")
  fi
  chmod 600 "$ENV_FILE"
  _filled=""
  for _name in NEO4J_PASSWORD OPENROUTER_API_KEY VOYAGE_API_KEY KT_TOKEN; do
    eval "_val=\${$_name:-}"
    if [ "$_name" = KT_TOKEN ]; then
      # A token in the shell wins for the Hermes side, as it does in compose, and is not written down.
      [ -z "$_val" ] || continue
      _val=$(gen_secret)
    elif [ "$_name" = NEO4J_PASSWORD ] && [ -z "$_val" ]; then
      _val=$(gen_secret)
    fi
    [ -n "$_val" ] || continue
    if env_fill "$ENV_FILE" "$_name" "$_val"; then _filled="$_filled $_name"; fi
  done
  [ -z "$_filled" ] || say "filled blank values in $ENV_FILE:$_filled"
  if [ -n "${KT_TOKEN:-}" ]; then
    HK_TOKEN=$KT_TOKEN
  else
    HK_TOKEN=$(env_get "$ENV_FILE" KT_TOKEN)
  fi
  [ -n "$HK_TOKEN" ] || die "no KT_TOKEN to give Hermes."
}

# ---------------------------------------------------------------- the stack

stack_up() {
  if [ -z "$NO_BUILD" ]; then
    say "building the images (slow the first time: it builds the memory-kernel image and downloads its models)"
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" build
  fi
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d --wait
  _tries=0
  until curl -fsS "$LOCAL_URL/healthz" >/dev/null; do
    _tries=$((_tries + 1))
    [ "$_tries" -lt 5 ] || die "the stack is up but $LOCAL_URL/healthz does not answer."
    sleep 2
  done
  say "the stack is healthy at $LOCAL_URL"
}

# ---------------------------------------------------------------- Hermes side (hosted mode reuses these)

install_plugin() {
  if [ -d "$HERMES_HOME/plugins/hermes-kernel" ]; then
    UPDATED=1
    say "the plugin is installed already: updating it"
    "$HERMES_BIN" plugins update hermes-kernel
    return 0
  fi
  _src=$PLUGIN_SOURCE
  if [ -z "$_src" ]; then
    if [ -e "$ROOT/.git" ]; then _src="file://$ROOT#plugin"; else _src="Bonfires-ai/hermes-kernel/plugin"; fi
  fi
  case "$_src" in
    file://*)
      say "note: Hermes will warn about a local URL. It clones the commit, so it installs HEAD, not uncommitted edits."
      ;;
  esac
  "$HERMES_BIN" plugins install "$_src" --enable
}

# write_hermes_token TOKEN: HERMES_KERNEL_TOKEN in Hermes's .env, replacing any earlier line, keeping every other line.
write_hermes_token() {
  [ -n "${1:-}" ] || die "write_hermes_token needs a token."
  mkdir -p "$HERMES_HOME"
  _file=$HERMES_HOME/.env
  new_tmp "$HERMES_HOME" .env
  if [ -f "$_file" ]; then _src=$_file; else _src=/dev/null; fi
  if HK_V=$1 awk '
      BEGIN { v = ENVIRON["HK_V"]; done = 0 }
      /^(export[ \t]+)?HERMES_KERNEL_TOKEN=/ { if (!done) { print "HERMES_KERNEL_TOKEN=" v; done = 1 } next }
      { print }
      END { if (!done) print "HERMES_KERNEL_TOKEN=" v }
    ' "$_src" >"$HK_TMP"; then
    mv "$HK_TMP" "$_file" || die "could not write the token to $_file"
    HK_TMP=""
    chmod 600 "$_file"
  else
    die "could not write the token to $_file"
  fi
}

# write_url URL: where the plugin finds the turn service (hermes-kernel.json in Hermes's home).
write_url() {
  case "$1" in
    *[\"\\]*) die "the URL cannot contain quotes or backslashes." ;;
  esac
  mkdir -p "$HERMES_HOME"
  new_tmp "$HERMES_HOME" .hermes-kernel
  printf '{"url": "%s"}\n' "$1" >"$HK_TMP"
  mv "$HK_TMP" "$HERMES_HOME/hermes-kernel.json" || die "could not write $HERMES_HOME/hermes-kernel.json"
  HK_TMP=""
}

finish() {
  if [ -n "$UPDATED" ]; then
    say "If Hermes was already running, restart its gateway so it loads the update: hermes gateway restart --detach"
  fi
  say "Start a new Hermes session and type /kernel"
}

# resolve_profile: Hermes re-homes onto the sticky profile (hermes profile use) unless the exported home is already a
# profile directory. Do the same, so the plugin, the token and the URL all land in the one home Hermes will read.
resolve_profile() {
  [ "$(basename "$(dirname "$HERMES_HOME")")" != profiles ] || return 0
  [ -f "$HERMES_HOME/active_profile" ] || return 0
  _name=$(head -n 1 "$HERMES_HOME/active_profile" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case "$_name" in
    '' | default) return 0 ;;
    */* | *\\* | *..*) die "the active Hermes profile name \"$_name\" is not usable (no slashes or \"..\")." ;;
  esac
  HERMES_HOME=$HERMES_HOME/profiles/$_name
  export HERMES_HOME
  say "using the active Hermes profile \"$_name\" ($HERMES_HOME)"
}

# ---------------------------------------------------------------- the local path

run_local() {
  ENV_FILE=$(dirname "$COMPOSE_FILE")/.env
  resolve_profile
  check_hermes
  check_local
  resolve_secrets
  write_env
  # A token in the shell wins in compose too, so the stack must see it.
  if [ -n "${KT_TOKEN:-}" ]; then export KT_TOKEN; fi
  stack_up
  install_plugin
  write_hermes_token "$HK_TOKEN"
  write_url "${URL:-$LOCAL_URL}"
  "$HERMES_BIN" config set memory.provider hermes-kernel
  # Last, so a running gateway reloads its plugins and finds the token already there.
  "$HERMES_BIN" plugins enable hermes-kernel
  finish
}

# ---------------------------------------------------------------- the hosted path

ask_mode() {
  while :; do
    printf 'Run hermes-kernel hosted (a Bonfires key, no Docker) or local (a Docker stack on this machine)? [hosted/local]: ' >&2
    _ans=""
    read -r _ans || [ -n "$_ans" ] || die "no mode was given and there is nothing to ask: pass --mode local or --mode hosted."
    _ans=$(printf '%s' "$_ans" | tr 'A-Z' 'a-z' | tr -d ' \t')
    case "$_ans" in
      local | l)
        MODE=local
        return 0
        ;;
      hosted | h)
        MODE=hosted
        return 0
        ;;
    esac
    say "please answer hosted or local."
  done
}

# check_url URL: the key is a bearer, so a plain http:// URL is refused unless it stays on this machine.
check_url() {
  case "$1" in
    *[!A-Za-z0-9._~:/?#@%+=\&,-]*) die "the URL has characters that do not belong in one." ;;
    https://*)
      _scheme=https
      _rest=${1#https://}
      ;;
    http://*)
      _scheme=http
      _rest=${1#http://}
      ;;
    *) die "the URL must start with https:// (got \"$1\")." ;;
  esac
  _auth=${_rest%%[/?#]*}
  [ -n "$_auth" ] || die "the URL has no host."
  case "$_auth" in
    *@*) die "the URL must not carry a user name or password (an @ before the host)." ;;
  esac
  _host=${_auth%%:*}
  case "${_auth#"$_host"}" in
    '' | :[0-9]*) ;;
    *) die "the URL's port is not a number." ;;
  esac
  case "${_auth#"$_host"}" in
    :*[!0-9]*) die "the URL's port is not a number." ;;
  esac
  if [ "$_scheme" = http ] && [ "$_host" != localhost ] && [ "$_host" != 127.0.0.1 ]; then
    die "refusing $1: your key would travel unencrypted. Use an https:// URL (http:// is allowed only for localhost)."
  fi
}

resolve_key() {
  HK_KEY=${HERMES_KERNEL_TOKEN:-}
  if [ -z "$HK_KEY" ]; then
    printf 'Your Bonfires API key (input hidden): ' >&2
    read -r -s HK_KEY || true
    printf '\n' >&2
  fi
  HK_KEY=$(printf '%s' "$HK_KEY" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  [ -n "$HK_KEY" ] || die "no Bonfires API key. Set HERMES_KERNEL_TOKEN in the environment (it is never taken from the command line) and run again."
  case "$HK_KEY" in
    *[!A-Za-z0-9._~+/=:-]*) die "that key has characters a Bonfires key does not have. Check that you copied all of it and nothing else." ;;
  esac
}

# json_field NAME FILE: the first string value of "NAME" in a one-object JSON file. No jq, no Python.
json_field() {
  tr '\n' ' ' <"$2" | sed -n -E 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"(([^"\\]|\\.)*)".*/\1/p' | tr -d '[:cntrl:]'
}

# The service's own wording of why it said no, when it gave one.
detail_of() {
  _d=$(json_field detail "$HK_BODY")
  # A front that is not the service itself (Bonfires' API, before the hosted service) says why in "message".
  [ -n "$_d" ] || _d=$(json_field message "$HK_BODY")
  if [ -n "$_d" ]; then say " ($_d)"; fi
}

# curl's own one-line reason, when it gave one (it cannot hold the key: the key is only in the config file).
err_of() {
  _e=$(head -n 1 "$HK_ERR" 2>/dev/null | tr -d '[:cntrl:]') || true
  if [ -n "$_e" ]; then printf '; %s' "$_e"; fi
}

# check_service: GET /v1/status with the key, before anything is written. The key goes to curl in a private config
# file, never on a command line.
check_service() {
  command -v curl >/dev/null 2>&1 || die "curl was not found."
  HK_CFG=$(mktemp "${TMPDIR:-/tmp}/hk-curl.XXXXXX") || die "could not create a temporary file."
  HK_BODY=$(mktemp "${TMPDIR:-/tmp}/hk-body.XXXXXX") || die "could not create a temporary file."
  HK_ERR=$(mktemp "${TMPDIR:-/tmp}/hk-err.XXXXXX") || die "could not create a temporary file."
  chmod 600 "$HK_CFG" "$HK_BODY" "$HK_ERR"
  printf 'url = "%s/v1/status"\nheader = "Authorization: Bearer %s"\n' "$1" "$HK_KEY" >"$HK_CFG"
  _code=$(curl -sS --max-time 20 -o "$HK_BODY" -w '%{http_code}' -K "$HK_CFG" 2>"$HK_ERR") || true
  # The key was only ever in this file: gone before anything else happens.
  rm -f "$HK_CFG"
  HK_CFG=""
  case "$_code" in
    200) ;;
    401) die "the service at $1 refused this key: it is not a key it knows. Check that you copied all of it.$(detail_of)" ;;
    403) die "the service at $1 will not take this key$(detail_of)" ;;
    000 | 5??) die "the service at $1 cannot be reached or is away just now (answer: $_code$(err_of)). Your key was not refused. Try again in a minute.$(detail_of)" ;;
    *) die "the service at $1 answered $_code, which this script does not know.$(detail_of)" ;;
  esac
  _have=$(tr '\n' ' ' <"$HK_BODY" | sed -n 's/.*"versions"[[:space:]]*:[[:space:]]*{[^}]*"service"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  _want=$(sed -n 's/^version:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*/\1/p' "$ROOT/plugin/plugin.yaml" | head -n 1)
  [ -n "$_want" ] || die "could not read the plugin's version from $ROOT/plugin/plugin.yaml."
  _mm=$(printf '%s' "$_have" | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\)\([^0-9].*\)\{0,1\}$/\1/p')
  if [ -z "$_mm" ] || [ "$_mm" != "$_want" ]; then
    die "the service at $1 is version \"${_have:-unknown}\" and this plugin is version \"$_want\"; they must share major.minor. Nothing was written."
  fi
  say "the service at $1 took the key (version $_have)"
}

# warn_url_override URL: the plugin reads HERMES_KERNEL_URL before hermes-kernel.json, so one that names another service
# silently wins over the URL just written. Say so, and how to remove it; never edit the person's environment or file.
# The other value is not echoed: only the URL written here passed check_url.
warn_url_override() {
  _set=$(printf '%s' "${HERMES_KERNEL_URL:-}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [ -n "$_set" ] && [ "${_set%/}" != "$1" ]; then
    warn "HERMES_KERNEL_URL is set in your environment to another URL, and the plugin prefers it to $1, the URL written here. Remove it: run \"unset HERMES_KERNEL_URL\" and take it out of your shell profile, then restart Hermes."
  fi
  _set=$(env_get "$HERMES_HOME/.env" HERMES_KERNEL_URL)
  if [ -n "$_set" ] && [ "${_set%/}" != "$1" ]; then
    warn "$HERMES_HOME/.env sets HERMES_KERNEL_URL to another URL, and the plugin prefers it to $1, the URL written here. Remove that line from $HERMES_HOME/.env, then restart Hermes."
  fi
}

run_hosted() {
  resolve_profile
  check_hermes
  _url=${URL:-$HOSTED_URL}
  _url=${_url%/}
  check_url "$_url"
  resolve_key
  check_service "$_url"
  install_plugin
  write_hermes_token "$HK_KEY"
  write_url "$_url"
  "$HERMES_BIN" config set memory.provider hermes-kernel
  # Last, so a running gateway reloads its plugins and finds the token already there.
  "$HERMES_BIN" plugins enable hermes-kernel
  warn_url_override "$_url"
  finish
}

# ---------------------------------------------------------------- main

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-build)
        NO_BUILD=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --mode | --hermes | --project | --compose-file | --plugin-source | --url)
        [ $# -ge 2 ] || die "$1 needs a value."
        case "$1" in
          --mode) MODE=$2 ;;
          --hermes) HERMES_BIN=$2 ;;
          --project) PROJECT=$2 ;;
          --compose-file) COMPOSE_FILE=$2 ;;
          --plugin-source) PLUGIN_SOURCE=$2 ;;
          --url) URL=$2 ;;
        esac
        shift 2
        ;;
      *) die "unknown option: $1 (try --help)" ;;
    esac
  done
  MODE=$(printf '%s' "$MODE" | tr 'A-Z' 'a-z')
  if [ -z "$MODE" ]; then ask_mode; fi
  case "$MODE" in
    local) run_local ;;
    hosted) run_hosted ;;
    *) die "unknown mode: $MODE (local or hosted)" ;;
  esac
}

main "$@"
