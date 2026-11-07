"""Teaching (a plain read-back, the teacher's confirm), the verb book (constructions joined with uses) and catalogues."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from kernel_turns.mk_client import KernelBusy, NotTeacher, Refused
from kernel_turns.models import JsonValue, UseRecord
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import ConfirmRequest, TeachDraft, Teaching
from kernel_turns.turns import UnknownTurn
from tests.conftest import BONFIRE
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


async def test_teaching_reads_back_and_confirms_by_teacher_only() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase", "confirm_phrase")
    teaching = Teaching(mk, store)
    draft = await teaching.begin(BONFIRE, _draft(turn_id=turn))
    assert draft.read_back == (
        "brew {X} for {R} / brew {X}: take water → take herb → make {X} from them → give {X} to {R}"
        ' (if left out, R is me). Answer with just "yes" to keep it.'
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
        await teaching.confirm(BONFIRE, "d1", confirm("Milo"))
    confirmed = await teaching.confirm(BONFIRE, "d1", confirm("Kira Dace"))
    assert (confirmed.draft_id, confirmed.phrases) == ("d1", ["brew.d1.0", "brew.d1.1"])

    problems = ["form 'brew' has no literal with a letter", "clause 'fly to the moon' does not read: no construction"]
    mk.teach_answer = {"draft_id": "", "specs": [], "problems": list(problems)}
    refused = await teaching.begin(BONFIRE, _draft(turn_id=turn, forms=["brew"], body=["fly to the moon"], defaults={}))
    assert (refused.draft_id, refused.problems) == ("", problems)  # the kernel's words, verbatim
    assert refused.read_back == "brew: fly to the moon"


async def test_teach_needs_the_editor_affordance() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    plain = await afforded_turn(store, "look", turn_id="t-plain")
    with pytest.raises(Refused, match="teach_phrase is not afforded"):
        await teaching.begin(BONFIRE, _draft(turn_id=plain))
    assert mk.calls_of("teach") == []  # nothing reached the kernel
    confirm = ConfirmRequest(platform="telegram", group="-100777", by="Kira Dace", turn_id=plain)
    mk.rows = [{"id": "brew.d1.0", "status": "staged", "provenance": {"teacher": "Milo", "draft_id": "d1"}}]
    with pytest.raises(Refused, match="confirm_phrase is not afforded"):
        await teaching.confirm(BONFIRE, "d1", confirm)
    assert mk.calls_of("confirm") == []
    kira = await afforded_turn(store, "teach_phrase", "confirm_phrase", turn_id="t-kira")
    with pytest.raises(Refused, match="not afforded"):  # a turn of another speaker or chat gates no draft of theirs
        await teaching.begin(BONFIRE, _draft(turn_id=kira, teacher="Milo"))
    with pytest.raises(Refused, match="not afforded"):
        await teaching.begin(BONFIRE, _draft(turn_id=kira, chat="-100888"))
    elsewhere = await afforded_turn(store, "teach_phrase", turn_id="t-other")
    other = await store.get_turn(BONFIRE, elsewhere)
    assert other is not None
    await store.save_turn(other.model_copy(update={"group": "-100999"}))  # another group's turn does not count
    with pytest.raises(Refused):
        await teaching.begin(BONFIRE, _draft(turn_id=elsewhere))


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


async def test_verb_book_joins_constructions_and_uses() -> None:
    mk = FakeMk()
    mk.rows = [BASE, BREW_FOR]
    store = MemoryTurnStore()
    await store.save_catalogue(BONFIRE, "telegram", "-100777", CATALOGUE)
    for use in (_use("brew.d1.0", 5, "Milo"), _use("brew.d1.0", 30, "Ana"), _use("other.d9.0", 40, "Ana")):
        await store.record_use(use)
    await store.record_use(_use("brew.d1.0", 1, "Kira Dace"))
    verbs = await Teaching(mk, store).verbs(BONFIRE, "telegram", "-100777")
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


async def test_a_draft_taught_with_two_forms_is_one_verb() -> None:
    mk = FakeMk()
    brew = _taught("brew.d1.1", "brew {X}", ["brew", {"slot": "x", "fills": "np"}])
    mk.rows = [BREW_FOR, brew]
    store = MemoryTurnStore()
    for use in (_use("brew.d1.0", 5, "Milo"), _use("brew.d1.1", 9, "Ana")):
        await store.record_use(use)
    mk.catalogues["65f0c0ffee0000000000abcd"] = {"sha256": "ab", "tools": CATALOGUE["tools"]}  # none stored here
    verbs = await Teaching(mk, store).verbs(BONFIRE, "telegram", "-100777")
    assert [(v.id, v.forms, v.slots) for v in verbs] == [("brew.d1.0", ["brew {X} for {R}", "brew {X}"], ["X", "R"])]
    assert verbs[0].steps == ["take", "take", "make", "give"]  # from the kernel's installed catalogue
    assert [u.speaker for u in verbs[0].uses] == ["Ana", "Milo"]
    assert verbs[0].standing == {"confirmed": 1, "uses_ok": 4, "uses_failed": 2, "uses_world": 0}


async def test_competing_readings_of_a_form_reach_the_caller() -> None:
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
    draft = await Teaching(mk, store).begin(BONFIRE, _draft(turn_id=turn))
    assert list(draft.competes) == ["look {X}"]
    assert draft.competes["look {X}"][0].name == "look" and draft.problems == []


async def test_teach_accepts_structured_steps() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase")
    steps: list[str | dict[str, JsonValue]] = [
        {"call": "web_search", "args": {"query": "{X}"}},
        {"call": "think", "args": {"instruction": "summarise it in three lines", "input": "it"}},
        "write it",
    ]
    view = await Teaching(mk, store).begin(BONFIRE, _draft(turn_id=turn, forms=["sitrep {X}"], body=steps, defaults={}))
    assert view.draft_id == "d1"
    assert mk.calls_of("teach")[-1]["body"] == steps  # unchanged: the kernel compiles them
    assert (
        view.read_back
        == 'sitrep {X}: web_search(query={X}) → think(instruction=summarise it in three lines, input=it) → write it. Answer with just "yes" to keep it.'
    )


async def test_a_confirm_refused_while_a_draft_waits_tells_the_person_to_answer_with_just_yes() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    longer = await afforded_turn(store, "web_search", turn_id="t-longer")  # "yes, keep it": no yes frame, no confirm
    mk.rows = [{"id": "brew.d1.0", "status": "staged", "provenance": {"teacher": "Kira Dace", "draft_id": "d1"}}]
    request = ConfirmRequest(platform="telegram", group="-100777", by="Kira Dace", turn_id=longer)
    with pytest.raises(Refused, match='just "yes"') as waiting:
        await teaching.confirm(BONFIRE, "d1", request)
    assert "not afforded" not in str(waiting.value) and "not available" not in str(waiting.value)
    assert mk.calls_of("confirm") == []
    # another speaker's draft is not this speaker's: the plain refusal stays
    mk.rows = [{"id": "brew.d1.0", "status": "staged", "provenance": {"teacher": "Milo", "draft_id": "d1"}}]
    with pytest.raises(Refused, match="confirm_phrase is not afforded"):
        await teaching.confirm(BONFIRE, "d1", request)
    # and with no draft at all there is nothing to refuse: the bonfire does not hold it
    mk.rows = []
    with pytest.raises(UnknownTurn, match="no staged draft 'd1'"):
        await teaching.confirm(BONFIRE, "d1", request)


def _kira_staged(mk: FakeMk) -> None:
    mk.rows = [{"id": "brew.d1.0", "status": "staged", "provenance": {"teacher": "Kira Dace", "draft_id": "d1"}}]


def _card(turn_id: str, **changes: object) -> ConfirmRequest:
    fields: dict[str, object] = {
        "platform": "telegram",
        "group": "-100777",
        "by": "Kira Dace",
        "turn_id": turn_id,
        "card": True,
    }
    return ConfirmRequest.model_validate({**fields, **changes})


async def test_the_plain_read_back_has_no_plain_yes_and_a_refused_draft_has_none() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    turn = await afforded_turn(store, "teach_phrase")
    teaching = Teaching(mk, store)
    draft = await teaching.begin(BONFIRE, _draft(turn_id=turn))
    assert draft.plain == (
        "brew {X} for {R} / brew {X}: take water → take herb → make {X} from them → give {X} to {R}"
        " (if left out, R is me)"
    )
    assert draft.read_back == f'{draft.plain}. Answer with just "yes" to keep it.'  # as it always was
    mk.teach_answer = {"draft_id": "", "specs": [], "problems": ["nope"]}
    assert (await teaching.begin(BONFIRE, _draft(turn_id=turn))).plain == ""


async def test_a_card_confirms_with_no_yes_frame() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    await teaching.begin(BONFIRE, _draft(turn_id=await afforded_turn(store, "teach_phrase", turn_id="t-draft")))
    _kira_staged(mk)
    plain = await afforded_turn(store, "web_search", turn_id="t-card")  # no yes frame: confirm_phrase is not afforded
    confirmed = await teaching.confirm(BONFIRE, "d1", _card(plain))
    assert (confirmed.draft_id, confirmed.phrases) == ("d1", ["brew.d1.0", "brew.d1.1"])
    assert mk.calls_of("confirm") == [{"draft_id": "d1", "by": "Kira Dace"}]


async def test_a_card_needs_a_draft_of_this_person_waiting() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    turn = await afforded_turn(store, "web_search", turn_id="t-card")
    with pytest.raises(UnknownTurn):  # nothing staged at all
        await teaching.confirm(BONFIRE, "d1", _card(turn))
    mk.rows = [{"id": "brew.d1.0", "status": "staged", "provenance": {"teacher": "Milo", "draft_id": "d1"}}]
    with pytest.raises(Refused):  # staged by somebody else
        await teaching.confirm(BONFIRE, "d1", _card(turn))
    assert mk.calls_of("confirm") == []


async def test_a_card_from_another_speaker_or_group_is_refused_and_an_unknown_turn_is_unknown() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    _kira_staged(mk)
    milo = await afforded_turn(store, "web_search", speaker="Milo", turn_id="t-milo")
    with pytest.raises(Refused):  # Milo's turn cannot carry Kira's card
        await teaching.confirm(BONFIRE, "d1", _card(milo))
    elsewhere = await afforded_turn(store, "web_search", turn_id="t-other")
    other = await store.get_turn(BONFIRE, elsewhere)
    assert other is not None
    await store.save_turn(other.model_copy(update={"group": "-100999"}))
    with pytest.raises(Refused):  # a turn of another group
        await teaching.confirm(BONFIRE, "d1", _card(elsewhere))
    with pytest.raises(Refused):  # the request's own group is not the turn's
        await teaching.confirm(BONFIRE, "d1", _card(await afforded_turn(store, "look", turn_id="t-g"), group="-100555"))
    with pytest.raises(UnknownTurn):
        await teaching.confirm(BONFIRE, "d1", _card("t-nowhere"))
    assert mk.calls_of("confirm") == []


async def test_a_card_that_is_not_the_teachers_is_the_kernels_to_refuse() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    await teaching.begin(BONFIRE, _draft(turn_id=await afforded_turn(store, "teach_phrase", turn_id="t-draft")))
    # Milo has a draft of his own waiting, and the turn is his: only the kernel knows d1 is Kira's
    mk.rows = [{"id": "stew.d2.0", "status": "staged", "provenance": {"teacher": "Milo", "draft_id": "d2"}}]
    milo = await afforded_turn(store, "web_search", speaker="Milo", turn_id="t-milo")
    with pytest.raises(NotTeacher):
        await teaching.confirm(BONFIRE, "d1", _card(milo, by="Milo"))


async def test_without_card_no_yes_frame_is_still_refused() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    _kira_staged(mk)
    plain = await afforded_turn(store, "web_search", turn_id="t-longer")
    with pytest.raises(Refused, match='just "yes"'):
        await teaching.confirm(BONFIRE, "d1", _card(plain, card=False))
    with pytest.raises(Refused, match='just "yes"'):  # and absent means false
        await teaching.confirm(
            BONFIRE, "d1", ConfirmRequest(platform="telegram", group="-100777", by="Kira Dace", turn_id=plain)
        )
    assert mk.calls_of("confirm") == []


def _confirm(turn: str, *, card: bool = False) -> ConfirmRequest:
    return ConfirmRequest(platform="telegram", group="-100777", by="Kira Dace", turn_id=turn, card=card)


@pytest.mark.parametrize("card", [False, True])
async def test_confirm_of_a_draft_this_bonfire_does_not_hold_is_unknown(card: bool) -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    theirs = await afforded_turn(store, "teach_phrase", bonfire_id="a", turn_id="t-a")
    mine = await afforded_turn(store, "confirm_phrase", bonfire_id="b", turn_id="t-b")
    held = await teaching.begin("a", _draft(turn_id=theirs))
    with pytest.raises(UnknownTurn, match=f"no staged draft '{held.draft_id}'"):
        await teaching.confirm("b", held.draft_id, _confirm(mine, card=card))
    assert mk.calls_of("confirm") == []


async def test_a_draft_held_goes_to_the_kernel_even_once_kept() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    turn = await afforded_turn(store, "teach_phrase", "confirm_phrase")
    held = await teaching.begin(BONFIRE, _draft(turn_id=turn))
    await teaching.confirm(BONFIRE, held.draft_id, _confirm(turn))
    await teaching.confirm(BONFIRE, held.draft_id, _confirm(turn))
    assert len(mk.calls_of("confirm")) == 2  # what the kernel says of a second confirm stays the kernel's to say


async def test_when_the_constructions_cannot_be_read_the_kernel_answers_the_confirm() -> None:
    mk = FakeMk()
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    turn = await afforded_turn(store, "confirm_phrase")
    mk.unborn = True  # the kernel has no such bonfire yet: both routes answer 404
    with pytest.raises(Refused, match="first conversation"):
        await teaching.confirm(BONFIRE, "d1", _confirm(turn))
    mk.unborn, mk.busy = False, True
    with pytest.raises(KernelBusy):
        await teaching.confirm(BONFIRE, "d1", _confirm(turn))
    assert len(mk.calls_of("confirm")) == 2


AGENT_A: dict[str, JsonValue] = {
    "schema_version": "kernel-catalogue/v1",
    "tools": [{"name": "a_tool", "x-side-effect": False}],
}
AGENT_B: dict[str, JsonValue] = {
    "schema_version": "kernel-catalogue/v1",
    "tools": [{"name": "b_tool", "x-side-effect": False}],
}


async def test_a_kept_catalogue_installs_only_into_its_own_bonfire() -> None:
    mk = FakeMk()
    mk.unborn = True
    store = MemoryTurnStore()
    teaching = Teaching(mk, store)
    await teaching.put_catalogue("a", "hermes", "main", AGENT_A)  # the same group, in two bonfires: kept, not installed
    await teaching.put_catalogue("b", "hermes", "main", AGENT_B)
    assert mk.catalogues == {}
    mk.unborn = False
    assert await teaching.install_kept("a") is True
    assert list(mk.catalogues) == ["a"]
    assert "a_tool" in str(mk.catalogues["a"]) and "b_tool" not in str(mk.catalogues["a"])
    assert await store.catalogue("b", "hermes", "main") is None
    assert await teaching.install_kept("b") is True
    assert "b_tool" in str(mk.catalogues["b"]) and "a_tool" not in str(mk.catalogues["b"])


async def test_an_install_in_one_bonfire_never_waits_on_the_same_group_in_another() -> None:
    class HeldInstall(FakeMk):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.held = asyncio.Event()

        async def put_catalogue(self, bonfire: str, catalogue: Mapping[str, JsonValue]) -> None:
            if bonfire == "a":
                self.held.set()
                await self.release.wait()
            await super().put_catalogue(bonfire, catalogue)

    mk = HeldInstall()
    teaching = Teaching(mk, MemoryTurnStore())
    first = asyncio.create_task(teaching.put_catalogue("a", "hermes", "main", AGENT_A))
    await mk.held.wait()
    await asyncio.wait_for(teaching.put_catalogue("b", "hermes", "main", AGENT_B), 1)
    assert list(mk.catalogues) == ["b"]
    mk.release.set()
    await first
