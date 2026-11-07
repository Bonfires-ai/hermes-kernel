"""The model's calls: admitted when afforded, deduped, run once; a phrase runs as a child turn on the kernel's loop."""

from __future__ import annotations

import pytest

from kernel_turns import keys
from kernel_turns.groups import SingleBonfire
from kernel_turns.mk_client import Refused
from kernel_turns.models import JsonValue
from kernel_turns.store import MemoryTurnStore
from kernel_turns.turns import TurnEngine
from tests.fakes import CATALOGUE, FakeMk, Script, frame, team_turn

MAKE = {"title": "onboard josh"}
PARTIAL_MAKE = Script(steps=[], partial=True, frames=[frame("make", title=None)])
TRIAGE_ROW: dict[str, JsonValue] = {
    "id": "triage.d2.0",
    "kind": "taught",
    "status": "live",
    "meaning": [["take", "?t", "?x"]],
    "provenance": {
        "teacher": "Kira Dace",
        "draft_id": "d2",
        "form": "triage {X} for {R}",
        "request": {"forms": ["triage {X} for {R}"], "body": [], "defaults": {"R": "me"}, "replace": False},
    },
}
FILE_ROW: dict[str, JsonValue] = {
    **TRIAGE_ROW,
    "id": "file.d3.0",
    "provenance": {"teacher": "Kira Dace", "draft_id": "d3", "form": "file {X}", "request": {"defaults": {}}},
}


async def _engine(mk: FakeMk, groups: SingleBonfire) -> tuple[TurnEngine, MemoryTurnStore]:
    store = MemoryTurnStore()
    await store.save_catalogue("telegram", "-100777", CATALOGUE)
    return TurnEngine(mk, store, groups), store


async def test_call_admits_an_afforded_verb(groups: SingleBonfire) -> None:
    mk = FakeMk({"make onboard josh": PARTIAL_MAKE})
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("make onboard josh"))
    called = await engine.call(view.turn_id, "make", MAKE)
    step = called.step
    assert called.kind == "step" and step is not None
    assert step.direct and step.tool == "make" and step.args == MAKE
    assert step.step_id == f"{view.turn_id}:call:0"
    assert step.effect_key == keys.effect_key(view.turn_id, "make", MAKE, 0)
    assert not step.needs_confirmation  # make's x-consent is speaker_turn
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.calls == [step]


async def test_a_confirm_consent_verb_waits_for_the_board(groups: SingleBonfire) -> None:
    mk = FakeMk({"archive JUJ-3": Script(steps=[], partial=True, frames=[frame("archive", ticket="JUJ-3")])})
    engine, _ = await _engine(mk, groups)
    view = await engine.begin(team_turn("archive JUJ-3"))
    called = await engine.call(view.turn_id, "archive", {"ticket": "JUJ-3"})
    assert called.step is not None and called.step.needs_confirmation


async def test_repeat_call_returns_the_first_result(groups: SingleBonfire) -> None:
    mk = FakeMk({"make onboard josh": PARTIAL_MAKE})
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("make onboard josh"))
    first = await engine.call(view.turn_id, "make", MAKE)
    assert first.step is not None
    again = await engine.call(view.turn_id, "make", {"title": "onboard josh"})
    assert again.kind == "step" and again.step == first.step  # not committed yet: the same step, the same key
    ticket = {"id": "JUJ-9", "title": "onboard josh"}
    await engine.report(view.turn_id, first.step.step_id, "committed", ticket)
    done = await engine.call(view.turn_id, "make", MAKE)
    assert done.kind == "done" and done.value == ticket

    # the message is delivered again after its calls committed: the same turn, the same first result
    redelivered = await engine.begin(team_turn("make onboard josh"))
    assert redelivered.turn_id == view.turn_id
    done = await engine.call(redelivered.turn_id, "make", MAKE)
    assert done.kind == "done" and done.value == ticket
    assert [(r.tool, r.verdict) for r in await store.ledger(view.turn_id)] == [
        ("make", "committed")
    ]  # nothing ran twice
    other = await engine.call(view.turn_id, "make", {"title": "another"})  # different args: a second effect
    assert other.kind == "step" and other.step is not None and other.step.step_id.endswith(":call:1")


async def test_a_declined_call_is_final_and_a_tool_error_may_run_again(groups: SingleBonfire) -> None:
    mk = FakeMk({"make onboard josh": PARTIAL_MAKE})
    engine, _ = await _engine(mk, groups)
    view = await engine.begin(team_turn("make onboard josh"))
    first = await engine.call(view.turn_id, "make", MAKE)
    assert first.step is not None
    await engine.report(view.turn_id, first.step.step_id, "tool_error", None, "the tap is down")
    again = await engine.call(view.turn_id, "make", MAKE)
    assert again.kind == "step" and again.step == first.step  # the same key: it runs once
    await engine.report(view.turn_id, first.step.step_id, "declined", None, "not today")
    with pytest.raises(Refused, match="already ended"):
        await engine.call(view.turn_id, "make", MAKE)
    with pytest.raises(Refused, match="already reported declined"):
        await engine.report(view.turn_id, first.step.step_id, "committed", {})


