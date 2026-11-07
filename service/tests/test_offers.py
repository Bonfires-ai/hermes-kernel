"""The agent's offer to teach a phrase: recorded on its turn as an open editor frame, it composes with the same
speaker's next "yes" (the editor is afforded, the offer is in the view) and is closed by a "no"."""

from __future__ import annotations

import pytest

from kernel_turns.affordances import afford, offer_frame, offer_reply
from kernel_turns.groups import SingleBonfire
from kernel_turns.mk_client import Refused
from kernel_turns.models import Frame, JsonValue
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import TeachDraft, Teaching
from kernel_turns.turns import TurnEngine, TurnView
from tests.fakes import CATALOGUE, FakeMk, Script, frame, later, team_turn

READ_ONLY = ["look", "read", "search"]
ROUTINE = "every morning I take the new ticket and give it to Milo"
FORMS = ["triage {X} for {R}", "triage {X}"]
BODY = ["take {X} from the inbox", "give it to {R}"]
DEFAULTS = {"R": "me"}
STAGED: dict[str, JsonValue] = {
    "id": "fetch.d2.0",
    "kind": "taught",
    "status": "staged",
    "provenance": {"teacher": "Kira Dace"},
}


def _reply(name: str) -> Script:
    return Script(steps=[], partial=True, frames=[frame(name, "reply", complete=True)])


async def _engine(groups: SingleBonfire) -> tuple[TurnEngine, MemoryTurnStore, FakeMk]:
    mk = FakeMk(
        {
            ROUTINE: Script(steps=[], partial=True, frames=[frame("take", ticket=None)]),
            "yes": _reply("yes"),
            "no": _reply("no"),
        }
    )
    store = MemoryTurnStore()
    await store.save_catalogue("telegram", "-100777", CATALOGUE)
    return TurnEngine(mk, store, groups), store, mk


async def _offered(engine: TurnEngine) -> TurnView:
    """Kira describes a routine; the agent offers to teach it on that turn."""
    first = await engine.begin(team_turn(ROUTINE, message_id="1"))
    return await engine.offer(first.turn_id, FORMS, BODY, DEFAULTS)


async def test_offer_is_recorded_as_an_open_editor_frame(groups: SingleBonfire) -> None:
    engine, store, _ = await _engine(groups)
    view = await _offered(engine)
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.offer is not None
    assert (stored.offer.forms, stored.offer.body, stored.offer.defaults) == (FORMS, BODY, DEFAULTS)
    assert stored.offer.to == "Kira Dace" and stored.offer.drafted is None
    shape = {"name", "kind", "construction", "roles", "complete"}
    offers = [f.model_dump(include=shape) for f in view.frames if f.construction == "offer"]
    assert offers == [
        {"name": "teach_phrase", "kind": "editor", "construction": "offer", "roles": {}, "complete": False}
    ]
    assert [f.name for f in view.frames] == ["take", "teach_phrase"]  # the kernel's reading stays
    assert "teach_phrase" not in view.affordances  # the offer waits for the answer: nothing to draft on its own turn

    # a second offer replaces the first; the message delivered again is re-planned and keeps the offer
    again = await engine.offer(view.turn_id, ["triage {X}"], ["take {X} from the inbox"], {})
    assert [f.name for f in again.frames].count("teach_phrase") == 1
    replanned = await engine.begin(team_turn(ROUTINE, message_id="1"))
    assert [f.name for f in replanned.frames] == [
        "take",
        "teach_phrase",
    ] and "teach_phrase" not in replanned.affordances
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.offer is not None and stored.offer.forms == ["triage {X}"]


async def test_an_offer_is_made_to_a_speaker_on_a_message_turn(groups: SingleBonfire) -> None:
    engine, _, _ = await _engine(groups)
    nobody = await engine.begin(team_turn(ROUTINE, message_id="1", speaker=None))
    with pytest.raises(Refused, match="no speaker"):
        await engine.offer(nobody.turn_id, FORMS, BODY, {})


async def test_no_closes_the_offer(groups: SingleBonfire) -> None:
    engine, _, _ = await _engine(groups)
    await _offered(engine)
    no = await engine.begin(team_turn("no", message_id="2", at=later(1)))
    assert "teach_phrase" not in no.affordances and no.offer is None
    assert "declined" in no.context and "triage {X} for {R}" in no.context
    yes = await engine.begin(team_turn("yes", message_id="3", at=later(2)))  # a yes after the no answers nothing
    assert "teach_phrase" not in yes.affordances and yes.offer is None and "offered" not in yes.context


async def test_another_speakers_yes_affords_nothing(groups: SingleBonfire) -> None:
    engine, store, _ = await _engine(groups)
    offered = await _offered(engine)
    milo = await engine.begin(team_turn("yes", message_id="2", speaker="Milo", at=later(1)))
    assert "teach_phrase" not in milo.affordances and milo.offer is None and "offered" not in milo.context
    # the rule itself: an offer composes only with an answer of the speaker it was made to
    turn = await store.get_turn(offered.turn_id)
    assert offer_reply(turn, "Milo", "yes") is None
    assert offer_reply(turn, "Kira Dace", "yes") is not None


async def test_a_drafted_offer_is_no_longer_context(groups: SingleBonfire) -> None:
    engine, store, mk = await _engine(groups)
    offered = await _offered(engine)
    yes = await engine.begin(team_turn("yes", message_id="2", at=later(1)))
    teaching = Teaching(mk, store, groups)
    draft = await teaching.begin(
        TeachDraft(
            turn_id=yes.turn_id,
            platform="telegram",
            group="-100777",
            chat="-100777",
            teacher="Kira Dace",
            forms=FORMS,
            body=BODY,
            defaults=DEFAULTS,
        )
    )
    assert draft.draft_id == "d1"
    stored = await store.get_turn(offered.turn_id)
    assert stored is not None and stored.offer is not None and stored.offer.drafted == "d1"
    assert offer_reply(stored, "Kira Dace", "yes") is None
    again = await engine.begin(team_turn("yes", message_id="2", at=later(1)))  # the yes delivered again: re-planned
    assert "teach_phrase" not in again.affordances and again.offer is None and "offered" not in again.context


def test_the_offer_frame_affords_the_editor_only_when_accepted() -> None:
    assert afford([offer_frame()], CATALOGUE, "talk_only", False) == READ_ONLY
    assert afford([offer_frame()], CATALOGUE, "talk_only", False, offered=True) == [*READ_ONLY, "teach_phrase"]
    assert afford([Frame.model_validate(frame("teach", "editor"))], CATALOGUE, "talk_only", False) == [
        *READ_ONLY,
        "teach_phrase",
    ]
