"""The turn engine over a scripted kernel: the ledger is written before each resume, a gone plan re-plans with
``committed=``, and a verdict the kernel never took still ends the turn."""

from __future__ import annotations

import pytest

from kernel_turns import keys
from kernel_turns.groups import SingleBonfire
from kernel_turns.mk_client import Refused
from kernel_turns.models import Verdict
from kernel_turns.store import MemoryTurnStore
from kernel_turns.turns import TurnEngine, TurnView
from tests.fakes import FakeMk, LoggingStore, LosingStore, LostWrite, Script, team_turn

WATER = {"item": "water"}
TEA = {"item": "thé"}
GIFT = {"item": "thé", "person": "Kira Dace — café"}
PARENT = "m0"  # a phrase's child turn runs the kernel's steps; a message turn issues none (test_calls.py)
BREW = Script(steps=[("take", WATER), ("make", TEA), ("give", GIFT)], phrase="brew.d1.0")


def _engine(
    mk: FakeMk, groups: SingleBonfire, store: MemoryTurnStore | None = None
) -> tuple[TurnEngine, MemoryTurnStore]:
    store = store if store is not None else MemoryTurnStore()
    return TurnEngine(mk, store, groups), store


async def test_plan_gone_replans_with_the_ledger(groups: SingleBonfire) -> None:
    events: list[str] = []
    mk = FakeMk({"brew thé for Kira Dace": BREW}, events)
    engine, store = _engine(mk, groups, LoggingStore(events))
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    made = await engine.report(view.turn_id, view.pending.step_id, "committed", "water#1")
    assert made.pending is not None
    cup = {"cup": {"size": "large", "of": "thé"}, "steeped": [3, 4.5]}

    mk.forget()  # the kernel lost the plan (it expired, or the catalogue reloaded)
    events.clear()
    nxt = await engine.report(view.turn_id, made.pending.step_id, "committed", cup)
    assert events[:3] == ["add_ledger", "resume", "plan"]  # the ledger row is written before the resume
    assert mk.last_committed() == [
        {"tool": "take", "args": WATER, "value": "water#1"},
        {"tool": "make", "args": TEA, "value": cup},
    ]
    assert nxt.status == "licensed"
    assert nxt.pending is not None and (nxt.pending.tool, nxt.pending.args) == ("give", GIFT)
    assert nxt.pending.effect_key == keys.effect_key(view.turn_id, "give", GIFT, 0)
    assert [r.verdict for r in await store.ledger(view.turn_id)] == ["committed", "committed"]


async def test_get_replans_a_gone_plan(groups: SingleBonfire) -> None:
    mk = FakeMk({"brew thé for Kira Dace": BREW})
    engine, _ = _engine(mk, groups)
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    made = await engine.report(view.turn_id, view.pending.step_id, "committed", "water#1")
    assert made.pending is not None
    given = await engine.report(view.turn_id, made.pending.step_id, "committed", {"id": "c-1"})
    assert given.pending is not None

    plans = len(mk.calls_of("plan"))
    alive = await engine.get(view.turn_id)
    assert len(mk.calls_of("plan")) == plans  # the plan is alive: the stored view, no plan call
    assert alive == given

    mk.forget()
    view_again = await engine.get(view.turn_id)
    assert len(mk.calls_of("plan")) == plans + 1
    assert mk.last_committed() == [
        {"tool": "take", "args": WATER, "value": "water#1"},
        {"tool": "make", "args": TEA, "value": {"id": "c-1"}},
    ]
    assert view_again.pending is not None and (view_again.pending.tool, view_again.pending.args) == ("give", GIFT)
    assert view_again.pending.effect_key == given.pending.effect_key
    assert view_again.plan_id != given.plan_id


async def test_a_tool_error_the_kernel_cannot_take_records_a_failed_use(groups: SingleBonfire) -> None:
    """The kernel out of reach when the tool_error comes: the turn ends here as a repair, and its use still counts."""
    mk = FakeMk({"brew thé for Kira Dace": BREW})
    engine, store = _engine(mk, groups)
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    mk.down = True
    broke = await engine.report(view.turn_id, view.pending.step_id, "tool_error", detail="the tap timed out")
    assert (broke.status, broke.pending) == ("repair", None)
    uses = await store.uses(view.bonfire_id)
    assert [(u.phrase, u.outcome) for u in uses] == [("brew.d1.0", "failed")]


async def test_a_stop_after_a_refused_report_never_issues_the_step_again(groups: SingleBonfire) -> None:
    mk = FakeMk({"brew thé for Kira Dace": BREW})
    engine, store = _engine(mk, groups)
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    mk.crash_on_resume = True
    with pytest.raises(RuntimeError):
        await engine.report(view.turn_id, view.pending.step_id, "declined", detail="not today")
    assert [r.verdict for r in await store.ledger(view.turn_id)] == ["declined"]  # written before the resume

    plans = len(mk.calls_of("plan"))
    after = await engine.get(view.turn_id)  # the service is back: the decline stands
    assert (after.status, after.pending) == ("repair", None)
    assert after.repair is not None and "declined: not today" in after.repair
    assert len(mk.calls_of("plan")) == plans
    assert [u.outcome for u in await store.uses(view.bonfire_id)] == ["declined"]


