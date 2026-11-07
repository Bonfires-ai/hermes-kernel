"""One customer can never reach another's data, through any route, on either store.

Customer A is seeded with real rows: a turn with a committed ledger row and a pending step, a turn in repair, a live
phrase and a staged draft, a use, a catalogue, an open, a closed and a failed session, and numbers of its own at the
kernel and at the worker. Customer B holds a phrase with the same id as A's. Then every ``/v1`` route is called on A's
ids with B's key, with no key or an unknown one, and with the key of a bonfire this service must not serve. Each time
A's state is exactly what it was, and the kernel hears nothing of A.

``PROBES`` holds one probe per route, keyed by ``(method, path)``, and ``test_every_route_has_a_probe`` fails for a route
that has none: a new route fails here until it has one. A probe's ``call`` is plain HTTP (any ``httpx.Client``) and its
``check`` reads only the answer and ``Ids``, so a live service can be probed the same way. ``owner`` is what the same
call answers with A's own key: the ids are real, so a 404 for B is about the key and nothing else.

The app is built the hosted way, from ``Settings(auth_url=..., handoff_url=...)``, with the real ``KeyCheck`` and
``HandoffClient`` over ``httpx.MockTransport``, so the guard ``create_app`` wraps around the key check is what is under
test; an injected ``authenticate`` would go round it. The Mongo cases run only with ``KT_TEST_MONGO_URI`` set, and share
one database (``conftest.shared_database``), emptied after each test.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import AsyncMongoClient

from kernel_turns import keys
from kernel_turns.app import create_app
from kernel_turns.callers import KeyCheck
from kernel_turns.config import Settings
from kernel_turns.handoff import HandoffClient
from kernel_turns.models import JsonValue, LedgerRow, TurnRecord
from kernel_turns.sessions import MemorySessionStore, MongoSessionStore, SessionStore
from kernel_turns.store import Doc, MemoryTurnStore, MongoTurnStore, TurnStore
from tests.fakes import CATALOGUE, FakeMk, Script, frame

MONGO_URI = os.environ.get("KT_TEST_MONGO_URI")
NO_MONGO = "KT_TEST_MONGO_URI is not set"

#: two good bonfires, and one this service must not serve
A, B, C = "bonfire-a", "bonfire-b", "bonfire-c"
KEY_A, KEY_B, KEY_C = "key-a", "key-b", "key-c"
KEYS = {KEY_A: A, KEY_B: B, KEY_C: C}
UNKNOWN_KEY = "key-nobody-holds"
SEAM_TOKEN = "seam-token"
AUTH_URL = "http://platform.test/internal/turn-service/keys/check"
HANDOFF_URL = "http://worker.test/internal/turn-service"

PLATFORM, GROUP, SPEAKER = "telegram", "-100777", "Kira Dace"
TEAM = f"/v1/groups/{PLATFORM}/{GROUP}"
AT = "2026-11-07T10:00:00+00:00"
MAKE: dict[str, JsonValue] = {"title": "onboard sam"}
#: what only A's data holds, beside its ids
VALUE, FORM, SECRET, SPENT = "JUJ-9", "fetch {X}", "the door code is 4417", "0.4242"
#: the catalogue B puts on A's group: unlike A's, so a write that reached A's rows would show
THEIRS: dict[str, JsonValue] = {
    "schema_version": "kernel-catalogue/v1",
    "tools": [{"name": "their_tool", "x-side-effect": False, "input_schema": {"type": "object", "properties": {}}}],
}

SCRIPTS = {
    "make onboard sam": Script(steps=[], partial=True, frames=[frame("make", title=None)]),
    "teach fetch": Script(steps=[], partial=True, frames=[frame("teach", "editor")]),
    "yes": Script(steps=[], partial=True, frames=[frame("yes", "reply", complete=True)]),
    # applies the first phrase taught (``brew.d1.0`` in either bonfire) and is answered: a use
    "fetch water": Script(steps=[], phrase="brew.d1.0", answer="fetched"),
}


def _worker(waiting: int = 0, engine: str = "kernel", handles: tuple[str, ...] = ("hermes",)) -> dict[str, JsonValue]:
    return {"engine": engine, "agent_handles": list(handles), "waiting": waiting, "failed": []}


#: what the worker says of each bonfire. A's every word names A, so a leak of it shows in the answer
WORKERS: dict[str, dict[str, JsonValue]] = {
    A: {
        **_worker(waiting=3),
        "failed": [
            {"key": f"{A}:conv-a:s9", "conversation_id": "conv-a", "reason": f"{A} did not build", "attempts": 2}
        ],
        "held": f"{A} is over its ceiling",
        "last_built_at": "2026-11-06T09:00:00+00:00",
    },
    B: _worker(),
}
#: a Graphiti bonfire, and a bot's kernel bonfire: a good key for either is refused
WRONG_KINDS: dict[str, dict[str, JsonValue]] = {
    "graphiti": _worker(engine="graphiti", handles=()),
    "a bot's kernel": _worker(handles=("some-bot",)),
}
NOT_SERVED = {"detail": "this key's bonfire is not set up for hermes-kernel"}


# -- the probes ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ids:
    """Customer A's real ids, and what only A's data holds."""

    bonfire: str
    platform: str
    group: str
    chat: str
    speaker: str
    #: a message turn with a committed ledger row, and the step it still has pending
    turn: str
    turn_message: str
    text: str
    step: str
    #: a turn in repair after a ``tool_error``: what a retry follows
    broken_turn: str
    #: a turn that affords ``teach_phrase``
    editor_turn: str
    #: a draft waiting for its teacher's yes, and the form of the phrase that is live
    draft: str
    form: str
    #: a failed session
    session: str
    #: a chat with a session open, and a message stored in it
    conversation: str
    message: str
    #: strings only A's data holds: none may reach another customer's answer
    private: tuple[str, ...]


Call = Callable[[httpx.Client, Ids, str | None], httpx.Response]
Check = Callable[[httpx.Response, Ids], None]


@dataclass(frozen=True)
class Probe:
    #: makes the request with ``key`` (None sends no ``Authorization``)
    call: Call
    #: what another customer's key must be answered
    check: Check
    #: what the same call answers with A's own key
    owner: Check
    #: the call reaches the kernel, for the caller's own bonfire
    kernel: bool = False


def _auth(key: str | None) -> dict[str, str]:
    return {} if key is None else {"Authorization": f"Bearer {key}"}


def _turn_body(ids: Ids, message_id: str, text: str) -> dict[str, JsonValue]:
    return {
        "platform": ids.platform,
        "group": ids.group,
        "chat": ids.chat,
        "speaker": ids.speaker,
        "message_id": message_id,
        "text": text,
        "at": AT,
        "participants": [ids.speaker],
    }


def _nothing_of_theirs(answer: httpx.Response, ids: Ids) -> None:
    for held in ids.private:
        assert held not in answer.text, f"the answer holds {held!r}, which only the other customer's data holds"


def unknown(detail: Callable[[Ids], str]) -> Check:
    """404, told what an id that never existed is told: nothing more."""

    def check(answer: httpx.Response, ids: Ids) -> None:
        assert answer.status_code == 404, answer.text
        assert answer.json() == {"detail": detail(ids)}

    return check


def answered(status: int = 200) -> Check:
    """Answered, from the caller's own data alone."""

    def check(answer: httpx.Response, ids: Ids) -> None:
        assert answer.status_code == status, answer.text
        _nothing_of_theirs(answer, ids)

    return check


