"""Closed sessions go to a URL, oldest first per bonfire: none lost, none built twice, no bonfire waiting on another."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.callers import Caller
from kernel_turns.config import Settings
from kernel_turns.handoff import (
    AGENT_HANDLE,
    Body,
    HandoffApi,
    HandoffClient,
    Outcome,
    hand_over_forever,
    hand_over_once,
)
from kernel_turns.sessions import MemorySessionStore, MessageIn, Session, SessionTake
from kernel_turns.store import MemoryTurnStore
from tests.fakes import FakeMk

T0 = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
A, B = "bonfire-a", "bonfire-b"
TEXT = "the person's own words"
SEAM_TOKEN = "seam-" + "token-xyz"
URL = "http://seam.test/kernel"
ACCEPTED = Outcome("accepted")
LATER = Outcome("later", "the hand-off answered 503")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Seam:
    """A fake ``HandoffApi``: ``answer`` says what each send gets; a copy of every body is kept."""

    def __init__(self, answer: Callable[[str, Body], Outcome | Exception] = lambda _b, _s: ACCEPTED) -> None:
        self.answer = answer
        self.sent: list[tuple[str, Body]] = []

    async def send(self, bonfire_id: str, body: Body) -> Outcome:
        self.sent.append((bonfire_id, copy.deepcopy(body)))
        out = self.answer(bonfire_id, body)
        if isinstance(out, Exception):
            raise out
        return out

    def chats(self) -> set[tuple[str, object]]:
        return {(bonfire, body["conversation_id"]) for bonfire, body in self.sent}


def _msg(message_id: str, chat: str, at: datetime = T0, text: str = TEXT) -> MessageIn:
    return MessageIn(conversation_id=chat, message_id=message_id, speaker="Kira", text=text, timestamp=at)


class World:
    def __init__(self, store: MemorySessionStore | None = None) -> None:
        self.store = store or MemorySessionStore()
        self.clock = Clock()
        self.take = SessionTake(self.store, 1800, 100, self.clock)

    async def closed(self, bonfire: str, chat: str, *ids: str) -> Session:
        """A chat's session holding ``ids``, closed a second after the one before it."""
        self.clock.now += timedelta(seconds=1)
        await self.take.add(bonfire, [_msg(i, chat, self.clock.now) for i in ids], ["hermes"])
        assert await self.take.close(bonfire, chat)
        return next(
            s for s in self.store.sessions.values() if s.conversation_id == chat and s.closed_at == self.clock.now
        )

    def ids_of(self, session: Session) -> list[str]:
        return [m.message_id for m in self.store.messages.values() if m.session_id == session.id]

    def texts_of(self, session: Session) -> list[str | None]:
        return [m.text for m in self.store.messages.values() if m.session_id == session.id]

    def state(self, session: Session) -> str:
        return self.store.sessions[session.id].state

    async def once(self, api: HandoffApi) -> int:
        return await hand_over_once(self.store, api, self.clock)


def ids_in(body: Body) -> list[str]:
    messages = body["messages"]
    assert isinstance(messages, list)
    return [m["message_id"] for m in messages]


# --- The pass ---------------------------------------------------------------------------------------------------------


async def test_oldest_first_per_bonfire() -> None:
    world = World()
    await world.closed(A, "a1", "m1")
    await world.closed(A, "a2", "m2")
    await world.closed(B, "b1", "m3")
    seam = Seam()
    assert await world.once(seam) == 2
    assert seam.chats() == {(A, "a1"), (B, "b1")}
    seam.sent.clear()
    assert await world.once(seam) == 1
    assert seam.chats() == {(A, "a2")}
    seam.sent.clear()
    assert await world.once(seam) == 0
    assert seam.sent == []


async def test_a_stuck_bonfire_never_holds_up_another() -> None:
    world = World()
    a1, a2 = await world.closed(A, "a1", "m1"), await world.closed(A, "a2", "m2")
    await world.closed(B, "b1", "m3")
    await world.closed(B, "b2", "m4")
    seam = Seam(lambda bonfire, _: LATER if bonfire == A else ACCEPTED)

    await world.once(seam)  # A answers later, B goes
    assert seam.chats() == {(A, "a1"), (B, "b1")}
    seam.sent.clear()
    await world.once(seam)  # A1 waits out its delay, and A2 waits behind it
    assert seam.chats() == {(B, "b2")}
    assert (world.state(a1), world.state(a2)) == ("closed", "closed")

    seam.sent.clear()
    seam.answer = lambda _b, _s: ACCEPTED
    world.clock.now += timedelta(seconds=31)
    await world.once(seam)  # A1 again, and still before A2
    assert seam.chats() == {(A, "a1")}
    seam.sent.clear()
    await world.once(seam)
    assert seam.chats() == {(A, "a2")}


