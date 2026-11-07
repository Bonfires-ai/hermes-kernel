"""What a turn affords: the pure rules, and a message turn that issues nothing and carries the speaker's open frames."""

from __future__ import annotations

from kernel_turns.affordances import afford, open_frames, render_form
from kernel_turns.groups import SingleBonfire
from kernel_turns.models import ChildCall, Frame, JsonValue
from kernel_turns.store import MemoryTurnStore
from kernel_turns.turns import TurnEngine
from tests.fakes import CATALOGUE, FakeMk, Script, cli_turn, frame, later, team_turn

READ_ONLY = ["look", "read", "search"]
NO_TOOLS = Script(steps=[], partial=True)


def _frames(*rows: dict[str, JsonValue]) -> list[Frame]:
    return [Frame.model_validate(r) for r in rows]


async def _engine(mk: FakeMk, groups: SingleBonfire) -> tuple[TurnEngine, MemoryTurnStore]:
    store = MemoryTurnStore()
    await store.save_catalogue("telegram", "-100777", CATALOGUE)
    await store.save_catalogue("cli", "session-1", CATALOGUE)
    return TurnEngine(mk, store, groups), store


def test_partial_frame_affords_its_tool() -> None:
    frames = _frames(frame("make", title=None))
    assert afford(frames, CATALOGUE, "talk_only", False) == ["make", *READ_ONLY]


def test_open_group_affords_every_verb() -> None:
    frames = _frames(frame("take"))
    assert afford(frames, CATALOGUE, "open", False) == ["take", "make", "archive", *READ_ONLY]
    assert afford([], None, "talk_only", False) == []  # no catalogue, no frames: nothing


def test_editor_and_confirm_affordances() -> None:
    editor = _frames(frame("teach", "editor"))
    assert afford(editor, CATALOGUE, "talk_only", False) == [*READ_ONLY, "teach_phrase"]
    yes = _frames(frame("yes", "reply", complete=True))
    assert afford(yes, CATALOGUE, "talk_only", True) == [*READ_ONLY, "confirm_phrase"]
    assert afford(yes, CATALOGUE, "talk_only", False) == READ_ONLY  # no staged draft of the speaker's: no confirm


async def test_confirm_needs_the_speakers_own_staged_draft(groups: SingleBonfire) -> None:
    mk = FakeMk({"yes": Script(steps=[], partial=True, frames=[frame("yes", "reply", complete=True)])})
    engine, _ = await _engine(mk, groups)
    staged = {"id": "fetch.d2.0", "kind": "taught", "status": "staged", "provenance": {"teacher": "Kira Dace"}}
    mk.rows = [staged]
    kira = await engine.begin(team_turn("yes", message_id="1"))
    assert "confirm_phrase" in kira.affordances
    milo = await engine.begin(team_turn("yes", message_id="2", speaker="Milo"))  # yes from someone else
    assert "confirm_phrase" not in milo.affordances
    mk.rows = [{**staged, "status": "live"}]
    later_yes = await engine.begin(team_turn("yes", message_id="3", at=later(1)))
    assert "confirm_phrase" not in later_yes.affordances


def test_render_form_fills_slots_from_args_then_defaults() -> None:
    forms = ["triage {X} for {R}", "triage {X}"]
    assert render_form(forms, {"X": "JUJ-3"}, {"R": "me"}) == ("triage JUJ-3 for me", "")
    assert render_form(forms, {"x": "JUJ-3", "R": "Ana"}, {}) == ("triage JUJ-3 for Ana", "")  # slot names ignore case
    assert render_form(["file {X}"], {}, {}) == (None, "X has no value and no default")
    assert render_form(forms, {"X": "JUJ-3"}, {}) == ("triage JUJ-3", "")  # the second form needs no R


