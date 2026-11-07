"""The model's calls on a message turn: admitted when afforded, deduped, run once.

A tool call becomes a direct step with the effect key of ``(turn, tool, args, occurrence)``; the same tool and args
again is the same effect: the step again until it committed, then its first value. An argument the catalogue's
``input_schema`` does not declare is refused, so no spelling of a call makes a second effect. A phrase call renders the
phrase's form and begins it as a child turn, which runs the kernel's own loop; a call that renders the text of a child
already begun is that child again, however its args were spelled. A repeat of an earlier call with the same args is
answered from that call BEFORE the affordance gate: a message delivered again may read differently, and the first result
still stands. Refused (409) with a sentence the model can say: not afforded, unknown, an argument
undeclared, missing or not text, a slot with no value, a phrase the kernel did not read as the called phrase.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, model_validator

from kernel_turns import keys
from kernel_turns.affordances import (
    CONFIRM,
    TEACH,
    THINK,
    canon_key,
    missing_args,
    needs_confirmation,
    render_form,
    runner_of,
    tool_row,
)
from kernel_turns.mk_client import KernelApi, Refused
from kernel_turns.models import ChildCall, JsonValue, LedgerRow, PendingStep, Runner, TurnRecord
from kernel_turns.store import TurnStore
from kernel_turns.verbs import Verb, tool_names, verb_book

#: statuses a turn does not leave by being begun again
FINISHED: frozenset[str] = frozenset({"answered", "done", "repair"})


class CallView(BaseModel):
    """What the model's call became. ``step``: run this step, then report it; ``done``: the same call already ran,
    ``value`` is its first result; ``turn``: a taught phrase began as the child turn ``turn_id`` (``step`` is its first
    pending step, if any), which runs on the kernel's own loop."""

    kind: Literal["step", "done", "turn"]
    step: PendingStep | None = None
    value: JsonValue = None
    turn_id: str | None = None
    #: who runs ``step`` (its ``runner``)
    runner: Runner = "agent"

    @model_validator(mode="after")
    def _runner_of_step(self) -> CallView:
        if self.step is not None:
            self.runner = self.step.runner
        return self


