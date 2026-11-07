"""Taking in conversation: messages group into sessions, one open session per chat.

The plugin posts messages; a separate writer reads *closed* sessions straight from Mongo and builds them into memory.
The two collections below (in ``KT_MONGO_DB``) are the contract between the two programs:

* ``sessions``: ``{_id, bonfire_id, conversation_id, state, agent_handles, opened_at, closed_at, last_message_at,
  message_count, attempts, reason, built_at, last_ts}``. ``state`` is ``open``, ``closed``, ``running``, ``built``,
  ``failed`` or ``held``; this service only ever moves ``open`` to ``closed`` and ``failed`` back to ``closed``
  (the writer owns the rest). ``last_message_at`` is the service clock at the latest message (the quiet window runs
  from it); ``last_ts`` is the latest *stored message* timestamp (the writer ignores it).
* ``messages``: ``{_id: message_id, session_id, seq, speaker, text, timestamp, addressees}``. ``_id`` is unique, so a
  resend is a duplicate. ``timestamp`` is timezone-aware and strictly increasing within a session (mk refuses
  anything else).

A session closes after ``idle_s`` of quiet, at ``max_messages`` messages, or on request. A message for a chat with no
open session opens a new one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, Field
from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

log = logging.getLogger(__name__)

Doc = dict[str, object]
State = Literal["open", "closed", "running", "built", "failed", "held"]
Retry = Literal["retried", "unknown", "not_failed"]


class MessageIn(BaseModel):
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)
    #: must carry a timezone: mk refuses a naive one
    timestamp: AwareDatetime
    addressees: list[str] | None = None


class AddRequest(BaseModel):
    messages: list[MessageIn] = Field(max_length=500)
    agent_handles: list[str] = []


class AddResult(BaseModel):
    accepted: int
    duplicates: int


class CloseRequest(BaseModel):
    conversation_id: str = Field(min_length=1)


class CloseResult(BaseModel):
    closed: bool


class Session(BaseModel):
    id: str
    bonfire_id: str
    conversation_id: str
    state: State
    agent_handles: list[str]
    opened_at: datetime
    closed_at: datetime | None = None
    last_message_at: datetime
    message_count: int = 0
    attempts: int = 0
    reason: str | None = None
    built_at: datetime | None = None
    last_ts: datetime | None = None


class StoredMessage(BaseModel):
    message_id: str
    session_id: str
    seq: int
    speaker: str
    text: str
    timestamp: datetime
    addressees: list[str]


class SessionStore(Protocol):
    async def has_message(self, message_id: str) -> bool: ...

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        """The chat's open session, created if there is none. Two callers at once get the same one."""
        ...

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        """Store ``message`` (at the already-ordered ``timestamp``) as the session's next; its ``seq``, or None when
        the ``message_id`` is already stored."""
        ...

    async def close_session(self, session_id: str, now: datetime) -> None: ...

    async def close_open(self, conversation_id: str, now: datetime) -> bool:
        """Close the chat's open session; False when it has none."""
        ...

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        """Close every open session whose latest message was at or before ``last_before``; how many."""
        ...

    async def retry(self, session_id: str) -> Retry:
        """``failed`` back to ``closed`` with ``reason`` cleared and ``attempts`` 0."""
        ...


