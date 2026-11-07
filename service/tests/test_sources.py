"""The agent's catalogue: stored once the kernel accepts it, installed only when it changed; a phrase paused at an
agent step waits."""

from __future__ import annotations

import pytest

from kernel_turns.groups import SingleBonfire
from kernel_turns.mk_client import KernelBusy, Refused
from kernel_turns.models import JsonValue
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import Teaching
from kernel_turns.turns import TurnEngine
from tests.fakes import CATALOGUE, FakeMk, Script, later, team_turn

BONFIRE = "65f0c0ffee0000000000abcd"
WEB_SEARCH: dict[str, JsonValue] = {
    "name": "web_search",
    "description": "search the web",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "x-side-effect": False,
    "x-consent": "speaker_turn",
    "x-runner": "agent",
}
THINK: dict[str, JsonValue] = {
    "name": "think",
    "description": "the agent does this step itself with its own context",
    "input_schema": {
        "type": "object",
        "properties": {"instruction": {"type": "string"}, "input": {"type": "string"}},
        "required": ["instruction"],
    },
    "x-side-effect": False,
    "x-consent": "speaker_turn",
    "x-runner": "agent",
}
AGENT: dict[str, JsonValue] = {"schema_version": "kernel-catalogue/v1", "tools": [WEB_SEARCH, THINK]}


def _names(catalogue: dict[str, JsonValue]) -> list[str]:
    tools = catalogue["tools"]
    assert isinstance(tools, list)
    return [str(t["name"]) for t in tools if isinstance(t, dict)]


async def test_a_failed_agent_install_is_retried(groups: SingleBonfire) -> None:
    mk, store = FakeMk(), MemoryTurnStore()
    teaching = Teaching(mk, store, groups)
    await teaching.put_catalogue("telegram", "-100777", CATALOGUE)
    mk.busy = True
    with pytest.raises(KernelBusy):
        await teaching.put_catalogue("telegram", "-100777", AGENT, "agent")
    assert "web_search" not in _names(await store.catalogue("telegram", "-100777") or {})  # the engine reads no phantom
    mk.busy = False
    await teaching.put_catalogue("telegram", "-100777", AGENT, "agent")  # the identical retry installs
    assert "web_search" in _names(mk.catalogues[BONFIRE])
    assert "web_search" in _names(await store.catalogue("telegram", "-100777") or {})


async def test_agent_put_without_change_installs_nothing(groups: SingleBonfire) -> None:
    mk, store = FakeMk(), MemoryTurnStore()
    teaching = Teaching(mk, store, groups)
    await teaching.put_catalogue("telegram", "-100777", CATALOGUE)
    await teaching.put_catalogue("telegram", "-100777", AGENT, "agent")
    assert len(mk.calls_of("put_catalogue")) == 2
    await teaching.put_catalogue("telegram", "-100777", AGENT, "agent")  # same rows again
    assert len(mk.calls_of("put_catalogue")) == 2
    await teaching.put_catalogue(
        "telegram", "-100777", {**AGENT, "tools": [*AGENT["tools"], {**WEB_SEARCH, "name": "fetch"}]}, "agent"
    )
    assert len(mk.calls_of("put_catalogue")) == 3


async def test_agent_put_in_another_row_order_installs_nothing(groups: SingleBonfire) -> None:
    mk, store = FakeMk(), MemoryTurnStore()
    teaching = Teaching(mk, store, groups)
    tools: list[JsonValue] = [WEB_SEARCH, {**WEB_SEARCH, "name": "fetch"}]
    two: dict[str, JsonValue] = {**AGENT, "tools": tools}
    await teaching.put_catalogue("telegram", "-100777", two, "agent")
    n = len(mk.calls_of("put_catalogue"))
    await teaching.put_catalogue("telegram", "-100777", {**two, "tools": list(reversed(tools))}, "agent")
    assert len(mk.calls_of("put_catalogue")) == n


async def _engine(mk: FakeMk, groups: SingleBonfire) -> tuple[TurnEngine, MemoryTurnStore]:
    store = MemoryTurnStore()
    teaching = Teaching(mk, store, groups)
    await teaching.put_catalogue("telegram", "-100777", CATALOGUE)
    await teaching.put_catalogue("telegram", "-100777", AGENT, "agent")
    return TurnEngine(mk, store, groups), store


async def test_a_waiting_phrase_shows_on_the_next_turn(groups: SingleBonfire) -> None:
    mk = FakeMk({"sitrep": Script(steps=[("think", {"instruction": "summarise"})], phrase="sitrep.d1.0")})
    engine, _ = await _engine(mk, groups)
    child = await engine.begin(team_turn("sitrep", message_id="9:call:0"), parent="9")
    assert child.pending is not None and child.pending.runner == "agent"

    mine = await engine.begin(team_turn("so?", message_id="10", at=later(2)))  # the same speaker
    assert mine.waiting is not None and mine.waiting.turn_id == child.turn_id
    assert mine.waiting.step.tool == "think" and "think" in mine.context and "phrase_continue" in mine.context
    other = await engine.begin(team_turn("so?", message_id="11", speaker="Milo", at=later(3)))
    assert other.waiting is None  # another speaker's turn does not see it
    assert (await engine.get(mine.turn_id)).waiting is not None

    await engine.report(child.turn_id, child.pending.step_id, "committed", "three lines")
    assert (await engine.begin(team_turn("so?", message_id="12", at=later(4)))).waiting is None  # it went on


async def test_think_is_not_a_call(groups: SingleBonfire) -> None:
    engine, _ = await _engine(FakeMk(), groups)
    view = await engine.begin(team_turn("hello"))
    with pytest.raises(Refused, match="phrase_continue"):
        await engine.call(view.turn_id, "think", {"instruction": "x"})