async def test_body_is_what_the_worker_accepts() -> None:
    world = World()
    message = _msg("m1", "chat").model_copy(update={"addressees": ["hermes"]})
    await world.take.add(A, [message], ["somebody-else"])
    await world.take.close(A, "chat")
    seam = Seam()
    await world.once(seam)

    ((bonfire, body),) = seam.sent
    assert bonfire == A
    assert set(body) == {"conversation_id", "messages", "agent_handles"}
    assert body["conversation_id"] == "chat"
    assert body["agent_handles"] == [AGENT_HANDLE] == ["hermes"]  # the bonfire's, whatever a caller stored
    messages = body["messages"]
    assert isinstance(messages, list)
    (sent,) = messages
    assert set(sent) <= {"message_id", "speaker", "text", "timestamp", "addressees"}
    assert sent["message_id"] == "m1"  # plain: the stored key is the service's own
    assert (sent["speaker"], sent["text"], sent["addressees"]) == ("Kira", TEXT, ["hermes"])
    assert datetime.fromisoformat(sent["timestamp"]).tzinfo is not None


async def test_accepted_removes_the_text_and_is_not_waiting() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1", "m2")
    assert await world.once(Seam()) == 1

    assert world.state(session) == "handed"
    assert world.store.sessions[session.id].handed_at == world.clock.now
    assert world.texts_of(session) == [None, None]
    assert await world.store.waiting(A) == []
    assert await world.once(seam := Seam()) == 0  # never again
    assert seam.sent == []
    # the ids stay: the same message sent again is still a duplicate
    assert await world.store.has_message(A, "m1")
    assert (await world.take.add(A, [_msg("m1", "chat")], ["hermes"])).duplicates == 1


async def test_refused_is_failed_with_the_reason_and_keeps_the_text() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1")
    await world.once(Seam(lambda _b, _s: Outcome("refused", "handles differ")))

    stored = world.store.sessions[session.id]
    assert (stored.state, stored.reason) == ("failed", "handles differ")
    assert world.texts_of(session) == [TEXT]
    assert [s.state for s in await world.store.waiting(A)] == ["failed"]

    assert await world.take.retry(A, session.id) == "retried"
    assert world.state(session) == "closed"
    seam = Seam()
    assert await world.once(seam) == 1  # the same session, with its text, goes
    assert ids_in(seam.sent[0][1]) == ["m1"]
    assert world.state(session) == "handed"


async def test_a_refused_session_stops_holding_up_the_next_and_comes_first_when_retried() -> None:
    world = World()
    first, second = await world.closed(A, "a1", "m1"), await world.closed(A, "a2", "m2")
    third = await world.closed(A, "a3", "m3")
    await world.once(Seam(lambda _b, _s: Outcome("refused", "no")))
    assert (world.state(first), world.state(second)) == ("failed", "closed")

    seam = Seam()
    await world.once(seam)
    assert seam.chats() == {(A, "a2")}

    await world.take.retry(A, first.id)  # it keeps its place: older than the third
    seam.sent.clear()
    await world.once(seam)
    assert seam.chats() == {(A, "a1")}
    assert world.state(third) == "closed"


