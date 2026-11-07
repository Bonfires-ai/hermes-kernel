"""The turn store against a real MongoDB (testcontainers ``mongo:7``; never the stack's own Mongo)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import AsyncMongoClient
from testcontainers.mongodb import MongoDbContainer

from kernel_turns.models import LedgerRow, PendingStep, TurnRecord, UseRecord
from kernel_turns.store import MongoTurnStore

pytestmark = pytest.mark.integration

DB = "kernel_turns_test"
BONFIRE = "65f0c0ffee0000000000abcd"
AT = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def mongo_uri() -> Iterator[str]:
    with MongoDbContainer("mongo:7") as mongo:
        yield mongo.get_connection_url()


@pytest.fixture
async def client(mongo_uri: str) -> AsyncIterator[AsyncMongoClient[dict[str, object]]]:
    client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(mongo_uri, tz_aware=True)
    await client.drop_database(DB)
    yield client
    await client.close()


async def _store(client: AsyncMongoClient[dict[str, object]]) -> MongoTurnStore:
    store = MongoTurnStore(client, DB)
    await store.ensure_indexes()
    return store


def _turn(turn_id: str, at: datetime, text: str) -> TurnRecord:
    step = PendingStep(
        step_id="p1:0:0",
        tool="give",
        args={"person": "Kira Dace — café", "item": "thé 🎉"},
        effect_key="e" * 64,
        needs_confirmation=False,
    )
    return TurnRecord(
        turn_id=turn_id,
        platform="telegram",
        group="-100777",
        chat="-100777",
        message_id=turn_id,
        speaker="Kira Dace",
        text=text,
        at=at,
        participants=["Kira Dace", "Milo"],
        bonfire_id=BONFIRE,
        # a kernel plan holds keys Mongo is wary of ("$", ".") and non-ASCII: kept exactly
        plan={"plan_id": "p1", "status": "needs_tools", "memory_results": {"?x": {"a.b": 1, "$c": "é"}}},
        status="licensed",
        licence=["take", "give"],
        pending=step,
        phrase="brew.d1.0",
        issued=[step],
        problems=[],
    )


def _row(step_id: str, verdict: str, value: object) -> LedgerRow:
    return LedgerRow.model_validate(
        {
            "turn_id": "t1",
            "step_id": step_id,
            "tool": "take",
            "args": {"item": "thé"},
            "value": value,
            "verdict": verdict,
            "detail": None,
            "at": AT,
        }
    )


async def test_mongo_store_round_trips_and_keeps_the_ledger(client: AsyncMongoClient[dict[str, object]]) -> None:
    store = await _store(client)
    first = _turn("t1", AT, "brew thé for Kira Dace")
    await store.save_turn(first)
    assert await store.get_turn("t1") == first
    assert await store.get_turn("t-unknown") is None
    again = first.model_copy(update={"status": "done", "pending": None})
    await store.save_turn(again)  # a save replaces the turn
    assert await store.get_turn("t1") == again

    rows = [_row("p1:0:0", "committed", {"id": "t-1", "title": "Kira Dace — café"}), _row("p1:1:0", "tool_error", None)]
    rows.append(_row("p2:1:0", "committed", ["a", 1, None]))
    for row in rows:
        await store.add_ledger(row)
    await store.add_ledger(rows[0])  # the same (turn, step, verdict) again: one row
    reopened = await _store(client)  # a new store over the same database (a restarted service)
    assert await reopened.ledger("t1") == rows  # insertion order
    assert await reopened.ledger("t-unknown") == []

    use = UseRecord(
        bonfire_id=BONFIRE,
        phrase="brew.d1.0",
        speaker="Milo",
        platform="telegram",
        chat="-100777",
        at=AT,
        outcome="ok",
    )
    await reopened.record_use(use)
    await reopened.record_use(use.model_copy(update={"outcome": "declined", "speaker": "Ana"}))
    assert [u.outcome for u in await reopened.uses(BONFIRE)] == ["ok", "declined"]
    assert await reopened.uses("65f0c0ffee0000000000beef") == []

    later = _turn("t2", AT + timedelta(minutes=5), "take water")
    await reopened.save_turn(later)
    assert [t.turn_id for t in await reopened.recent_turns(BONFIRE, 20)] == ["t2", "t1"]
    assert [t.turn_id for t in await reopened.recent_turns(BONFIRE, 1)] == ["t2"]

    catalogue = {
        "$schema": "x",
        "schema_version": "kernel-catalogue/v1",
        "tools": [{"name": "look", "x-side-effect": False}],
    }
    assert await reopened.catalogue("telegram", "-100777") is None
    await reopened.save_catalogue("telegram", "-100777", catalogue)
    assert await reopened.catalogue("telegram", "-100777") == catalogue


async def test_previous_turn_is_the_speakers_earlier_message_turn_in_the_chat(
    client: AsyncMongoClient[dict[str, object]],
) -> None:
    store = await _store(client)

    def turn(turn_id: str, minutes: int, **update: object) -> TurnRecord:
        return _turn(turn_id, AT + timedelta(minutes=minutes), "take the new one").model_copy(update=update)

    first = turn("m1", 0)
    rows = [
        first,
        turn("c1", 1, parent="m1"),  # a phrase's child turn is never one
        turn("o1", 2, speaker="Milo"),  # another speaker's
        turn("e1", 3, chat="-100888"),  # the same speaker in another chat
        turn("b1", 4, bonfire_id="65f0c0ffee0000000000beef"),  # another bonfire's
    ]
    now = turn("m2", 5)
    for row in [*rows, now, turn("m3", 6)]:  # m3 is later than the turn asked about
        await store.save_turn(row)
    assert await store.previous_turn(now) == first
    assert await store.previous_turn(first) is None


async def test_waiting_turn_and_catalogue_sources(client: AsyncMongoClient[dict[str, object]]) -> None:
    store = await _store(client)
    step = PendingStep(
        step_id="p1:0:0", tool="think", args={}, effect_key="e" * 64, needs_confirmation=False, runner="agent"
    )

    def turn(turn_id: str, minutes: int, **update: object) -> TurnRecord:
        base = _turn(turn_id, AT + timedelta(minutes=minutes), "sitrep").model_copy(update={"pending": step})
        return base.model_copy(update=update)

    asking = turn("m2", 5, parent=None, status="licensed")
    rows = [
        turn("c1", 1, parent="m1"),  # the one waiting
        turn("c0", 0, parent="m0"),  # an older one: the latest wins
        turn("c2", 2, parent="m2"),  # the asking turn's own child
        turn("co", 3, parent="m1", speaker="Milo"),
        turn("ce", 3, parent="m1", chat="-100888"),
        turn("cl", 6, parent="m1"),  # after the asking turn
        turn("cd", 4, parent="m1", status="done"),
    ]
    for row in [*rows, asking]:
        await store.save_turn(row)
    found = await store.waiting_turn(asking)
    assert found is not None and found.turn_id == "c1"

    assert await store.source("telegram", "-100777", "agent") is None
    await store.save_source("telegram", "-100777", "agent", {"tools": []})
    assert await store.source("telegram", "-100777", "agent") == {"tools": []}
