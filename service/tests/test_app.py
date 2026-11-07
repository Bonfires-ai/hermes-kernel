"""The HTTP routes over a scripted kernel and the in-process store: bodies, status codes, the bearer token."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.config import Settings
from kernel_turns.models import JsonValue
from kernel_turns.store import MemoryTurnStore
from tests.fakes import FakeMk, Script, frame

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
    response = client.post("/v1/turns", json=body, headers=AUTH)
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
    assert taught.json()["read_back"] == "sitrep {X}: web_search(query={X}) → write it"
    assert mk.calls_of("teach")[-1].get("body") == [step, "write it"]
    offer = client.post(
        f"/v1/turns/{editor['turn_id']}/offers", json={"forms": ["sitrep {X}"], "body": [step]}, headers=AUTH
    )
    assert offer.status_code == 200 and offer.json()["offer"] is None