async def _refusal_with_a_lost_save(
    groups: SingleBonfire, verdict: Verdict
) -> tuple[FakeMk, LosingStore, TurnEngine, TurnView]:
    """A turn whose ``take`` was reported ``verdict``: the ledger row is written, the turn's own save is lost, so the
    stored turn still holds ``take`` as pending and the kernel never heard of the verdict."""
    mk = FakeMk({"brew thé for Kira Dace": BREW})
    store = LosingStore()
    engine = TurnEngine(mk, store, groups)
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    store.lose_the_save_after_the_next_ledger_write()
    with pytest.raises(LostWrite):
        await engine.report(view.turn_id, view.pending.step_id, verdict, detail="not today")
    stored = await store.get_turn(view.turn_id)
    assert stored is not None and stored.pending == view.pending  # the gap: the refused step is still stored pending
    assert [r.verdict for r in await store.ledger(view.turn_id)] == [verdict]
    assert mk.calls_of("resume") == []
    return mk, store, engine, view


@pytest.mark.parametrize(
    ("reader", "verdict", "outcome"),
    [
        ("get", "declined", "declined"),
        ("get_gone", "precondition_failed", "failed"),
        ("begin", "declined", "declined"),
    ],
)
async def test_a_lost_save_after_a_refused_report_never_issues_the_step_again(
    groups: SingleBonfire, reader: str, verdict: Verdict, outcome: str
) -> None:
    mk, store, engine, view = await _refusal_with_a_lost_save(groups, verdict)
    assert view.pending is not None
    plans = len(mk.calls_of("plan"))

    if reader == "begin":
        begun = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)  # the message delivered again
        assert (begun.status, begun.pending) == ("repair", None)
    else:
        if reader == "get_gone":
            mk.forget()  # the probe would find the plan gone and plan the turn again
        got = await engine.get(view.turn_id)
        assert (got.status, got.pending) == ("repair", None)

    after = await engine.get(view.turn_id)
    assert (after.status, after.pending) == ("repair", None)
    assert after.repair is not None and f"{verdict}: not today" in after.repair
    again = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert (again.status, again.pending) == ("repair", None)
    assert len(mk.calls_of("plan")) == plans  # no plan issued the refused step again
    assert mk.calls_of("resume") == []  # nor was a probe needed: the ledger said it
    assert [u.outcome for u in await store.uses(view.bonfire_id)] == [outcome]
    with pytest.raises(Refused):
        await engine.report(view.turn_id, view.pending.step_id, "committed", "water#1")


async def test_a_report_after_a_lost_save_completes_the_refusal_and_cannot_overturn_it(groups: SingleBonfire) -> None:
    mk, store, engine, view = await _refusal_with_a_lost_save(groups, "declined")
    assert view.pending is not None
    with pytest.raises(Refused):  # the step was declined: a committed report for it is not taken
        await engine.report(view.turn_id, view.pending.step_id, "committed", "water#1")
    ended = await engine.get(view.turn_id)
    assert (ended.status, ended.pending) == ("repair", None)
    assert [r.verdict for r in await store.ledger(view.turn_id)] == ["declined"]
    assert mk.calls_of("resume") == []

    mk, store, engine, view = await _refusal_with_a_lost_save(groups, "declined")
    assert view.pending is not None
    resent = await engine.report(view.turn_id, view.pending.step_id, "declined", detail="not today")  # the same report
    assert (resent.status, resent.pending) == ("repair", None)
    assert [r.verdict for r in await store.ledger(view.turn_id)] == ["declined"]  # one row
    declined = {"verdict": "declined", "value": None, "detail": "not today"}
    step = {"step_id": view.pending.step_id, "tool": "take", "args": WATER}
    assert mk.calls_of("resume") == [[{**step, **declined}]]  # the kernel took the verdict itself
    assert [u.outcome for u in await store.uses(view.bonfire_id)] == ["declined"]


async def test_report_refuses_a_step_that_is_not_pending_and_builds_its_row_from_the_stored_step(
    groups: SingleBonfire,
) -> None:
    mk = FakeMk({"brew thé for Kira Dace": BREW})
    engine, store = _engine(mk, groups)
    view = await engine.begin(team_turn("brew thé for Kira Dace"), parent=PARENT)
    assert view.pending is not None
    later_step = f"{view.plan_id}:1:0"  # a real step of the program, but not the pending one

    for stale in ("p1:9:0", later_step, "nonsense"):
        with pytest.raises(Refused):
            await engine.report(view.turn_id, stale, "committed", "nothing")
    assert await store.ledger(view.turn_id) == []  # nothing was written for them

    after = await engine.report(view.turn_id, view.pending.step_id, "committed", "water#1")
    assert after.pending is not None and after.pending.tool == "make"
    (row,) = await store.ledger(view.turn_id)
    assert (row.step_id, row.tool, row.args, row.value) == (view.pending.step_id, "take", WATER, "water#1")
    with pytest.raises(Refused):  # the reported step is no longer pending
        await engine.report(view.turn_id, view.pending.step_id, "committed", "water#2")