async def test_direct_report_writes_the_ledger_only(groups: SingleBonfire) -> None:
    mk = FakeMk({"make onboard josh": PARTIAL_MAKE})
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("make onboard josh"))
    called = await engine.call(view.turn_id, "make", MAKE)
    assert called.step is not None
    after = await engine.report(view.turn_id, called.step.step_id, "committed", {"id": "JUJ-9"})
    assert mk.calls_of("resume") == [] and mk.calls_of("retry") == []  # the kernel never hears of it
    [row] = await store.ledger(view.turn_id)
    assert (row.step_id, row.tool, row.args, row.value) == (called.step.step_id, "make", MAKE, {"id": "JUJ-9"})
    assert after.status == "unread" and after.pending is None
    await engine.report(view.turn_id, called.step.step_id, "committed", {"id": "JUJ-9"})  # the same report again
    assert len(await store.ledger(view.turn_id)) == 1


async def test_phrase_call_begins_a_child_turn(groups: SingleBonfire) -> None:
    triage = {"ticket": "JUJ-3"}
    mk = FakeMk(
        {
            "triage JUJ-3": Script(
                steps=[], partial=True, frames=[frame("triage.d2.0", "phrase", construction="frame.triage.d2.0.triage")]
            ),
            "triage JUJ-3 for me": Script(steps=[("take", triage)], phrase="triage.d2.0"),
        }
    )
    mk.rows = [TRIAGE_ROW, FILE_ROW]
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("triage JUJ-3"))
    assert view.affordances[0] == "triage.d2.0"
    called = await engine.call(view.turn_id, "triage.d2.0", {"X": "JUJ-3"})
    assert called.kind == "turn" and called.turn_id is not None
    child = await store.get_turn(called.turn_id)
    assert child is not None and child.text == "triage JUJ-3 for me" and child.parent == view.turn_id
    assert child.message_id == "42:call:0" and child.status == "licensed" and child.phrase == "triage.d2.0"
    assert called.step is not None and called.step == child.pending and called.step.tool == "take"
    assert not called.step.direct  # the child runs the kernel's steps: its report resumes the kernel
    assert mk.calls_of("plan")[-1]["committed"] == []

    done = await engine.report(called.turn_id, called.step.step_id, "committed", {"id": "JUJ-3"})
    assert done.status == "done" and done.parent == view.turn_id
    again = await engine.call(view.turn_id, "triage.d2.0", {"X": "JUJ-3"})  # the same phrase call: the same child
    assert again.kind == "turn" and again.turn_id == called.turn_id and again.step is None
    assert len(mk.calls_of("plan")) == 2  # the message and the child, planned once each


async def test_a_phrase_call_is_its_rendered_sentence_however_the_args_are_spelled(groups: SingleBonfire) -> None:
    """A phrase's effect is the sentence it renders, so args that render the same sentence are the
    same call (slot names ignore case; a default given explicitly is the default): one child, one effect key."""
    mk = FakeMk(
        {
            "triage JUJ-3": Script(
                steps=[], partial=True, frames=[frame("triage.d2.0", "phrase", construction="frame.triage.d2.0.triage")]
            ),
            "triage JUJ-3 for me": Script(steps=[("take", {"ticket": "JUJ-3"})], phrase="triage.d2.0"),
        }
    )
    mk.rows = [TRIAGE_ROW, FILE_ROW]
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("triage JUJ-3"))
    first = await engine.call(view.turn_id, "triage.d2.0", {"X": "JUJ-3"})
    assert first.kind == "turn" and first.step is not None
    for spelled in ({"x": "JUJ-3"}, {"X": "JUJ-3", "R": "me"}, {"x": "JUJ-3", "r": "me"}):
        again = await engine.call(view.turn_id, "triage.d2.0", spelled)
        assert again.kind == "turn" and again.turn_id == first.turn_id, spelled
        assert again.step is not None and again.step.effect_key == first.step.effect_key
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and len(stored.children) == 1
    assert len(mk.calls_of("plan")) == 2  # the message and its one child, nothing planned again


