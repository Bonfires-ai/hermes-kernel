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
    assert await w.turns.source("telegram", "-100777", "agent") == CATALOGUE
    w.mk.unborn = False  # founded: the same catalogue is now installed, not skipped as unchanged
    assert client.put("/v1/groups/telegram/-100777/catalogue/agent", json=CATALOGUE, headers=AUTH).status_code == 204
    assert BONFIRE in w.mk.catalogues


async def test_teach_before_founding_says_when_it_can_start(world: tuple[World, TestClient]) -> None:
    w, client = world
    turn = await afforded_turn(w.turns, "teach_phrase")
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
    assert got["failed"] == [{"session_id": "s1", "conversation_id": "c1", "reason": "mk refused", "attempts": 3}]
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
