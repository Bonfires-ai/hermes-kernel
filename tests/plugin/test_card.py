"""A card to keep a phrase: only the real `clarify` result, seen in the hook, can keep one."""

import json
import logging
import threading
from typing import Any

import pytest

from plugin.card import CHOICES, CLOSED, CONFIRM_TIMEOUT_S, CardBook, card_lines, question
from plugin.client import Refused, Unavailable
from plugin.turns import LocalTurn

PLAIN = "research {X}: web_search(query={X}) → think(instruction=one line, input=it)"
ASKED = f"Keep this phrase? {PLAIN}"


class Service:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.confirmed: list[tuple[str, dict[str, Any], float]] = []

    def confirm(self, draft_id, body, *, timeout=30.0):
        self.confirmed.append((draft_id, body, timeout))
        if self.error:
            raise self.error
        return {"draft_id": draft_id, "phrases": ["research.d1.0", "research.d1.1"]}


def turn() -> LocalTurn:
    return LocalTurn("h1", "s1", "telegram", "g", "g", "Ana", turn_id="m1", known_platform="telegram", private=True)


def answer(choice: Any = "Keep it", *, asked=ASKED, offered: Any = CHOICES, status="answered") -> dict[str, Any]:
    return {"question": asked, "choices_offered": list(offered), "status": status, "user_response": choice}


def result(*responses: Any) -> str:
    return json.dumps({"responses": list(responses), "outcome": "submitted"}, ensure_ascii=False)


def book(service: Any = None) -> CardBook:
    cards = CardBook(service if service is not None else Service())
    cards.open("s1", "d1", turn(), ASKED)
    return cards


def hook(cards: CardBook, res: Any, **over: Any) -> Any:
    kw = {"tool_name": "clarify", "args": {}, "result": res, "session_id": "s1", "status": "ok"}
    kw.update(over)
    return cards.on_post_tool_call(**kw)


def test_the_card_asks_the_plain_read_back_and_keep_it_comes_first():
    assert question(PLAIN) == ASKED
    assert CHOICES == ("Keep it", "Change it", "Throw it away")


def test_the_instructions_give_the_exact_questions_value():
    lines = "\n".join(card_lines("d1", PLAIN, "Ana"))
    assert f"Draft d1: {PLAIN}" in lines
    given = json.loads(lines.split("as JSON: ", 1)[1].split("\n", 1)[0])
    assert given == [{"question": ASKED, "choices": list(CHOICES)}]
    for part in ("confirm_phrase", "Change it", "Throw it away", 'plain "yes"'):
        assert part in lines


def test_keep_it_confirms_as_the_speaker_with_card_on_a_timeout_under_hermess_own():
    service = Service()
    cards = book(service)
    assert hook(cards, result(answer())) is None
    assert service.confirmed == [
        (
            "d1",
            {"platform": "telegram", "group": "g", "turn_id": "m1", "by": "Ana", "card": True},
            CONFIRM_TIMEOUT_S,
        )
    ]
    assert CONFIRM_TIMEOUT_S < 30  # Hermes abandons a post_tool_call callback at 30 s, and skips it for 60 s after
    decision = cards.decision("d1")
    assert decision is not None and decision.kept and decision.phrases == ("research.d1.0", "research.d1.1")


def test_a_result_hermes_hands_as_a_dict_is_read_too():
    service = Service()
    hook(book(service), json.loads(result(answer())))
    assert len(service.confirmed) == 1


REFUSED = {
    "wrong question": lambda: result(answer(asked="Keep this phrase? something else")),
    "a question around the read-back": lambda: result(answer(asked=f"{ASKED} (keeps it)")),
    "reordered choices": lambda: result(answer(offered=("Change it", "Keep it", "Throw it away"))),
    "an extra choice": lambda: result(answer(offered=(*CHOICES, "Keep it forever"))),
    "a missing choice": lambda: result(answer(offered=CHOICES[:2])),
    "no choices": lambda: result({**answer(), "choices_offered": None}),
    "skipped": lambda: result(answer(status="skipped")),
    "unanswered": lambda: result(answer(status="unanswered")),
    "a status of yes": lambda: result(answer(status="yes")),
    "a list": lambda: result(answer(["Keep it"])),
    "null": lambda: result(answer(None)),
    "a number": lambda: result(answer(1)),
    "typed text": lambda: result(answer("keep it please")),
    "the label left on": lambda: result(answer("Keep it (Recommended)")),
    "not json": lambda: "{not json",
    "empty": lambda: "",
    "none": lambda: None,
    "a number result": lambda: 7,
    "json null": lambda: "null",
    "a json list": lambda: "[]",
    "responses not a list": lambda: json.dumps({"responses": {"question": ASKED}}),
    "a response not an object": lambda: json.dumps({"responses": ["Keep it"]}),
    "no responses": lambda: json.dumps({"responses": []}),
    "the undelivered reply": lambda: json.dumps({"responses": [], "outcome": "undelivered", "notice": "Keep it"}),
    "two answers to the card": lambda: result(answer("Throw it away"), answer("Keep it")),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_result_that_is_not_the_card_changes_nothing_and_the_card_stays_pending(name):
    service = Service()
    cards = book(service)
    hook(cards, REFUSED[name]())
    assert service.confirmed == []
    assert cards.decision("d1") is None
    hook(cards, result(answer()))  # still pending: the real answer is taken
    assert len(service.confirmed) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"tool_name": "web_search"},
        {"tool_name": "clarify_extra"},
        {"tool_name": None},
        {"session_id": "s2"},
        {"session_id": ""},
        {"session_id": None},
        {"status": "error"},
    ],
    ids=["another tool", "a like name", "no tool", "another session", "empty session", "no session", "a failed call"],
)
def test_a_call_that_is_not_this_sessions_clarify_changes_nothing(over):
    service = Service()
    cards = book(service)
    hook(cards, result(answer()), **over)
    assert service.confirmed == []
    assert cards.decision("d1") is None
    hook(cards, result(answer()))
    assert len(service.confirmed) == 1


