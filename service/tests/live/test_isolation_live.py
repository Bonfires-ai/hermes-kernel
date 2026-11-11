"""``test_isolation``'s probes against a live turn service: customer B gets nothing of customer A's.

Run by hand against a deployed service, with the keys of two bonfires, never on every push (it writes to both):

    KT_LIVE_URL=https://.../kernel KT_LIVE_KEY_A_FILE=path KT_LIVE_KEY_B_FILE=path pytest service/tests/live -q

Each key file holds its key on the first line. The key is read here and never shown: do not add ``-l``, which prints a
failing test's locals, and a request's headers can be among them.

Key A begins a turn and posts a message, on ids made fresh for this run. Then every ``PROBES`` entry runs with key B on
A's ids, in the dict's order, not ``sorted(PROBES)``: sorted puts ``messages`` before ``sessions/close``, and on one live
key B's own message opens B's session in that chat, so the close then answers ``{"closed": true}``. Around each probe,
what A reads of its own (its latest turns with their ledgers, and its messages in open sessions) must not change.

A live A has one turn, one chat and one message. The probes that name A's broken, editor, earlier or waiting turn get that
one turn. A has no draft, failed session or pending step here, so those probes are sent ids that never existed, and only
show that B is refused for them; ``test_isolation`` covers them on real rows.

What a run leaves: in A, one turn and one open session of one message; in B, a few turns, a catalogue on this run's group
and one open session of one message. Each session closes after the service's quiet time and is then built, which spends:
B's bonfire is founded by it if it never was, and A's memory gains the message. So A must already be founded, or this
stops before writing: A's message would found it, and a hosted e2e run on that key, which needs a bonfire never founded,
would then stop at its first stage. ``KT_LIVE_FOUND_A=yes`` lets it found A anyway.

The answers come through Bonfires' API, in front of the turn service. Where the in-process test refuses B (404, in the
words for an id that never existed), the live answer must be that refusal; the gateway may add its own fields, or carry
the text in ``message`` instead of ``detail``. If the gateway refuses on its own instead (a 401 or 403 the service never
gives B here, or other words), the probe asserts only that B was refused and that nothing of A's is in the body, and warns.
Everywhere else the in-process check holds as it is.
"""

from __future__ import annotations

import json
import os
import uuid
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.test_isolation import AT, PROBES, Ids, Probe

URL = os.environ.get("KT_LIVE_URL", "")
KEY_A_FILE, KEY_B_FILE = "KT_LIVE_KEY_A_FILE", "KT_LIVE_KEY_B_FILE"
LIVE = bool(URL and os.environ.get(KEY_A_FILE) and os.environ.get(KEY_B_FILE))
needs_live = pytest.mark.skipif(
    not LIVE, reason=f"KT_LIVE_URL, {KEY_A_FILE} and {KEY_B_FILE} are not all set: no live service to probe"
)

Route = tuple[str, str]
#: what a refusal of B can be, live
REFUSED = frozenset({401, 403, 404})
PLATFORM = "telegram"


class GatewayRefusal(UserWarning):
    """B was refused, but not in the turn service's own words: the in-process test pins those."""


class Key(str):
    """A key. Its repr never shows it, so neither does a failing assertion."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<a key, not shown>"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _name(route: Route) -> str:
    return " ".join(route)


def _read_key(variable: str) -> Key:
    path = Path(os.environ[variable])
    if not path.is_file():
        pytest.fail(f"{variable} names {path}, which is not a file")
    lines = path.read_text(encoding="utf-8").splitlines()
    key = lines[0].strip() if lines else ""
    if not key:
        pytest.fail(f"the first line of {path} ({variable}) is empty")
    return Key(key)


# -- what a live answer must be ------------------------------------------------------------------------------------


def refused_in_process(probe: Probe) -> bool:
    """Whether the in-process test refuses B this route: its check is one ``unknown`` built. The other routes answer B
    from B's own data, and a refusal there is a failure."""
    return getattr(probe.check, "__qualname__", "").startswith("unknown.")


def _nothing_of_theirs_beyond_what_was_sent(answer: httpx.Response, ids: Ids) -> None:
    """None of A's strings is in the answer, except one B itself sent: a refusal may echo the id it was asked for."""
    request = answer.request
    sent = str(request.url) + request.content.decode("utf-8", "replace")
    for held in ids.private:
        if held not in sent:
            assert held not in answer.text, f"the answer holds {held!r}, which only the other customer's data holds"


def _in_the_service_s_words(probe: Probe, answer: httpx.Response, ids: Ids) -> bool:
    """The refusal the in-process test expects, under the gateway's wrapping: its text in ``detail``, or in ``message``
    alone, beside fields of the gateway's own, which hold nothing of A's."""
    try:
        body = answer.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    words = body.get("detail") if isinstance(body.get("detail"), str) else body.get("message")
    if not isinstance(words, str):
        return False
    try:
        probe.check(httpx.Response(answer.status_code, json={"detail": words}, request=answer.request), ids)
    except (AssertionError, ValueError):
        return False
    _nothing_of_theirs_beyond_what_was_sent(answer, ids)
    return True