@pytest.mark.parametrize("answer", [500, 503, httpx.ReadTimeout("slow"), 401, 403, 404, 429])
async def test_5xx_timeout_and_a_wrong_seam_token_try_later(answer: int | Exception) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"message": "no"})

    world = World()
    session = await world.closed(A, "chat", "m1")
    client = HandoffClient(URL, SEAM_TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    assert await world.once(client) == 0

    stored = world.store.sessions[session.id]
    assert stored.state == "closed"
    assert stored.retry_after == world.clock.now + timedelta(seconds=30)
    assert stored.attempts == 1
    assert world.texts_of(session) == [TEXT]


async def test_a_404_is_a_wrong_url_and_fails_nothing() -> None:
    """The route never answers 404: only a wrong URL or a deploy-order slip does, and neither is the session's fault."""
    world = World()
    first, second = await world.closed(A, "a1", "m1"), await world.closed(A, "a2", "m2")
    tried: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        tried.append(json.loads(request.read())["conversation_id"])
        return httpx.Response(404, json={"detail": "Not Found"})

    client = client_over(respond)
    for _ in range(2):
        assert await world.once(client) == 0
        world.clock.now += timedelta(seconds=61)

    assert (world.state(first), world.state(second)) == ("closed", "closed")
    assert tried == ["a1", "a1"]  # the next session did not move up
    assert not [s for s in await world.store.waiting(A) if s.state == "failed"]


async def test_each_attempt_waits_twice_as_long_up_to_ten_minutes() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1")
    seam = Seam(lambda _b, _s: LATER)
    waits = []
    for _ in range(7):
        await world.once(seam)
        stored = world.store.sessions[session.id]
        assert stored.retry_after is not None
        waits.append((stored.retry_after - world.clock.now).total_seconds())
        world.clock.now = stored.retry_after
    assert waits == [30, 60, 120, 240, 480, 600, 600]


async def test_a_week_of_failures_still_waits_ten_minutes() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1")
    world.store.sessions[session.id].attempts = 5000  # 30.0 * 2**5000 does not fit a float
    await world.once(Seam(lambda _b, _s: LATER))
    assert world.store.sessions[session.id].retry_after == world.clock.now + timedelta(seconds=600)


class CrashesOnMark(MemorySessionStore):
    """The process dies once between the answer and the mark."""

    crash = True

    async def mark_handed(self, bonfire_id: str, session_id: str, now: datetime) -> None:
        if self.crash:
            self.crash = False
            raise ConnectionError("gone")
        await super().mark_handed(bonfire_id, session_id, now)


async def test_a_crash_after_send_resends_the_same_body() -> None:
    world = World(CrashesOnMark())
    session = await world.closed(A, "chat", "m1", "m2")
    seam = Seam()
    assert await world.once(seam) == 0
    assert world.state(session) == "closed"
    assert world.texts_of(session) == [TEXT, TEXT]

    world.clock.now += timedelta(seconds=31)
    assert await world.once(seam) == 1
    first, second = seam.sent
    assert first == second
    assert world.state(session) == "handed"


@pytest.mark.parametrize("closes", ["on request", "by the sweep"])
async def test_a_message_after_the_close_never_changes_what_is_resent(closes: str) -> None:
    world = World()
    await world.take.add(A, [_msg("m1", "chat"), _msg("m2", "chat")], ["hermes"])
    (session,) = world.store.sessions.values()
    if closes == "on request":
        await world.take.close(A, "chat")
    else:
        world.clock.now += timedelta(seconds=1800)
        assert await world.take.close_idle() == 1

    seam = Seam(lambda _b, _s: LATER)
    await world.once(seam)
    # the plugin resends one message and sends a new one, while the session waits for its second try
    added = await world.take.add(A, [_msg("m2", "chat"), _msg("m3", "chat")], ["hermes"])
    assert (added.accepted, added.duplicates) == (1, 1)
    world.clock.now += timedelta(seconds=31)
    await world.once(seam)

    first, second = seam.sent
    assert first == second
    assert sorted(ids_in(first[1])) == ["m1", "m2"]
    assert sorted(world.ids_of(session)) == ["m1", "m2"]
    newer = next(s for s in world.store.sessions.values() if s.id != session.id)
    assert (newer.state, world.ids_of(newer)) == ("open", ["m3"])


async def test_tied_timestamps_rise_by_a_millisecond() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1", "m2", "m3")
    for message in world.store.messages.values():  # what a crash can leave
        message.timestamp = T0
    seam = Seam()
    await world.once(seam)

    messages = seam.sent[0][1]["messages"]
    assert isinstance(messages, list)
    sent = [datetime.fromisoformat(m["timestamp"]) for m in messages]
    assert sent == [T0, T0 + timedelta(milliseconds=1), T0 + timedelta(milliseconds=2)]
    assert [m.timestamp for m in world.store.messages.values() if m.session_id == session.id] == [T0, T0, T0]


async def test_an_empty_session_settles_without_a_post() -> None:
    world = World()
    session = await world.store.open_session("chat", A, ["hermes"], T0)
    await world.store.close_session(A, session.id, T0)
    seam = Seam()
    assert await world.once(seam) == 1
    assert seam.sent == []
    assert world.state(session) == "handed"


class ReadFails(MemorySessionStore):
    async def messages_of(self, bonfire_id: str, session_id: str) -> list[object]:  # pyright: ignore[reportIncompatibleMethodOverride]
        if bonfire_id == A:
            raise ConnectionError("gone")
        return await super().messages_of(bonfire_id, session_id)  # pyright: ignore[reportReturnType]


async def test_one_bonfires_exception_never_stops_the_next() -> None:
    world = World(ReadFails())
    stuck = await world.closed(A, "a1", "m1")
    fine = await world.closed(B, "b1", "m2")
    assert await world.once(Seam()) == 1
    assert (world.state(stuck), world.state(fine)) == ("closed", "handed")

    other = World()
    stuck = await other.closed(A, "a1", "m1")
    fine = await other.closed(B, "b1", "m2")
    seam = Seam(lambda bonfire, _: RuntimeError("boom") if bonfire == A else ACCEPTED)
    assert await other.once(seam) == 1
    assert (other.state(stuck), other.state(fine)) == ("closed", "handed")


async def test_an_exception_puts_the_head_off_with_a_reason() -> None:
    world = World(ReadFails())
    stuck = await world.closed(A, "a1", "m1")
    behind = await world.closed(A, "a2", "m2")
    seam = Seam()

    await world.once(seam)
    stored = world.store.sessions[stuck.id]
    assert (stored.state, stored.attempts) == ("closed", 1)
    assert stored.reason == "the hand-off failed: ConnectionError"
    assert stored.retry_after == world.clock.now + timedelta(seconds=30)

    await world.once(seam)  # not again before its time, and nothing goes ahead of it
    assert world.store.sessions[stuck.id].attempts == 1

    world.clock.now += timedelta(seconds=30)
    await world.once(seam)
    stored = world.store.sessions[stuck.id]
    assert stored.attempts == 2
    assert stored.retry_after == world.clock.now + timedelta(seconds=60)
    assert world.state(behind) == "closed"
    assert seam.sent == []


class PutOffFails(ReadFails):
    async def put_off(self, bonfire_id: str, session_id: str, reason: str, until: datetime) -> None:
        raise ConnectionError("gone")


async def test_a_put_off_that_fails_never_stops_the_next_bonfire() -> None:
    world = World(PutOffFails())
    stuck = await world.closed(A, "a1", "m1")
    fine = await world.closed(B, "b1", "m2")
    assert await world.once(Seam()) == 1
    assert (world.state(stuck), world.state(fine)) == ("closed", "handed")


async def test_the_loop_outlives_a_failing_pass() -> None:
    world = World()
    await world.closed(A, "chat", "m1")
    real = world.store.closed_heads
    failures = [ConnectionError("gone")]

    async def blips(now: datetime) -> list[Session]:
        if failures:
            raise failures.pop()
        return await real(now)

    world.store.closed_heads = blips  # pyright: ignore[reportAttributeAccessIssue]
    seam = Seam()
    loop = asyncio.create_task(hand_over_forever(world.store, seam, world.clock, every_s=0.001))
    async with asyncio.timeout(5):
        while not seam.sent:
            assert not loop.done(), "a failed pass ended the loop"
            await asyncio.sleep(0.001)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert seam.chats() == {(A, "chat")}


# --- The client -------------------------------------------------------------------------------------------------------


def client_over(respond: Callable[[httpx.Request], httpx.Response | Exception]) -> HandoffClient:
    def handler(request: httpx.Request) -> httpx.Response:
        out = respond(request)
        if isinstance(out, Exception):
            raise out
        return out

    return HandoffClient(URL, SEAM_TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_the_request_is_the_contract() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"job_id": "j", "queued": True})

    outcome = await client_over(respond).send("a/b c", {"conversation_id": "chat"})
    assert outcome == ACCEPTED
    (request,) = requests
    assert request.method == "POST"
    assert request.url.raw_path == b"/kernel/bonfires/a%2Fb%20c/sessions"
    assert request.headers["authorization"] == f"Bearer {SEAM_TOKEN}"
    assert json.loads(request.read()) == {"conversation_id": "chat"}


