"""Taking in conversation: messages group into one open session per chat, which closes on quiet, size or request."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.config import Settings
from kernel_turns.keys import message_key
from kernel_turns.sessions import MemorySessionStore, MessageIn, Session, SessionTake
from kernel_turns.store import MemoryTurnStore
from tests.fakes import FakeMk

T0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
HANDLES = ["hermes"]
BONFIRE = "b1"
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
    return SessionTake(store, 1800, max_messages, clock), store, clock


async def test_a_resend_is_the_same_message() -> None:
    take, store, _ = _take()
    first = await take.add(BONFIRE, [_msg("m1")], HANDLES)
    again = await take.add(BONFIRE, [_msg("m1")], HANDLES)
    assert (first.accepted, first.duplicates) == (1, 0)
    assert (again.accepted, again.duplicates) == (0, 1)
    (session,) = store.sessions.values()
    assert session.message_count == 1


async def test_a_session_closes_after_the_quiet_window() -> None:
    take, store, clock = _take()
    await take.add(BONFIRE, [_msg("m1")], HANDLES)
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
    await take.add(BONFIRE, [_msg(f"m{i}") for i in range(100)], HANDLES)
    (first,) = store.sessions.values()
    assert (first.state, first.message_count) == ("closed", 100)
    assert first.closed_at is not None
    await take.add(BONFIRE, [_msg("m100")], HANDLES)
    states = sorted((s.state, s.message_count) for s in store.sessions.values())
    assert states == [("closed", 100), ("open", 1)]


async def test_a_message_after_close_opens_a_new_session() -> None:
    take, store, _ = _take()
    await take.add(BONFIRE, [_msg("m1"), _msg("m2")], HANDLES)
    assert await take.close(BONFIRE, "telegram:1") is True
    assert await take.close(BONFIRE, "telegram:1") is False  # nothing open
    await take.add(BONFIRE, [_msg("m3")], HANDLES)
    counts = sorted((s.state, s.message_count) for s in store.sessions.values())
    assert counts == [("closed", 2), ("open", 1)]


async def test_two_chats_keep_their_own_sessions() -> None:
    take, store, _ = _take()
    await take.add(BONFIRE, [_msg("a1", "telegram:1"), _msg("b1", "telegram:2"), _msg("a2", "telegram:1")], HANDLES)
    by_chat = {s.conversation_id: s for s in store.sessions.values()}
    assert {chat: s.message_count for chat, s in by_chat.items()} == {"telegram:1": 2, "telegram:2": 1}
    seqs = {m.message_id: m.seq for m in store.messages.values()}
    assert seqs == {"a1": 1, "b1": 1, "a2": 2}
    assert store.messages[message_key(BONFIRE, "a2")].session_id == by_chat["telegram:1"].id


async def test_timestamps_never_run_backwards_in_a_session() -> None:
    take, store, _ = _take()
    sub = timedelta(microseconds=100), timedelta(microseconds=900)  # both store as the same millisecond
    batch = [
        _msg("m1", ts=T0 + timedelta(seconds=5)),
        _msg("m2", ts=T0 + timedelta(seconds=1)),  # backwards
        _msg("m3", ts=T0 + timedelta(seconds=1)),  # equal to the bumped one
        _msg("m4", ts=T0 + timedelta(seconds=9) + sub[0]),
        _msg("m5", ts=T0 + timedelta(seconds=9) + sub[1]),
    ]
    await take.add(BONFIRE, batch, HANDLES)
    stamps = [store.messages[message_key(BONFIRE, f"m{i}")].timestamp for i in range(1, 6)]
    assert all(a < b for a, b in zip(stamps, stamps[1:], strict=False))
    assert all(t.microsecond % 1000 == 0 for t in stamps)


class _SweepsBetween(MemorySessionStore):
    """Closes the open session after it was handed out and before the message lands, once."""

    swept = False

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        if not self.swept:
            self.swept = True
            await self.close_session(session.bonfire_id, session.id, now)
        return await super().append(session, message, timestamp, agent_handles, now)


async def test_a_message_never_lands_in_a_session_closed_under_it() -> None:
    store, clock = _SweepsBetween(), Clock()
    take = SessionTake(store, 1800, 100, clock)
    await take.add(BONFIRE, [_msg("m1")], HANDLES)  # opens S, S is closed before the append
    await take.add(BONFIRE, [_msg("m2")], HANDLES)
    by_state = sorted((s.state, s.message_count) for s in store.sessions.values())
    assert by_state == [("closed", 0), ("open", 2)]
    assert len({m.session_id for m in store.messages.values()}) == 1


async def test_a_resend_after_the_writer_removed_the_text_is_still_a_duplicate() -> None:
    take, store, _ = _take()
    await take.add(BONFIRE, [_msg("m1")], HANDLES)
    await take.close(BONFIRE, "telegram:1")
    store.messages[message_key(BONFIRE, "m1")].text = None  # the writer unsets `text` and keeps the rest
    result = await take.add(BONFIRE, [_msg("m1")], HANDLES)
    assert (result.accepted, result.duplicates) == (0, 1)
    assert len(store.sessions) == 1


class _Gated(MemorySessionStore):
    """Holds an add inside the store until released."""

    def __init__(self) -> None:
        super().__init__()
        self.inside, self.release = asyncio.Event(), asyncio.Event()

    async def append(
        self, session: Session, message: MessageIn, timestamp: datetime, agent_handles: list[str], now: datetime
    ) -> int | None:
        self.inside.set()
        await self.release.wait()
        return await super().append(session, message, timestamp, agent_handles, now)


async def test_a_close_waits_for_an_add_in_flight() -> None:
    store, clock = _Gated(), Clock()
    take = SessionTake(store, 1800, 100, clock)
    adding = asyncio.create_task(take.add(BONFIRE, [_msg("m1")], HANDLES))
    await store.inside.wait()
    clock.now = T0 + timedelta(seconds=1800)
    sweeping = asyncio.create_task(take.close_idle())
    closing = asyncio.create_task(take.close(BONFIRE, "telegram:1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not sweeping.done()
    assert not closing.done()
    store.release.set()
    await adding
    await asyncio.gather(sweeping, closing)
    (session,) = store.sessions.values()
    assert (session.state, session.message_count) == ("closed", 1)
    assert store.messages[message_key(BONFIRE, "m1")].session_id == session.id


@pytest.fixture
def api() -> tuple[TestClient, MemorySessionStore]:
    store = MemorySessionStore()
    settings = Settings(token=SecretStr(TOKEN), bonfire_id=BONFIRE)
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
    session.retry_after = datetime(2026, 11, 7, tzinfo=UTC)
    assert client.post(url, headers=AUTH).status_code == 204
    assert (session.state, session.reason, session.attempts, session.retry_after) == ("closed", None, 0, None)
    assert client.post("/v1/sessions/nope/retry", headers=AUTH).status_code == 404


def test_an_empty_text_or_a_naive_timestamp_is_refused(api: tuple[TestClient, MemorySessionStore]) -> None:
    client, store = api
    good = _msg("m1").model_dump(mode="json")
    for bad in ({**good, "text": ""}, {**good, "timestamp": "2026-11-06T10:00:00"}):
        r = client.post("/v1/messages", json={"messages": [bad], "agent_handles": HANDLES}, headers=AUTH)
        assert r.status_code == 422
    assert not store.sessions


@pytest.mark.parametrize(
    "fields",
    [
        {"conversation_id": "   "},
        {"conversation_id": "\t\n"},
        {"addressees": [""]},
        {"addressees": ["Kira", ""]},
    ],
)
def test_what_mk_can_never_build_is_refused_at_the_door(
    api: tuple[TestClient, MemorySessionStore], fields: dict[str, object]
) -> None:
    """mk refuses these for good once a session holds one, and every later session of the bonfire waits behind it."""
    client, store = api
    bad = {**_msg("m1").model_dump(mode="json"), **fields}
    r = client.post("/v1/messages", json={"messages": [bad], "agent_handles": HANDLES}, headers=AUTH)
    assert r.status_code == 422
    assert not store.sessions
    assert not store.messages


@pytest.mark.parametrize(
    "fields",
    [
        {"conversation_id": "telegram:-100777"},
        {"conversation_id": "cli:"},  # the plugin's, with no chat and no session id
        {"conversation_id": " padded "},  # kept as sent: a chat's id is not ours to change
        {"addressees": None},
        {"addressees": []},
        {"addressees": ["hermes"]},
        {"addressees": ["   "]},  # a person named only spaces: the plugin addresses them, and mk takes it
        {"speaker": "   ", "addressees": ["hermes"]},
    ],
)
def test_what_mk_takes_still_comes_in(api: tuple[TestClient, MemorySessionStore], fields: dict[str, object]) -> None:
    """A refusal here makes the plugin drop its whole batch, so the door refuses only what mk does."""
    client, store = api
    message = {**_msg("m1").model_dump(mode="json"), **fields}
    r = client.post("/v1/messages", json={"messages": [message], "agent_handles": HANDLES}, headers=AUTH)
    assert r.json() == {"accepted": 1, "duplicates": 0}
    (session,) = store.sessions.values()
    assert session.conversation_id == message["conversation_id"]


# --- What one add may hold: the plugin sends at most 64 messages, and graph-memory takes conversation ids up to 512 ---


def _batch(n: int, chat: str = "telegram:1") -> list[dict[str, object]]:
    sent = [_msg(f"m{i}", ts=T0 + timedelta(seconds=i)).model_dump(mode="json") for i in range(n)]
    return [{**m, "conversation_id": chat} for m in sent]  # as the wire has it: MessageIn would refuse a long one


def test_an_add_takes_at_most_64_messages(api: tuple[TestClient, MemorySessionStore]) -> None:
    client, store = api
    r = client.post("/v1/messages", json={"messages": _batch(65), "agent_handles": HANDLES}, headers=AUTH)
    assert r.status_code == 422
    assert not store.sessions and not store.messages
    r = client.post("/v1/messages", json={"messages": _batch(64), "agent_handles": HANDLES}, headers=AUTH)
    assert r.json() == {"accepted": 64, "duplicates": 0}


def test_a_conversation_id_is_at_most_512_characters(api: tuple[TestClient, MemorySessionStore]) -> None:
    client, store = api
    longest, too_long = "c" * 512, "c" * 513
    r = client.post("/v1/messages", json={"messages": _batch(1, too_long), "agent_handles": HANDLES}, headers=AUTH)
    assert r.status_code == 422
    assert not store.sessions
    assert client.post("/v1/sessions/close", json={"conversation_id": too_long}, headers=AUTH).status_code == 422
    r = client.post("/v1/messages", json={"messages": _batch(1, longest), "agent_handles": HANDLES}, headers=AUTH)
    assert r.json() == {"accepted": 1, "duplicates": 0}
    assert client.post("/v1/sessions/close", json={"conversation_id": longest}, headers=AUTH).json() == {"closed": True}