def check_live(route: Route, probe: Probe, answer: httpx.Response, ids: Ids) -> None:
    """``probe.check``, as the in-process test runs it, with the gateway's room on the routes where B is refused."""
    try:
        probe.check(answer, ids)
        return
    except (AssertionError, ValueError):
        if not refused_in_process(probe):
            raise
    if _in_the_service_s_words(probe, answer, ids):
        return
    # The gateway can refuse before the turn service sees the request (a 401 or 403 the service never gives B here), or
    # word a refusal its own way. Then the live assertion is: refused, and no data of A's in the body. The service's
    # own answer is pinned in-process.
    assert answer.status_code in REFUSED, (
        f"{_name(route)}: B was answered {answer.status_code}, not refused: {answer.text}"
    )
    _nothing_of_theirs_beyond_what_was_sent(answer, ids)
    shown = f"{_name(route)}: refused ({answer.status_code}), not in the service's words: {answer.text[:200]}"
    warnings.warn(GatewayRefusal(shown), stacklevel=2)


# -- the live run ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Live:
    client: httpx.Client
    ids: Ids
    key_a: Key
    key_b: Key


def a_state(live: Live) -> tuple[object, ...]:
    """What A reads of its own that a probe could touch: its latest turns with their ledgers, and how many of its
    messages are in open sessions. (Not ``GET /v1/turns/{id}``: a licensed turn is re-planned on read, so it can change
    with no one else involved.)"""
    client, ids, mine = live.client, live.ids, _auth(live.key_a)
    turns = client.get(f"/v1/groups/{ids.platform}/{ids.group}/turns", headers=mine)
    status = client.get("/v1/status", headers=mine)
    for answer in (turns, status):
        assert answer.status_code == 200, f"A's own {answer.request.url.path}: {answer.status_code} {answer.text}"
    return turns.json(), status.json()["open_messages"]


#: why a probe of an A with no memory stops before it sends anything
NOT_FOUNDED = "A's bonfire has no memory yet, and this probe's message would found it; a hosted e2e run on that key needs a bonfire never founded. Run the e2e first, or set KT_LIVE_FOUND_A=yes. Nothing was sent."


def _field(answer: httpx.Response, name: str) -> object:
    body: object = answer.json()
    assert isinstance(body, dict) and name in body, f"no {name!r} in {answer.request.url.path}'s answer: {answer.text}"
    return body[name]  # pyright: ignore[reportUnknownVariableType]


def _text(answer: httpx.Response, name: str) -> str:
    value = _field(answer, name)
    assert isinstance(value, str) and value, (
        f"{name!r} in {answer.request.url.path}'s answer is not an id: {answer.text}"
    )
    return value


def seed(client: httpx.Client, key_a: Key, key_b: Key, *, found_a: bool = False) -> Live:
    """Key A's turn and message, on ids fresh for this run, after the checks that make a refusal of B mean something."""
    mine, theirs = client.get("/v1/status", headers=_auth(key_a)), client.get("/v1/status", headers=_auth(key_b))
    assert mine.status_code == 200, f"key A: GET /v1/status answered {mine.status_code}: {mine.text}"
    # A key the service does not serve is refused everywhere: its refusals would prove nothing.
    assert theirs.status_code == 200, f"key B: GET /v1/status answered {theirs.status_code}: {theirs.text}"
    bonfire = _text(mine, "bonfire_id")
    assert _text(theirs, "bonfire_id") != bonfire, "both keys are of one bonfire: the probe needs two customers"
    if _field(mine, "founded") is not True and not found_a:
        pytest.fail(NOT_FOUNDED)
    run = uuid.uuid4().hex[:12]
    group, speaker = f"kt-live-{run}", f"kt-live-speaker-{run}"
    turn_message, text = f"m-{run}-turn", f"kt-live {run}: a turn only customer A began"
    conversation, message = f"kt-live-conv-{run}", f"m-{run}-said"
    said = f"kt-live {run}: a message only customer A sent"
    body: dict[str, object] = {"platform": PLATFORM, "group": group, "chat": group, "speaker": speaker}
    body |= {"message_id": turn_message, "text": text, "at": AT, "participants": [speaker]}
    begun = client.post("/v1/turns", json=body, headers=_auth(key_a))
    assert begun.status_code == 200, f"key A: POST /v1/turns answered {begun.status_code}: {begun.text}"
    turn = _text(begun, "turn_id")
    sent = {"conversation_id": conversation, "message_id": message, "speaker": speaker, "text": said, "timestamp": AT}
    posted = client.post("/v1/messages", json={"messages": [sent]}, headers=_auth(key_a))
    assert posted.status_code == 200 and posted.json() == {"accepted": 1, "duplicates": 0}, posted.text
    ids = Ids(
        bonfire=bonfire,
        platform=PLATFORM,
        group=group,
        chat=group,
        speaker=speaker,
        turn=turn,
        turn_message=turn_message,
        text=text,
        step=f"{run}-plan:0:0",  # never existed
        broken_turn=turn,
        editor_turn=turn,
        earlier_turn=turn,
        waiting_turn=turn,
        draft=f"{run}-draft",  # never existed
        form="kt-live {X}",
        session=uuid.uuid4().hex,  # never existed
        conversation=conversation,
        message=message,
        private=(bonfire, turn, text, said),
    )
    # The positive control: A reads its own turn, so a 404 for B is about the key, not the route or the gateway.
    own = client.get(f"/v1/turns/{turn}", headers=_auth(key_a))
    assert own.status_code == 200, f"key A cannot read its own turn: {own.status_code} {own.text}"
    return Live(client, ids, key_a, key_b)


