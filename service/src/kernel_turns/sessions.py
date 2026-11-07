"""Taking in conversation: messages group into sessions, one open session per chat.

The plugin posts messages; a separate writer reads *closed* sessions straight from Mongo and builds them into memory.
The two collections below (in ``KT_MONGO_DB``) are the contract between the two programs. With ``KT_HANDOFF_URL`` set no
writer runs: ``handoff.py`` sends each closed session to that URL instead.

* ``sessions``: ``{_id, bonfire_id, conversation_id, state, agent_handles, opened_at, closed_at, last_message_at,
  message_count, attempts, reason, built_at, last_ts}``. ``state`` is ``open``, ``closed``, ``running``, ``built``,
  ``failed``, ``held`` or ``handed``. This service moves ``open`` to ``closed`` and ``failed`` back to ``closed``; the
  writer owns the rest, except that with a hand-off URL ``closed`` goes to ``handed`` or ``failed`` here and no writer
  runs. ``last_message_at`` is the service clock at the latest message (the quiet window runs from it); ``last_ts`` is
  the latest *stored message* timestamp (the writer ignores it).
* ``messages``: ``{_id: keys.message_key(bonfire_id, message_id), bonfire_id, message_id, session_id, seq, speaker,
  text, timestamp, addressees}``. ``_id`` is unique, so a resend is a duplicate, and the same ``message_id`` in another
  bonfire is another message. The kernel is only ever handed the plain ``message_id``. A message stored before the
  bonfire was in the key has neither field and its plain id as ``_id``; the writer reads it as that id. ``timestamp`` is
  timezone-aware and strictly increasing within a session (mk refuses anything else).

Guarantees the writer may rely on, and what it must not assume:

* A message is only ever added to an ``open`` session: the add is guarded on ``state``, so a session that has been
  closed (and perhaps built) never gains a message. The ids a session holds when it closes are the ids it keeps, which
  is what makes a resent hand-off the same job: the receiver keys it on them. A message arriving later opens the chat's
  next session, or is a duplicate if its id is stored. Mongo inserts before it checks that guard, so this leans on
  ``SessionTake``'s lock, which orders every add and close: a second process could read a message for the moment before
  it is taken back. Hosted runs one, and stops the old task before starting the new.
* The writer must NOT delete a message document. To drop the text after building it removes only ``text`` (``$unset``)
  and keeps ``_id``, ``bonfire_id``, ``message_id``, ``session_id``, ``seq``, ``speaker`` and ``timestamp``, so a resent
  ``message_id`` stays a duplicate. ``text`` may therefore be absent on a message.
* ``timestamp`` is truncated to milliseconds (what BSON keeps) before it is ordered, so stored values are strictly
  increasing within a session.
* After a crash ``seq`` may have a gap or a tie and ``message_count`` may be off by one. Sort a session's messages by
  ``(timestamp, seq)``; never assume ``seq`` is contiguous or ``message_count`` exact.
* A session belongs to one bonfire. The writer takes only sessions of the bonfire it builds, and every read or write
  of a chat, session or message here names a bonfire: what another bonfire holds looks as if it did not exist. Only
  the quiet sweep (``close_quiet``) and the hand-off's look for work (``closed_heads``) span them.

A session closes after ``idle_s`` of quiet, at ``max_messages`` messages, or on request. A message for a chat with no
open session in its bonfire opens a new one.
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

from kernel_turns.keys import message_key
from kernel_turns.store import drop_index_if_there

log = logging.getLogger(__name__)

Doc = dict[str, object]
State = Literal["open", "closed", "running", "built", "failed", "held", "handed"]
Retry = Literal["retried", "unknown", "not_failed"]


class SessionClosed(Exception):
    """The session was closed between being opened for an add and the add."""


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
    #: a stopped or put-off session is not tried again before this
    retry_after: datetime | None = None
    handed_at: datetime | None = None
    failed_at: datetime | None = None


class StoredMessage(BaseModel):
    bonfire_id: str
    message_id: str
    session_id: str
    seq: int
    speaker: str
    text: str | None  #: None once the writer has removed it
    timestamp: datetime
    addressees: list[str]


class SessionStore(Protocol):
    async def has_message(self, bonfire_id: str, message_id: str) -> bool: ...

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        """The chat's open session in this bonfire, created if there is none. Two callers at once get the same one."""
        ...

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        """Store ``message`` (at the already-ordered ``timestamp``) as the session's next; its ``seq``, or None when
        the ``message_id`` is already stored in the session's bonfire. Raises ``SessionClosed`` when the session is no
        longer open."""
        ...

    async def close_session(self, bonfire_id: str, session_id: str, now: datetime) -> None: ...

    async def close_open(self, bonfire_id: str, conversation_id: str, now: datetime) -> bool:
        """Close the chat's open session in this bonfire; False when it has none."""
        ...

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        """Close every open session, of any bonfire, whose latest message was at or before ``last_before``; how many."""
        ...

    async def retry(self, bonfire_id: str, session_id: str) -> Retry:
        """``failed`` back to ``closed`` with ``reason`` cleared, ``attempts`` 0 and no ``retry_after``. ``unknown`` for
        another bonfire's session."""
        ...

    async def waiting(self, bonfire_id: str) -> list[Session]:
        """The bonfire's sessions in state ``open``, ``closed``, ``running``, ``held`` or ``failed``."""
        ...

    async def last_built_at(self, bonfire_id: str) -> datetime | None:
        """The newest ``built_at`` of the bonfire's sessions."""
        ...

    async def closed_heads(self, now: datetime) -> list[Session]:
        """Each bonfire's oldest ``closed`` session by ``(closed_at, id)``, of any bonfire. A head whose ``retry_after``
        is still to come is left out and nothing behind it takes its place: a bonfire's sessions go in order."""
        ...

    async def messages_of(self, bonfire_id: str, session_id: str) -> list[StoredMessage]:
        """The session's messages, by ``(timestamp, seq)`` and then id, so that a tie reads the same way every time."""
        ...

    async def mark_handed(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        """``closed`` to ``handed``, then the text goes (the ids stay, so a resent message is still a duplicate). In that
        order: text gone from a session still ``closed`` would send fewer ids next time, and that is another job."""
        ...

    async def mark_refused(self, bonfire_id: str, session_id: str, reason: str, now: datetime) -> None:
        """``closed`` to ``failed`` with ``reason``. The text stays, for ``retry``."""
        ...

    async def put_off(self, bonfire_id: str, session_id: str, reason: str, until: datetime) -> None:
        """Stays ``closed`` and waits for ``until``, with one more attempt counted."""
        ...


WAITING_STATES = ("open", "closed", "running", "held", "failed")


def _due(session: Session, now: datetime) -> bool:
    return session.retry_after is None or session.retry_after <= now


class MemorySessionStore:
    """``SessionStore`` in process memory; ``sessions`` and ``messages`` (by ``message_key``) are open for tests to read
    and set."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.messages: dict[str, StoredMessage] = {}

    async def has_message(self, bonfire_id: str, message_id: str) -> bool:
        return message_key(bonfire_id, message_id) in self.messages

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        for session in self.sessions.values():
            if self._is_open_chat(session, bonfire_id, conversation_id):
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

    @staticmethod
    def _is_open_chat(session: Session, bonfire_id: str, conversation_id: str) -> bool:
        return (
            session.bonfire_id == bonfire_id and session.conversation_id == conversation_id and session.state == "open"
        )

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        key = message_key(session.bonfire_id, message.message_id)
        if key in self.messages:
            return None
        stored = self.sessions[session.id]
        if stored.state != "open":
            raise SessionClosed(session.id)
        stored.message_count += 1
        stored.last_message_at = now
        stored.last_ts = timestamp
        stored.agent_handles += [h for h in agent_handles if h not in stored.agent_handles]
        self.messages[key] = StoredMessage(
            bonfire_id=session.bonfire_id,
            message_id=message.message_id,
            session_id=session.id,
            seq=stored.message_count,
            speaker=message.speaker,
            text=message.text,
            timestamp=timestamp,
            addressees=message.addressees or [],
        )
        return stored.message_count

    async def close_session(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        session = self.sessions.get(session_id)
        if session is not None and session.bonfire_id == bonfire_id and session.state == "open":
            session.state, session.closed_at = "closed", now

    async def close_open(self, bonfire_id: str, conversation_id: str, now: datetime) -> bool:
        for session in self.sessions.values():
            if self._is_open_chat(session, bonfire_id, conversation_id):
                session.state, session.closed_at = "closed", now
                return True
        return False

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        quiet = [s for s in self.sessions.values() if s.state == "open" and s.last_message_at <= last_before]
        for session in quiet:
            session.state, session.closed_at = "closed", now
        return len(quiet)

    async def retry(self, bonfire_id: str, session_id: str) -> Retry:
        session = self.sessions.get(session_id)
        if session is None or session.bonfire_id != bonfire_id:
            return "unknown"
        if session.state != "failed":
            return "not_failed"
        session.state, session.reason, session.attempts, session.retry_after = "closed", None, 0, None
        return "retried"

    async def waiting(self, bonfire_id: str) -> list[Session]:
        return [
            s.model_copy() for s in self.sessions.values() if s.bonfire_id == bonfire_id and s.state in WAITING_STATES
        ]

    async def last_built_at(self, bonfire_id: str) -> datetime | None:
        built = [s.built_at for s in self.sessions.values() if s.bonfire_id == bonfire_id and s.built_at is not None]
        return max(built, default=None)

    async def closed_heads(self, now: datetime) -> list[Session]:
        closed = [s for s in self.sessions.values() if s.state == "closed"]
        heads: dict[str, Session] = {}
        for session in sorted(closed, key=lambda s: (s.closed_at or s.opened_at, s.id)):
            heads.setdefault(session.bonfire_id, session)
        return [s.model_copy() for s in heads.values() if _due(s, now)]

    async def messages_of(self, bonfire_id: str, session_id: str) -> list[StoredMessage]:
        mine = [m for m in self.messages.values() if m.session_id == session_id and m.bonfire_id == bonfire_id]
        return [m.model_copy() for m in sorted(mine, key=lambda m: (m.timestamp, m.seq, m.message_id))]

    def _closed(self, bonfire_id: str, session_id: str) -> Session | None:
        session = self.sessions.get(session_id)
        return session if session and session.bonfire_id == bonfire_id and session.state == "closed" else None

    async def mark_handed(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        session = self._closed(bonfire_id, session_id)
        if session is None:
            return
        session.state, session.handed_at, session.reason, session.retry_after = "handed", now, None, None
        for message in self.messages.values():
            if message.session_id == session_id:
                message.text = None

    async def mark_refused(self, bonfire_id: str, session_id: str, reason: str, now: datetime) -> None:
        session = self._closed(bonfire_id, session_id)
        if session is not None:
            session.state, session.reason, session.failed_at, session.retry_after = "failed", reason, now, None

    async def put_off(self, bonfire_id: str, session_id: str, reason: str, until: datetime) -> None:
        session = self._closed(bonfire_id, session_id)
        if session is not None:
            session.reason, session.retry_after = reason, until
            session.attempts += 1


class MongoSessionStore:
    """``SessionStore`` over MongoDB. Call ``ensure_indexes`` once at start.

    The atomic steps: a unique ``messages._id`` is the dedupe; ``find_one_and_update(upsert)`` on
    ``{bonfire_id, conversation_id, state: "open"}`` is the open session, and a unique partial index on ``(bonfire_id,
    conversation_id)`` where ``state`` is ``open`` makes a second concurrent upsert fail with a duplicate key (the loser
    reads the winner's).
    """

    def __init__(self, client: AsyncMongoClient[Doc], db: str) -> None:
        database = client[db]
        self._sessions = database["sessions"]
        self._messages = database["messages"]

    async def ensure_indexes(self) -> None:
        # A new name, because a kept volume holds the old key: re-creating a name with other keys throws. And the old
        # index must go: left in place it refuses a second bonfire's session on the same chat.
        await drop_index_if_there(self._sessions, "one_open_session_per_chat")
        await self._sessions.create_index(
            [("bonfire_id", ASCENDING), ("conversation_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"state": "open"},
            name="one_open_session_per_chat_v2",
        )
        await self._sessions.create_index([("state", ASCENDING), ("last_message_at", ASCENDING)], name="quiet_sweep")
        await self._sessions.create_index(
            [("state", ASCENDING), ("closed_at", ASCENDING), ("_id", ASCENDING)], name="closed_heads"
        )
        await self._messages.create_index(
            [("session_id", ASCENDING), ("timestamp", ASCENDING), ("seq", ASCENDING), ("_id", ASCENDING)],
            name="session_messages",
        )

    async def has_message(self, bonfire_id: str, message_id: str) -> bool:
        return await self._messages.find_one({"_id": message_key(bonfire_id, message_id)}, {"_id": 1}) is not None

    async def open_session(
        self, conversation_id: str, bonfire_id: str, agent_handles: list[str], now: datetime
    ) -> Session:
        open_filter: Doc = {"bonfire_id": bonfire_id, "conversation_id": conversation_id, "state": "open"}
        fresh: Doc = {
            "_id": uuid.uuid4().hex,
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
        seq = session.message_count + 1  # the lock orders adds; after a crash a gap or a tie is accepted
        key = message_key(session.bonfire_id, message.message_id)
        try:
            await self._messages.insert_one(
                {
                    "_id": key,
                    "bonfire_id": session.bonfire_id,
                    "message_id": message.message_id,
                    "session_id": session.id,
                    "seq": seq,
                    "speaker": message.speaker,
                    "text": message.text,
                    "timestamp": timestamp,
                    "addressees": message.addressees or [],
                }
            )
        except DuplicateKeyError:
            return None
        counted = await self._sessions.update_one(
            {"_id": session.id, "bonfire_id": session.bonfire_id, "state": "open"},
            {
                "$inc": {"message_count": 1},
                "$set": {"last_message_at": now, "last_ts": timestamp},
                "$addToSet": {"agent_handles": {"$each": agent_handles}},
            },
        )
        if counted.matched_count == 0:  # closed since we opened it: the message belongs in the next session
            await self._messages.delete_one({"_id": key, "session_id": session.id})
            raise SessionClosed(session.id)
        return seq

    async def close_session(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        await self._sessions.update_one(
            {"_id": session_id, "bonfire_id": bonfire_id, "state": "open"},
            {"$set": {"state": "closed", "closed_at": now}},
        )

    async def close_open(self, bonfire_id: str, conversation_id: str, now: datetime) -> bool:
        result = await self._sessions.update_one(
            {"bonfire_id": bonfire_id, "conversation_id": conversation_id, "state": "open"},
            {"$set": {"state": "closed", "closed_at": now}},
        )
        return result.modified_count == 1

    async def close_quiet(self, last_before: datetime, now: datetime) -> int:
        result = await self._sessions.update_many(
            {"state": "open", "last_message_at": {"$lte": last_before}}, {"$set": {"state": "closed", "closed_at": now}}
        )
        return result.modified_count

    async def retry(self, bonfire_id: str, session_id: str) -> Retry:
        mine: Doc = {"_id": session_id, "bonfire_id": bonfire_id}
        result = await self._sessions.update_one(
            {**mine, "state": "failed"},
            {"$set": {"state": "closed", "reason": None, "attempts": 0}, "$unset": {"retry_after": ""}},
        )
        if result.modified_count == 1:
            return "retried"
        return "not_failed" if await self._sessions.find_one(mine, {"_id": 1}) else "unknown"

    async def waiting(self, bonfire_id: str) -> list[Session]:
        cursor = self._sessions.find({"bonfire_id": bonfire_id, "state": {"$in": list(WAITING_STATES)}})
        return [_session(doc) async for doc in cursor]

    async def last_built_at(self, bonfire_id: str) -> datetime | None:
        doc = await self._sessions.find_one(
            {"bonfire_id": bonfire_id, "built_at": {"$ne": None}}, {"built_at": 1}, sort=[("built_at", -1)]
        )
        built = doc.get("built_at") if doc else None
        return built if isinstance(built, datetime) else None

    async def closed_heads(self, now: datetime) -> list[Session]:
        cursor = await self._sessions.aggregate(
            [
                {"$match": {"state": "closed"}},
                {"$sort": {"closed_at": ASCENDING, "_id": ASCENDING}},
                {"$group": {"_id": "$bonfire_id", "head": {"$first": "$$ROOT"}}},
                {"$replaceWith": "$head"},
            ]
        )
        return [session for session in [_session(doc) async for doc in cursor] if _due(session, now)]

    async def messages_of(self, bonfire_id: str, session_id: str) -> list[StoredMessage]:
        # A message stored before the bonfire was in the key has no `bonfire_id`, and None matches that.
        mine: Doc = {"session_id": session_id, "bonfire_id": {"$in": [bonfire_id, None]}}
        cursor = self._messages.find(mine).sort([("timestamp", ASCENDING), ("seq", ASCENDING), ("_id", ASCENDING)])
        return [_message(doc, bonfire_id) async for doc in cursor]

    @staticmethod
    def _closed(bonfire_id: str, session_id: str) -> Doc:
        return {"_id": session_id, "bonfire_id": bonfire_id, "state": "closed"}

    async def mark_handed(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        marked = await self._sessions.update_one(
            self._closed(bonfire_id, session_id),
            {"$set": {"state": "handed", "handed_at": now, "reason": None}, "$unset": {"retry_after": ""}},
        )
        if marked.matched_count == 1:  # the write below is keyed by session id alone: never on its own
            await self._messages.update_many({"session_id": session_id}, {"$unset": {"text": ""}})

    async def mark_refused(self, bonfire_id: str, session_id: str, reason: str, now: datetime) -> None:
        await self._sessions.update_one(
            self._closed(bonfire_id, session_id),
            {"$set": {"state": "failed", "reason": reason, "failed_at": now}, "$unset": {"retry_after": ""}},
        )

    async def put_off(self, bonfire_id: str, session_id: str, reason: str, until: datetime) -> None:
        await self._sessions.update_one(
            self._closed(bonfire_id, session_id),
            {"$set": {"reason": reason, "retry_after": until}, "$inc": {"attempts": 1}},
        )


def _session(doc: Doc) -> Session:
    return Session.model_validate({**doc, "id": doc["_id"]})


def _message(doc: Doc, bonfire_id: str) -> StoredMessage:
    """A message as read back: ``text`` may be gone, and one stored before the bonfire was in the key has neither
    ``bonfire_id`` nor ``message_id``: its ``_id`` is the plain id."""
    return StoredMessage.model_validate(
        {
            **doc,
            "bonfire_id": bonfire_id,
            "message_id": doc.get("message_id", doc["_id"]),
            "text": doc.get("text"),
            "addressees": doc.get("addressees") or [],
        }
    )


class SessionTake:
    """Groups messages into sessions and closes them. One lock, across bonfires, orders the seq and timestamps of a
    batch (one small process); the store's unique index is what keeps a second service process from opening a second
    session."""

    def __init__(
        self,
        store: SessionStore,
        idle_s: float,
        max_messages: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._idle = timedelta(seconds=idle_s)
        self._max = max_messages
        self._clock = clock
        self._lock = asyncio.Lock()

    async def add(self, bonfire_id: str, batch: list[MessageIn], agent_handles: list[str]) -> AddResult:
        accepted = duplicates = 0
        async with self._lock:
            for message in batch:
                if await self._store.has_message(bonfire_id, message.message_id):
                    duplicates += 1
                    continue
                stored = await self._append(bonfire_id, message, agent_handles)
                if stored is None:
                    duplicates += 1
                    continue
                accepted += 1
                seq, session_id, now = stored
                if seq >= self._max:
                    await self._store.close_session(bonfire_id, session_id, now)
        return AddResult(accepted=accepted, duplicates=duplicates)

    async def _append(
        self, bonfire_id: str, message: MessageIn, agent_handles: list[str]
    ) -> tuple[int, str, datetime] | None:
        """Add one message to its chat's open session: ``(seq, session id, now)``, or None for a duplicate."""
        while True:
            now = self._clock()
            session = await self._store.open_session(message.conversation_id, bonfire_id, agent_handles, now)
            # BSON keeps milliseconds: order on what will be stored, and keep mk's times strictly increasing
            timestamp = message.timestamp.replace(microsecond=message.timestamp.microsecond // 1000 * 1000)
            if session.last_ts is not None and timestamp <= session.last_ts:
                timestamp = session.last_ts + timedelta(milliseconds=1)
            try:
                seq = await self._store.append(session, message, timestamp, agent_handles, now)
            except SessionClosed:
                continue  # closed under us (the sweep): open the next one and try again
            return None if seq is None else (seq, session.id, now)

    async def close(self, bonfire_id: str, conversation_id: str) -> bool:
        async with self._lock:
            return await self._store.close_open(bonfire_id, conversation_id, self._clock())

    async def close_idle(self) -> int:
        async with self._lock:  # never between an add's insert and its update
            now = self._clock()
            return await self._store.close_quiet(now - self._idle, now)

    async def retry(self, bonfire_id: str, session_id: str) -> Retry:
        return await self._store.retry(bonfire_id, session_id)
