"""Captured messages kept on disk while the turn service is unreachable.

The capture queue lives in memory, and a Hermes that stops while it is not empty
(a restart, a crash, a kill, all during an outage) would take it along. Hermes
does not reliably call a plugin on the way out, so nothing is flushed at exit:
the backlog is kept in a file the next Hermes process finds instead, under
`$HERMES_HOME`.

Only during an outage. In steady state the queue empties within moments of each
message and nothing is written; the first batch that fails snapshots the
backlog, later arrivals are appended, and delivery shrinks and finally removes
the file. The writes are synchronous because they are small and rare: the start
and end of an outage.

A spool that cannot be written costs durability, never an exception: every
method reports failure by its return value, logs it (rate-limited), and the
queue carries on in memory exactly as before. One that cannot be READ is never
mistaken for an empty one (`load` returns None), so a transient error at boot
does not delete the backlog it was keeping.

One Hermes process owns the file. Two processes sharing a `$HERMES_HOME` would
overwrite each other's snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Disk trouble during an outage would otherwise log once per message.
_ERROR_LOG_INTERVAL_SECONDS = 60.0

# Not following a symlink planted at the spool's path: anything able to plant
# one runs as the same user and could do the damage directly, but there is no
# reason to write or read through one. Absent on Windows.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Opening a FIFO planted at the path would otherwise block, on every boot,
# until something wrote to it. Harmless on a regular file.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# What `json.dumps` and the write can raise beyond OSError: a payload JSON
# cannot encode (TypeError, ValueError). The default `ensure_ascii` keeps the
# text a UTF-8 codec can always write - a lone surrogate, valid in a str,
# becomes an escape instead of a UnicodeEncodeError.
_WRITE_ERRORS = (OSError, TypeError, ValueError)


class Spool:
    """One JSON payload per line, at `path`. Private to the user running Hermes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Not 0.0: `time.monotonic()` counts from the machine's boot, and on a
        # fresh one - a new task, a CI runner - a clock starting at zero
        # rate-limited away the very first warning.
        self._last_error_log = float("-inf")

    def replace(self, payloads: Iterable[dict[str, Any]]) -> bool:
        """Make the file hold exactly `payloads`; none removes it.

        Written beside the target and renamed over it, so a process killed
        mid-write leaves either the old snapshot or the new one, never half.
        The temporary file is always a new one (removed first, then created
        with O_EXCL): whatever was left at that path - a FIFO, which an open
        for writing would block on, or a symlink - is never written through.
        """
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            lines = [json.dumps(payload) + "\n" for payload in payloads]
            if not lines:
                return self.clear()
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
            os.replace(tmp, self.path)
        except _WRITE_ERRORS as exc:
            self._report("save", type(exc).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def append(self, payload: dict[str, Any]) -> bool:
        """Add one payload after whatever the file holds.

        Only to a regular file: a FIFO with a reader on the other end accepts
        the write and keeps nothing, and reporting that as saved would claim a
        durability that does not exist.
        """
        try:
            line = json.dumps(payload) + "\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _NONBLOCK, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                self._report("append to", "not a regular file")
                return False
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line)
        except _WRITE_ERRORS as exc:
            self._report("append to", type(exc).__name__)
            return False
        return True

    def load(self) -> tuple[list[dict[str, Any]], int] | None:
        """Everything readable, in order, and how many lines were not; None
        when the file exists but could not be read.

        A process killed mid-append leaves a torn last line; it is skipped
        and counted rather than costing the rest of the backlog. A file that
        could not be read at all is a different answer from an empty one: the
        caller must leave it alone for the next process to try.
        """
        try:
            fd = os.open(self.path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
        except FileNotFoundError:
            return [], 0
        except OSError as exc:
            self._report("read", type(exc).__name__)
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                self._report("read", "not a regular file")
                return None
            with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            self._report("read", type(exc).__name__)
            return None
        payloads: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
            else:
                skipped += 1
        return payloads, skipped

    def clear(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            self._report("remove", type(exc).__name__)
            return False
        return True

    def _report(self, action: str, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_error_log < _ERROR_LOG_INTERVAL_SECONDS:
            return
        self._last_error_log = now
        logger.warning(
            "hermes-kernel: could not %s the capture spool at %s (%s); queued messages are in memory only",
            action,
            self.path,
            reason,
        )
