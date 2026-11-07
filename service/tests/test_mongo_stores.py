"""One bonfire can never reach another's rows: the stores' behaviour over memory and over a real MongoDB.

The Mongo cases run only with ``KT_TEST_MONGO_URI`` set (a throwaway server); the memory cases always run, and the
behaviour tests are the same for both.

The behaviour tests share one database and empty it between tests. mongod keeps a file open for every collection and
index until the checkpoint after its drop, so a database per test passes a container's default 1024 open files in a
few fast runs, and WiredTiger's panic ends the server. Only the tests of the indexes themselves take a fresh one.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import ASCENDING, AsyncMongoClient, MongoClient
from pymongo.errors import DuplicateKeyError

from kernel_turns.app import create_app
from kernel_turns.config import Settings
from kernel_turns.handoff import Body, Outcome, hand_over_once
from kernel_turns.keys import message_key
from kernel_turns.models import LedgerRow, PendingStep, TurnRecord
from kernel_turns.sessions import (
    MemorySessionStore,
    MessageIn,
    MongoSessionStore,
    Session,
    SessionClosed,
    SessionStore,
    SessionTake,
)
from kernel_turns.store import Doc, MemoryTurnStore, MongoTurnStore, TurnStore
from kernel_turns.turns import TurnEngine, UnknownTurn
from tests.fakes import FakeMk, Script, team_turn

MONGO_URI = os.environ.get("KT_TEST_MONGO_URI")
NO_MONGO = "KT_TEST_MONGO_URI is not set"

A, B = "bonfire-a", "bonfire-b"
AT = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
HANDLES = ["hermes"]
THIRTY_DAYS_S = 2592000

Database = tuple[AsyncMongoClient[Doc], str]


@dataclass
class Stores:
    turns: TurnStore
    sessions: SessionStore


@asynccontextmanager
async def _database() -> AsyncIterator[Database]:
    """A fresh database on the test server, gone afterwards."""
    assert MONGO_URI
    client: AsyncMongoClient[Doc] = AsyncMongoClient(MONGO_URI, tz_aware=True)
    name = f"kt_test_{uuid.uuid4().hex}"
    try:
        yield client, name
    finally:
        await client.drop_database(name)
        await client.close()


@pytest.fixture
async def mongo() -> AsyncIterator[Database]:
    if not MONGO_URI:
        pytest.skip(NO_MONGO)
    async with _database() as database:
        yield database


@pytest.fixture(scope="session")
def shared_database() -> Iterator[str]:
    name = f"kt_test_{uuid.uuid4().hex}"
    yield name
    if MONGO_URI:
        with MongoClient(MONGO_URI) as client:
            client.drop_database(name)


@pytest.fixture(params=["memory", pytest.param("mongo", marks=pytest.mark.skipif(not MONGO_URI, reason=NO_MONGO))])
async def store(request: pytest.FixtureRequest, shared_database: str) -> AsyncIterator[Stores]:
    if request.param == "memory":
        yield Stores(MemoryTurnStore(), MemorySessionStore())
        return
    assert MONGO_URI
    client: AsyncMongoClient[Doc] = AsyncMongoClient(MONGO_URI, tz_aware=True)
    turns, sessions = MongoTurnStore(client, shared_database), MongoSessionStore(client, shared_database)
    try:
        await turns.ensure_indexes()
        await sessions.ensure_indexes()
        yield Stores(turns, sessions)
    finally:
        for collection in await client[shared_database].list_collection_names():
            await client[shared_database][collection].delete_many({})
        await client.close()


def _take(store: Stores) -> SessionTake:
    return SessionTake(store.sessions, 1800, 100, lambda: AT)


def _msg(message_id: str, chat: str = "chat") -> MessageIn:
    return MessageIn(conversation_id=chat, message_id=message_id, speaker="Kira", text="hi", timestamp=AT)


def _turn(turn_id: str, bonfire_id: str) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        platform="cli",
        group="group",
        chat="chat",
        message_id=turn_id,
        speaker="Kira",
        text="look",
        at=AT,
        participants=[],
        bonfire_id=bonfire_id,
        plan=None,
        status="unread",
        licence=None,
        pending=None,
        phrase=None,
    )


def _row(bonfire_id: str, value: str) -> LedgerRow:
    return LedgerRow(
        bonfire_id=bonfire_id,
        turn_id="t1",
        step_id="p:0:0",
        tool="take",
        args={"item": "thé"},
        value=value,
        verdict="committed",
        at=AT,
    )


# -- messages and sessions -------------------------------------------------------------------------------------------


async def test_same_message_id_in_two_bonfires_is_two_messages(store: Stores) -> None:
    take = _take(store)
    assert (await take.add(A, [_msg("m1")], HANDLES)).accepted == 1
    second = await take.add(B, [_msg("m1")], HANDLES)
    assert (second.accepted, second.duplicates) == (1, 0)
    again = await take.add(A, [_msg("m1")], HANDLES)
    assert (again.accepted, again.duplicates) == (0, 1)
    assert await store.sessions.has_message(A, "m1")
    assert await store.sessions.has_message(B, "m1")
    assert not await store.sessions.has_message("bonfire-c", "m1")


async def test_same_chat_in_two_bonfires_has_two_open_sessions(store: Stores) -> None:
    a = await store.sessions.open_session("chat", A, HANDLES, AT)
    b = await store.sessions.open_session("chat", B, HANDLES, AT)
    assert a.id != b.id
    assert (a.bonfire_id, b.bonfire_id) == (A, B)
    assert (await store.sessions.open_session("chat", A, HANDLES, AT)).id == a.id
    assert await store.sessions.close_open(A, "chat", AT) is True
    assert await store.sessions.close_open(A, "chat", AT) is False  # A's is closed; B's stays open
    assert (await store.sessions.open_session("chat", B, HANDLES, AT)).id == b.id


async def test_a_session_is_closed_and_retried_only_in_its_bonfire(store: Stores) -> None:
    session = await store.sessions.open_session("chat", A, HANDLES, AT)
    assert await store.sessions.retry(A, session.id) == "not_failed"
    assert await store.sessions.retry(B, session.id) == "unknown"  # as if it did not exist, not "not failed"
    await store.sessions.close_session(B, session.id, AT)
    assert [s.state for s in await store.sessions.waiting(A)] == ["open"]
    await store.sessions.close_session(A, session.id, AT)
    assert [s.state for s in await store.sessions.waiting(A)] == ["closed"]
    assert await store.sessions.waiting(B) == []


# -- turns and the ledger --------------------------------------------------------------------------------------------


async def test_get_turn_of_another_bonfire_is_none(store: Stores) -> None:
    await store.turns.save_turn(_turn("t1", A))
    assert await store.turns.get_turn(B, "t1") is None
    found = await store.turns.get_turn(A, "t1")
    assert found is not None
    assert found.bonfire_id == A


async def test_a_turn_id_another_bonfire_holds_is_never_overwritten(mongo: Database) -> None:
    client, name = mongo
    turns = MongoTurnStore(client, name)
    await turns.save_turn(_turn("t1", A))
    with pytest.raises(DuplicateKeyError):
        await turns.save_turn(_turn("t1", B))
    assert await turns.get_turn(B, "t1") is None
    assert await turns.get_turn(A, "t1") is not None


async def test_each_bonfire_has_its_own_ledger_rows(store: Stores) -> None:
    await store.turns.add_ledger(_row(A, "a's"))
    await store.turns.add_ledger(_row(B, "b's"))  # the same (turn, step, verdict) in another bonfire is another row
    await store.turns.add_ledger(_row(A, "a's again"))  # the same report twice is one row
    assert [(r.bonfire_id, r.value) for r in await store.turns.ledger(A, "t1")] == [(A, "a's")]
    assert [(r.bonfire_id, r.value) for r in await store.turns.ledger(B, "t1")] == [(B, "b's")]
    assert await store.turns.ledger("bonfire-c", "t1") == []


async def test_previous_and_waiting_turns_stay_in_their_bonfire(store: Stores) -> None:
    def later(turn: TurnRecord, minutes: int) -> TurnRecord:
        return turn.model_copy(update={"at": AT + timedelta(minutes=minutes)})

    def paused(turn_id: str, bonfire_id: str) -> TurnRecord:
        step = PendingStep(step_id="s", tool="think", args={}, effect_key="k", needs_confirmation=False)
        return _turn(turn_id, bonfire_id).model_copy(update={"parent": "parent", "status": "licensed", "pending": step})

    # A and B share a chat and a speaker, and B's are always the nearer in time
    for turn in (
        _turn("a-before", A),
        later(_turn("b-before", B), 1),
        paused("a-paused", A),
        later(paused("b-paused", B), 1),
    ):
        await store.turns.save_turn(turn)
    now = later(_turn("now", A), 2)
    previous, waiting = await store.turns.previous_turn(now), await store.turns.waiting_turn(now)
    assert previous is not None
    assert previous.turn_id == "a-before"
    assert waiting is not None
    assert waiting.turn_id == "a-paused"
    alone = later(_turn("alone", "bonfire-c"), 2)  # the same chat and speaker in a bonfire with neither
    assert await store.turns.previous_turn(alone) is None
    assert await store.turns.waiting_turn(alone) is None


async def test_the_same_message_in_two_bonfires_is_two_turns(store: Stores) -> None:
    mk = FakeMk({"brew": Script(steps=[("take", {"item": "water"})])})
    engine = TurnEngine(mk, store.turns)
    a = await engine.begin(A, team_turn("brew"), parent="the-parent")
    b = await engine.begin(B, team_turn("brew"), parent="the-parent")
    assert a.turn_id != b.turn_id
    assert (a.bonfire_id, b.bonfire_id) == (A, B)
    assert a.pending is not None
    assert b.pending is not None
    assert a.pending.effect_key != b.pending.effect_key  # the platform's dedupe cannot join them either
    await engine.report(A, a.turn_id, a.pending.step_id, "committed", "water#1")
    assert [r.bonfire_id for r in await store.turns.ledger(A, a.turn_id)] == [A]
    assert await store.turns.ledger(B, b.turn_id) == []
    with pytest.raises(UnknownTurn):
        await engine.get(B, a.turn_id)


# -- catalogues ------------------------------------------------------------------------------------------------------


async def test_catalogues_and_sources_are_per_bonfire(store: Stores) -> None:
    await store.turns.save_catalogue(A, "cli", "group", {"tools": ["a"]})
    await store.turns.save_catalogue(B, "cli", "group", {"tools": ["b"]})
    await store.turns.save_source(A, "cli", "group", "agent", {"tools": ["a-source"]})
    await store.turns.save_source(B, "cli", "group", "agent", {"tools": ["b-source"]})
    await store.turns.save_source(B, "cli", "other", "agent", {"tools": ["b-other"]})
    assert await store.turns.catalogue(A, "cli", "group") == {"tools": ["a"]}
    assert await store.turns.catalogue(B, "cli", "group") == {"tools": ["b"]}
    assert await store.turns.source(A, "cli", "group", "agent") == {"tools": ["a-source"]}
    assert await store.turns.source(B, "cli", "group", "agent") == {"tools": ["b-source"]}
    assert await store.turns.source(A, "cli", "other", "agent") is None
    assert await store.turns.catalogue("bonfire-c", "cli", "group") is None
    assert await store.turns.kept_groups(A, "agent") == [("cli", "group")]
    assert await store.turns.kept_groups(B, "agent") == [("cli", "group"), ("cli", "other")]


# -- a kept volume -------------------------------------------------------------------------------------------------


# --- The hand-off's reads and marks ------------------------------------------------------------------------------------


class Seam:
    """Takes whatever it is sent."""

    def __init__(self, answer: Outcome | None = None) -> None:
        self.answer = answer or Outcome("accepted")
        self.sent: list[tuple[str, Body]] = []

    async def send(self, bonfire_id: str, body: Body) -> Outcome:
        self.sent.append((bonfire_id, body))
        return self.answer


async def _closed(store: Stores, bonfire: str, chat: str, *ids: str, text: str = "hi") -> Session:
    """A closed session holding ``ids``."""
    message = _msg("-", chat).model_copy(update={"text": text})
    await _take(store).add(bonfire, [message.model_copy(update={"message_id": i}) for i in ids], HANDLES)
    (session,) = [s for s in await store.sessions.waiting(bonfire) if s.conversation_id == chat and s.state == "open"]
    assert await store.sessions.close_open(bonfire, chat, session.last_message_at)
    return session


async def test_closed_heads_are_each_bonfires_oldest_and_a_delay_holds_the_ones_behind(store: Stores) -> None:
    sessions = store.sessions
    a_new = await _closed(store, A, "new", "m1")
    a_old = await _closed(store, A, "old", "m2")
    b_only = await _closed(store, B, "b", "m3")
    # the writer's order is the closed time, whatever order they opened in
    for session, at in ((a_new, AT + timedelta(minutes=2)), (a_old, AT + timedelta(minutes=1))):
        await _set_closed_at(store, session, at)

    assert {s.id for s in await sessions.closed_heads(AT)} == {a_old.id, b_only.id}

    await sessions.put_off(A, a_old.id, "away", AT + timedelta(seconds=30))
    assert {s.id for s in await sessions.closed_heads(AT)} == {b_only.id}  # nothing of A goes ahead of its head
    assert {s.id for s in await sessions.closed_heads(AT + timedelta(seconds=30))} == {a_old.id, b_only.id}


async def _set_closed_at(store: Stores, session: Session, at: datetime) -> None:
    if isinstance(store.sessions, MemorySessionStore):
        store.sessions.sessions[session.id].closed_at = at
    else:
        assert isinstance(store.sessions, MongoSessionStore)
        await store.sessions._sessions.update_one({"_id": session.id}, {"$set": {"closed_at": at}})  # pyright: ignore[reportPrivateUsage]


async def test_a_sweep_closes_many_at_one_time_and_the_id_still_orders_them(store: Stores) -> None:
    first = await _closed(store, A, "one", "m1")
    second = await _closed(store, A, "two", "m2")
    await _set_closed_at(store, first, AT)
    await _set_closed_at(store, second, AT)
    (head,) = await store.sessions.closed_heads(AT)
    assert head.id == min(first.id, second.id)


async def test_the_hand_off_gives_each_bonfire_its_own_messages(store: Stores) -> None:
    """The same chat and the same message ids in two bonfires, in one pass."""
    in_a = await _closed(store, A, "chat", "m1", "m2", text="said in a")
    in_b = await _closed(store, B, "chat", "m1", text="said in b")
    seam = Seam()
    assert await hand_over_once(store.sessions, seam, lambda: AT) == 2

    sent = {bonfire: body["messages"] for bonfire, body in seam.sent}
    assert [m["text"] for m in sent[A]] == ["said in a", "said in a"]
    assert [m["text"] for m in sent[B]] == ["said in b"]
    assert {m["message_id"] for m in sent[A]} == {"m1", "m2"}  # the plain id, never the stored key
    assert not await store.sessions.messages_of(B, in_a.id)
    assert not await store.sessions.messages_of(A, in_b.id)


async def test_handed_removes_the_text_and_keeps_the_ids(store: Stores) -> None:
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1", "m2")
    other = await _closed(store, B, "chat", "m1")
    await sessions.mark_handed(A, session.id, AT)

    assert [(m.message_id, m.text) for m in await sessions.messages_of(A, session.id)] == [("m1", None), ("m2", None)]
    assert [m.text for m in await sessions.messages_of(B, other.id)] == ["hi"]
    assert await sessions.waiting(A) == []
    assert await sessions.has_message(A, "m1")
    added = await _take(store).add(A, [_msg("m1")], HANDLES)
    assert (added.accepted, added.duplicates) == (0, 1)


async def test_refused_keeps_the_text_and_retry_puts_it_back(store: Stores) -> None:
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1")
    await sessions.put_off(A, session.id, "away", AT + timedelta(days=1))
    await sessions.mark_refused(A, session.id, "other handles", AT)

    (failed,) = await sessions.waiting(A)
    assert (failed.state, failed.reason, failed.failed_at, failed.retry_after) == ("failed", "other handles", AT, None)
    assert await sessions.closed_heads(AT) == []
    assert [m.text for m in await sessions.messages_of(A, session.id)] == ["hi"]

    assert await _take(store).retry(A, session.id) == "retried"
    (head,) = await sessions.closed_heads(AT)
    assert (head.id, head.state, head.reason, head.attempts) == (session.id, "closed", None, 0)


async def test_put_off_counts_an_attempt_and_stays_closed(store: Stores) -> None:
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1")
    await sessions.put_off(A, session.id, "away", AT + timedelta(seconds=30))
    await sessions.put_off(A, session.id, "still away", AT + timedelta(seconds=60))

    (waiting,) = await sessions.waiting(A)
    assert (waiting.state, waiting.attempts, waiting.reason) == ("closed", 2, "still away")
    assert waiting.retry_after == AT + timedelta(seconds=60)


async def test_a_mark_moves_only_a_closed_session_of_its_own_bonfire(store: Stores) -> None:
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1")
    await sessions.mark_handed(B, session.id, AT)
    await sessions.mark_refused(B, session.id, "no", AT)
    await sessions.put_off(B, session.id, "no", AT + timedelta(days=1))
    (held,) = await sessions.waiting(A)
    assert (held.state, held.reason, held.attempts, held.retry_after) == ("closed", None, 0, None)
    assert [m.text for m in await sessions.messages_of(A, session.id)] == ["hi"]

    # two answers for one session (a process stopping as another starts): the first stands
    await sessions.mark_handed(A, session.id, AT)
    await sessions.mark_refused(A, session.id, "no", AT)
    await sessions.put_off(A, session.id, "away", AT + timedelta(days=1))
    assert await sessions.waiting(A) == []

    refused = await _closed(store, A, "other", "m2")
    await sessions.mark_refused(A, refused.id, "no", AT)
    await sessions.mark_handed(A, refused.id, AT)  # a failed session keeps the text its retry needs
    (failed,) = await sessions.waiting(A)
    assert failed.state == "failed"
    assert [m.text for m in await sessions.messages_of(A, refused.id)] == ["hi"]


async def test_a_message_that_loses_the_race_with_the_close_leaves_the_session_as_it_was(store: Stores) -> None:
    sessions = store.sessions
    await _take(store).add(A, [_msg("m1")], HANDLES)
    (stale,) = await sessions.waiting(A)
    assert await sessions.close_open(A, "chat", AT)

    with pytest.raises(SessionClosed):
        await sessions.append(stale, _msg("m2"), AT + timedelta(seconds=1), HANDLES, AT)

    assert [m.message_id for m in await sessions.messages_of(A, stale.id)] == ["m1"]
    assert not await sessions.has_message(A, "m2")


async def test_a_failure_removing_text_never_sends_the_session_again(store: Stores) -> None:
    """The state moves first: text gone from a session still closed would send fewer ids, which is another job."""
    if not isinstance(store.sessions, MongoSessionStore):
        pytest.skip("memory has no second write to fail")
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1", "m2")

    class Away:
        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

        async def update_many(self, *_: object) -> None:
            raise ConnectionError("gone")

    real, sessions._messages = sessions._messages, Away()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    seam = Seam()
    await hand_over_once(sessions, seam, lambda: AT)
    sessions._messages = real  # pyright: ignore[reportPrivateUsage]

    assert len(seam.sent) == 1
    assert await sessions.closed_heads(AT) == []
    assert await sessions.waiting(A) == []
    assert [m.text for m in await sessions.messages_of(A, session.id)] == ["hi", "hi"]  # left behind: see mark_handed


async def test_an_unreadable_session_holds_up_only_its_own_bonfire(
    store: Stores, caplog: pytest.LogCaptureFixture
) -> None:
    if not isinstance(store.sessions, MongoSessionStore):
        pytest.skip("only Mongo rows can be unreadable")
    sessions = store.sessions
    behind = await _closed(store, A, "behind", "m1")
    await _closed(store, B, "chat", "m2")
    raw = sessions._sessions  # pyright: ignore[reportPrivateUsage]
    await raw.insert_one({"_id": "broken", "bonfire_id": A, "state": "closed", "closed_at": AT - timedelta(hours=1)})

    seam = Seam()
    assert await hand_over_once(sessions, seam, lambda: AT) == 1

    assert [bonfire for bonfire, _ in seam.sent] == [B]  # nothing of A: not the broken one, nor what is behind it
    assert "broken" in caplog.text
    await raw.delete_one({"_id": "broken"})
    assert [s.id for s in await sessions.closed_heads(AT)] == [behind.id]


async def test_a_message_that_cannot_be_read_stops_its_session_and_is_never_skipped(
    store: Stores, caplog: pytest.LogCaptureFixture
) -> None:
    """A skip would send fewer ids, which is a new job. The session waits, with a reason, until the row is mended."""
    if not isinstance(store.sessions, MongoSessionStore):
        pytest.skip("only Mongo rows can be unreadable")
    caplog.set_level(logging.DEBUG, logger="kernel_turns")
    words = "the person's own words"
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m1", "m2", text=words)
    behind = await _closed(store, A, "behind", "m3")
    await _closed(store, B, "chat", "m4")
    await _set_closed_at(store, session, AT)
    await _set_closed_at(store, behind, AT + timedelta(seconds=1))
    raw = sessions._messages  # pyright: ignore[reportPrivateUsage]
    await raw.update_one({"_id": message_key(A, "m2")}, {"$unset": {"speaker": ""}})

    seam = Seam()
    assert await hand_over_once(sessions, seam, lambda: AT) == 1
    assert [bonfire for bonfire, _ in seam.sent] == [B]

    waiting = {s.id: s for s in await sessions.waiting(A)}
    stuck = waiting[session.id]
    assert (stuck.state, stuck.attempts) == ("closed", 1)
    assert stuck.reason == "the hand-off failed: ValidationError"
    assert stuck.retry_after == AT + timedelta(seconds=30)
    assert words not in caplog.text
    assert await sessions.closed_heads(AT) == []  # and nothing of A goes ahead of it

    await raw.update_one({"_id": message_key(A, "m2")}, {"$set": {"speaker": "Kira"}})
    seam.sent.clear()
    assert await hand_over_once(sessions, seam, lambda: AT + timedelta(seconds=30)) == 1
    ((bonfire, body),) = seam.sent
    assert bonfire == A
    assert sorted(m["message_id"] for m in body["messages"]) == ["m1", "m2"]


async def test_messages_read_back_in_one_order_and_an_old_row_by_its_plain_id(store: Stores) -> None:
    """A crash can leave two messages with one timestamp and one seq."""
    if not isinstance(store.sessions, MongoSessionStore):
        pytest.skip("only Mongo holds rows of the old shape")
    sessions = store.sessions
    session = await _closed(store, A, "chat", "m3")
    raw = sessions._messages  # pyright: ignore[reportPrivateUsage]
    await raw.update_one({"_id": message_key(A, "m3")}, {"$set": {"timestamp": AT, "seq": 1}})
    row: Doc = {"session_id": session.id, "seq": 1, "speaker": "Kira", "text": "hi", "timestamp": AT, "addressees": []}
    await raw.insert_one({**row, "_id": message_key(A, "m2"), "bonfire_id": A, "message_id": "m2"})
    await raw.insert_one({**row, "_id": "old-plain-id"})  # stored before the bonfire was in the key

    read = await sessions.messages_of(A, session.id)
    assert [m.message_id for m in read] == ["m2", "m3", "old-plain-id"]
    assert all(m.bonfire_id == A for m in read)
    assert await sessions.messages_of(B, session.id) == [m.model_copy(update={"bonfire_id": B}) for m in read[2:]]


async def _old_indexes(client: AsyncMongoClient[Doc], name: str) -> None:
    """The unique indexes a stack from before this release left on its volume, over the rows it left."""
    await client[name]["sessions"].create_index(
        [("conversation_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"state": "open"},
        name="one_open_session_per_chat",
    )
    await client[name]["ledger"].create_index(
        [("turn_id", ASCENDING), ("step_id", ASCENDING), ("verdict", ASCENDING)], unique=True, name="ledger_key"
    )
    await client[name]["sessions"].insert_one(
        {
            "_id": "kept",
            "bonfire_id": A,
            "conversation_id": "chat",
            "state": "open",
            "agent_handles": HANDLES,
            "opened_at": AT,
            "last_message_at": AT,
        }
    )
    await client[name]["ledger"].insert_one({"turn_id": "old", "step_id": "s", "verdict": "committed", "at": AT})


async def test_ensure_indexes_replaces_the_old_unique_indexes(mongo: Database) -> None:
    client, name = mongo
    await _old_indexes(client, name)
    turns, sessions = MongoTurnStore(client, name), MongoSessionStore(client, name)
    for _ in range(2):  # every start runs it
        await turns.ensure_indexes()
        await sessions.ensure_indexes()
    on_sessions = await client[name]["sessions"].index_information()
    on_ledger = await client[name]["ledger"].index_information()
    assert "one_open_session_per_chat" not in on_sessions
    assert "ledger_key" not in on_ledger
    open_key = on_sessions["one_open_session_per_chat_v2"]
    assert open_key["key"] == [("bonfire_id", 1), ("conversation_id", 1)]
    assert open_key["unique"] is True
    assert open_key["partialFilterExpression"] == {"state": "open"}
    ledger_key = on_ledger["ledger_key_v2"]
    assert ledger_key["key"] == [("bonfire_id", 1), ("turn_id", 1), ("step_id", 1), ("verdict", 1)]
    assert ledger_key["unique"] is True
    # what the index change is for: the kept open session stays A's, and B can open the same chat
    assert (await sessions.open_session("chat", A, HANDLES, AT)).id == "kept"
    assert (await sessions.open_session("chat", B, HANDLES, AT)).id != "kept"


async def test_retention_adds_ttl_on_turns_and_ledger_only_when_set(mongo: Database) -> None:
    client, name = mongo

    async def retention(collection: str) -> list[object]:
        found = (await client[name][collection].index_information()).get("retention")
        return [found["key"], found["expireAfterSeconds"]] if found else []

    await MongoTurnStore(client, name).ensure_indexes()
    assert await retention("turns") == []
    assert await retention("ledger") == []
    await MongoTurnStore(client, name, retention_days=30).ensure_indexes()
    assert await retention("turns") == [[("at", 1)], THIRTY_DAYS_S]
    assert await retention("ledger") == [[("at", 1)], THIRTY_DAYS_S]
    await MongoTurnStore(client, name, retention_days=30).ensure_indexes()
    # the setting changed on a kept volume: the start must not throw on an index of that name with other options
    await MongoTurnStore(client, name, retention_days=60).ensure_indexes()
    assert await retention("ledger") == [[("at", 1)], 2 * THIRTY_DAYS_S]
    await MongoTurnStore(client, name).ensure_indexes()
    assert await retention("turns") == []
    assert await retention("ledger") == []


async def test_the_service_starts_on_a_kept_volume(mongo: Database) -> None:
    client, name = mongo
    assert MONGO_URI
    await _old_indexes(client, name)
    settings = Settings(
        token=SecretStr("s3cret-token"),
        bonfire_id=A,
        mongo_uri=SecretStr(MONGO_URI),
        mongo_db=name,
        retention_days=30,
    )
    with TestClient(create_app(settings, mk=FakeMk())) as service:
        assert service.get("/healthz").status_code == 200
    assert {"one_open_session_per_chat_v2", "closed_heads"} <= set(await client[name]["sessions"].index_information())
    assert "session_messages" in await client[name]["messages"].index_information()
    assert "ledger_key_v2" in await client[name]["ledger"].index_information()
    assert (await client[name]["turns"].index_information())["retention"]["expireAfterSeconds"] == THIRTY_DAYS_S
