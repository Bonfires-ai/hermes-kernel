"""The HTTP routes over a scripted kernel and the in-process store: bodies, status codes, who is calling."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.callers import Caller, KeysAway, ReadOnlyKey, Unauthorized
from kernel_turns.config import Settings
from kernel_turns.models import JsonValue
from kernel_turns.sessions import MemorySessionStore
from kernel_turns.store import MemoryTurnStore
from tests.fakes import CATALOGUE, FakeMk, Script, frame

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WATER = {"item": "water"}
GIFT = {"item": "thé", "person": "Kira Dace — café"}
TEAM = "/v1/groups/telegram/-100777"


def _settings(token: str = TOKEN) -> Settings:
    return Settings(token=SecretStr(token), bonfire_id="65f0c0ffee0000000000abcd")


GIVE_ROW: dict[str, JsonValue] = {
    "id": "give.d1.0",
    "kind": "taught",
    "form": ["give", {"slot": "x", "fills": "np"}],
    "meaning": [["take", "?w", "water"], ["give", "?g", "?w", "?x"], ["answer", "?g"]],
    "standing": {"confirmed": 1, "uses_ok": 0, "uses_failed": 0, "uses_world": 0},
    "status": "live",
    "provenance": {"teacher": "Kira Dace", "chat_id": "-100777", "draft_id": "d1", "form": "give {X}"},
}
STAGED_ROW: dict[str, JsonValue] = {**GIVE_ROW, "id": "fetch.d2.0", "status": "staged"}
STAGED_ROW["provenance"] = {"teacher": "Kira Dace", "chat_id": "-100777", "draft_id": "d2", "form": "fetch {X}"}


@pytest.fixture
def mk() -> FakeMk:
    fake = FakeMk(
        {
            # the message reads in part (a taught phrase read it); the phrase's text reads completely
            "please give thé to Kira Dace": Script(
                steps=[], partial=True, frames=[frame("give.d1.0", "phrase", construction="frame.give.d1.0.give")]
            ),
            "give thé to Kira Dace": Script(steps=[("take", WATER), ("give", GIFT)], phrase="give.d1.0"),
            "teach fetch": Script(steps=[], partial=True, frames=[frame("teach", "editor")]),
            "yes": Script(steps=[], partial=True, frames=[frame("yes", "reply", complete=True)]),
        }
    )
    fake.rows = [GIVE_ROW]
    return fake


@pytest.fixture
def client(mk: FakeMk) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(), mk=mk, store=MemoryTurnStore())) as client:
        yield client


def _begin(
    client: TestClient,
    text: str = "please give thé to Kira Dace",
    group: str = "-100777",
    message_id: str = "42",
    speaker: str = "Kira Dace",
    headers: dict[str, str] = AUTH,
) -> dict[str, JsonValue]:
    body = {
        "platform": "telegram",
        "group": group,
        "chat": group,
        "speaker": speaker,
        "message_id": message_id,
        "text": text,
        "at": "2026-10-22T18:00:00+00:00",
        "participants": ["Kira Dace", "Milo"],
    }
    response = client.post("/v1/turns", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_requests_without_the_token_are_refused(client: TestClient) -> None:
    turns = {
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "message_id": "1",
        "text": "hi",
        "at": "2026-10-22T18:00:00Z",
    }
    assert client.post("/v1/turns", json=turns).status_code == 401
    wrong = client.post("/v1/turns", json=turns, headers={"Authorization": "Bearer not-the-token"})
    assert wrong.status_code == 401 and wrong.headers["WWW-Authenticate"] == "Bearer"
    assert client.post("/v1/turns", json=turns, headers={"Authorization": TOKEN}).status_code == 401  # no scheme
    assert client.get("/healthz").status_code == 200
    # FastAPI's generated docs and schema are off: only /healthz is open
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path

    with TestClient(create_app(_settings(token=""), mk=FakeMk(), store=MemoryTurnStore())) as open_app:
        assert open_app.post("/v1/turns", json=turns, headers={"Authorization": "Bearer "}).status_code == 401
        assert open_app.post("/v1/turns", json=turns, headers=AUTH).status_code == 401  # no token = refuse all
        assert open_app.get("/healthz").json() == {"status": "ok"}


def test_the_offers_route_records_an_offer(client: TestClient) -> None:
    turn = _begin(client, "teach fetch", message_id="60")["turn_id"]
    offer = {"forms": ["fetch {X}"], "body": ["take {X}"], "defaults": {}}
    view = client.post(f"/v1/turns/{turn}/offers", json=offer, headers=AUTH)
    assert view.status_code == 200, view.text
    assert any(f["construction"] == "offer" for f in view.json()["frames"]) and view.json()["offer"] is None
    assert client.post(f"/v1/turns/{turn}/offers", json={**offer, "forms": []}, headers=AUTH).status_code == 422
    assert client.post("/v1/turns/no-such-turn/offers", json=offer, headers=AUTH).status_code == 404
    assert client.post(f"/v1/turns/{turn}/offers", json=offer).status_code == 401


def test_teach_and_offer_routes_accept_structured_steps(client: TestClient, mk: FakeMk) -> None:
    editor = _begin(client, "teach fetch", message_id="50")
    step = {"call": "web_search", "args": {"query": "{X}"}}
    draft = {
        "turn_id": editor["turn_id"],
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "teacher": "Kira Dace",
        "forms": ["sitrep {X}"],
        "body": [step, "write it"],
    }
    taught = client.post("/v1/teaching", json=draft, headers=AUTH)
    assert taught.status_code == 200
    assert (
        taught.json()["read_back"] == 'sitrep {X}: web_search(query={X}) → write it. Answer with just "yes" to keep it.'
    )
    assert mk.calls_of("teach")[-1].get("body") == [step, "write it"]
    offer = client.post(
        f"/v1/turns/{editor['turn_id']}/offers", json={"forms": ["sitrep {X}"], "body": [step]}, headers=AUTH
    )
    assert offer.status_code == 200 and offer.json()["offer"] is None


def test_a_refused_call_is_logged_by_its_reason_with_no_value(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "the-address-of-the-safehouse"
    begun = _begin(client)
    with caplog.at_level(logging.INFO):
        refused = client.post(
            f"/v1/turns/{begun['turn_id']}/calls", json={"tool": "give.d1.0", "args": {"X": secret}}, headers=AUTH
        )
    assert refused.status_code == 409 and secret in refused.json()["detail"]  # the sentence goes back to the model
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("kernel_turns")]
    assert any("call_refused" in line and "reason=not_read" in line and "tool=give.d1.0" in line for line in lines)
    assert secret not in caplog.text


# -- two callers, two bonfires ------------------------------------------------------------------------------------------

BONFIRE_A = "bonfire-a"
BONFIRE_B = "bonfire-b"
AS_A = {"Authorization": "Bearer key-a"}
AS_B = {"Authorization": "Bearer key-b"}
MAKE = {"title": "onboard sam"}
ASK_YES = {"platform": "telegram", "group": "-100777", "by": "Kira Dace"}
MESSAGE = {
    "conversation_id": "c1",
    "message_id": "m1",
    "speaker": "Kira Dace",
    "text": "hello",
    "timestamp": "2026-10-22T18:00:00+00:00",
}


async def _two_keys(bearer: str) -> Caller:
    bonfires = {"key-a": BONFIRE_A, "key-b": BONFIRE_B}
    if bearer not in bonfires:
        raise Unauthorized("that key is not known")
    return Caller(bonfire_id=bonfires[bearer])


@pytest.fixture
def mk_ab() -> FakeMk:
    return FakeMk(
        {
            "make onboard sam": Script(steps=[], partial=True, frames=[frame("make", title=None)]),
            "teach fetch": Script(steps=[], partial=True, frames=[frame("teach", "editor")]),
            "yes": Script(steps=[], partial=True, frames=[frame("yes", "reply", complete=True)]),
        }
    )


@pytest.fixture
def turns_ab() -> MemoryTurnStore:
    return MemoryTurnStore()


@pytest.fixture
def sessions_ab() -> MemorySessionStore:
    return MemorySessionStore()


@pytest.fixture
def client_ab(mk_ab: FakeMk, turns_ab: MemoryTurnStore, sessions_ab: MemorySessionStore) -> Iterator[TestClient]:
    # settings.bonfire_id is neither caller's: a route that read it would show
    app = create_app(_settings(), mk=mk_ab, store=turns_ab, sessions=sessions_ab, authenticate=_two_keys)
    with TestClient(app) as client:
        yield client


def _draft_body(turn: JsonValue) -> dict[str, JsonValue]:
    return {
        "turn_id": turn,
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "teacher": "Kira Dace",
        "forms": ["fetch {X}"],
        "body": ["take {X}"],
    }


async def test_turn_of_another_caller_is_404_not_403(
    client_ab: TestClient, mk_ab: FakeMk, turns_ab: MemoryTurnStore
) -> None:
    assert client_ab.put(f"{TEAM}/catalogue/agent", json=CATALOGUE, headers=AS_A).status_code == 204
    turn = _begin(client_ab, "make onboard sam", headers=AS_A)["turn_id"]
    assert isinstance(turn, str)
    url = f"/v1/turns/{turn}"
    called = client_ab.post(f"{url}/calls", json={"tool": "make", "args": MAKE}, headers=AS_A)
    assert called.status_code == 200, called.text
    step = called.json()["step"]["step_id"]
    offer = {"forms": ["fetch {X}"], "body": ["take {X}"]}
    report = {"step_id": step, "verdict": "committed", "value": {"id": "JUJ-9"}}
    kernel_heard = list(mk_ab.seen)

    theirs = {
        "get": client_ab.get(url, headers=AS_B),
        "calls": client_ab.post(f"{url}/calls", json={"tool": "make", "args": {"title": "theirs"}}, headers=AS_B),
        "offers": client_ab.post(f"{url}/offers", json=offer, headers=AS_B),
        "report": client_ab.post(f"{url}/report", json=report, headers=AS_B),
        "retry": client_ab.post(f"{url}/retry", headers=AS_B),
    }
    assert {name: answer.status_code for name, answer in theirs.items()} == dict.fromkeys(theirs, 404)
    nothing = client_ab.get("/v1/turns/no-such-turn", headers=AS_A)
    assert (nothing.status_code, nothing.json()["detail"]) == (404, "no turn 'no-such-turn'")
    assert theirs["get"].json()["detail"] == f"no turn {turn!r}"  # told what a turn that never existed tells
    assert mk_ab.seen == kernel_heard
    assert await turns_ab.ledger(BONFIRE_A, turn) == []

    assert client_ab.get(url, headers=AS_A).status_code == 200
    assert client_ab.post(f"{url}/offers", json=offer, headers=AS_A).status_code == 200
    assert client_ab.post(f"{url}/report", json=report, headers=AS_A).status_code == 200
    assert client_ab.post(f"{url}/retry", headers=AS_A).status_code == 409  # the turn is there; it has nothing to retry
    assert len(await turns_ab.ledger(BONFIRE_A, turn)) == 1


def test_teaching_from_a_turn_of_another_caller_is_404(client_ab: TestClient) -> None:
    turn = _begin(client_ab, "teach fetch", headers=AS_A)["turn_id"]
    assert client_ab.post("/v1/teaching", json=_draft_body(turn), headers=AS_B).status_code == 404
    assert client_ab.post("/v1/teaching", json=_draft_body(turn), headers=AS_A).status_code == 200


def test_session_of_another_caller_is_404(client_ab: TestClient, sessions_ab: MemorySessionStore) -> None:
    added = client_ab.post("/v1/messages", json={"messages": [MESSAGE]}, headers=AS_A)
    assert added.json() == {"accepted": 1, "duplicates": 0}
    closed = client_ab.post("/v1/sessions/close", json={"conversation_id": "c1"}, headers=AS_B)
    assert closed.json() == {"closed": False}
    session = next(iter(sessions_ab.sessions.values()))
    assert (session.bonfire_id, session.state) == (BONFIRE_A, "open")
    session.state, session.reason = "failed", "mk refused it"

    theirs = client_ab.post(f"/v1/sessions/{session.id}/retry", headers=AS_B)
    assert theirs.status_code == 404 and session.state == "failed"
    nothing = client_ab.post("/v1/sessions/no-such-session/retry", headers=AS_B)
    assert nothing.status_code == 404
    assert client_ab.get("/v1/status", headers=AS_B).json()["failed"] == []
    assert client_ab.get("/v1/status", headers=AS_A).json()["failed"][0]["session_id"] == session.id

    assert client_ab.post(f"/v1/sessions/{session.id}/retry", headers=AS_A).status_code == 204
    assert session.state == "closed"


def test_confirm_of_a_draft_the_bonfire_does_not_hold_is_404(client_ab: TestClient, mk_ab: FakeMk) -> None:
    editor = _begin(client_ab, "teach fetch", message_id="50", headers=AS_A)["turn_id"]
    taught = client_ab.post("/v1/teaching", json=_draft_body(editor), headers=AS_A)
    draft = taught.json()["draft_id"]
    assert draft

    # a turn of their own, spoken by a person of the same name: all they lack is the draft
    theirs = _begin(client_ab, "yes", message_id="51", headers=AS_B)["turn_id"]
    for card in (False, True):
        body = {**ASK_YES, "turn_id": theirs, "card": card}
        answer = client_ab.post(f"/v1/teaching/{draft}/confirm", json=body, headers=AS_B)
        assert answer.status_code == 404, answer.text
    assert mk_ab.calls_of("confirm") == []

    mine = _begin(client_ab, "yes", message_id="51", headers=AS_A)["turn_id"]
    body = {**ASK_YES, "turn_id": mine}
    assert client_ab.post("/v1/teaching/no-such-draft/confirm", json=body, headers=AS_A).status_code == 404
    kept = client_ab.post(f"/v1/teaching/{draft}/confirm", json=body, headers=AS_A)
    assert kept.status_code == 200, kept.text
    assert kept.json()["phrases"] == [f"brew.{draft}.0"]


def test_the_kernel_hears_only_the_bonfire_of_the_caller(
    client_ab: TestClient, mk_ab: FakeMk, sessions_ab: MemorySessionStore
) -> None:
    assert client_ab.put(f"{TEAM}/catalogue/agent", json=CATALOGUE, headers=AS_A).status_code == 204
    editor = _begin(client_ab, "teach fetch", message_id="50", headers=AS_A)["turn_id"]
    draft = client_ab.post("/v1/teaching", json=_draft_body(editor), headers=AS_A).json()["draft_id"]
    yes = _begin(client_ab, "yes", message_id="51", headers=AS_A)["turn_id"]
    body = {**ASK_YES, "turn_id": yes}
    assert client_ab.post(f"/v1/teaching/{draft}/confirm", json=body, headers=AS_A).status_code == 200
    assert client_ab.get(f"{TEAM}/verbs", headers=AS_A).status_code == 200
    recall = client_ab.post("/v1/context", json={"query": "who is here"}, headers=AS_A)
    assert recall.json() == {"context": "", "degraded": False}
    assert client_ab.post("/v1/messages", json={"messages": [MESSAGE]}, headers=AS_A).status_code == 200
    status = client_ab.get("/v1/status", headers=AS_A).json()

    assert (status["bonfire_id"], status["open_messages"]) == (BONFIRE_A, 1)
    assert {call for call, _ in mk_ab.seen} == {"put_catalogue", "plan", "constructions", "teach", "confirm", "context"}
    assert {bonfire for _, bonfire in mk_ab.seen} == {BONFIRE_A}
    assert {session.bonfire_id for session in sessions_ab.sessions.values()} == {BONFIRE_A}

    assert len(client_ab.get(f"{TEAM}/turns", headers=AS_A).json()) == 2
    assert client_ab.get(f"{TEAM}/turns", headers=AS_B).json() == []
    other = client_ab.get("/v1/status", headers=AS_B).json()
    assert (other["bonfire_id"], other["open_messages"]) == (BONFIRE_B, 0)


def test_no_v1_route_goes_without_its_caller(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    # `app.routes` holds the included router, not its routes: walking it directly would find nothing to check
    routes = [(c.path, c.original_route) for c in iter_route_contexts(app.routes) if (c.path or "").startswith("/v1")]
    assert len(routes) == 16
    for path, route in routes:
        assert isinstance(path, str) and isinstance(route, APIRoute)
        assert "who" in inspect.signature(route.endpoint).parameters, path
        assert any(d.call is app.state.caller for d in route.dependant.dependencies), path
        assert route.methods, path
        for method in route.methods:
            assert client.request(method, re.sub(r"{[^}]+}", "x", path)).status_code == 401, (method, path)


def test_authenticate_gets_a_bearer_and_is_asked_once(mk: FakeMk) -> None:
    asked: list[str] = []

    async def authenticate(bearer: str) -> Caller:
        asked.append(bearer)
        return Caller(bonfire_id=BONFIRE_A)

    with TestClient(create_app(_settings(), mk=mk, store=MemoryTurnStore(), authenticate=authenticate)) as client:
        for header in ("", "key-a", "Basic key-a", "Bearer", "Bearer "):
            assert client.get("/v1/status", headers={"Authorization": header}).status_code == 401, header
        assert asked == []  # none of these names a key
        assert client.get("/v1/status", headers=AS_A).status_code == 200
        assert client.get("/v1/status", headers=AUTH).status_code == 200  # what the bearer means is authenticate's
    assert asked == ["key-a", TOKEN]


def test_a_key_is_refused_by_what_is_wrong_with_it(mk: FakeMk) -> None:
    refusals: dict[str, Exception] = {
        "unknown": Unauthorized("that key is not known"),
        "read-only": ReadOnlyKey("this key is read-only"),
        "away": KeysAway("keys cannot be checked right now"),
    }

    async def authenticate(bearer: str) -> Caller:
        raise refusals[bearer]

    with TestClient(create_app(_settings(), mk=mk, store=MemoryTurnStore(), authenticate=authenticate)) as client:
        answers = {
            bearer: client.get("/v1/status", headers={"Authorization": f"Bearer {bearer}"}) for bearer in refusals
        }
    assert answers["unknown"].status_code == 401 and answers["unknown"].headers["WWW-Authenticate"] == "Bearer"
    assert answers["read-only"].status_code == 403
    assert answers["read-only"].json() == {"detail": "this key is read-only"}
    assert answers["away"].status_code == 503 and answers["away"].headers["Retry-After"] == "5"
    # one fixed sentence, whatever the exception says: a seam's status or words never reach the person
    assert answers["away"].json() == {"detail": "the key check is unavailable"}