@pytest.mark.parametrize("status", [200, 201, 202, 204])
async def test_any_2xx_is_accepted(status: int) -> None:
    assert await client_over(lambda _: httpx.Response(status)).send(A, {}) == ACCEPTED


@pytest.mark.parametrize("status", [400, 409, 413, 422])
async def test_a_refusal_carries_the_answers_message(status: int) -> None:
    body = {"error": "kernel.handles_mismatch", "message": "other handles", "context": {}}
    outcome = await client_over(lambda _: httpx.Response(status, json=body)).send(A, {})
    assert outcome == Outcome("refused", "other handles")


async def test_a_plain_detail_is_a_message_too() -> None:
    outcome = await client_over(lambda _: httpx.Response(409, json={"detail": "other handles"})).send(A, {})
    assert outcome == Outcome("refused", "other handles")


@pytest.mark.parametrize(
    "response",
    [
        # FastAPI's own 422: every error quotes its `input`, which can be the person's words
        httpx.Response(422, json={"detail": [{"type": "extra", "loc": ["body"], "msg": "x", "input": TEXT}]}),
        httpx.Response(413, text=f"<html>{TEXT}</html>"),
        httpx.Response(400, json={"message": {"nested": TEXT}}),
        httpx.Response(400, json=[TEXT]),
    ],
)
async def test_a_refusal_with_no_plain_message_keeps_only_its_status(response: httpx.Response) -> None:
    outcome = await client_over(lambda _: response).send(A, {})
    assert outcome == Outcome("refused", f"the hand-off refused it with {response.status_code}")


