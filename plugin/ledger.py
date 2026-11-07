"""The step ledger: the results of agent steps dispatched and not yet held by kernel-turns, and what each phrase child
turn's lines are fenced by (`StepLedger`, written through to a file under Hermes's home; `runs.py` drives the phrases)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


_LEDGER_KEPT = 64
#: the values kept per child turn (agent results)
_MAX_CARRIED = 200
#: the evicted child turns remembered
_EVICTED_KEPT = 1024
#: how long a ledger that lost what it carried fences every child turn without a record
UNSURE_FOR_S = 24 * 3600.0

#: the agent values a child turn's steps gave, `(tool, value)`, as reported
Carried = Sequence[tuple[str, str]]
#: the tool of a `Carried` entry that stands for values not known (the child turn committed agent steps, and what they
#: gave is gone): every line of the child turn is then fenced, whatever it carries
UNKNOWN = "*"
#: the source of that fence
UNKNOWN_SOURCE = "phrase"
FENCE_ALL: Carried = ((UNKNOWN, ""),)
#: a held result, and the real name of the tool that gave it (`None`: an older plugin's entry, the value alone)
Held = tuple[str, str | None]


def _held(entry: object) -> Held | None:
    """A ledger file's entry: `{"value", "tool"}`, or an older plugin's value alone (kept: dropping it would run the
    step again)."""
    if isinstance(entry, str):
        return entry, None
    if not isinstance(entry, dict):
        return None
    value, tool = entry.get("value"), entry.get("tool")
    if not isinstance(value, str):
        return None
    return value, tool if isinstance(tool, str) and tool else None


class _Child:
    """What the ledger knows of one child turn's agent values: how many steps gave one, the values, and whether the
    record has them all (a record started while the ledger was unsure, or after an eviction, has not)."""

    __slots__ = ("complete", "steps", "values")

    def __init__(self, steps: int = 0, values: list[tuple[str, str]] | None = None, complete: bool = True) -> None:
        self.steps, self.values, self.complete = steps, values or [], complete


class StepLedger:
    """The results of agent steps dispatched and not yet held by kernel-turns, by effect key, each with its tool's real
    name; and by child turn, the values its lines are fenced by (`carry`), until it ends (`end`). With a `path` (under
    Hermes's home) it is written through, so a restart between a dispatch and its report keeps the result, and the
    name that fences it, and a restart in the middle of a phrase keeps what its lines are fenced by.

    What it cannot tell fails closed (`carried` answers `FENCE_ALL`): a child turn whose record is partial, one whose
    record was evicted (its id is kept as a tombstone), and every child turn without a record while the ledger is
    `unsure` (it has no file, its file could not be read, or is an older plugin's, which kept no values): for `UNSURE_FOR_S` from the
    restart that found it so, kept in the file across further restarts."""

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._held: OrderedDict[str, Held] = OrderedDict()
        self._carried: OrderedDict[str, _Child] = OrderedDict()
        self._evicted: OrderedDict[str, None] = OrderedDict()
        self._unsure_until = 0.0
        if path is None:  # nothing survives a restart: what it carried is not known after one, so it fences all
            self._unsure_until = clock() + UNSURE_FOR_S
        elif path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("hermes-kernel: the agent step ledger %s could not be read; starting empty", path)
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("held"), dict):
            until = raw.get("unsure_until")
            self._unsure_until = float(until) if isinstance(until, int | float) else 0.0
            self._load_carried(raw.get("carried"))
            tombstones = raw.get("evicted")
            self._evicted.update(
                (c, None) for c in (tombstones if isinstance(tombstones, list) else []) if isinstance(c, str)
            )
            held = raw["held"]
        else:  # unreadable, or an older plugin's flat file (even an empty one): it knew no carried values
            self._unsure_until = max(self._unsure_until, self._clock() + UNSURE_FOR_S)
            held = raw if isinstance(raw, dict) else {}
        for key, entry in held.items():
            entry_held = _held(entry)
            if isinstance(key, str) and entry_held is not None:
                self._held[key] = entry_held

    def _load_carried(self, raw: object) -> None:
        for child, entry in raw.items() if isinstance(raw, dict) else []:
            if not isinstance(child, str) or not isinstance(entry, dict):
                continue
            steps, values = entry.get("steps"), entry.get("values")
            pairs = [
                (v[0], v[1])
                for v in (values if isinstance(values, list) else [])
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], str)
            ]
            self._carried[child] = _Child(
                steps if isinstance(steps, int) else len(pairs), pairs, entry.get("complete") is not False
            )

    @property
    def unsure(self) -> bool:
        return self._clock() < self._unsure_until

    def get(self, key: str) -> Held | None:
        with self._lock:
            return self._held.get(key)

    def record(self, key: str, value: str, tool: str) -> None:
        """`tool`: the real name the step was dispatched under."""
        with self._lock:
            self._held[key] = (value, tool)
            while len(self._held) > _LEDGER_KEPT:
                self._held.popitem(last=False)
            self._save()

    def forget(self, key: str) -> None:
        with self._lock:
            if self._held.pop(key, None) is not None:
                self._save()

    def carry(self, child: str, tool: str, value: str) -> None:
        """`child`'s lines are fenced by `value` from now on (`tool`: the real name the fence decides by)."""
        with self._lock:
            record = self._carried.pop(child, None)
            if record is None:
                record = _Child(complete=not (self.unsure or child in self._evicted))
            if (tool, value) not in record.values:
                record.steps += 1
                record.values = [*record.values, (tool, value)]
                if len(record.values) > _MAX_CARRIED:  # a value dropped is a value not known
                    record.values, record.complete = record.values[-_MAX_CARRIED:], False
            self._carried[child] = record
            while len(self._carried) > _LEDGER_KEPT:
                gone, _ = self._carried.popitem(last=False)
                self._evicted[gone] = None
            while len(self._evicted) > _EVICTED_KEPT:
                self._evicted.popitem(last=False)
            self._save()

    def carried(self, child: str) -> Carried:
        """The values `child`'s lines are fenced by; `FENCE_ALL` when they are not all known."""
        with self._lock:
            record = self._carried.get(child)
            if record is None:
                return FENCE_ALL if self.unsure or child in self._evicted else []
            if not record.complete or (record.steps and not record.values):
                return FENCE_ALL
            return list(record.values)

    def end(self, child: str) -> None:
        """`child`'s turn ended: its values are no longer kept."""
        with self._lock:
            changed = self._carried.pop(child, None) is not None
            changed = self._evicted.pop(child, 0) is None or changed
            if changed:
                self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            body = {
                "held": {key: {"value": value, "tool": tool} for key, (value, tool) in self._held.items()},
                "carried": {
                    c: {"steps": r.steps, "values": [list(v) for v in r.values], "complete": r.complete}
                    for c, r in self._carried.items()
                },
                "evicted": list(self._evicted),
                "unsure_until": self._unsure_until,
            }
            # results are kept here: the file is the owner's alone
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(body, ensure_ascii=False))
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("hermes-kernel: the agent step ledger %s could not be written", self._path, exc_info=True)
            self._distrust(tmp)

    def _distrust(self, tmp: Path) -> None:
        """A save failed, so the file is older than what is carried. Empty it, so a restart finds a file it cannot read
        and is unsure. Removing it would not do: no file reads as a first start, sure of everything. Emptying needs no
        space on a full disk, and the next save that works writes it whole again."""
        assert self._path is not None
        try:
            tmp.unlink(missing_ok=True)
            os.close(os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        except OSError:
            logger.warning("hermes-kernel: the stale step ledger %s could not be emptied", self._path, exc_info=True)
