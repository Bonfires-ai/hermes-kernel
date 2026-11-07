"""One bonfire for every conversation, and a turn the kernel did not read says why."""

from __future__ import annotations

from kernel_turns.groups import SingleBonfire
from kernel_turns.store import MemoryTurnStore
from kernel_turns.turns import TurnEngine
from tests.fakes import FakeMk, team_turn


async def test_every_chat_maps_to_the_one_bonfire() -> None:
    groups = SingleBonfire("hermes")
    a = await groups.find("telegram", "-100123")
    b = await groups.find("cli", "session-1")
    assert (a.bonfire_id, b.bonfire_id) == ("hermes", "hermes")
    assert a.unread == "open"


async def test_an_unread_turn_says_why() -> None:
    engine = TurnEngine(FakeMk(), MemoryTurnStore(), SingleBonfire("hermes"))  # no script: the kernel reads nothing
    view = await engine.begin(team_turn("something the grammar does not know"))
    assert view.status == "unread"
    assert view.problems == ["no construction read the turn"]
    assert (await engine.get(view.turn_id)).problems == view.problems
