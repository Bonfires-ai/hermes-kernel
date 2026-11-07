"""One bonfire can never reach another's rows: the stores' behaviour over memory and over a real MongoDB.

The Mongo cases run only with ``KT_TEST_MONGO_URI`` set (a throwaway server); the memory cases always run, and the
behaviour tests are the same for both.

The behaviour tests share one database and empty it between tests. mongod keeps a file open for every collection and
index until the checkpoint after its drop, so a database per test passes a container's default 1024 open files in a
few fast runs, and WiredTiger's panic ends the server. Only the tests of the indexes themselves take a fresh one.
"""

from __future__ import annotations

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
from kernel_turns.models import LedgerRow, PendingStep, TurnRecord
from kernel_turns.sessions import MemorySessionStore, MessageIn, MongoSessionStore, SessionStore, SessionTake
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
    assert "one_open_session_per_chat_v2" in await client[name]["sessions"].index_information()
    assert "ledger_key_v2" in await client[name]["ledger"].index_information()
    assert (await client[name]["turns"].index_information())["retention"]["expireAfterSeconds"] == THIRTY_DAYS_S