class MemorySessionStore:
    """``SessionStore`` in process memory; ``sessions`` and ``messages`` are open for tests to read and set."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.messages: dict[str, StoredMessage] = {}

    async def has_message(self, message_id: str) -> bool:
        return message_id in self.messages

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        for session in self.sessions.values():
            if session.conversation_id == conversation_id and session.state == "open":
                return session.model_copy()
        session = Session(
            id=uuid.uuid4().hex,
            bonfire_id=bonfire_id,
            conversation_id=conversation_id,
            state="open",
            agent_handles=list(agent_handles),
            opened_at=now,
            last_message_at=now,
        )
        self.sessions[session.id] = session
        return session.model_copy()

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        if message.message_id in self.messages:
            return None
        stored = self.sessions[session.id]
        stored.message_count += 1
        stored.last_message_at = now
        stored.last_ts = timestamp
        stored.agent_handles += [h for h in agent_handles if h not in stored.agent_handles]
        self.messages[message.message_id] = StoredMessage(
            message_id=message.message_id,
            session_id=session.id,
            seq=stored.message_count,
            speaker=message.speaker,
            text=message.text,
            timestamp=timestamp,
            addressees=message.addressees or [],
        )
        return stored.message_count

    async def close_session(self, session_id: str, now: datetime) -> None:
        session = self.sessions[session_id]
        if session.state == "open":
            session.state, session.closed_at = "closed", now

    async def close_open(self, conversation_id: str, now: datetime) -> bool:
        for session in self.sessions.values():
            if session.conversation_id == conversation_id and session.state == "open":
                session.state, session.closed_at = "closed", now
                return True
        return False

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        quiet = [s for s in self.sessions.values() if s.state == "open" and s.last_message_at <= last_before]
        for session in quiet:
            session.state, session.closed_at = "closed", now
        return len(quiet)

    async def retry(self, session_id: str) -> Retry:
        session = self.sessions.get(session_id)
        if session is None:
            return "unknown"
        if session.state != "failed":
            return "not_failed"
        session.state, session.reason, session.attempts = "closed", None, 0
        return "retried"


class MongoSessionStore:
    """``SessionStore`` over MongoDB. Call ``ensure_indexes`` once at start.

    The atomic steps: a unique ``messages._id`` is the dedupe; ``find_one_and_update(upsert)`` on
    ``{conversation_id, state: "open"}`` is the open session, and a unique partial index on ``conversation_id`` where
    ``state`` is ``open`` makes a second concurrent upsert fail with a duplicate key (the loser reads the winner's).
    """

    def __init__(self, client: AsyncMongoClient[Doc], db: str) -> None:
        database = client[db]
        self._sessions = database["sessions"]
        self._messages = database["messages"]

    async def ensure_indexes(self) -> None:
        await self._sessions.create_index(
            [("conversation_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"state": "open"},
            name="one_open_session_per_chat",
        )
        await self._sessions.create_index([("state", ASCENDING), ("last_message_at", ASCENDING)], name="quiet_sweep")

    async def has_message(self, message_id: str) -> bool:
        return await self._messages.find_one({"_id": message_id}, {"_id": 1}) is not None

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        open_filter: Doc = {"conversation_id": conversation_id, "state": "open"}
        fresh: Doc = {
            "_id": uuid.uuid4().hex,
            "bonfire_id": bonfire_id,
            "agent_handles": agent_handles,
            "opened_at": now,
            "closed_at": None,
            "last_message_at": now,
            "message_count": 0,
            "attempts": 0,
            "reason": None,
            "built_at": None,
            "last_ts": None,
        }
        try:
            doc = await self._sessions.find_one_and_update(
                open_filter, {"$setOnInsert": fresh}, upsert=True, return_document=ReturnDocument.AFTER
            )
        except DuplicateKeyError:  # a concurrent request opened it between our find and insert
            doc = await self._sessions.find_one(open_filter)
        if doc is None:  # it closed again in that instant: take the next one
            return await self.open_session(conversation_id, bonfire_id, agent_handles, now)
        return _session(doc)

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        after = await self._sessions.find_one_and_update(
            {"_id": session.id},
            {
                "$inc": {"message_count": 1},
                "$set": {"last_message_at": now, "last_ts": timestamp},
                "$addToSet": {"agent_handles": {"$each": agent_handles}},
            },
            return_document=ReturnDocument.AFTER,
        )
        if after is None:
            raise LookupError(f"no session {session.id!r}")
        seq = int(str(after["message_count"]))
        try:
            await self._messages.insert_one(
                {
                    "_id": message.message_id,
                    "session_id": session.id,
                    "seq": seq,
                    "speaker": message.speaker,
                    "text": message.text,
                    "timestamp": timestamp,
                    "addressees": message.addressees or [],
                }
            )
        except DuplicateKeyError:
            await self._sessions.update_one({"_id": session.id}, {"$inc": {"message_count": -1}})
            return None
        return seq

    async def close_session(self, session_id: str, now: datetime) -> None:
        await self._sessions.update_one(
            {"_id": session_id, "state": "open"}, {"$set": {"state": "closed", "closed_at": now}}
        )

    async def close_open(self, conversation_id: str, now: datetime) -> bool:
        result = await self._sessions.update_one(
            {"conversation_id": conversation_id, "state": "open"}, {"$set": {"state": "closed", "closed_at": now}}
        )
        return result.modified_count == 1

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        result = await self._sessions.update_many(
            {"state": "open", "last_message_at": {"$lte": last_before}}, {"$set": {"state": "closed", "closed_at": now}}
        )
        return result.modified_count

    async def retry(self, session_id: str) -> Retry:
        result = await self._sessions.update_one(
            {"_id": session_id, "state": "failed"}, {"$set": {"state": "closed", "reason": None, "attempts": 0}}
        )
        if result.modified_count == 1:
            return "retried"
        return "not_failed" if await self._sessions.find_one({"_id": session_id}, {"_id": 1}) else "unknown"


def _session(doc: Doc) -> Session:
    return Session.model_validate({**doc, "id": doc["_id"]})


class SessionTake:
    """Groups messages into sessions and closes them. One lock orders the seq and timestamps of a batch; the store's
    unique index is what keeps a second service process from opening a second session."""

    def __init__(
        self,
        store: SessionStore,
        idle_s: float,
        max_messages: int,
        clock: Callable[[], datetime],
        *,
        bonfire_id: str,
    ) -> None:
        self._store = store
        self._idle = timedelta(seconds=idle_s)
        self._max = max_messages
        self._clock = clock
        self._bonfire_id = bonfire_id
        self._lock = asyncio.Lock()

    async def add(self, batch: list[MessageIn], agent_handles: list[str]) -> AddResult:
        accepted = duplicates = 0
        async with self._lock:
            for message in batch:
                if await self._store.has_message(message.message_id):
                    duplicates += 1
                    continue
                now = self._clock()
                session = await self._store.open_session(message.conversation_id, self._bonfire_id, agent_handles, now)
                timestamp = message.timestamp
                if session.last_ts is not None and timestamp <= session.last_ts:
                    timestamp = session.last_ts + timedelta(milliseconds=1)  # mk wants strictly increasing times
                seq = await self._store.append(session, message, timestamp, agent_handles, now)
                if seq is None:
                    duplicates += 1
                    continue
                accepted += 1
                if seq >= self._max:
                    await self._store.close_session(session.id, now)
        return AddResult(accepted=accepted, duplicates=duplicates)

    async def close(self, conversation_id: str) -> bool:
        async with self._lock:
            return await self._store.close_open(conversation_id, self._clock())

    async def close_idle(self) -> int:
        now = self._clock()
        return await self._store.close_quiet(now - self._idle, now)

    async def retry(self, session_id: str) -> Retry:
        return await self._store.retry(session_id)