async def test_a_long_message_is_cut() -> None:
    outcome = await client_over(lambda _: httpx.Response(409, json={"message": "x" * 5000})).send(A, {})
    assert outcome.reason == "x" * 500


@pytest.mark.parametrize("status", [301, 401, 403, 404, 408, 429, 500, 502, 503, 504])
async def test_everything_else_is_later(status: int) -> None:
    outcome = await client_over(lambda _: httpx.Response(status, json={"message": TEXT})).send(A, {})
    assert outcome == Outcome("later", f"the hand-off answered {status}")


async def test_no_answer_is_later() -> None:
    outcome = await client_over(lambda _: httpx.ConnectError(TEXT)).send(A, {})
    assert outcome == Outcome("later", "the hand-off did not answer: ConnectError")


async def test_a_failing_hand_off_logs_ids_and_counts_and_nothing_else(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    echo = {"detail": [{"input": TEXT}], "message": TEXT, "token": SEAM_TOKEN}
    answers: list[httpx.Response | Exception] = [
        httpx.Response(500, json=echo),
        httpx.Response(401, json=echo),
        httpx.ReadTimeout(f"{TEXT} {SEAM_TOKEN}"),
        httpx.Response(422, json=echo),
        httpx.Response(202),
    ]

    world = World(CrashesOnMark())
    session = await world.closed(A, "chat", "m1")
    client = client_over(lambda _: answers.pop(0))
    for _ in range(5):
        await hand_over_once(world.store, client, world.clock)
        world.clock.now += timedelta(hours=1)
        await world.take.retry(A, session.id)

    assert answers == []
    assert session.id in caplog.text
    assert TEXT not in caplog.text
    assert SEAM_TOKEN not in caplog.text


# --- The app ----------------------------------------------------------------------------------------------------------


def _local() -> Settings:
    return Settings(token=SecretStr("s3cret-token"), bonfire_id=A)


def _hosted() -> Settings:
    return Settings(
        auth_url="http://seam.test/check",
        auth_token=SecretStr("auth-seam"),
        handoff_url=URL,
        handoff_token=SecretStr(SEAM_TOKEN),
        handoff_every_s=0.01,
    )


async def _who(_: str) -> Caller:
    return Caller(A)


def _until(done: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not done():
        assert time.monotonic() < deadline, "the loop did not get there"
        time.sleep(0.01)


async def test_local_runs_no_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []

    async def loop(*args: object, **kwargs: object) -> None:
        started.append(args)

    monkeypatch.setattr("kernel_turns.app.hand_over_forever", loop)
    world = World()
    session = await world.closed(A, "chat", "m1")
    with TestClient(create_app(_local(), mk=FakeMk(), store=MemoryTurnStore(), sessions=world.store)) as service:
        assert service.get("/healthz").status_code == 200
    assert started == []
    assert world.state(session) == "closed"  # the writer's
    assert world.texts_of(session) == [TEXT]


async def test_hosted_runs_the_loop_with_the_given_api() -> None:
    world = World()
    session = await world.closed(A, "chat", "m1")
    app = create_app(
        _hosted(), mk=FakeMk(), store=MemoryTurnStore(), sessions=world.store, authenticate=_who, handoff=Seam()
    )
    with TestClient(app):
        _until(lambda: world.state(session) == "handed")


async def test_hosted_builds_the_client_from_settings_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Recording] = []

    class Recording(Seam):
        def __init__(self, url: str, token: str) -> None:
            super().__init__()
            self.url, self.token, self.closed = url, token, False
            made.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr("kernel_turns.app.HandoffClient", Recording)
    world = World()
    session = await world.closed(A, "chat", "m1")
    app = create_app(_hosted(), mk=FakeMk(), store=MemoryTurnStore(), sessions=world.store, authenticate=_who)
    with TestClient(app):
        _until(lambda: world.state(session) == "handed")
    (client,) = made
    assert (client.url, client.token) == (URL, SEAM_TOKEN)
    assert client.closed