def exactly(body: JsonValue) -> Check:
    def check(answer: httpx.Response, ids: Ids) -> None:
        assert (answer.status_code, answer.json()) == (200, body)

    return check


def shows(status: int, *fields: str) -> Check:
    """For the owner: answered ``status``, holding each of these of its ids."""

    def check(answer: httpx.Response, ids: Ids) -> None:
        assert answer.status_code == status, answer.text
        for name in fields:
            assert getattr(ids, name) in answer.text, name

    return check


no_turn = unknown(lambda ids: f"no turn {ids.turn!r}")


def _begin(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    """The very message of A's turn."""
    return client.post("/v1/turns", json=_turn_body(ids, ids.turn_message, ids.text), headers=_auth(key))


def _a_turn_of_its_own(answer: httpx.Response, ids: Ids) -> None:
    answered()(answer, ids)
    assert answer.json()["turn_id"] != ids.turn  # the same message in another bonfire is another turn


def _get_turn(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.get(f"/v1/turns/{ids.turn}", headers=_auth(key))


def _call(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    """The call A made and committed: its value is A's."""
    return client.post(f"/v1/turns/{ids.turn}/calls", json={"tool": "make", "args": MAKE}, headers=_auth(key))


def _offer(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    offer = {"forms": ["sip {X}"], "body": ["take {X}"]}
    return client.post(f"/v1/turns/{ids.turn}/offers", json=offer, headers=_auth(key))


def _report(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    report = {"step_id": ids.step, "verdict": "committed", "value": {"id": "theirs"}}
    return client.post(f"/v1/turns/{ids.turn}/report", json=report, headers=_auth(key))


def _retry_turn(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.post(f"/v1/turns/{ids.broken_turn}/retry", headers=_auth(key))


def _teach(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    draft = {
        "turn_id": ids.editor_turn,
        "platform": ids.platform,
        "group": ids.group,
        "chat": ids.chat,
        "teacher": ids.speaker,
        "forms": ["sip {X}"],
        "body": ["take {X}"],
    }
    return client.post("/v1/teaching", json=draft, headers=_auth(key))


def _drafted(answer: httpx.Response, ids: Ids) -> None:
    assert answer.status_code == 200 and answer.json()["draft_id"], answer.text


def _confirm(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    """A yes of the caller's own, by a person of the teacher's name: all another customer lacks is the draft."""
    yes = client.post("/v1/turns", json=_turn_body(ids, "m-probe-yes", "yes"), headers=_auth(key))
    if yes.status_code != 200:
        return yes
    body = {"platform": ids.platform, "group": ids.group, "by": ids.speaker, "turn_id": yes.json()["turn_id"]}
    return client.post(f"/v1/teaching/{ids.draft}/confirm", json=body, headers=_auth(key))


def _put_catalogue(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.put(f"/v1/groups/{ids.platform}/{ids.group}/catalogue/agent", json=THEIRS, headers=_auth(key))


def _no_content(answer: httpx.Response, ids: Ids) -> None:
    assert (answer.status_code, answer.content) == (204, b""), answer.text


def _verbs(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.get(f"/v1/groups/{ids.platform}/{ids.group}/verbs", headers=_auth(key))


def _recent_turns(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.get(f"/v1/groups/{ids.platform}/{ids.group}/turns", headers=_auth(key))


def _add_message(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    message = {
        "conversation_id": ids.conversation,
        "message_id": ids.message,
        "speaker": ids.speaker,
        "text": "theirs",
        "timestamp": AT,
    }
    return client.post("/v1/messages", json={"messages": [message]}, headers=_auth(key))


def _close(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.post("/v1/sessions/close", json={"conversation_id": ids.conversation}, headers=_auth(key))


def _retry_session(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.post(f"/v1/sessions/{ids.session}/retry", headers=_auth(key))


def _recall(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.post(
        "/v1/context", json={"query": "what is the door code", "speaker": ids.speaker}, headers=_auth(key)
    )


def _status(client: httpx.Client, ids: Ids, key: str | None) -> httpx.Response:
    return client.get("/v1/status", headers=_auth(key))


#: one per route under /v1, by (method, path). Run one after another with the same key, in this order, each still holds
#: on fresh ids: the close comes before the message that would open the caller's own session in that chat
PROBES: dict[tuple[str, str], Probe] = {
    ("POST", "/v1/turns"): Probe(_begin, _a_turn_of_its_own, shows(200, "turn"), kernel=True),
    ("GET", "/v1/turns/{turn_id}"): Probe(_get_turn, no_turn, shows(200, "turn")),
    ("POST", "/v1/turns/{turn_id}/calls"): Probe(_call, no_turn, shows(200, "turn")),
    ("POST", "/v1/turns/{turn_id}/offers"): Probe(_offer, no_turn, shows(200, "turn")),
    ("POST", "/v1/turns/{turn_id}/report"): Probe(_report, no_turn, shows(200, "turn")),
    ("POST", "/v1/turns/{turn_id}/retry"): Probe(
        _retry_turn, unknown(lambda ids: f"no turn {ids.broken_turn!r}"), shows(200, "broken_turn")
    ),
    ("POST", "/v1/teaching"): Probe(_teach, unknown(lambda ids: f"no turn {ids.editor_turn!r}"), _drafted),
    ("POST", "/v1/teaching/{draft_id}/confirm"): Probe(
        _confirm, unknown(lambda ids: f"no staged draft {ids.draft!r}"), shows(200, "draft"), kernel=True
    ),
    ("PUT", "/v1/groups/{platform}/{group}/catalogue/{source}"): Probe(
        _put_catalogue, _no_content, _no_content, kernel=True
    ),
    ("GET", "/v1/groups/{platform}/{group}/verbs"): Probe(_verbs, answered(), shows(200, "form"), kernel=True),
    ("GET", "/v1/groups/{platform}/{group}/turns"): Probe(_recent_turns, answered(), shows(200, "turn", "text")),
    ("POST", "/v1/sessions/close"): Probe(_close, exactly({"closed": False}), exactly({"closed": True})),
    ("POST", "/v1/messages"): Probe(
        _add_message, exactly({"accepted": 1, "duplicates": 0}), exactly({"accepted": 0, "duplicates": 1})
    ),
    ("POST", "/v1/sessions/{session_id}/retry"): Probe(
        _retry_session, unknown(lambda ids: f"no session {ids.session!r}"), shows(204)
    ),
    ("POST", "/v1/context"): Probe(_recall, answered(), shows(200, "bonfire"), kernel=True),
    ("GET", "/v1/status"): Probe(_status, answered(), shows(200, "bonfire", "session")),
}
ROUTES = sorted(PROBES)


def _route_id(route: tuple[str, str]) -> str:
    return " ".join(route)


# -- the world -----------------------------------------------------------------------------------------------------------


class Kernel(FakeMk):
    """``FakeMk`` whose memory is its bonfire's own: recall answers with the bonfire it was asked of."""

    async def context(self, bonfire: str, query: str, speaker: str | None) -> tuple[str, bool]:
        await super().context(bonfire, query, speaker)
        return f"what {bonfire} remembers", False


@dataclass
class Seam:
    """The two hosted seams over ``httpx.MockTransport``: the key check, and the worker. Records what each was asked."""

    workers: dict[str, dict[str, JsonValue]]
    keys_asked: list[str] = field(default_factory=list)
    status_asked: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)

    def check_key(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {SEAM_TOKEN}":
            return httpx.Response(401)
        key = json.loads(request.content)["key"]
        self.keys_asked.append(key)
        if key not in KEYS:
            return httpx.Response(404)
        return httpx.Response(200, json={"bonfire_id": KEYS[key], "permission": "write"})

    def worker(self, request: httpx.Request) -> httpx.Response:
        found = re.fullmatch(r"/internal/turn-service/bonfires/([^/]+)/(status|sessions)", request.url.path)
        if request.headers.get("Authorization") != f"Bearer {SEAM_TOKEN}" or found is None:
            return httpx.Response(401 if found else 404)
        bonfire, what = found.groups()
        if what == "sessions":
            self.sent.append(bonfire)
            return httpx.Response(202)
        self.status_asked.append(bonfire)
        held = self.workers.get(bonfire)
        return httpx.Response(200, json=held) if held is not None else httpx.Response(404)


def _over(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@dataclass
class World:
    app: FastAPI
    client: TestClient
    mk: Kernel
    turns: TurnStore
    sessions: SessionStore
    seam: Seam

    def run(self, fn: Callable[..., Any], *args: object) -> Any:
        """``fn(*args)`` on the app's own event loop, where the stores live."""
        portal = self.client.portal
        assert portal is not None
        return portal.call(fn, *args)

    def state(self, bonfire: str) -> dict[str, Any]:
        """Everything the stores and the kernel hold of ``bonfire``."""
        return self.run(self._state, bonfire)

    async def _state(self, bonfire: str) -> dict[str, Any]:
        turns = await self.turns.recent_turns(bonfire, 1000)
        sessions = sorted(await self.sessions.waiting(bonfire), key=lambda s: s.id)
        return copy.deepcopy(
            {
                "turns": turns,
                "ledger": {t.turn_id: await self.turns.ledger(bonfire, t.turn_id) for t in turns},
                "uses": await self.turns.uses(bonfire),
                "catalogue": await self.turns.catalogue(bonfire, PLATFORM, GROUP),
                "source": await self.turns.source(bonfire, PLATFORM, GROUP, "agent"),
                "kept": await self.turns.kept_groups(bonfire, "agent"),
                "sessions": sessions,
                "messages": {s.id: await self.sessions.messages_of(bonfire, s.id) for s in sessions},
                "built": await self.sessions.last_built_at(bonfire),
                "staged": self.mk.staged.get(bonfire),
                "drafts": {k: v for k, v in self.mk.drafts.items() if k[0] == bonfire},
                "installed": self.mk.catalogues.get(bonfire),
            }
        )

    def settle(self) -> None:
        """Let any task a request started run: a warmer started for a bonfire plans at once if it is founded."""
        self.run(asyncio.sleep, 0.05)


def _hosted() -> Settings:
    return Settings(
        bonfire_id=None,  # explicit: a KT_BONFIRE_ID in the shell would make this a local stack
        auth_url=AUTH_URL,
        auth_token=SecretStr(SEAM_TOKEN),
        handoff_url=HANDOFF_URL,
        handoff_token=SecretStr(SEAM_TOKEN),
        handoff_every_s=3600,  # no hand-off pass runs mid-test
    )


async def _empty(client: AsyncMongoClient[Doc], name: str) -> None:
    for collection in await client[name].list_collection_names():
        await client[name][collection].delete_many({})


@pytest.fixture(params=["memory", pytest.param("mongo", marks=pytest.mark.skipif(not MONGO_URI, reason=NO_MONGO))])
def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, shared_database: str) -> Iterator[World]:
    seam = Seam(dict(WORKERS))
    monkeypatch.setattr(
        "kernel_turns.app.KeyCheck", lambda url, token: KeyCheck(url, token, client=_over(seam.check_key))
    )
    monkeypatch.setattr(
        "kernel_turns.app.HandoffClient", lambda url, token: HandoffClient(url, token, client=_over(seam.worker))
    )
    mk = Kernel(SCRIPTS)
    # A has spent; only C is founded, so A's and B's warmers wait, and a warmer started for C would plan at once
    spend: dict[str, JsonValue] = {A: {"charged_usd": SPENT, "ceiling_usd": "20"}}
    mk.healthz = {"status": "ok", "version": "2.0.1", "bonfires": {C: {}}, "spend": spend}
    mongo: AsyncMongoClient[Doc] | None = None
    turns: TurnStore
    sessions: SessionStore
    if request.param == "mongo":
        assert MONGO_URI
        mongo = AsyncMongoClient(MONGO_URI, tz_aware=True)
        turns, sessions = MongoTurnStore(mongo, shared_database), MongoSessionStore(mongo, shared_database)
    else:
        turns, sessions = MemoryTurnStore(), MemorySessionStore()
    app = create_app(_hosted(), mk=mk, store=turns, sessions=sessions)
    with TestClient(app) as client:
        w = World(app, client, mk, turns, sessions, seam)
        try:
            if isinstance(turns, MongoTurnStore) and isinstance(sessions, MongoSessionStore):
                w.run(turns.ensure_indexes)  # an injected store's indexes are the caller's to make
                w.run(sessions.ensure_indexes)
            yield w
        finally:
            if mongo is not None:
                w.run(_empty, mongo, shared_database)
                w.run(mongo.close)


@pytest.fixture
def app(world: World) -> FastAPI:
    return world.app


async def _broken_turn(turns: TurnStore) -> str:
    """A's turn in repair after a ``tool_error``, as a phrase step that failed leaves it."""
    turn_id = keys.turn_id(A, PLATFORM, GROUP, GROUP, "m-broken")
    at = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    turn = TurnRecord(
        turn_id=turn_id,
        platform=PLATFORM,
        group=GROUP,
        chat=GROUP,
        message_id="m-broken",
        speaker=SPEAKER,
        text="make onboard sam",
        at=at,
        participants=[SPEAKER],
        bonfire_id=A,
        plan={"plan_id": "p-gone"},
        status="repair",
        licence=["make"],
        pending=None,
        phrase=None,
    )
    await turns.save_turn(turn)
    row = LedgerRow(
        bonfire_id=A,
        turn_id=turn_id,
        step_id="p-gone:0:0",
        tool="make",
        args=dict(MAKE),
        verdict="tool_error",
        detail="the tracker timed out",
        at=at,
    )
    await turns.add_ledger(row)
    return turn_id


def _seed(w: World) -> Ids:
    c = w.client

    def ok(answer: httpx.Response, status: int = 200) -> httpx.Response:
        assert answer.status_code == status, answer.text
        return answer

    def begin(key: str, message_id: str, text: str) -> str:
        body = {"platform": PLATFORM, "group": GROUP, "chat": GROUP, "speaker": SPEAKER, "message_id": message_id}
        body |= {"text": text, "at": AT, "participants": [SPEAKER]}
        return ok(c.post("/v1/turns", json=body, headers=_auth(key))).json()["turn_id"]

    def teach(key: str, turn: str, form: str) -> str:
        body = {"turn_id": turn, "platform": PLATFORM, "group": GROUP, "chat": GROUP, "teacher": SPEAKER}
        body |= {"forms": [form], "body": ["take {X}"]}
        return ok(c.post("/v1/teaching", json=body, headers=_auth(key))).json()["draft_id"]

    def confirm(key: str, draft: str, message_id: str) -> None:
        body = {"platform": PLATFORM, "group": GROUP, "by": SPEAKER, "turn_id": begin(key, message_id, "yes")}
        ok(c.post(f"/v1/teaching/{draft}/confirm", json=body, headers=_auth(key)))

    def say(key: str, conversation: str, message_id: str, text: str) -> None:
        message = {"conversation_id": conversation, "message_id": message_id, "speaker": SPEAKER, "text": text}
        added = ok(c.post("/v1/messages", json={"messages": [{**message, "timestamp": AT}]}, headers=_auth(key)))
        assert added.json() == {"accepted": 1, "duplicates": 0}

    def close(key: str, conversation: str) -> None:
        closed = ok(c.post("/v1/sessions/close", json={"conversation_id": conversation}, headers=_auth(key)))
        assert closed.json() == {"closed": True}

    # A: a turn with a committed call and a pending one; a live phrase and a staged draft; a use; three sessions
    ok(c.put(f"{TEAM}/catalogue/agent", json=CATALOGUE, headers=_auth(KEY_A)), 204)
    turn = begin(KEY_A, "m-make", "make onboard sam")
    called = ok(c.post(f"/v1/turns/{turn}/calls", json={"tool": "make", "args": MAKE}, headers=_auth(KEY_A)))
    report = {"step_id": called.json()["step"]["step_id"], "verdict": "committed", "value": {"id": VALUE}}
    ok(c.post(f"/v1/turns/{turn}/report", json=report, headers=_auth(KEY_A)))
    pending = {"tool": "make", "args": {"title": "onboard kim"}}
    step = ok(c.post(f"/v1/turns/{turn}/calls", json=pending, headers=_auth(KEY_A))).json()["step"]["step_id"]
    editor = begin(KEY_A, "m-teach", "teach fetch")
    confirm(KEY_A, teach(KEY_A, editor, FORM), "m-yes")
    draft = teach(KEY_A, editor, "brew {X}")
    begin(KEY_A, "m-use", "fetch water")
    broken = w.run(_broken_turn, w.turns)
    say(KEY_A, "conv-open", "m-open", SECRET)
    for chat in ("conv-closed", "conv-failed"):
        say(KEY_A, chat, f"m-{chat}", f"a message in {chat}")
        close(KEY_A, chat)
    failed = next(s for s in w.run(w.sessions.waiting, A) if s.conversation_id == "conv-failed")
    w.run(w.sessions.mark_refused, A, failed.id, "the worker refused it", datetime.now(UTC))

    # B: a catalogue, and a live phrase under the same id as A's first one (``brew.d1.0``), unused
    ok(c.put(f"{TEAM}/catalogue/agent", json=CATALOGUE, headers=_auth(KEY_B)), 204)
    confirm(KEY_B, teach(KEY_B, begin(KEY_B, "m-b-teach", "teach fetch"), "pour {X}"), "m-b-yes")

    seeded = w.state(A)
    assert {turn, broken, editor} <= {t.turn_id for t in seeded["turns"]}
    assert [r.verdict for r in seeded["ledger"][turn]] == ["committed"]
    assert [u.phrase for u in seeded["uses"]] == ["brew.d1.0"]
    assert sorted(s.state for s in seeded["sessions"]) == ["closed", "failed", "open"]
    assert [r["status"] for r in seeded["staged"]] == ["live", "staged"]
    assert seeded["source"] == CATALOGUE
    w.mk.seen.clear()
    w.mk.calls.clear()
    w.seam.keys_asked.clear()
    w.seam.status_asked.clear()
    return Ids(
        bonfire=A,
        platform=PLATFORM,
        group=GROUP,
        chat=GROUP,
        speaker=SPEAKER,
        turn=turn,
        turn_message="m-make",
        text="make onboard sam",
        step=step,
        broken_turn=broken,
        editor_turn=editor,
        draft=draft,
        form=FORM,
        session=failed.id,
        conversation="conv-open",
        message="m-open",
        private=(A, turn, broken, editor, failed.id, VALUE, FORM, SECRET, SPENT),
    )


@pytest.fixture
def seeded(world: World) -> tuple[World, Ids]:
    return world, _seed(world)


# -- every route, and nothing else -----------------------------------------------------------------------------------


def _is_v1(path: str | None) -> bool:
    return path is not None and (path == "/v1" or path.startswith("/v1/"))


def v1_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) under /v1. FastAPI 0.162 keeps an ``_IncludedRouter`` in ``app.routes``: walking it for
    ``APIRoute`` finds nothing and passes, so this walks ``iter_route_contexts``, and an empty walk fails."""
    found = {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        if context.path is not None and _is_v1(context.path)
        for method in context.methods or {"(no method)"}  # a route without methods still has to match a probe
    }
    assert found, "the walk found no /v1 route: it is walking the wrong thing"
    return found


def _calls(dependant: Dependant) -> list[object]:
    """Every callable a route depends on, at any depth."""
    found: list[object] = []
    for sub in dependant.dependencies:
        found += [sub.call, *_calls(sub)]
    return found


def test_every_route_has_a_probe(app: FastAPI) -> None:
    assert v1_routes(app) == set(PROBES)


def test_nothing_open_but_healthz(app: FastAPI) -> None:
    assert {c.path for c in iter_route_contexts(app.routes) if not _is_v1(c.path)} == {"/healthz"}


def test_every_route_takes_the_caller(app: FastAPI) -> None:
    contexts = [c for c in iter_route_contexts(app.routes) if _is_v1(c.path)]
    assert len(contexts) == len(PROBES)
    for context in contexts:
        dependant = getattr(context, "dependant", None)  # the route as it runs, the router's dependencies included
        assert isinstance(dependant, Dependant), context.path
        assert app.state.caller in _calls(dependant), context.path


# -- the other customer, no key, a bonfire not served ----------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES, ids=_route_id)
def test_the_other_customer_gets_nothing(route: tuple[str, str], seeded: tuple[World, Ids]) -> None:
    w, ids = seeded
    probe, before = PROBES[route], w.state(A)
    probe.check(probe.call(w.client, ids, KEY_B), ids)
    w.settle()
    assert w.state(A) == before
    assert {bonfire for _, bonfire in w.mk.seen} <= {B}, w.mk.seen
    assert bool(w.mk.seen) is probe.kernel, w.mk.seen  # a route that asks the kernel asked it of B
    assert set(w.seam.status_asked) <= {B} and w.seam.sent == []


@pytest.mark.parametrize("route", ROUTES, ids=_route_id)
def test_the_owner_s_same_call_reaches_its_rows(route: tuple[str, str], seeded: tuple[World, Ids]) -> None:
    w, ids = seeded
    probe = PROBES[route]
    probe.owner(probe.call(w.client, ids, KEY_A), ids)


@pytest.mark.parametrize("route", ROUTES, ids=_route_id)
def test_no_key_is_401(route: tuple[str, str], seeded: tuple[World, Ids]) -> None:
    w, ids = seeded
    before = w.state(A), w.state(B)
    for key in (None, UNKNOWN_KEY):
        answer = PROBES[route].call(w.client, ids, key)
        assert answer.status_code == 401, (key, answer.text)
        assert answer.headers["WWW-Authenticate"] == "Bearer"
    w.settle()
    assert (w.state(A), w.state(B)) == before
    assert w.mk.seen == [] and w.seam.status_asked == []
    assert w.seam.keys_asked == [UNKNOWN_KEY]  # no bearer, no question asked


@pytest.mark.parametrize("kind", sorted(WRONG_KINDS))
@pytest.mark.parametrize("route", ROUTES, ids=_route_id)
def test_a_wrong_kind_of_bonfire_is_403_and_the_kernel_sees_nothing(
    route: tuple[str, str], kind: str, seeded: tuple[World, Ids]
) -> None:
    w, ids = seeded
    w.seam.workers[C] = WRONG_KINDS[kind]
    before = w.state(A), w.state(B), w.state(C)
    answer = PROBES[route].call(w.client, ids, KEY_C)
    assert (answer.status_code, answer.json()) == (403, NOT_SERVED)
    w.settle()  # C is founded at the kernel: a warmer started for it would have planned by now
    assert w.mk.seen == [], w.mk.seen
    assert w.seam.status_asked == [C] and w.seam.sent == []
    assert (w.state(A), w.state(B), w.state(C)) == before
