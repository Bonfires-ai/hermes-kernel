"""The in-process store every unit test runs on keeps the same contract as the Mongo one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kernel_turns.models import LedgerRow, TurnRecord
from kernel_turns.store import MemoryTurnStore

AT = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)


def _turn(turn_id: str, minutes: int) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        platform="telegram",
        group="-100777",
        chat="-100777",
        message_id=turn_id,
        speaker=None,
        text="look",
        at=AT + timedelta(minutes=minutes),
        participants=[],
        bonfire_id="b1",
        plan=None,
        status="unread",
        licence=None,
        pending=None,
        phrase=None,
        issued=[],
        problems=[],
    )


async def test_memory_store_keeps_one_row_per_key_and_copies() -> None:
    store = MemoryTurnStore()
    row = LedgerRow(
        turn_id="t1", step_id="p:0:0", tool="take", args={"item": "thé"}, value=None, verdict="committed", at=AT
    )
    await store.add_ledger(row)
    await store.add_ledger(row)
    await store.add_ledger(row.model_copy(update={"verdict": "tool_error"}))
    assert [r.verdict for r in await store.ledger("t1")] == ["committed", "tool_error"]

    turn = _turn("t1", 0)
    await store.save_turn(turn)
    loaded = await store.get_turn("t1")
    assert loaded == turn
    assert loaded is not None
    loaded.problems.append("changed after loading")  # a loaded turn is a copy, as Mongo's would be
    assert await store.get_turn("t1") == turn
    await store.save_turn(_turn("t2", 3))
    await store.save_turn(_turn("t3", 1))
    assert [t.turn_id for t in await store.recent_turns("b1", 2)] == ["t2", "t3"]

    naive = LedgerRow.model_validate({**row.model_dump(), "at": datetime(2026, 10, 22, 18, 0)})
    assert naive.at.tzinfo is not None  # a naive time is read as UTC
