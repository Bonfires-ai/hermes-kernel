"""Finished turns to the turn service, without ever holding Hermes up.

`Capture.turn` only queues. A daemon worker, started on the first `turn` or `close` and never at import or
construction, drains the queue in batches of up to 64, one batch in flight. While the service is away the backlog is
mirrored to a disk spool (`spool.py`) so a restart finds it; a new `Capture` on the same path sends it first, oldest
first. A `close` rides the same queue, so a session closes after its last turn.

Failure rules: `Unavailable` and `Unauthorized` keep the data and retry with backoff (the second says to check
`HERMES_KERNEL_TOKEN`); `Refused` (a 4xx the service will always give) drops that batch, because one bad message must
not block every later one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .client import Refused, Unauthorized, Unavailable
from .spool import Spool

logger = logging.getLogger(__name__)

QUEUE_MAX = 1000
BATCH_MAX = 64
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
SHUTDOWN_FLUSH_S = 5.0
#: bounds the spool; the writer splits long text itself, so nothing shorter is cut
TEXT_MAX_CHARS = 100_000

TOKEN_HINT = "the turn service rejected the token: check HERMES_KERNEL_TOKEN"


class Sender(Protocol):
    def post_messages(self, batch: list[dict[str, Any]], agent_handles: list[str]) -> Any: ...

    def close_session(self, conversation_id: str) -> Any: ...


def message_id(conversation_id: str, session_id: str, turn_number: int, role: str, text: str) -> str:
    """The same turn resent has the same id; different text, a different one. JSON keeps the fields unambiguous."""
    fields = json.dumps([conversation_id, session_id, turn_number, role, text], ensure_ascii=True)
    return hashlib.sha256(fields.encode("utf-8")).hexdigest()


def next_backoff(current: float) -> float:
    """Double, cap, then jitter downward only, so the result never exceeds the cap."""
    return min(current * 2, BACKOFF_MAX_S) * (0.5 + random.random() * 0.5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


Item = tuple[int, dict[str, Any]]  # (sequence number, payload as spooled)


class Capture:
    def __init__(
        self,
        client: Sender,
        spool_path: Path | None,
        *,
        agent_handle: str,
        clock: Callable[[], datetime] = _utc_now,
        queue_max: int = QUEUE_MAX,
        autostart: bool = True,
    ) -> None:
        """One Hermes has one handle, so it is fixed per instance; `set_agent_handle` is for a handle that is only
        known at `initialize`. `autostart=False` leaves the worker unstarted so tests can call `drain_once`."""
        self._client = client
        self._spool = Spool(spool_path) if spool_path is not None else None
        self._handle = agent_handle
        self._clock = clock
        self._queue_max = queue_max
        self._autostart = autostart
        self._lock = threading.RLock()
        self._queue: deque[Item] = deque()
        self._seq = 0
        self._spooling = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._flushing = False
        self._backoff = BACKOFF_START_S
        self._drop_logged = False
        self._logged_error: str | None = None
        self.last_error: str | None = None

    # public

    def set_agent_handle(self, handle: str) -> None:
        self._handle = handle

    @property
    def backlog(self) -> int:
        """Messages and closes not yet delivered: the queue, which the spool mirrors while it is in use."""
        with self._lock:
            return len(self._queue)

    def turn(
        self,
        conversation_id: str,
        speaker: str,
        user_text: str,
        assistant_text: str,
        session_id: str,
        turn_number: int,
    ) -> None:
        """Queue the user's message and the reply (an empty one is skipped). Never blocks on the network."""
        now = self._clock()
        handle = self._handle
        items: list[dict[str, Any]] = []
        for role, who, text, addressee, when in (
            ("user", speaker, user_text, handle, now),
            ("assistant", handle, assistant_text, speaker, now + timedelta(milliseconds=1)),
        ):
            if not text.strip():
                continue
            text = text[:TEXT_MAX_CHARS]
            message = {
                "conversation_id": conversation_id,
                "message_id": message_id(conversation_id, session_id, turn_number, role, text),
                "speaker": who,
                "text": text,
                "timestamp": when.isoformat(),
                "addressees": [addressee],
            }
            items.append({"kind": "message", "handle": handle, "message": message})
        if items:
            self._enqueue(items)

    def close(self, conversation_id: str) -> None:
        self._enqueue([{"kind": "close", "conversation_id": conversation_id}])

    def shutdown(self) -> None:
        """A bounded flush, at most 5 s, then stop. Durability does not depend on it."""
        self._start(spawn=False)
        self._flushing = True
        with self._lock:
            pending = bool(self._queue)
        thread = self._thread
        if pending and thread is None and self._autostart:
            self._spawn()
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(SHUTDOWN_FLUSH_S)
        self._stop.set()
        self._wake.set()

    # queueing

    def _enqueue(self, payloads: list[dict[str, Any]]) -> None:
        self._start()
        with self._lock:
            overflow = False
            for payload in payloads:
                if len(self._queue) >= self._queue_max:
                    self._queue.popleft()
                    overflow = True
                self._seq += 1
                self._queue.append((self._seq, payload))
            if overflow and not self._drop_logged:
                self._drop_logged = True
                logger.warning(
                    "hermes-kernel: capture backlog is full (%d); dropping the oldest messages until the service "
                    "is back",
                    self._queue_max,
                )
            if self._spooling and self._spool is not None:
                if overflow:
                    self._spool.replace(p for _, p in self._queue)
                else:
                    for payload in payloads:
                        self._spool.append(payload)
        self._wake.set()

    def _start(self, *, spawn: bool = True) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._restore()
        if spawn and self._autostart:
            self._spawn()

    def _spawn(self) -> None:
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name="hermes-kernel-capture", daemon=True)
                self._thread.start()

    def _restore(self) -> None:
        """Put the spool's contents ahead of anything queued since. A spool that cannot be read is left alone."""
        if self._spool is None:
            return
        loaded = self._spool.load()
        if loaded is None:
            return
        payloads, skipped = loaded
        valid = [p for p in payloads if p.get("kind") in ("message", "close")]
        skipped += len(payloads) - len(valid)
        if skipped:
            logger.warning("hermes-kernel: skipped %d unreadable lines in the capture spool", skipped)
        if not valid and not skipped:
            return
        restored: deque[Item] = deque()
        for payload in valid[-self._queue_max :]:
            self._seq += 1
            restored.append((self._seq, payload))
        restored.extend(self._queue)
        self._queue = deque(list(restored)[-self._queue_max :])
        self._spooling = True
        self._spool.replace(p for _, p in self._queue)

    # draining

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ok = self.drain_once()
            except Exception:  # the worker must outlive anything
                logger.exception("hermes-kernel: capture worker error")
                ok = False
            with self._lock:
                empty = not self._queue
            if self._flushing and (empty or not ok):
                return
            if ok and empty:
                self._wake.wait()
                self._wake.clear()
            elif not ok:
                self._wake.wait(self._backoff)
                self._wake.clear()

    def drain_once(self) -> bool:
        """Send the next batch. True when it went (or there was nothing), False when it must be retried."""
        self._start(spawn=False)
        with self._lock:
            if not self._queue:
                return True
            batch = self._next_batch()
        last_seq = batch[-1][0]
        try:
            self._send(batch)
        except Unauthorized:
            self._failed(TOKEN_HINT, at_cap=True)
            return False
        except Unavailable as exc:
            self._failed(f"turn service unavailable: {exc.detail}")
            return False
        except Refused as exc:
            self._refused(last_seq, len(batch), exc)
            return True
        except Exception as exc:  # noqa: BLE001 - nothing may escape the worker
            self._failed(f"capture failed: {type(exc).__name__}: {exc}")
            return False
        self._delivered(last_seq)
        return True

    def _next_batch(self) -> list[Item]:
        first = self._queue[0]
        if first[1]["kind"] == "close":
            return [first]
        batch: list[Item] = []
        for item in self._queue:
            if item[1]["kind"] != "message" or len(batch) >= BATCH_MAX:
                break
            batch.append(item)
        return batch

    def _send(self, batch: list[Item]) -> None:
        payload = batch[0][1]
        if payload["kind"] == "close":
            self._client.close_session(payload["conversation_id"])
            return
        handles = list(dict.fromkeys(p["handle"] for _, p in batch))
        self._client.post_messages([p["message"] for _, p in batch], handles)

    def _remove_through(self, last_seq: int) -> None:
        with self._lock:
            while self._queue and self._queue[0][0] <= last_seq:
                self._queue.popleft()
            if self._spool is not None and self._spooling:
                if self._queue:
                    self._spool.replace(p for _, p in self._queue)
                elif self._spool.clear():
                    self._spooling = False

    def _delivered(self, last_seq: int) -> None:
        self._remove_through(last_seq)
        self._backoff = BACKOFF_START_S
        self._drop_logged = False
        self._logged_error = None
        self.last_error = None

    def _refused(self, last_seq: int, count: int, exc: Refused) -> None:
        self._remove_through(last_seq)
        self.last_error = f"the turn service refused {count} captured item(s) with {exc.status}: {exc.detail}"
        logger.warning("hermes-kernel: dropped %d captured item(s), the service refused them: %s", count, exc.detail)

    def _failed(self, error: str, *, at_cap: bool = False) -> None:
        self.last_error = error
        if error != self._logged_error:
            self._logged_error = error
            logger.warning("hermes-kernel: capture is holding %d item(s): %s", self.backlog, error)
        self._backoff = BACKOFF_MAX_S if at_cap else next_backoff(self._backoff)
        with self._lock:
            if self._spool is not None and not self._spooling and self._queue:
                self._spooling = self._spool.replace(p for _, p in self._queue)
