"""Run `hermes memory setup` the way the README says (the menu: arrow up from "Built-in only" to hermes-kernel, Enter), from a script, for `e2e.sh`.

    E2E_SETUP_TOKEN=<token> python e2e_setup.py <hermes>

HERMES_HOME comes from the environment. The URL prompt takes its default (Enter); the token prompt gets the token from
`E2E_SETUP_TOKEN` (not from the command line). Afterwards it checks, without printing the token, that Hermes wrote
`HERMES_KERNEL_TOKEN` to `$HERMES_HOME/.env` and the URL to `hermes-kernel.json`, and exits 1 if either is missing.

Needs `pexpect`.
Naming the provider (`hermes memory setup hermes-kernel`) only activates it in Hermes v0.23.0 and asks for nothing, so
the menu is the path that stores the token.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pexpect  # pyright: ignore[reportMissingModuleSource]


def require_own_hermes_home() -> Path:
    """Refuse, before anything is spawned, unless HERMES_HOME is set, absolute, and not the real ~/.hermes (after realpath)."""
    raw = os.environ.get("HERMES_HOME", "")
    if not raw:
        sys.exit(
            "refusing to run: HERMES_HOME is not set. Hermes migrates the home it starts on, so point it at a throwaway directory first."
        )
    if not os.path.isabs(raw):
        sys.exit(f"refusing to run: HERMES_HOME must be an absolute path, got '{raw}'.")
    real = os.path.realpath(os.path.expanduser("~/.hermes"))
    if os.path.realpath(raw) == real:
        sys.exit(f"refusing to run: HERMES_HOME resolves to the real Hermes home ({real}). Use a throwaway directory.")
    return Path(raw)


def main() -> int:
    home = require_own_hermes_home()
    hermes = sys.argv[1]
    token = os.environ["E2E_SETUP_TOKEN"]
    child = pexpect.spawn(hermes, ["memory", "setup"], encoding="utf-8", timeout=60, dimensions=(50, 140))
    child.expect("Memory provider setup", timeout=60)
    time.sleep(2)
    child.send("\x1b[A")  # the cursor starts on "Built-in only", the last entry; hermes-kernel is the one above it
    time.sleep(1)
    child.send("\r")
    child.expect("Turn service URL", timeout=60)
    child.sendline("")
    child.expect("bearer token", timeout=60)
    child.sendline(token)
    child.expect("Start a new session", timeout=60)
    child.close()
    env_lines = (home / ".env").read_text(encoding="utf-8").splitlines()
    if f"HERMES_KERNEL_TOKEN={token}" not in env_lines:
        print("e2e_setup: HERMES_KERNEL_TOKEN did not land in the Hermes .env", file=sys.stderr)
        return 1
    saved = json.loads((home / "hermes-kernel.json").read_text(encoding="utf-8"))
    if not str(saved.get("url", "")).startswith("http"):
        print("e2e_setup: the turn service URL did not land in hermes-kernel.json", file=sys.stderr)
        return 1
    print("e2e_setup: hermes memory setup saved the token (not shown) and the URL", saved["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
