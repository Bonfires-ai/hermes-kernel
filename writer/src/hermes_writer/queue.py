"""The writer's side of the sessions contract (see ``kernel_turns.sessions`` in the turn service).

Only the writer moves a session out of ``closed``: ``running``, ``built``, ``failed``, ``held``. It never deletes a
document: a built session keeps its message ids (only ``text`` goes), so a resent message stays a duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from pymongo import ASCENDING, ReturnDocument
from pymongo.database import Database


class Claimed(TypedDict):
    id: str
    conversation_id: str
    agent_handles: list[str]
    messages: list[dict[str, Any]]


def _utc(when: datetime) -> datetime:
    # Mongo hands back naive UTC.
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


class Queue:
    def __init__(self, db: Database[Any], *, clock: Callable[[], datetime] | None = None) -> None:
        self._sessions = db["sessions"]
        self._messages = db["messages"]
        self._clock = clock or (lambda: datetime.now(UTC))

    def reset_running(self) -> int:
        """After a restart nothing is running: put those sessions back (attempts unchanged)."""
        return self._sessions.update_many({"state": "running"}, {"$set": {"state": "closed"}}).modified_count

    def claim(self) -> Claimed | None:
        """The oldest closed session (by ``closed_at``, so one put back keeps its place), now ``running``."""
        doc = self._sessions.find_one_and_update(
            {"state": "closed"},
            {"$set": {"state": "running", "started_at": self._clock()}},
            sort=[("closed_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        # Neither contiguous ``seq`` nor an exact ``message_count`` is assumed, and ``last_ts`` is ignored.
        found = self._messages.find({"session_id": doc["_id"]}).sort([("timestamp", ASCENDING), ("seq", ASCENDING)])
        messages = [
            {
                "message_id": str(m["_id"]),
                "speaker": m["speaker"],
                "text": m["text"],
                "timestamp": _utc(m["timestamp"]).isoformat(),
                "addressees": list(m.get("addressees") or []),
            }
            for m in found
            if m.get("text")
        ]
        return {
            "id": doc["_id"],
            "conversation_id": doc["conversation_id"],
            "agent_handles": list(doc.get("agent_handles") or []),
            "messages": messages,
        }

    def built(self, session_id: str) -> None:
        self._sessions.update_one({"_id": session_id}, {"$set": {"state": "built", "built_at": self._clock()}})
        self._messages.update_many({"session_id": session_id}, {"$unset": {"text": ""}})

    def stopped(self, session_id: str, reason: str) -> int:
        """Back to ``closed`` for another try; the attempts so far, this one included."""
        doc = self._sessions.find_one_and_update(
            {"_id": session_id},
            {"$set": {"state": "closed", "reason": reason}, "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["attempts"]) if doc else 0

    def requeue(self, session_id: str, reason: str) -> None:
        """Back to ``closed`` with no attempt counted."""
        self._sessions.update_one({"_id": session_id}, {"$set": {"state": "closed", "reason": reason}})

    def failed(self, session_id: str, reason: str) -> None:
        self._sessions.update_one({"_id": session_id}, {"$set": {"state": "failed", "reason": reason}})

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
