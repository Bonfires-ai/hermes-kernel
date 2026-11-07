"""The refusal every Python script here makes before it starts Hermes. `e2e.sh` has the same rule in bash.

Hermes migrates the home it starts on, so a script must never reach the real one. Hermes strips HERMES_HOME, expands
variables in it, and takes a profile under the real home as a home of its own (`hermes_constants.py`), so this does the
same before comparing. The real home comes from the password database as well as from HOME, which can point elsewhere.
"""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


def real_hermes_homes() -> set[str]:
    homes = {pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME", "")}
    return {os.path.realpath(os.path.join(home, ".hermes")) for home in homes if home}


def require_own_hermes_home() -> Path:
    """Refuse, before anything is spawned, unless HERMES_HOME is an absolute path outside the real Hermes home."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        sys.exit(
            "refusing to run: HERMES_HOME is not set. Hermes migrates the home it starts on, so point it at a throwaway directory first."
        )
    if not os.path.isabs(raw) or "$" in raw:
        sys.exit(f"refusing to run: HERMES_HOME must be a plain absolute path, got '{raw}'.")
    target = os.path.realpath(raw)
    for real in real_hermes_homes():
        if target == real or target.startswith(real + os.sep):
            sys.exit(f"refusing to run: HERMES_HOME is in the real Hermes home ({real}). Use a throwaway directory.")
    return Path(raw)
