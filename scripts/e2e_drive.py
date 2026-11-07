"""Drive one interactive Hermes chat from a script, for `e2e.sh`.

    python e2e_drive.py <hermes> <transcript> <prompts.json> [--quiet-s N] [--max-s N]

Starts `<hermes> chat --cli` under a pseudo terminal (HERMES_HOME comes from the environment), sends each prompt from
the JSON list in turn, waits until the screen has been quiet for `--quiet-s` seconds (or `--max-s` passes), and ends
the chat with `/exit`, which closes the session. The transcript has each prompt, then what Hermes printed after it,
with terminal codes removed. A prompt that is an object `{"run": "<shell command>"}` runs that command in the middle
of the chat instead (used to stop and start the stack while a conversation is open, and to check what that did). A
command that exits non-zero ends the run: the chat is closed, the transcript written, and the exit status is 1.

Needs `pexpect`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pexpect  # pyright: ignore[reportMissingModuleSource]

# CSI sequences, OSC strings (ended by BEL or ST; a bare "up to the next BEL" would swallow the text after one that ends
# with ST), character-set selections, and carriage returns.
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\r")


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


def clean(raw: bytes) -> str:
    return ANSI.sub("", raw.decode("utf-8", "replace"))


def settle(child: Any, quiet_s: float, max_s: float) -> bytes:
    """Everything printed until the screen has been quiet for `quiet_s`, or `max_s` has passed."""
    out = b""
    started = last = time.time()
    while time.time() - last < quiet_s and time.time() - started < max_s:
        try:
            out += child.read_nonblocking(65536, timeout=1)
            last = time.time()
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            break
    return out


def main() -> int:
    _ = require_own_hermes_home()
    args = sys.argv[1:]
    quiet_s, max_s = 15.0, 240.0
    for flag in ("--quiet-s", "--max-s"):
        if flag in args:
            at = args.index(flag)
            value = float(args[at + 1])
            quiet_s, max_s = (value, max_s) if flag == "--quiet-s" else (quiet_s, value)
            del args[at : at + 2]
    hermes, transcript, prompts_file = args
    prompts = json.loads(Path(prompts_file).read_text(encoding="utf-8"))
    child = pexpect.spawn(hermes, ["chat", "--cli"], env=None, encoding=None, timeout=150, dimensions=(50, 160))
    parts: list[str] = []
    failed = False
    child.expect(b"Welcome to Hermes", timeout=90)
    parts.append(clean(settle(child, 8, 30)))
    for prompt in prompts:
        if isinstance(prompt, dict):
            done = subprocess.run(prompt["run"], shell=True, capture_output=True, text=True, check=False)  # noqa: S602
            parts.append(f"\n=== RUN {prompt['run']} -> exit {done.returncode}\n{done.stdout}{done.stderr}")
            if done.returncode != 0:
                failed = True
                break
            continue
        child.send(prompt.encode())
        time.sleep(0.7)
        _ = child.send(b"\r")
        parts.append(f"\n=== PROMPT {prompt}\n")
        parts.append(clean(settle(child, quiet_s, max_s)))
    child.send(b"/exit")
    time.sleep(0.7)
    child.send(b"\r")
    time.sleep(5)
    parts.append("\n=== EXIT\n" + clean(settle(child, 3, 20)))
    try:
        child.close()
    except Exception:  # noqa: BLE001  the chat is over either way
        pass
    Path(transcript).write_text("".join(parts), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
