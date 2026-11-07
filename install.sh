#!/usr/bin/env bash
# One script that sets up hermes-kernel for a person's Hermes: the local stack, the plugin, and the token that joins them.
#
#   ./install.sh [--mode local] [--hermes PATH] [--project NAME] [--compose-file FILE] [--no-build]
#                [--plugin-source SOURCE] [--url URL]
#
# Each option has an environment equivalent: HERMES_KERNEL_MODE, HERMES_BIN, HERMES_KERNEL_PROJECT,
# HERMES_KERNEL_COMPOSE_FILE, HERMES_KERNEL_NO_BUILD, HERMES_KERNEL_PLUGIN_SOURCE, HERMES_KERNEL_URL.
# Keys come from the environment only (OPENROUTER_API_KEY, VOYAGE_API_KEY, NEO4J_PASSWORD, KT_TOKEN) or a hidden prompt,
# never from the command line.
#
# This writes to your real Hermes home ($HERMES_HOME, default ~/.hermes). It never installs or upgrades Hermes.
# Bash 3.2 and POSIX tools only: the same file runs on a Mac.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)

WANT_HERMES="v0.23.0"
LOCAL_URL="http://127.0.0.1:8787"

MODE=${HERMES_KERNEL_MODE:-local}
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
die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- small file helpers

# env_get FILE KEY: the first value of KEY in FILE, without surrounding spaces or quotes; nothing if absent.
env_get() {
  [ -f "$1" ] || return 0
  grep -E "^$2=" "$1" | head -n 1 | cut -d= -f2- |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/" || true
}

# env_fill FILE KEY VALUE: set KEY only when it is absent or blank. Returns 0 when it wrote, 1 when it kept the value.
# The value travels in the environment of awk, never on a command line.
env_fill() {
  _file=$1
  [ -z "$(env_get "$_file" "$2")" ] || return 1
  _tmp=$(mktemp "$(dirname "$_file")/.hk.XXXXXX")
  chmod 600 "$_tmp"
  if HK_K=$2 HK_V=$3 awk '
      BEGIN { k = ENVIRON["HK_K"]; v = ENVIRON["HK_V"]; done = 0 }
      {
        if (!done && index($0, k "=") == 1) { print k "=" v; done = 1; next }
        print
      }
      END { if (!done) print k "=" v }
    ' "$_file" >"$_tmp"; then
    mv "$_tmp" "$_file"
  else
    rm -f "$_tmp"
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
  if ! printf '%s\n' "$_ver" | grep -Eq "v0\\.23\\.0([^0-9.]|\$)"; then
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
    if [ -d "$ROOT/.git" ]; then _src="file://$ROOT#plugin"; else _src="Bonfires-ai/hermes-kernel/plugin"; fi
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
  _tmp=$(mktemp "$HERMES_HOME/.env.XXXXXX")
  chmod 600 "$_tmp"
  if [ -f "$_file" ]; then _src=$_file; else _src=/dev/null; fi
  if HK_V=$1 awk '
      BEGIN { v = ENVIRON["HK_V"]; done = 0 }
      /^(export[ \t]+)?HERMES_KERNEL_TOKEN=/ { if (!done) { print "HERMES_KERNEL_TOKEN=" v; done = 1 } next }
      { print }
      END { if (!done) print "HERMES_KERNEL_TOKEN=" v }
    ' "$_src" >"$_tmp"; then
    mv "$_tmp" "$_file"
    chmod 600 "$_file"
  else
    rm -f "$_tmp"
    die "could not write the token to $_file"
  fi
}

# write_url URL: where the plugin finds the turn service (hermes-kernel.json in Hermes's home).
write_url() {
  case "$1" in
    *[\"\\]*) die "the URL cannot contain quotes or backslashes." ;;
  esac
  mkdir -p "$HERMES_HOME"
  _tmp=$(mktemp "$HERMES_HOME/.hermes-kernel.XXXXXX")
  printf '{"url": "%s"}\n' "$1" >"$_tmp"
  chmod 600 "$_tmp"
  mv "$_tmp" "$HERMES_HOME/hermes-kernel.json"
}

finish() {
  if [ -n "$UPDATED" ]; then
    say "If Hermes was already running, restart its gateway so it loads the update: hermes gateway restart --detach"
  fi
  say "Start a new Hermes session and type /kernel"
}

# ---------------------------------------------------------------- the local path

run_local() {
  ENV_FILE=$(dirname "$COMPOSE_FILE")/.env
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
  case "$MODE" in
    local) run_local ;;
    hosted) die "hosted mode is not offered yet." ;;
    *) die "unknown mode: $MODE (local)" ;;
  esac
}

main "$@"