def probe_live(live: Live, route: Route) -> None:
    probe, before = PROBES[route], a_state(live)
    check_live(route, probe, probe.call(live.client, live.ids, live.key_b), live.ids)
    assert a_state(live) == before, f"{_name(route)} with B's key changed what A reads of its own"


@pytest.fixture(scope="module")
def live() -> Iterator[Live]:
    key_a, key_b = _read_key(KEY_A_FILE), _read_key(KEY_B_FILE)
    with httpx.Client(base_url=URL, timeout=httpx.Timeout(60.0)) as client:
        yield seed(client, key_a, key_b, found_a=os.environ.get("KT_LIVE_FOUND_A") == "yes")


@pytest.mark.live
@needs_live
@pytest.mark.parametrize("route", list(PROBES), ids=_name)  # run in the order given, the dict's: see the docstring
def test_the_other_customer_gets_nothing_live(route: Route, live: Live) -> None:
    probe_live(live, route)


# -- the live check itself, offline ----------------------------------------------------------------------------------

SAMPLE = Ids(
    bonfire="bonfire-a",
    platform=PLATFORM,
    group="g",
    chat="g",
    speaker="s",
    turn="turn-a",
    turn_message="m",
    text="a's text",
    step="p:0:0",
    broken_turn="turn-a",
    editor_turn="turn-a",
    earlier_turn="turn-a",
    waiting_turn="turn-a",
    draft="draft-a",
    form="f {X}",
    session="session-a",
    conversation="c",
    message="m2",
    private=("bonfire-a", "turn-a", "a's text"),
)
GET_TURN: Route = ("GET", "/v1/turns/{turn_id}")
STATUS: Route = ("GET", "/v1/status")
REFUSAL = f"no turn {SAMPLE.turn!r}"


def _answer(status: int, body: object, path: str = f"/v1/turns/{SAMPLE.turn}") -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), request=httpx.Request("GET", f"https://api.test{path}"))


def test_the_base_url_keeps_its_path() -> None:
    client = httpx.Client(base_url="https://api.test/kernel")
    assert client.build_request("GET", "/v1/status").url == "https://api.test/kernel/v1/status"


def test_the_refused_routes_are_the_in_process_404s() -> None:
    assert refused_in_process(PROBES[GET_TURN]) and not refused_in_process(PROBES[STATUS])


@pytest.mark.parametrize(
    "body",
    [{"detail": REFUSAL}, {"message": REFUSAL}, {"detail": REFUSAL, "message": "Not Found", "error": "not_found"}],
    ids=["the service's own", "in message", "beside the gateway's fields"],
)
def test_the_service_s_refusal_passes_quietly(body: object) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        check_live(GET_TURN, PROBES[GET_TURN], _answer(404, body), SAMPLE)


@pytest.mark.parametrize("status", sorted(REFUSED))
def test_the_gateway_s_own_refusal_passes_with_a_warning(status: int) -> None:
    with pytest.warns(GatewayRefusal):
        check_live(GET_TURN, PROBES[GET_TURN], _answer(status, {"message": "Forbidden"}), SAMPLE)


def test_a_refusal_holding_their_data_fails() -> None:
    leaky = {"message": "Forbidden", "context": {"bonfire": SAMPLE.bonfire}}
    with pytest.raises(AssertionError, match=SAMPLE.bonfire):
        check_live(GET_TURN, PROBES[GET_TURN], _answer(403, leaky), SAMPLE)
    with pytest.raises(AssertionError, match=SAMPLE.bonfire):
        check_live(GET_TURN, PROBES[GET_TURN], _answer(404, {"detail": REFUSAL, "owner": SAMPLE.bonfire}), SAMPLE)


def test_a_refusal_where_b_is_answered_in_process_fails() -> None:
    with pytest.raises(AssertionError):
        check_live(STATUS, PROBES[STATUS], _answer(403, {"message": "Forbidden"}, "/v1/status"), SAMPLE)


def test_an_answer_where_b_is_refused_fails() -> None:
    with pytest.raises(AssertionError):
        check_live(GET_TURN, PROBES[GET_TURN], _answer(200, {"turn_id": "turn-b"}), SAMPLE)