class CallsMixin:
    """``TurnEngine.call``; it uses the engine's store, kernel client, locks, ``_load``, ``_catalogue`` and ``_plan``."""

    if TYPE_CHECKING:
        _mk: KernelApi
        _store: TurnStore

        def _lock(self, turn_id: str) -> asyncio.Lock: ...
        async def _load(self, bonfire_id: str, turn_id: str) -> TurnRecord: ...
        async def _catalogue(self, turn: TurnRecord) -> dict[str, JsonValue] | None: ...
        async def _plan(self, turn: TurnRecord, ledger: list[LedgerRow]) -> TurnRecord: ...
        async def get(self, bonfire_id: str, turn_id: str) -> object: ...

    async def call(self, bonfire_id: str, turn_id: str, tool: str, args: dict[str, JsonValue]) -> CallView:
        """Admit the model's call of an afforded tool or phrase (see the module docstring)."""
        async with self._lock(turn_id):
            turn = await self._load(bonfire_id, turn_id)
            if tool in (TEACH, CONFIRM):
                raise Refused(f"{tool} is not a call: it has its own route and the teacher's yes", "own_route")
            if tool == THINK:
                raise Refused("think is a phrase step, answered with phrase_continue, not a call", "think_step")
            wanted = canon_key(tool, args)
            earlier = next((c for c in turn.calls if canon_key(c.tool, c.args) == wanted), None)
            if earlier is not None:
                return await self._again(turn, earlier)
            asked = next((c for c in turn.children if canon_key(c.tool, c.args) == wanted), None)
            if asked is not None:
                return await self._child_view(bonfire_id, asked.turn_id)
            row = tool_row(await self._catalogue(turn), tool)
            phrase = any(f.kind == "phrase" and f.name == tool for f in turn.frames)
            if tool not in turn.affordances:
                if row is None and not phrase:
                    raise Refused(f"{tool} is not a tool or phrase this group knows", "unknown")
                afforded = ", ".join(turn.affordances) or "nothing"
                raise Refused(f"{tool} is not afforded this turn (afforded: {afforded})", "not_afforded")
            if phrase:
                return await self._call_phrase(turn, tool, args)
            if row is None:
                raise Refused(f"{tool} is not a tool or phrase this group knows", "unknown")
            return await self._call_tool(turn, row, args)

    async def _again(self, turn: TurnRecord, same: PendingStep) -> CallView:
        """The first result of a tool call made before: its value once committed, else the same step."""
        mine = [r for r in await self._store.ledger(turn.bonfire_id, turn.turn_id) if r.step_id == same.step_id]
        done = next((r for r in mine if r.verdict == "committed"), None)
        if done is not None:
            return CallView(kind="done", step=same, value=done.value)
        ended = next((r for r in mine if r.verdict != "tool_error"), None)
        if ended is not None:
            raise Refused(
                f"{same.tool} already ended in this turn ({ended.verdict}: {ended.detail or 'no detail'})", "ended"
            )
        return CallView(kind="step", step=same)

    async def _child_view(self, bonfire_id: str, child_id: str) -> CallView:
        child = await self._store.get_turn(bonfire_id, child_id)
        return CallView(kind="turn", step=child.pending if child else None, turn_id=child_id)

    async def _call_tool(self, turn: TurnRecord, row: dict[str, JsonValue], args: dict[str, JsonValue]) -> CallView:
        tool = str(row["name"])
        problem = missing_args(row, args)
        if problem is not None:
            raise Refused(problem, "bad_args")
        ledger = await self._store.ledger(turn.bonfire_id, turn.turn_id)
        occurrence = sum(
            1
            for r in ledger
            if r.verdict == "committed" and r.tool == tool and keys.canonical(r.args) == keys.canonical(args)
        )
        step = PendingStep(
            step_id=f"{turn.turn_id}:call:{len(turn.calls) + len(turn.children)}",
            tool=tool,
            args=dict(args),
            effect_key=keys.effect_key(turn.turn_id, tool, args, occurrence),
            needs_confirmation=needs_confirmation(row),
            direct=True,
            runner=runner_of(row),
        )
        await self._store.save_turn(turn.model_copy(update={"calls": [*turn.calls, step]}))
        return CallView(kind="step", step=step)

    async def _verb(self, turn: TurnRecord, tool: str) -> Verb:
        """The live verb book entry of the taught phrase a phrase frame named ``tool`` stands for (``tool`` is the
        phrase's spec id, one of the ids of its draft's rows)."""
        rows = await self._mk.constructions(turn.bonfire_id)
        verbs = verb_book(rows, tool_names(await self._catalogue(turn)), [])
        found = next((v for v in verbs if tool in v.ids and v.status == "live"), None)
        if found is None:
            raise Refused(f"{tool} is not a phrase this group has taught", "not_taught")
        return found

    async def _call_phrase(self, turn: TurnRecord, tool: str, args: dict[str, JsonValue]) -> CallView:
        verb = await self._verb(turn, tool)
        slots = {s.lower() for s in verb.slots}
        stray = [k for k in args if k.lower() not in slots]
        if stray or any(not isinstance(v, str) for v in args.values()):
            raise Refused(f"{tool} takes {', '.join(verb.slots) or 'no arguments'}, each as text", "stray_arg")
        text, why = render_form(verb.forms, args, verb.defaults)
        if text is None:
            raise Refused(f"{tool}: {why}", "slot_missing")
        # the phrase's effect is the sentence it renders: args spelled another way (a slot's case, a default given)
        # that render a recorded child's text are that call again
        for called in turn.children:
            child = await self._store.get_turn(turn.bonfire_id, called.turn_id)
            if child is not None and child.text == text and child.phrase in verb.ids:
                return CallView(kind="turn", step=child.pending, turn_id=child.turn_id)
        # the next free child id; a stored child is reused only for the same rendered text (a child the kernel read as
        # unread is saved without being admitted, and a lost parent save leaves one behind)
        n = len(turn.calls) + len(turn.children)
        while True:
            message_id = f"{turn.message_id}:call:{n}"
            child_id = keys.turn_id(turn.bonfire_id, turn.platform, turn.group, turn.chat, message_id)
            stored = await self._store.get_turn(turn.bonfire_id, child_id)
            if stored is None or stored.text == text:
                break
            n += 1
        async with self._lock(child_id):
            child = await self._store.get_turn(turn.bonfire_id, child_id)
            if child is None:
                child = turn.child(child_id, message_id, text)
            if child.status not in FINISHED:
                child = await self._plan(child, await self._store.ledger(turn.bonfire_id, child_id))
        if child.status == "unread":
            raise Refused(
                f"{tool} did not read as {text!r} just now: {'; '.join(child.problems) or 'the kernel read nothing'}",
                "not_read",
            )
        if child.phrase not in verb.ids:
            raise Refused(
                f"{tool} did not read as {text!r} just now: the kernel read it as another phrase", "other_phrase"
            )
        await self._store.save_turn(
            turn.model_copy(
                update={"children": [*turn.children, ChildCall(tool=tool, args=dict(args), turn_id=child_id)]}
            )
        )
        return CallView(kind="turn", step=child.pending, turn_id=child_id)
