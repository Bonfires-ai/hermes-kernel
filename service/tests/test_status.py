"""Recall and status, and a bonfire that does not exist until its first session is built."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.config import Settings
from kernel_turns.sessions import MemorySessionStore, Session, State
from kernel_turns.store import MemoryTurnStore
from tests.fakes import CATALOGUE, FakeMk, afforded_turn

BONFIRE = "b1"
AUTH = {"Authorization": "Bearer s3cret-token"}
AT = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


class World:
    def __init__(self) -> None:
        self.mk = FakeMk()
        self.turns = MemoryTurnStore()
        self.sessions = MemorySessionStore()
        settings = Settings(token=SecretStr("s3cret-token"), bonfire_id=BONFIRE)
        self.app = create_app(settings, mk=self.mk, store=self.turns, sessions=self.sessions)


@pytest.fixture
def world() -> Iterator[tuple[World, TestClient]]:
    w = World()
    with TestClient(w.app) as client:
        yield w, client


def _session(
    session_id: str, state: State, *, reason: str | None = None, attempts: int = 0, message_count: int = 0
) -> Session:
    return Session(
        id=session_id,
        bonfire_id=BONFIRE,
        conversation_id=f"c{session_id[1:]}",
        state=state,
        agent_handles=[],
        opened_at=AT,
        last_message_at=AT,
        reason=reason,
        attempts=attempts,
        message_count=message_count,
    )


def test_context_on_an_unborn_bonfire_is_empty_not_an_error(world: tuple[World, TestClient]) -> None:
    w, client = world
    w.mk.unborn = True
    answer = client.post("/v1/context", json={"query": "who has the herb?", "speaker": "Ana"}, headers=AUTH)
    assert (answer.status_code, answer.json()) == (200, {"context": "", "degraded": True})


async def test_catalogue_before_founding_is_kept_and_answers_204(world: tuple[World, TestClient]) -> None:
    w, client = world
    w.mk.unborn = True
    answer = client.put("/v1/groups/telegram/-100777/catalogue/agent", json=CATALOGUE, headers=AUTH)
    assert answer.status_code == 204
    assert w.mk.catalogues == {}
    assert await w.turns.source(BONFIRE, "telegram", "-100777", "agent") == CATALOGUE
    w.mk.unborn = False  # founded: the same catalogue is now installed, not skipped as unchanged
    assert client.put("/v1/groups/telegram/-100777/catalogue/agent", json=CATALOGUE, headers=AUTH).status_code == 204
    assert BONFIRE in w.mk.catalogues


async def test_teach_before_founding_says_when_it_can_start(world: tuple[World, TestClient]) -> None:
    w, client = world
    turn = await afforded_turn(w.turns, "teach_phrase", bonfire_id=BONFIRE)
    w.mk.unborn = True
    body = {
        "turn_id": turn,
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "teacher": "Kira Dace",
        "forms": ["brew {X}"],
        "body": ["take water"],
    }
    answer = client.post("/v1/teaching", json=body, headers=AUTH)
    assert answer.status_code == 409
    assert "first conversation" in answer.json()["detail"]


def test_teach_on_a_turn_read_before_founding_gets_the_first_conversation_sentence(
    world: tuple[World, TestClient],
) -> None:
    # the natural path: the turn was begun while mk had no bonfire, so it has no frames and affords nothing
    w, client = world
    w.mk.unborn = True
    begun = {
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "speaker": "Kira Dace",
        "message_id": "m1",
        "text": "teach a phrase for me",
        "at": AT.isoformat(),
        "participants": ["Kira Dace"],
    }
    turn = client.post("/v1/turns", json=begun, headers=AUTH).json()
    assert "teach_phrase" not in turn["affordances"]
    body = {
        "turn_id": turn["turn_id"],
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "teacher": "Kira Dace",
        "forms": ["brew {X}"],
        "body": ["take water"],
    }
    answer = client.post("/v1/teaching", json=body, headers=AUTH)
    assert answer.status_code == 409
    assert "first conversation" in answer.json()["detail"]


def test_status_names_a_failed_session_and_the_spend(world: tuple[World, TestClient]) -> None:
    w, client = world
    w.mk.healthz = {
        "status": "ok",
        "version": "2.0.0",
        "bonfires": {BONFIRE: "ready"},
        "spend": {BONFIRE: {"charged_usd": 0.4, "ceiling_usd": 20}},
    }
    w.sessions.sessions["s1"] = _session("s1", "failed", reason="mk refused", attempts=3)
    w.sessions.sessions["s2"] = _session("s2", "open", message_count=4)
    w.sessions.sessions["s3"] = _session("s3", "closed")
    got = client.get("/v1/status", headers=AUTH).json()
    assert got["founded"] is True
    assert got["failed"] == [
        {"session_id": "s1", "conversation_id": "c1", "reason": "mk refused", "attempts": 3, "retryable": True}
    ]
    assert got["spend"] == {"charged_usd": 0.4, "ceiling_usd": 20}
    assert (got["open_messages"], got["sessions_waiting"], got["versions"]["mk"]) == (4, 1, "2.0.0")


def test_status_reads_spend_sent_as_strings(world: tuple[World, TestClient]) -> None:
    # `mk serve` sends its Decimal amounts as strings (seen live: "0.030284058" and "20")
    w, client = world
    w.mk.healthz = {
        "status": "ok",
        "version": "2.0.0",
        "bonfires": {BONFIRE: "bootstrap"},
        "spend": {BONFIRE: {"charged_usd": "0.030284058", "ceiling_usd": "20"}},
    }
    got = client.get("/v1/status", headers=AUTH).json()
    assert got["spend"] == {"charged_usd": 0.030284058, "ceiling_usd": 20}


def test_status_with_mk_down_still_answers(world: tuple[World, TestClient]) -> None:
    w, client = world
    w.mk.down = True
    answer = client.get("/v1/status", headers=AUTH)
    got = answer.json()
    assert (answer.status_code, got["founded"], got["spend"], got["versions"]["mk"]) == (200, False, None, None)


def test_a_plain_turn_on_an_unborn_bonfire_is_unread_with_a_neutral_problem(world: tuple[World, TestClient]) -> None:
    w, client = world
    w.mk.unborn = True
    turn = {
        "platform": "cli",
        "group": "s1",
        "chat": "s1",
        "speaker": "Ana",
        "message_id": "1",
        "text": "hello",
        "at": AT.isoformat(),
    }
    answer = client.post("/v1/turns", json=turn, headers=AUTH)
    got = answer.json()
    assert (answer.status_code, got["status"]) == (200, "unread")
    assert "no memory" in " ".join(got["problems"]) and "phrase" not in " ".join(got["problems"])


# --- Hosted: the worker's own answer -----------------------------------------------------------------------------------

from collections.abc import Callable  # noqa: E402

import httpx  # noqa: E402

from kernel_turns.callers import Caller, KeysAway, WrongBonfire, bonfire_guard  # noqa: E402
from kernel_turns.handoff import HandoffClient, Outcome, WorkerFailure, WorkerStatus, WorkerUnreadable  # noqa: E402
from kernel_turns.status import build_status  # noqa: E402

BUILT = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)


class Worker:
    """A fake ``HandoffApi``: ``status`` is what the worker says, or an exception for a read that fails."""

    def __init__(self, status: WorkerStatus | Exception | None = None) -> None:
        self.status = status or _worker()
        self.reads: list[str] = []

    async def send(self, bonfire_id: str, body: dict[str, object]) -> Outcome:
        return Outcome("accepted")

    async def worker_status(self, bonfire_id: str) -> WorkerStatus:
        self.reads.append(bonfire_id)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def _worker(**kw: object) -> WorkerStatus:
    base: dict[str, object] = {
        "engine": "kernel",
        "agent_handles": ["hermes"],
        "waiting": 0,
        "failed": [],
        "held": None,
        "last_built_at": BUILT,
    }
    return WorkerStatus.model_validate({**base, **kw})


HEALTH = {
    "status": "ok",
    "version": "2.0.1",
    "bonfires": {BONFIRE: "ready"},
    "spend": {BONFIRE: {"charged_usd": 0.5, "ceiling_usd": 5}},
}


async def _hosted(sessions: MemorySessionStore, worker: Worker, health: dict | None = None):  # type: ignore[type-arg]
    mk = FakeMk()
    mk.healthz = HEALTH if health is None else health
    return await build_status(mk, sessions, BONFIRE, worker=worker)


async def test_local_status_is_unchanged() -> None:
    sessions = MemorySessionStore()
    sessions.sessions["s1"] = _session("s1", "failed", reason="mk refused", attempts=3)
    sessions.sessions["s3"] = _session("s3", "closed", reason="put off", attempts=1)
    mk = FakeMk()
    mk.healthz = HEALTH
    got = (await build_status(mk, sessions, BONFIRE)).model_dump(mode="json")
    assert got["hosted"] is False
    assert got["held"] is None and got["sessions_waiting"] == 1
    assert got["failed"] == [
        {"session_id": "s1", "conversation_id": "c1", "reason": "mk refused", "attempts": 3, "retryable": True}
    ]


async def test_hosted_built_time_and_waiting_come_from_the_worker() -> None:
    sessions = MemorySessionStore()
    sessions.sessions["s1"] = _session("s1", "closed")
    sessions.sessions["s2"] = _session("s2", "open", message_count=2)
    got = await _hosted(sessions, Worker(_worker(waiting=2)))
    assert got.hosted is True
    assert got.last_built_at == BUILT
    assert got.sessions_waiting == 3 and got.open_messages == 2
    assert got.founded is True and got.versions.mk == "2.0.1"
    assert got.spend is not None and got.spend.ceiling_usd == 5


async def test_hosted_failed_lists_both_kinds() -> None:
    sessions = MemorySessionStore()
    sessions.sessions["s1"] = _session("s1", "failed", reason="refused", attempts=1)
    row = WorkerFailure(key="k9", conversation_id="chat", reason="mk said no", attempts=2)
    got = await _hosted(sessions, Worker(_worker(failed=[row])))
    assert [(f.session_id, f.retryable, f.reason) for f in got.failed] == [
        ("s1", True, "refused"),
        ("k9", False, "mk said no"),
    ]


async def test_hosted_held_comes_from_the_worker() -> None:
    got = await _hosted(MemorySessionStore(), Worker(_worker(held="bonfire spend ceiling reached")))
    assert got.held == "bonfire spend ceiling reached"


async def test_a_session_put_off_shows_in_held_on_hosted_only() -> None:
    sessions = MemorySessionStore()
    sessions.sessions["s1"] = _session("s1", "closed", reason="the hand-off answered 503", attempts=2)
    got = await _hosted(sessions, Worker())
    assert got.held is not None
    assert "waiting to be handed over" in got.held and "the hand-off answered 503" in got.held
    # the worker's own held wins; another bonfire's put-off session says nothing
    said = await _hosted(sessions, Worker(_worker(held="spend")))
    assert said.held == "spend"
    other = MemorySessionStore()
    other.sessions["x"] = _session("x", "closed", reason="r", attempts=1).model_copy(update={"bonfire_id": "zz"})
    assert (await _hosted(other, Worker())).held is None
    # and a closed session never tried has no reason
    fresh = MemorySessionStore()
    fresh.sessions["s1"] = _session("s1", "closed")
    assert (await _hosted(fresh, Worker())).held is None


async def test_worker_read_failing_is_said_not_hidden() -> None:
    got = await _hosted(MemorySessionStore(), Worker(KeysAway("down")))
    assert got.held == "the build queue can't be read right now"
    assert got.last_built_at is None and got.hosted is True
    bad = await _hosted(MemorySessionStore(), Worker(RuntimeError("boom")))
    assert bad.held == "the build queue can't be read right now"


async def test_spend_missing_stays_null_not_zero() -> None:
    got = await _hosted(MemorySessionStore(), Worker(), health={"status": "ok", "version": "2.0.1", "bonfires": {}})
    assert got.spend is None


# --- The client's read -------------------------------------------------------------------------------------------------


def _client(respond: Callable[[httpx.Request], httpx.Response | Exception]) -> HandoffClient:
    def handler(request: httpx.Request) -> httpx.Response:
        out = respond(request)
        if isinstance(out, Exception):
            raise out
        return out

    return HandoffClient("http://seam.test/", "tok", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


WIRE = {
    "engine": "kernel",
    "agent_handles": ["hermes"],
    "waiting": 1,
    "failed": [{"key": "k", "conversation_id": None, "reason": None, "attempts": 1}],
    "held": None,
    "last_built_at": "2026-11-07T01:00:00Z",
}


async def test_worker_status_is_a_get_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def respond(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=WIRE)

    got = await _client(respond).worker_status("b 1")
    assert seen[0].method == "GET" and str(seen[0].url) == "http://seam.test/bonfires/b%201/status"
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert got.waiting == 1 and got.failed[0].key == "k" and got.last_built_at == BUILT


@pytest.mark.parametrize(
    "respond",
    [
        lambda _: httpx.Response(503),
        lambda _: httpx.Response(404),
        lambda _: httpx.Response(200, text="<html>"),
        lambda _: httpx.Response(200, json=[1]),
        lambda _: httpx.Response(200, json={**WIRE, "waiting": "many"}),
        lambda _: httpx.Response(200, json={k: v for k, v in WIRE.items() if k != "engine"}),
        lambda _: httpx.ConnectError("secret-key-text"),
    ],
)
async def test_worker_status_failures_are_one_error_with_no_chain(
    respond: Callable[[httpx.Request], httpx.Response | Exception],
) -> None:
    from kernel_turns.handoff import WorkerUnreadable

    with pytest.raises(WorkerUnreadable) as info:
        await _client(respond).worker_status("b1")
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert "secret" not in str(info.value)


# --- The guard ---------------------------------------------------------------------------------------------------------


def _inner(bonfire: str = BONFIRE) -> Callable[[str], object]:
    async def inner(_: str) -> Caller:
        return Caller(bonfire)

    return inner  # type: ignore[return-value]


@pytest.mark.parametrize(
    "status", [_worker(engine="graphiti"), _worker(agent_handles=["bot"]), _worker(agent_handles=["hermes", "x"])]
)
async def test_wrong_kind_of_bonfire_is_403(status: WorkerStatus) -> None:
    guard = bonfire_guard(_inner(), Worker(status))  # type: ignore[arg-type]
    for _ in range(2):  # a wrong answer is not kept
        with pytest.raises(WrongBonfire):
            await guard("k")


async def test_first_sight_during_an_outage_is_503_never_403() -> None:
    guard = bonfire_guard(_inner(), Worker(RuntimeError("down")))  # type: ignore[arg-type]
    with pytest.raises(KeysAway):
        await guard("k")


async def test_a_good_bonfire_outlives_a_graph_memory_outage() -> None:
    worker = Worker()
    guard = bonfire_guard(_inner(), worker)  # type: ignore[arg-type]
    assert await guard("k") == Caller(BONFIRE)
    worker.status = RuntimeError("down")
    assert await guard("k") == Caller(BONFIRE)
    assert worker.reads == [BONFIRE]  # one read per bonfire, not per request
    with pytest.raises(KeysAway):
        await bonfire_guard(_inner("other"), worker)("k")  # type: ignore[arg-type]


async def test_the_guard_answers_403_over_http() -> None:
    mk = FakeMk()
    settings = Settings(token=SecretStr("s3cret-token"), bonfire_id=BONFIRE)
    sessions = MemorySessionStore()
    guarded = bonfire_guard(_inner(), Worker(_worker(engine="graphiti")))  # type: ignore[arg-type]
    app = create_app(settings, mk=mk, store=MemoryTurnStore(), sessions=sessions, authenticate=guarded)  # type: ignore[arg-type]
    with TestClient(app) as client:
        got = client.get("/v1/status", headers=AUTH)
    assert got.status_code == 403 and "not set up for hermes-kernel" in got.json()["detail"]


# --- Fix round 1 -------------------------------------------------------------------------------------------------------

import asyncio  # noqa: E402
import time  # noqa: E402


class Hung(Worker):
    async def worker_status(self, bonfire_id: str) -> WorkerStatus:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


async def test_a_hung_worker_read_answers_in_time_with_held_saying_so() -> None:
    mk = FakeMk()
    mk.healthz = HEALTH
    began = time.monotonic()
    got = await build_status(mk, MemorySessionStore(), BONFIRE, worker=Hung())
    assert time.monotonic() - began < 3
    assert got.held == "the build queue can't be read right now"


async def test_first_sight_with_a_hung_read_is_503_inside_3_s() -> None:
    began = time.monotonic()
    with pytest.raises(KeysAway):
        await bonfire_guard(_inner(), Hung())("k")  # type: ignore[arg-type]
    assert time.monotonic() - began < 3


async def test_the_worker_read_runs_beside_mk_health() -> None:
    class SlowMk(FakeMk):
        async def health(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(1.5)
            return await super().health()

    class SlowWorker(Worker):
        async def worker_status(self, bonfire_id: str) -> WorkerStatus:
            await asyncio.sleep(1.5)
            return _worker()

    mk = SlowMk()
    mk.healthz = HEALTH
    began = time.monotonic()
    await build_status(mk, MemorySessionStore(), BONFIRE, worker=SlowWorker())
    assert time.monotonic() - began < 2.5


async def test_a_worker_reason_is_cut_and_the_key_shows_only_its_last_part() -> None:
    row = WorkerFailure(
        key="kernel-session:b1:telegram:-1001:abcdef0123456789",
        conversation_id="telegram:-1001",
        reason="x" * 900,
        attempts=1,
    )
    got = await _hosted(MemorySessionStore(), Worker(_worker(failed=[row])))
    assert got.failed[0].session_id == "abcdef0123456789"
    assert got.failed[0].reason is not None and len(got.failed[0].reason) == 500


async def test_nulls_may_be_left_out_of_the_wire() -> None:
    wire = {"engine": "kernel", "agent_handles": ["hermes"], "waiting": 0, "failed": [{"key": "k", "attempts": 1}]}
    got = _client(lambda _: httpx.Response(200, json=wire))
    status = await got.worker_status("b1")
    assert status.held is None and status.last_built_at is None
    assert status.failed[0].reason is None and status.failed[0].conversation_id is None


# --- Final fix round: the guard's failure is visible and says the one fixed thing -------------------------------------

import logging  # noqa: E402


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        (RuntimeError("down with secret words"), "RuntimeError"),
        (WorkerUnreadable("the hand-off's status answered 401"), "the hand-off's status answered 401"),
    ],
)
async def test_an_unreadable_bonfire_is_one_warning_and_the_fixed_text(
    caplog: pytest.LogCaptureFixture, failure: Exception, problem: str
) -> None:
    caplog.set_level(logging.DEBUG, logger="kernel_turns")
    with pytest.raises(KeysAway) as info:
        await bonfire_guard(_inner(), Worker(failure))("k")  # type: ignore[arg-type]
    assert str(info.value) == "the key check is unavailable"
    (record,) = [r for r in caplog.records if "key_check_failed" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"kernel_turns.key_check_failed: status=worker status: {problem}"
    assert "secret" not in caplog.text
