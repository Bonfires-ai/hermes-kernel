"""Taking in conversation: messages group into one open session per chat, which closes on quiet, size or request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.config import Settings
from kernel_turns.sessions import MemorySessionStore, MessageIn, SessionTake
from kernel_turns.store import MemoryTurnStore
from tests.fakes import FakeMk

T0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
HANDLES = ["hermes"]
TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _msg(message_id: str, chat: str = "telegram:1", ts: datetime = T0, text: str = "hi") -> MessageIn:
    return MessageIn(conversation_id=chat, message_id=message_id, speaker="Kira", text=text, timestamp=ts)


def _take(max_messages: int = 100) -> tuple[SessionTake, MemorySessionStore, Clock]:
    store, clock = MemorySessionStore(), Clock()
    return SessionTake(store, 1800, max_messages, clock, bonfire_id="b1"), store, clock


async def test_a_resend_is_the_same_message() -> None:
    take, store, _ = _take()
    first = await take.add([_msg("m1")], HANDLES)
    again = await take.add([_msg("m1")], HANDLES)
    assert (first.accepted, first.duplicates) == (1, 0)
    assert (again.accepted, again.duplicates) == (0, 1)
    (session,) = store.sessions.values()
    assert session.message_count == 1


async def test_a_session_closes_after_the_quiet_window() -> None:
    take, store, clock = _take()
    await take.add([_msg("m1")], HANDLES)
    clock.now = T0 + timedelta(seconds=1799)
    await take.close_idle()
    (session,) = store.sessions.values()
    assert session.state == "open"
    clock.now = T0 + timedelta(seconds=1800)
    await take.close_idle()
    assert session.state == "closed"
    assert session.closed_at == clock.now


async def test_a_session_closes_at_100_messages() -> None:
    take, store, _ = _take(100)
    await take.add([_msg(f"m{i}") for i in range(100)], HANDLES)
    (first,) = store.sessions.values()
    assert (first.state, first.message_count) == ("closed", 100)
    assert first.closed_at is not None
    await take.add([_msg("m100")], HANDLES)
    states = sorted((s.state, s.message_count) for s in store.sessions.values())
    assert states == [("closed", 100), ("open", 1)]


async def test_a_message_after_close_opens_a_new_session() -> None:
    take, store, _ = _take()
    await take.add([_msg("m1"), _msg("m2")], HANDLES)
    assert await take.close("telegram:1") is True
    assert await take.close("telegram:1") is False  # nothing open
    await take.add([_msg("m3")], HANDLES)
    counts = sorted((s.state, s.message_count) for s in store.sessions.values())
    assert counts == [("closed", 2), ("open", 1)]


async def test_two_chats_keep_their_own_sessions() -> None:
    take, store, _ = _take()
    await take.add([_msg("a1", "telegram:1"), _msg("b1", "telegram:2"), _msg("a2", "telegram:1")], HANDLES)
    by_chat = {s.conversation_id: s for s in store.sessions.values()}
    assert {chat: s.message_count for chat, s in by_chat.items()} == {"telegram:1": 2, "telegram:2": 1}
    seqs = {m.message_id: m.seq for m in store.messages.values()}
    assert seqs == {"a1": 1, "b1": 1, "a2": 2}
    assert store.messages["a2"].session_id == by_chat["telegram:1"].id


async def test_timestamps_never_run_backwards_in_a_session() -> None:
    take, store, _ = _take()
    await take.add([_msg("m1", ts=T0 + timedelta(seconds=5)), _msg("m2", ts=T0 + timedelta(seconds=1))], HANDLES)
    assert store.messages["m2"].timestamp > store.messages["m1"].timestamp


@pytest.fixture
def api() -> tuple[TestClient, MemorySessionStore]:
    store = MemorySessionStore()
    settings = Settings(token=SecretStr(TOKEN), bonfire_id="b1")
    return TestClient(create_app(settings, mk=FakeMk(), store=MemoryTurnStore(), sessions=store)), store


def test_retry_moves_a_failed_session_back_to_closed(api: tuple[TestClient, MemorySessionStore]) -> None:
    client, store = api
    body = {"messages": [_msg("m1").model_dump(mode="json")], "agent_handles": HANDLES}
    assert client.post("/v1/messages", json=body, headers=AUTH).json() == {"accepted": 1, "duplicates": 0}
    assert client.post("/v1/sessions/close", json={"conversation_id": "telegram:1"}, headers=AUTH).json() == {
        "closed": True
    }
    (session,) = store.sessions.values()
    url = f"/v1/sessions/{session.id}/retry"
    assert client.post(url, headers=AUTH).status_code == 409  # closed, not failed
    assert session.state == "closed"
    session.state, session.reason, session.attempts = "failed", "mk refused", 3
    assert client.post(url, headers=AUTH).status_code == 204
    assert (session.state, session.reason, session.attempts) == ("closed", None, 0)
    assert client.post("/v1/sessions/nope/retry", headers=AUTH).status_code == 404


def test_an_empty_text_or_a_naive_timestamp_is_refused(api: tuple[TestClient, MemorySessionStore]) -> None:
    client, store = api
    good = _msg("m1").model_dump(mode="json")
    for bad in ({**good, "text": ""}, {**good, "timestamp": "2026-11-06T10:00:00"}):
        r = client.post("/v1/messages", json={"messages": [bad], "agent_handles": HANDLES}, headers=AUTH)
        assert r.status_code == 422
    assert not store.sessions
