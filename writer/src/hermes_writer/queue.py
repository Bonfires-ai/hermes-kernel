"""The writer's side of the sessions contract (see ``kernel_turns.sessions`` in the turn service).

Only the writer moves a session out of ``closed``: ``running``, ``built``, ``failed``, ``held``. It never deletes a
document: a built session keeps its message ids (only ``text`` goes), so a resent message stays a duplicate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from pymongo import ASCENDING, ReturnDocument
from pymongo.database import Database

log = logging.getLogger(__name__)


class Claimed(TypedDict):
    id: str
    conversation_id: str
    agent_handles: list[str]
    messages: list[dict[str, Any]]


def _utc(when: datetime) -> datetime:
    # Mongo hands back naive UTC.
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _rising(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``messages`` in order, each one at least 1 ms after the one before. A crash between storing a message and counting
    it leaves an orphan whose ``seq`` the next message takes, so a session can hold ties, and a tie must not make it
    unbuildable. Only what the kernel is handed changes: the stored time stays as it was sent."""
    last: datetime | None = None
    for message in messages:
        at = datetime.fromisoformat(message["timestamp"])
        if last is not None and at <= last:
            at = last + timedelta(milliseconds=1)
            message["timestamp"] = at.isoformat()
        last = at
    return messages


class Queue:
    def __init__(
        self, db: Database[Any], *, clock: Callable[[], datetime] | None = None, stop_backoff_s: float = 60
    ) -> None:
        self._sessions = db["sessions"]
        self._messages = db["messages"]
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending_release_at: datetime | None = None
        self._stop_backoff_s = stop_backoff_s

    def reset_running(self) -> int:
        """After a restart nothing is running: put those sessions back (attempts unchanged)."""
        return self._sessions.update_many({"state": "running"}, {"$set": {"state": "closed"}}).modified_count

    def claim(self) -> Claimed | None:
        """The oldest closed session (by ``closed_at``, so one put back keeps its place), now ``running``.

        None while any closed session is waiting out a stop's delay (``retry_after`` in the future): mk wants that
        session to land before any other, so taking the next one would only come back busy."""
        if self._sessions.find_one({"state": "closed", "retry_after": {"$gt": self._clock()}}, {"_id": 1}):
            return None
        doc = self._sessions.find_one_and_update(
            {"state": "closed"},
            {"$set": {"state": "running", "started_at": self._clock()}},
            sort=[("closed_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        # Neither contiguous ``seq`` nor an exact ``message_count`` is assumed, and ``last_ts`` is ignored.
        try:
            found = self._messages.find({"session_id": doc["_id"]}).sort([("timestamp", ASCENDING), ("seq", ASCENDING)])
            messages = _rising([m for m in (self._message(d) for d in found) if m is not None])
        except Exception:
            # The state is already ``running``: put it back, so a failed read leaves nothing stuck.
            self._sessions.update_one({"_id": doc["_id"]}, {"$set": {"state": "closed"}})
            raise
        return {
            "id": doc["_id"],
            "conversation_id": doc["conversation_id"],
            "agent_handles": list(doc.get("agent_handles") or []),
            "messages": messages,
        }

    @staticmethod
    def _message(m: dict[str, Any]) -> dict[str, Any] | None:
        """A message in the kernel's shape; None for one with no text (built) or a missing field (warned)."""
        if not m.get("text"):
            return None
        try:
            return {
                "message_id": str(m["_id"]),
                "speaker": m["speaker"],
                "text": m["text"],
                "timestamp": _utc(m["timestamp"]).isoformat(),
                "addressees": list(m.get("addressees") or []),
            }
        except (KeyError, TypeError, AttributeError, ValueError):
            log.warning("message %s of session %s is malformed and skipped", m.get("_id"), m.get("session_id"))
            return None

    def built(self, session_id: str, reason: str | None = None) -> None:
        fields: dict[str, Any] = {"state": "built", "built_at": self._clock()}
        if reason:
            fields["reason"] = reason
        self._sessions.update_one({"_id": session_id}, {"$set": fields})
        self._messages.update_many({"session_id": session_id}, {"$unset": {"text": ""}})

    def stopped(self, session_id: str, reason: str) -> int:
        """Back to ``closed`` for another try, after a delay of ``backoff * 2 ** (attempts - 1)`` (``retry_after``); the
        attempts so far, this one included."""
        doc = self._sessions.find_one_and_update(
            {"_id": session_id},
            {"$set": {"state": "closed", "reason": reason}, "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return 0
        attempts = int(doc["attempts"])
        delay = timedelta(seconds=self._stop_backoff_s * 2 ** (attempts - 1))
        self._sessions.update_one({"_id": session_id}, {"$set": {"retry_after": self._clock() + delay}})
        return attempts

    def requeue(self, session_id: str, reason: str) -> None:
        """Back to ``closed`` with no attempt counted."""
        self._sessions.update_one({"_id": session_id}, {"$set": {"state": "closed", "reason": reason}})

    def failed(self, session_id: str, reason: str) -> None:
        self._sessions.update_one(
            {"_id": session_id}, {"$set": {"state": "failed", "reason": reason, "failed_at": self._clock()}}
        )

    def last_failed(self) -> str | None:
        """The id of the session that failed most recently, if any."""
        doc = self._sessions.find_one({"state": "failed"}, sort=[("failed_at", -1)])
        return str(doc["_id"]) if doc else None

    def pending_release_due(self, every_s: float) -> bool:
        """True at most once per ``every_s`` seconds (and marks the time)."""
        now = self._clock()
        if self._pending_release_at is not None and now - self._pending_release_at < timedelta(seconds=every_s):
            return False
        self._pending_release_at = now
        return True

    def held(self, session_id: str, reason: str) -> None:
        self._sessions.update_one(
            {"_id": session_id}, {"$set": {"state": "held", "reason": reason, "held_at": self._clock()}}
        )

    def release_held(self, older_than_s: float) -> int:
        """Held sessions older than this many seconds go back to ``closed``."""
        cutoff = self._clock() - timedelta(seconds=older_than_s)
        return self._sessions.update_many(
            {"state": "held", "held_at": {"$lte": cutoff}}, {"$set": {"state": "closed"}}
        ).modified_count