def test_a_hostile_keep_it_for_a_question_that_is_not_the_cards_confirms_nothing():
    service = Service()
    cards = book(service)
    hook(cards, result(answer(asked="Is the sky blue?")))
    hook(cards, result(answer(asked="Is the sky blue?"), answer(status="unanswered")))
    assert service.confirmed == []
    assert cards.decision("d1") is None


def test_a_card_is_consumed_so_a_result_cannot_be_replayed():
    service = Service()
    cards = book(service)
    hook(cards, result(answer()))
    hook(cards, result(answer()))
    assert len(service.confirmed) == 1
    closing = Service()
    cards = book(closing)
    hook(cards, result(answer("Throw it away")))
    hook(cards, result(answer()))  # the same card answered again cannot turn a no into a keep
    assert closing.confirmed == []


def test_two_results_at_once_take_the_card_once():
    service = Service()
    cards = book(service)
    gate = threading.Barrier(8)

    def run():
        gate.wait()
        hook(cards, result(answer()))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(service.confirmed) == 1


@pytest.mark.parametrize("choice", ["Change it", "Throw it away"])
def test_change_it_and_throw_it_away_close_the_draft_and_call_nothing(choice):
    service = Service()
    cards = book(service)
    hook(cards, result(answer(choice)))
    assert service.confirmed == []
    decision = cards.decision("d1")
    assert decision is not None and decision.kind == CLOSED


def test_keep_it_with_the_service_down_is_chosen_and_the_resend_confirms():
    service = Service(Unavailable("away"))
    cards = book(service)
    hook(cards, result(answer()))
    decision = cards.decision("d1")
    assert decision is not None and decision.chosen and not decision.kept
    service.error = None
    kept = cards.resend(decision, timeout=35.0)
    assert kept["phrases"] == ["research.d1.0", "research.d1.1"]
    assert service.confirmed[-1][1]["card"] is True and service.confirmed[-1][2] == 35.0
    after = cards.decision("d1")
    assert after is not None and after.kept


def test_a_refused_keep_stays_chosen():
    service = Service(Refused(409, "no staged draft"))
    cards = book(service)
    hook(cards, result(answer()))
    decision = cards.decision("d1")
    assert decision is not None and decision.chosen


class Hostile:
    def __bool__(self):
        raise RuntimeError("no")

    def __eq__(self, other):
        raise RuntimeError("no")

    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "over",
    [
        {"result": Hostile()},
        {"tool_name": Hostile()},
        {"session_id": Hostile()},
        {"status": Hostile()},
        {"result": object()},
        {"result": b"\xff"},
    ],
)
def test_the_hook_never_raises_and_returns_nothing(over):
    assert hook(book(), result(answer()) if "result" not in over else over["result"], **over) is None


def test_the_hook_never_raises_when_the_service_raises_anything():
    cards = book(Service(RuntimeError("boom")))
    assert hook(cards, result(answer())) is None
    decision = cards.decision("d1")
    assert decision is not None and decision.chosen


def test_a_new_card_for_the_session_replaces_the_old_one():
    service = Service()
    cards = book(service)
    cards.open("s1", "d2", turn(), "Keep this phrase? another")
    hook(cards, result(answer()))  # the first card is gone
    assert service.confirmed == []
    hook(cards, result(answer(asked="Keep this phrase? another")))
    assert [c[0] for c in service.confirmed] == ["d2"]


def test_the_book_is_bounded():
    service = Service()
    cards = CardBook(service)
    for n in range(2000):
        cards.open(f"s{n}", f"d{n}", turn(), ASKED)
        hook(cards, result(answer("Throw it away")), session_id=f"s{n}")
    assert len(cards._pending) <= 256 and len(cards._decisions) <= 1024
    assert cards.decision("d1999") is not None and cards.decision("d0") is None


def test_the_log_holds_no_text_of_the_person_or_the_card(caplog):
    cards = book()
    with caplog.at_level(logging.DEBUG):
        hook(cards, result(answer("my secret words")))
        hook(cards, result(answer(asked="a question of the model's")))
        hook(cards, result(answer()))
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert said  # each result gives a reason
    for text in ("my secret words", "a question of the model's", "web_search", "Ana"):
        assert text not in said