async def test_a_phrase_slot_with_no_value_is_refused(groups: SingleBonfire) -> None:
    mk = FakeMk(
        {
            "file it": Script(
                steps=[], partial=True, frames=[frame("file.d3.0", "phrase", construction="frame.file.d3.0.file")]
            )
        }
    )
    mk.rows = [FILE_ROW]
    engine, _ = await _engine(mk, groups)
    view = await engine.begin(team_turn("file it"))
    with pytest.raises(Refused, match="X has no value and no default"):
        await engine.call(view.turn_id, "file.d3.0", {})
    with pytest.raises(Refused, match="file.d3.0 takes X"):
        await engine.call(view.turn_id, "file.d3.0", {"Y": "z"})
    assert len(mk.calls_of("plan")) == 1  # no child turn was planned


async def test_a_phrase_the_kernel_does_not_read_is_refused_and_leaves_no_call(groups: SingleBonfire) -> None:
    mk = FakeMk(
        {
            "file it": Script(
                steps=[], partial=True, frames=[frame("file.d3.0", "phrase", construction="frame.file.d3.0.file")]
            )
        }
    )
    mk.rows = [FILE_ROW]
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("file it"))
    with pytest.raises(Refused, match="did not read as 'file JUJ-3'"):
        await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-3"})
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.children == []
    mk.scripts["file JUJ-3"] = Script(steps=[("take", {"ticket": "JUJ-3"})], phrase="file.d3.0")
    ok = await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-3"})  # the same child turn, planned again
    assert ok.kind == "turn" and ok.step is not None


async def test_an_unread_phrase_call_does_not_poison_the_next_call(groups: SingleBonfire) -> None:
    mk = FakeMk({"file it": Script(steps=[], partial=True, frames=[frame("file.d3.0", "phrase")])})
    mk.rows = [FILE_ROW]
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("file it"))
    with pytest.raises(Refused, match="did not read"):
        await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-3"})  # the child is saved unread, never admitted
    mk.scripts["file JUJ-5"] = Script(steps=[("take", {"ticket": "JUJ-5"})], phrase="file.d3.0")
    ok = await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-5"})
    assert ok.step is not None and ok.step.args == {"ticket": "JUJ-5"} and ok.turn_id is not None
    child = await store.get_turn(ok.turn_id)
    assert child is not None and child.text == "file JUJ-5" and child.message_id.endswith(":call:1")
    again = await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-5"})
    assert again.turn_id == ok.turn_id


async def test_every_form_of_a_phrase_can_be_called_and_a_staged_draft_cannot(groups: SingleBonfire) -> None:
    forms = ["brew {X} for {R}", "brew {X}"]

    def row(k: int, draft: str, status: str) -> dict[str, JsonValue]:
        request: dict[str, JsonValue] = {"forms": list(forms), "body": [], "defaults": {}, "replace": False}
        provenance: dict[str, JsonValue] = {
            "teacher": "Kira Dace",
            "draft_id": draft,
            "form": forms[k],
            "request": request,
        }
        return {**TRIAGE_ROW, "id": f"brew.{draft}.{k}", "status": status, "provenance": provenance}

    rows = [row(0, "d1", "live"), row(1, "d1", "live")]
    staged = row(0, "d9", "staged")
    mk = FakeMk(
        {
            "brew tea": Script(
                steps=[], partial=True, frames=[frame("brew.d1.1", "phrase", construction="frame.brew.d1.1.brew")]
            ),
            "brew tea for me": Script(steps=[("take", {"ticket": "t"})], phrase="brew.d1.0"),
        }
    )
    mk.rows = [*rows, staged]
    engine, _ = await _engine(mk, groups)
    view = await engine.begin(team_turn("brew tea"))
    called = await engine.call(view.turn_id, "brew.d1.1", {"X": "tea", "R": "me"})
    assert called.kind == "turn" and called.step is not None  # the second form's frame names the same draft
    mk.scripts["brew tea"] = Script(steps=[], partial=True, frames=[frame("brew.d9.0", "phrase")])
    other = await engine.begin(team_turn("brew tea", message_id="43"))
    with pytest.raises(Refused, match="not a phrase this group has taught"):
        await engine.call(other.turn_id, "brew.d9.0", {"X": "tea", "R": "me"})


async def test_a_phrase_the_kernel_read_as_another_is_not_called(groups: SingleBonfire) -> None:
    mk = FakeMk(
        {
            "file it": Script(steps=[], partial=True, frames=[frame("file.d3.0", "phrase")]),
            "file JUJ-3": Script(steps=[("take", {"ticket": "JUJ-3"})], phrase="broad.d7.0"),
        }
    )
    mk.rows = [FILE_ROW]
    engine, store = await _engine(mk, groups)
    view = await engine.begin(team_turn("file it"))
    with pytest.raises(Refused, match="read it as another phrase"):
        await engine.call(view.turn_id, "file.d3.0", {"X": "JUJ-3"})
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.children == []
