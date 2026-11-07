"""Teaching (a plain read-back, the teacher's confirm), the verb book (constructions joined with uses) and catalogues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kernel_turns.groups import SingleBonfire
from kernel_turns.mk_client import NotTeacher, Refused
from kernel_turns.models import JsonValue, UseRecord
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import ConfirmRequest, TeachDraft, Teaching
from tests.fakes import FakeMk, afforded_turn

AT = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
CATALOGUE: dict[str, JsonValue] = {
    "schema_version": "kernel-catalogue/v1",
    "tools": [{"name": n, "x-side-effect": n != "look"} for n in ("look", "take", "make", "give")],
}


def _draft(**changes: object) -> TeachDraft:
    fields: dict[str, object] = {
        "platform": "telegram",
        "group": "-100777",
        "chat": "-100777",
        "teacher": "Kira Dace",
        "forms": ["brew {X} for {R}", "brew {X}"],
        "body": ["take water", "take herb", "make {X} from them", "give {X} to {R}"],
        "defaults": {"R": "me"},
    }
    return TeachDraft.model_validate({**fields, **changes})


async def test_teaching_reads_back_and_confirms_by_teacher_only(groups: SingleBonfire) -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase", "confirm_phrase")
    teaching = Teaching(mk, store, groups)
    draft = await teaching.begin(_draft(turn_id=turn))
    assert draft.read_back == (
        "brew {X} for {R} / brew {X}: take water → take herb → make {X} from them → give {X} to {R}"
        " (if left out, R is me)"
    )
    assert draft.draft_id == "d1" and draft.problems == []
    assert mk.calls_of("teach")[-1] == {
        "forms": ["brew {X} for {R}", "brew {X}"],
        "body": ["take water", "take herb", "make {X} from them", "give {X} to {R}"],
        "defaults": {"R": "me"},
        "teacher": "Kira Dace",
        "chat_id": "-100777",
        "replace": False,
    }

    milo = await afforded_turn(store, "confirm_phrase", speaker="Milo", turn_id="t-milo")

    def confirm(by: str) -> ConfirmRequest:
        return ConfirmRequest(platform="telegram", group="-100777", by=by, turn_id=milo if by == "Milo" else turn)

    with pytest.raises(NotTeacher):
        await teaching.confirm("d1", confirm("Milo"))
    confirmed = await teaching.confirm("d1", confirm("Kira Dace"))
    assert (confirmed.draft_id, confirmed.phrases) == ("d1", ["brew.d1.0", "brew.d1.1"])

    problems = ["form 'brew' has no literal with a letter", "clause 'fly to the moon' does not read: no construction"]
    mk.teach_answer = {"draft_id": "", "specs": [], "problems": list(problems)}
    refused = await teaching.begin(_draft(turn_id=turn, forms=["brew"], body=["fly to the moon"], defaults={}))
    assert (refused.draft_id, refused.problems) == ("", problems)  # the kernel's words, verbatim
    assert refused.read_back == "brew: fly to the moon"


async def test_teach_needs_the_editor_affordance(groups: SingleBonfire) -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store, groups)
    plain = await afforded_turn(store, "look", turn_id="t-plain")
    with pytest.raises(Refused, match="teach_phrase is not afforded"):
        await teaching.begin(_draft(turn_id=plain))
    assert mk.calls_of("teach") == []  # nothing reached the kernel
    confirm = ConfirmRequest(platform="telegram", group="-100777", by="Kira Dace", turn_id=plain)
    with pytest.raises(Refused, match="confirm_phrase is not afforded"):
        await teaching.confirm("d1", confirm)
    assert mk.calls_of("confirm") == []
    kira = await afforded_turn(store, "teach_phrase", "confirm_phrase", turn_id="t-kira")
    with pytest.raises(Refused, match="not afforded"):  # a turn of another speaker or chat gates no draft of theirs
        await teaching.begin(_draft(turn_id=kira, teacher="Milo"))
    with pytest.raises(Refused, match="not afforded"):
        await teaching.begin(_draft(turn_id=kira, chat="-100888"))
    elsewhere = await afforded_turn(store, "teach_phrase", turn_id="t-other")
    other = await store.get_turn(elsewhere)
    assert other is not None
    await store.save_turn(other.model_copy(update={"group": "-100999"}))  # another group's turn does not count
    with pytest.raises(Refused):
        await teaching.begin(_draft(turn_id=elsewhere))


def _taught(row_id: str, form_text: str, form: list[JsonValue], draft: str = "d1") -> dict[str, JsonValue]:
    """A taught row as ``GET …/constructions`` returns it (``CxnSpec.to_json``; slots lower-cased in ``form``)."""
    return {
        "id": row_id,
        "kind": "taught",
        "unit": "root",
        "fills": None,
        "form": form,
        "meaning": [
            ["take", "?w", "water"],
            ["take", "?h", "herb"],
            ["make", "?made", "?x", "?them"],
            ["give", "?gift", "?made", "?r"],
            ["answer", "?gift"],
        ],
        "answer_shape": None,
        "standing": {"confirmed": 1, "uses_ok": 2, "uses_failed": 1, "uses_world": 0},
        "status": "live",
        "provenance": {
            "teacher": "Kira Dace",
            "chat_id": "-100777",
            "draft_id": draft,
            "form": form_text,
            "request": {"forms": [form_text], "body": [], "defaults": {}, "replace": False},
            "confirmed_by": "Kira Dace",
        },
    }


BREW_FOR = _taught(
    "brew.d1.0", "brew {X} for {R}", ["brew", {"slot": "x", "fills": "np"}, "for", {"slot": "r", "fills": "np"}]
)
BASE: dict[str, JsonValue] = {
    "id": "base.look",
    "kind": "base",
    "unit": "root",
    "fills": None,
    "form": ["look"],
    "meaning": [["look", "?seen"], ["answer", "?seen"]],
    "answer_shape": None,
    "standing": {"confirmed": 0, "uses_ok": 9, "uses_failed": 0, "uses_world": 0},
    "status": "live",
    "provenance": {},
}


def _use(phrase: str, minutes: int, speaker: str) -> UseRecord:
    return UseRecord(
        bonfire_id="65f0c0ffee0000000000abcd",
        phrase=phrase,
        speaker=speaker,
        platform="telegram",
        chat="-100777",
        at=AT + timedelta(minutes=minutes),
        outcome="ok",
    )


async def test_verb_book_joins_constructions_and_uses(groups: SingleBonfire) -> None:
    mk = FakeMk()
    mk.rows = [BASE, BREW_FOR]
    store = MemoryTurnStore()
    await store.save_catalogue("telegram", "-100777", CATALOGUE)
    for use in (_use("brew.d1.0", 5, "Milo"), _use("brew.d1.0", 30, "Ana"), _use("other.d9.0", 40, "Ana")):
        await store.record_use(use)
    await store.record_use(_use("brew.d1.0", 1, "Kira Dace"))
    verbs = await Teaching(mk, store, groups).verbs("telegram", "-100777")
    assert len(verbs) == 1
    verb = verbs[0]
    assert (verb.id, verb.forms, verb.teacher, verb.chat, verb.status) == (
        "brew.d1.0",
        ["brew {X} for {R}"],
        "Kira Dace",
        "-100777",
        "live",
    )
    assert verb.slots == ["X", "R"]  # as the teacher wrote them
    assert verb.steps == ["take", "take", "make", "give"]  # the tools of the meaning, in order; "answer" is no tool
    assert verb.standing == {"confirmed": 1, "uses_ok": 2, "uses_failed": 1, "uses_world": 0}
    assert [(u.speaker, u.at) for u in verb.uses] == [
        ("Ana", AT + timedelta(minutes=30)),
        ("Milo", AT + timedelta(minutes=5)),
        ("Kira Dace", AT + timedelta(minutes=1)),
    ]


async def test_a_draft_taught_with_two_forms_is_one_verb(groups: SingleBonfire) -> None:
    mk = FakeMk()
    brew = _taught("brew.d1.1", "brew {X}", ["brew", {"slot": "x", "fills": "np"}])
    mk.rows = [BREW_FOR, brew]
    store = MemoryTurnStore()
    for use in (_use("brew.d1.0", 5, "Milo"), _use("brew.d1.1", 9, "Ana")):
        await store.record_use(use)
    mk.catalogues["65f0c0ffee0000000000abcd"] = {"sha256": "ab", "tools": CATALOGUE["tools"]}  # none stored here
    verbs = await Teaching(mk, store, groups).verbs("telegram", "-100777")
    assert [(v.id, v.forms, v.slots) for v in verbs] == [("brew.d1.0", ["brew {X} for {R}", "brew {X}"], ["X", "R"])]
    assert verbs[0].steps == ["take", "take", "make", "give"]  # from the kernel's installed catalogue
    assert [u.speaker for u in verbs[0].uses] == ["Ana", "Milo"]
    assert verbs[0].standing == {"confirmed": 1, "uses_ok": 4, "uses_failed": 2, "uses_world": 0}


async def test_competing_readings_of_a_form_reach_the_caller(groups: SingleBonfire) -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase")
    mk.teach_answer = {
        "draft_id": "d1",
        "specs": [],
        "problems": [],
        "competes": {
            "look {X}": [
                {
                    "name": "look",
                    "kind": "tool",
                    "construction": "look.1",
                    "roles": {"place": "inbox"},
                    "complete": True,
                }
            ]
        },
    }
    draft = await Teaching(mk, store, groups).begin(_draft(turn_id=turn))
    assert list(draft.competes) == ["look {X}"]
    assert draft.competes["look {X}"][0].name == "look" and draft.problems == []


async def test_teach_accepts_structured_steps(groups: SingleBonfire) -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase")
    steps: list[str | dict[str, JsonValue]] = [
        {"call": "web_search", "args": {"query": "{X}"}},
        {"call": "think", "args": {"instruction": "summarise it in three lines", "input": "it"}},
        "write it",
    ]
    view = await Teaching(mk, store, groups).begin(_draft(turn_id=turn, forms=["sitrep {X}"], body=steps, defaults={}))
    assert view.draft_id == "d1"
    assert mk.calls_of("teach")[-1]["body"] == steps  # unchanged: the kernel compiles them
    assert (
        view.read_back
        == "sitrep {X}: web_search(query={X}) → think(instruction=summarise it in three lines, input=it) → write it"
    )