async def test_context_is_the_speakers_uncommitted_frames(groups: SingleBonfire) -> None:
    both = Script(steps=[], partial=True, frames=[frame("take", ticket=None), frame("look", place="inbox")])
    mk = FakeMk({"take the new one then look at the inbox": both, "JUJ-3": NO_TOOLS})
    engine, _ = await _engine(mk, groups)
    first = await engine.begin(team_turn("take the new one then look at the inbox", message_id="1"))
    assert mk.calls_of("plan")[-1]["context"] == []  # nothing before it
    looked = await engine.call(first.turn_id, "look", {"place": "inbox"})
    assert looked.step is not None
    await engine.report(first.turn_id, looked.step.step_id, "committed", ["JUJ-1"])

    # the same speaker in another chat of the group: the take left open in the first chat is never context there
    await engine.begin(team_turn("JUJ-3", message_id="9", at=later(1)).model_copy(update={"chat": "-100888"}))
    assert mk.calls_of("plan")[-1]["context"] == []

    await engine.begin(team_turn("JUJ-3", message_id="2", at=later(1)))
    context = mk.calls_of("plan")[-1]["context"]
    assert [f["name"] for f in context] == ["take"]  # look committed: no composition for it
    assert context[0]["roles"] == {"ticket": None}

    await engine.begin(team_turn("JUJ-3", message_id="3", speaker="Milo", at=later(2)))
    assert mk.calls_of("plan")[-1]["context"] == []  # another speaker's turn is never context
    await engine.begin(team_turn("JUJ-3", message_id="4", at=later(3)))
    assert mk.calls_of("plan")[-1]["context"] == []  # Kira's previous turn (JUJ-3) had no frames of its own


def test_a_phrase_called_is_not_open() -> None:
    frames = _frames(frame("triage", "phrase"), frame("take"), frame("teach", "editor"))
    called = [ChildCall(tool="triage", args={}, turn_id="c1")]
    assert [f.name for f in open_frames(frames, [], [])] == ["triage", "take"]  # editor frames are never context
    assert [f.name for f in open_frames(frames, [], called)] == ["take"]  # the phrase was called: nothing waits on it


async def test_a_group_with_no_catalogue_still_has_frames(groups: SingleBonfire) -> None:
    mk = FakeMk({"make onboard sam": Script(steps=[], partial=True, frames=[frame("make", title=None)])})
    engine = TurnEngine(mk, MemoryTurnStore(), groups)
    view = await engine.begin(team_turn("make onboard sam"))
    assert view.affordances == ["make"]  # the fake kernel has no tools installed either
    cli = await engine.begin(cli_turn("hello"))
    assert cli.affordances == [] and cli.status == "unread"


async def test_a_failed_tool_call_leaves_its_frame_open(groups: SingleBonfire) -> None:
    mk = FakeMk(
        {"take the new one": Script(steps=[], partial=True, frames=[frame("take", ticket=None)]), "JUJ-3": NO_TOOLS}
    )
    engine, _ = await _engine(mk, groups)
    first = await engine.begin(team_turn("take the new one", message_id="1"))
    called = await engine.call(first.turn_id, "take", {"ticket": "the new one"})
    assert called.step is not None  # in flight: still open
    await engine.report(first.turn_id, called.step.step_id, "precondition_failed", None, "no such ticket")
    await engine.begin(team_turn("JUJ-3", message_id="2", at=later(1)))
    assert [f["name"] for f in mk.calls_of("plan")[-1]["context"]] == ["take"]  # asked "which ticket?": JUJ-3 answers


def test_context_dedupes_across_analyses_and_is_capped() -> None:
    many = [frame(f"t{i}", ticket=None) for i in range(10)]
    twice = _frames(frame("take", ticket=None), {**frame("take", ticket=None), "analysis": 1}, *many)
    names = [f.name for f in open_frames(twice, [], [])]
    assert names == ["take", *[f"t{i}" for i in range(7)]] and len(names) == 8


class _RepairingMk(FakeMk):
    """A kernel that cannot resolve "it" (its turn window never saw the model's direct call): status ``repair``."""

    async def plan(self, bonfire: str, body: dict[str, JsonValue]) -> dict[str, JsonValue]:  # type: ignore[override]
        plan = await super().plan(bonfire, body)
        return {
            **plan,
            "status": "repair",
            "diagnostics": ["window (op 3) refers to the turn 0 back, but the chat's turn window holds 0"],
            "frames": [frame("put", complete=True, ticket="it", place="Doing")],
        }


async def test_a_kernel_repair_on_a_message_turn_is_not_a_failure(groups: SingleBonfire) -> None:
    mk = _RepairingMk({"put it on Doing": Script(steps=[], partial=True)})
    engine, _ = await _engine(mk, groups)
    view = await engine.begin(team_turn("put it on Doing"))
    assert view.status == "unread" and view.repair is None
    assert [f.name for f in view.frames] == ["put"] and "put" in view.affordances
    assert "put(" in view.context  # the model is given the frame to act on
