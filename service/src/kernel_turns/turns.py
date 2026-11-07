"""The turn engine: ``begin``, ``report``, ``retry``, ``get`` and the model's calls (``CallsMixin``).

A conversation message becomes a turn of the caller's bonfire (``keys.turn_id``), and every public method names that
bonfire first: a turn of another bonfire is unknown, as if it did not exist. The kernel plans the turn (``mk serve``
``POST …/plan``) and it is one of:

* ``unread``: the text is over ``max_text``, the kernel cannot be reached or refused the request (an unknown bonfire
  included), or its plan is ``unread``. ``TurnView.problems`` says why. What the turn affords the model then follows
  the group's unread setting: ``open`` affords every catalogue tool; ``talk_only`` only a tool whose catalogue
  ``x-side-effect`` is ``false`` (a tool the catalogue does not name counts as side-effecting).
* ``licensed``: a program holds the turn, and the one pending step is the only step that may run. A licensed turn may
  have no pending step while the kernel has not taken its last report (out of reach): a ``get`` or ``begin`` re-plans it.
* ``answered`` (the program named no tool), ``done`` (a program that names tools ran to its end), ``repair`` (the
  kernel could not run it, or a step's verdict ended it).

``report`` writes the ledger row FIRST (built from the stored pending step, never from what the adapter sent), then
resumes the kernel's plan. A ``committed`` report whose plan is gone (``PlanGone`` or ``Refused``) re-plans with
``committed=`` every committed ledger row ``{tool, args, value}`` in ledger order; when the kernel is out of reach the
row stays and the turn keeps its licence with no pending step. A report with any other verdict ends the turn: when the
kernel cannot take it (gone, refused, out of reach) the turn becomes a repair here, so a refused step is never issued
again by a re-plan (``committed=`` holds only committed effects). ``report`` writes twice before the resume (the row,
then the turn with no pending step): a verdict that ends the turn and that the kernel never took, because the service
stopped after either write, ends the turn here the next time ``report``, ``get`` or ``begin`` reads it (``_unheard``);
the same report sent again completes it through the kernel, and a report of another verdict for that step is refused.
``retry`` is allowed only when the turn's last ledger row is a ``tool_error``; a gone plan re-plans, which issues the
failed step again under its effect key.

``begin`` of a turn that exists re-plans it when it is ``unread`` or ``licensed``: a message's turn with
``committed: []`` and its ``context`` (its calls are deduped here), a phrase's child turn with ``committed=`` its own
ledger; a finished turn (answered, done, repair) is returned as it is. ``get`` returns the live view of a licensed turn:
when its plan is gone (``PlanGone`` on a probe, or the ledger is ahead of the plan) it re-plans. The probe is a resume
naming a step id no plan has (``<plan_id>:probe``): the kernel answers 404 for a gone plan and 409, changing nothing,
otherwise.

A finished turn that applied a taught phrase records one ``UseRecord``: ``ok`` when answered or done, ``declined`` when
a decline ended it, ``failed`` when a ``precondition_failed`` or a ``tool_error`` did. A ``tool_error`` counts as final
because no adapter retries one. One use per turn: the turn keeps the outcome it recorded (``TurnRecord.use``), so a turn
retried after its ``tool_error`` records nothing more.
"""

from __future__ import annotations

import asyncio
import weakref
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from kernel_turns import keys
from kernel_turns.affordances import (
    CONFIRM,
    YES,
    afford,
    frames_line,
    has_staged_draft,
    native_of,
    offer_note,
    offer_reply,
    open_frames,
    reply_of,
    with_offer,
)
from kernel_turns.calls import FINISHED, CallsMixin
from kernel_turns.groups import group_of
from kernel_turns.mk_client import KernelApi, KernelBusy, KernelDown, MkError, PlanGone, Refused
from kernel_turns.models import (
    BodyStep,
    Frame,
    Group,
    JsonValue,
    LedgerRow,
    OfferReply,
    PendingStep,
    TeachOffer,
    TurnRecord,
    TurnStatus,
    UnreadSetting,
    UseOutcome,
    UseRecord,
    UtcDatetime,
    Verdict,
    Waiting,
)
from kernel_turns.plans import committed_rows, diagnostics, frames_of, licence_of, phrase_of, step_of
from kernel_turns.store import TurnStore

UNREAD_CONTEXT = "The kernel did not read this turn."


class UnknownTurn(LookupError):
    """This bonfire has no turn with this id."""


class BeginTurn(BaseModel):
    platform: str = Field(min_length=1)
    group: str = Field(min_length=1)
    chat: str = Field(min_length=1)
    speaker: str | None = None
    message_id: str = Field(min_length=1)
    text: str
    at: UtcDatetime
    #: the people the platform knows are in the chat
    participants: list[str] = Field(default_factory=list)


class TurnView(BaseModel):
    turn_id: str
    bonfire_id: str
    plan_id: str | None
    platform: str
    group: str
    chat: str
    speaker: str | None
    status: TurnStatus
    #: the group's unread setting, when it governs the turn (no licence); None otherwise
    unread: UnreadSetting | None
    licence: list[str] | None
    pending: PendingStep | None
    answer: JsonValue = None
    repair: str | None = None
    phrase: str | None = None
    #: the readings of the message (every analysis) and the tools and phrases the turn affords the model
    frames: list[Frame] = Field(default_factory=list)
    affordances: list[str] = Field(default_factory=list)
    #: the afforded catalogue tools the agent calls natively; ``think`` is not one
    native: list[str] = Field(default_factory=list)
    #: the turn whose phrase call began this one; None for a platform message
    parent: str | None = None
    #: the open offer to teach this turn's yes accepted (``teach_phrase`` is afforded to draft it); None otherwise
    offer: TeachOffer | None = None
    #: on a message turn: this speaker's phrase in this chat that paused at a step running in the agent (a ``think`` or
    #: an agent tool), if any: the model continues it or drops it
    waiting: Waiting | None = None
    #: why the kernel did not read or run the turn, when it did not say (its diagnostics otherwise); empty when it did
    problems: list[str] = Field(default_factory=list)
    #: a short plain-text note for the model
    context: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


def _issued(issued: list[PendingStep], step: PendingStep | None) -> list[PendingStep]:
    """``issued`` with ``step`` added; a step issued again (the same effect key) replaces its entry."""
    if step is None:
        return issued
    if any(s.effect_key == step.effect_key for s in issued):
        return [step if s.effect_key == step.effect_key else s for s in issued]
    return [*issued, step]


class TurnEngine(CallsMixin):
    """The turn service's rules over a ``KernelApi`` and a ``TurnStore``.

    One call per turn at a time (an ``asyncio.Lock`` per turn id): a second ``report`` waits for the first."""

    def __init__(self, mk: KernelApi, store: TurnStore, *, max_text: int = 4000) -> None:
        self._mk = mk
        self._store = store
        self._max_text = max_text
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, turn_id: str) -> asyncio.Lock:
        lock = self._locks.get(turn_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[turn_id] = lock
        return lock

    async def _load(self, bonfire_id: str, turn_id: str) -> TurnRecord:
        turn = await self._store.get_turn(bonfire_id, turn_id)
        if turn is None:
            raise UnknownTurn(f"no turn {turn_id!r}")
        return turn

    # -- begin -------------------------------------------------------------------------------------------------------

    async def begin(self, bonfire_id: str, req: BeginTurn, *, parent: str | None = None) -> TurnView:
        """Begin the turn of a platform message. ``parent`` is for the engine's own child turns (a phrase call) and for
        tests: a child runs the kernel's steps one at a time; a message turn issues nothing (``_apply``)."""
        group = group_of(bonfire_id, req.platform, req.group)
        turn_id = keys.turn_id(bonfire_id, req.platform, req.group, req.chat, req.message_id)
        async with self._lock(turn_id):
            turn = await self._store.get_turn(bonfire_id, turn_id)
            if turn is None:
                turn = TurnRecord(
                    turn_id=turn_id,
                    platform=req.platform,
                    group=req.group,
                    chat=req.chat,
                    message_id=req.message_id,
                    speaker=req.speaker,
                    text=req.text,
                    at=req.at,
                    participants=list(req.participants),
                    bonfire_id=group.bonfire_id,
                    plan=None,
                    status="unread",
                    licence=None,
                    pending=None,
                    phrase=None,
                    parent=parent,
                )
                turn = await self._plan(turn, [])
            elif turn.status not in FINISHED:
                turn = await self._replan(turn, await self._store.ledger(bonfire_id, turn_id))
            return await self._waiting_view(turn, group)

    # -- get ---------------------------------------------------------------------------------------------------------

    async def get(self, bonfire_id: str, turn_id: str) -> TurnView:
        async with self._lock(turn_id):
            turn = await self._load(bonfire_id, turn_id)
            if turn.status == "licensed":
                turn = await self._refresh(turn)
            return await self._waiting_view(turn, group_of(bonfire_id, turn.platform, turn.group))

    async def _refresh(self, turn: TurnRecord) -> TurnRecord:
        """A licensed turn as the kernel now holds it: re-planned when its plan is gone or behind the ledger; ended here
        when its ledger holds a verdict that ends it which the kernel never took (before the probe: a gone plan would
        be planned again, and issue the refused step again)."""
        ledger = await self._store.ledger(turn.bonfire_id, turn.turn_id)
        ended = await self._end_unheard(turn, ledger)
        if ended is not None:
            return ended
        pending = turn.pending
        plan_id = self._plan_id(turn)
        behind = pending is None or any(r.step_id == pending.step_id and r.verdict == "committed" for r in ledger)
        if behind or pending is None or plan_id is None:
            return await self._replan(turn, ledger)
        probe: dict[str, JsonValue] = {
            "step_id": f"{plan_id}:probe",  # no plan issues this id: a live plan answers 409 and changes nothing
            "tool": pending.tool,
            "args": pending.args,
            "verdict": "tool_error",
            "value": None,
            "detail": "kernel-turns probe",
        }
        try:
            await self._mk.resume(turn.bonfire_id, plan_id, [probe])
        except PlanGone:
            return await self._plan(turn, ledger)
        except MkError:
            return turn  # alive (409), or the kernel cannot say now: the stored view stands
        return turn

    # -- offers ------------------------------------------------------------------------------------------------------

    async def offer(
        self, bonfire_id: str, turn_id: str, forms: list[str], body: list[BodyStep], defaults: dict[str, str]
    ) -> TurnView:
        """Record the agent's offer to teach a phrase on a message turn: no side effect. The turn shows it as an open
        editor frame (construction ``offer``) and affords nothing more; the speaker's yes on their next message turn in
        the chat affords ``teach_phrase``. A second offer on the turn replaces the first."""
        async with self._lock(turn_id):
            turn = await self._load(bonfire_id, turn_id)
            if turn.parent is not None:
                raise Refused(f"turn {turn_id} is a phrase's child turn: an offer is made on a message")
            if turn.speaker is None:
                raise Refused(f"turn {turn_id} has no speaker to make the offer to")
            offer = TeachOffer(forms=forms, body=body, defaults=defaults, to=turn.speaker, at=_now())
            turn = turn.model_copy(update={"offer": offer, "frames": with_offer(turn.frames, True)})
            await self._store.save_turn(turn)
            return self._view(turn, group_of(bonfire_id, turn.platform, turn.group))

    # -- the trace ---------------------------------------------------------------------------------------------------

    async def recent(
        self, bonfire_id: str, platform: str, group: str, limit: int
    ) -> list[tuple[TurnRecord, list[LedgerRow]]]:
        """The group's latest turns (newest first), each with its ledger rows."""
        turns = await self._store.recent_turns(bonfire_id, limit)
        return [(turn, await self._store.ledger(bonfire_id, turn.turn_id)) for turn in turns]

    # -- report ------------------------------------------------------------------------------------------------------

    async def report(
        self,
        bonfire_id: str,
        turn_id: str,
        step_id: str,
        verdict: Verdict,
        value: JsonValue = None,
        detail: str | None = None,
    ) -> TurnView:
        async with self._lock(turn_id):
            turn = await self._load(bonfire_id, turn_id)
            direct = next((c for c in turn.calls if c.step_id == step_id), None)
            if direct is not None:
                return await self._report_direct(turn, direct, verdict, value, detail)
            pending = turn.pending
            if pending is None or pending.step_id != step_id:
                waiting = f"the pending step is {pending.step_id}" if pending is not None else "no step is pending"
                raise Refused(f"step {step_id} is not pending for turn {turn_id} ({waiting})")
            ledger = await self._store.ledger(bonfire_id, turn_id)
            refused = self._unheard(turn, ledger)
            if refused is not None and refused.verdict != verdict:
                # the step was already refused (its turn's save was lost): the same report again completes it below,
                # another verdict never overturns it
                await self._end_unheard(turn, ledger)
                raise Refused(f"step {step_id} of turn {turn_id} was reported {refused.verdict}, which ended the turn")
            row = LedgerRow(
                bonfire_id=bonfire_id,
                turn_id=turn_id,
                step_id=pending.step_id,
                tool=pending.tool,
                args=pending.args,
                value=value,
                verdict=verdict,
                detail=detail,
                at=_now(),
            )
            await self._store.add_ledger(row)  # FIRST: what happened in the world is never lost to a kernel fault
            before = turn.status
            turn = turn.model_copy(update={"pending": None})  # the kernel has not heard of it yet
            await self._store.save_turn(turn)
            result: dict[str, JsonValue] = {
                "step_id": row.step_id,
                "tool": row.tool,
                "args": row.args,
                "verdict": row.verdict,
                "value": row.value,
                "detail": row.detail,
            }
            plan_id = self._plan_id(turn)
            try:
                if plan_id is None:
                    raise PlanGone(f"turn {turn_id} holds no plan id")
                plan = await self._mk.resume(turn.bonfire_id, plan_id, [result])
            except (PlanGone, Refused) as exc:
                if verdict == "committed":
                    turn = await self._plan(turn, await self._store.ledger(bonfire_id, turn_id))
                else:
                    turn = await self._settle(before, self._ended_here(turn, row, exc), verdict)
            except (KernelBusy, KernelDown) as exc:
                if verdict == "committed":
                    note = (
                        f"the kernel did not take the last step yet ({exc.detail}); the turn re-plans on its next get"
                    )
                    turn = turn.model_copy(update={"problems": [note]})
                    await self._store.save_turn(turn)
                else:
                    turn = await self._settle(before, self._ended_here(turn, row, exc), verdict)
            else:
                applied = self._apply(turn, plan, await self._store.ledger(bonfire_id, turn_id))
                turn = await self._settle(before, applied, verdict)
            return self._view(turn, group_of(bonfire_id, turn.platform, turn.group))

    async def _report_direct(
        self, turn: TurnRecord, step: PendingStep, verdict: Verdict, value: JsonValue, detail: str | None
    ) -> TurnView:
        """A report of a step the model's call was admitted as: the ledger row only, the kernel is never resumed. The same
        verdict again is the same report; a step that committed, was declined or failed a precondition is final, a
        ``tool_error`` may still be reported again (the call returns the step to run once more under its key)."""
        group = group_of(turn.bonfire_id, turn.platform, turn.group)
        rows = [r for r in await self._store.ledger(turn.bonfire_id, turn.turn_id) if r.step_id == step.step_id]
        if any(r.verdict == verdict for r in rows):
            return self._view(turn, group)
        final = next((r for r in rows if r.verdict != "tool_error"), None)
        if final is not None:
            raise Refused(f"step {step.step_id} of turn {turn.turn_id} was already reported {final.verdict}")
        row = LedgerRow(
            bonfire_id=turn.bonfire_id,
            turn_id=turn.turn_id,
            step_id=step.step_id,
            tool=step.tool,
            args=step.args,
            value=value,
            verdict=verdict,
            detail=detail,
            at=_now(),
        )
        await self._store.add_ledger(row)
        return self._view(turn, group)

    @staticmethod
    def _ended_here(turn: TurnRecord, row: LedgerRow, exc: MkError) -> TurnRecord:
        """A verdict the kernel could not take ends the turn as a repair here, its licence kept."""
        said = row.detail if row.detail is not None else repr(row.value)
        problems = [f"tool step {row.step_id} ({row.tool}) {row.verdict}: {said}", f"(the kernel: {exc.detail})"]
        return turn.model_copy(update={"status": "repair", "pending": None, "problems": problems})

    # -- retry -------------------------------------------------------------------------------------------------------

    async def retry(self, bonfire_id: str, turn_id: str) -> TurnView:
        async with self._lock(turn_id):
            turn = await self._load(bonfire_id, turn_id)
            ledger = await self._store.ledger(bonfire_id, turn_id)
            last = ledger[-1] if ledger else None
            if turn.status != "repair" or last is None or last.verdict != "tool_error":
                raise Refused(f"turn {turn_id} cannot be retried: a retry follows a tool_error, and only that")
            plan_id = self._plan_id(turn)
            try:
                if plan_id is None:
                    raise PlanGone(f"turn {turn_id} holds no plan id")
                plan = await self._mk.retry(turn.bonfire_id, plan_id)
            except (PlanGone, Refused):  # the plan is gone, or never heard of the failure: plan it again
                turn = await self._plan(turn, ledger)
            else:
                turn = await self._settle(turn.status, self._apply(turn, plan, ledger), None)
            return self._view(turn, group_of(bonfire_id, turn.platform, turn.group))

    # -- planning ----------------------------------------------------------------------------------------------------

    async def _replan(self, turn: TurnRecord, ledger: list[LedgerRow]) -> TurnRecord:
        """Plan an unfinished turn again, unless its ledger holds a verdict that ends it which the kernel never took:
        that turn ends here, as the report would have ended it, because a re-plan would issue the refused step again."""
        ended = await self._end_unheard(turn, ledger)
        if ended is not None:
            return ended
        return await self._plan(turn, ledger)

    async def _end_unheard(self, turn: TurnRecord, ledger: list[LedgerRow]) -> TurnRecord | None:
        """``turn`` ended here as a repair (saved, its use recorded) when ``_unheard`` finds a verdict the kernel never
        took; None otherwise. The caller holds the turn's lock."""
        row = self._unheard(turn, ledger)
        if row is None:
            return None
        why = MkError("the kernel never took the verdict: the service stopped after the ledger write")
        return await self._settle(turn.status, self._ended_here(turn, row, why), row.verdict)

    @staticmethod
    def _unheard(turn: TurnRecord, ledger: list[LedgerRow]) -> LedgerRow | None:
        """The ledger row of a verdict that ends a licensed ``turn`` and that the kernel never took, if any.

        ``report`` writes the row, then saves the turn with no pending step, then resumes; it can stop after either
        write. After the first (the turn's save lost), the stored pending step already has a ``declined`` or
        ``precondition_failed`` row: no path issues a step again after those (a retry follows only a ``tool_error``).
        After the second, no step is pending and the last row, not committed, is the last issued step's. A
        ``tool_error`` row for the pending step is what a retry leaves (the kernel re-issues the failed step under its
        own step id), so it is never read as unheard: after a lost save it stays pending, and running it again under its
        effect key is that retry."""
        if turn.status != "licensed" or not ledger:
            return None
        pending = turn.pending
        if pending is not None:
            refusals = ("declined", "precondition_failed")
            return next((r for r in ledger if r.step_id == pending.step_id and r.verdict in refusals), None)
        last = ledger[-1]
        if not turn.issued or last.verdict == "committed":
            return None
        return last if last.step_id == turn.issued[-1].step_id else None

    async def _plan(self, turn: TurnRecord, ledger: list[LedgerRow]) -> TurnRecord:
        """Plan ``turn`` with ``committed=`` the ledger's committed rows; save it and return it."""
        before = turn.status
        if len(turn.text) > self._max_text:
            note = f"the text is longer than {self._max_text} characters"
            turn = turn.model_copy(update={"status": "unread", "licence": None, "pending": None, "problems": [note]})
            turn = await self._afforded(turn)
            await self._store.save_turn(turn)
            return turn
        message = turn.parent is None  # a phrase's child turn replays its own ledger and takes no context
        body: dict[str, JsonValue] = {
            "text": turn.text,
            "chat_id": turn.chat,
            "speaker": turn.speaker,
            "message_id": turn.message_id,
            "at": turn.at.isoformat(),
            "participants": list(turn.participants),
            "committed": [] if message else committed_rows(ledger),
        }
        if message:
            body["context"] = [f.model_dump(mode="json") for f in await self._context_frames(turn)]
        try:
            plan = await self._mk.plan(turn.bonfire_id, body)
        except MkError as exc:
            turn = self._unread_or_held(turn, f"the kernel cannot plan the turn: {exc.detail}")
            turn = await self._afforded(turn)
            await self._store.save_turn(turn)
            return turn
        if plan.get("status") == "unread" and turn.licence is not None:
            # a turn already holding a licence keeps enforcing it (e.g. "runtime busy: teaching")
            turn = self._unread_or_held(turn, "; ".join(diagnostics(plan)) or "the kernel did not read the turn now")
            await self._store.save_turn(turn)
            return turn
        return await self._settle(before, await self._afforded(self._apply(turn, plan, ledger)), None)

    # -- affordances -------------------------------------------------------------------------------------------------

    async def _context_frames(self, turn: TurnRecord) -> list[Frame]:
        """The speaker's previous message turn's frames that nothing has run for what a fragment may answer."""
        previous = await self._store.previous_turn(turn)
        if previous is None:
            return []
        ledger = await self._store.ledger(previous.bonfire_id, previous.turn_id)
        return open_frames(previous.frames, ledger, previous.children)

    async def _catalogue(self, turn: TurnRecord) -> dict[str, JsonValue] | None:
        """The catalogue last PUT for the group, else the one the kernel has installed (None when it cannot say)."""
        stored = await self._store.catalogue(turn.bonfire_id, turn.platform, turn.group)
        if stored is not None:
            return stored
        try:
            return await self._mk.get_catalogue(turn.bonfire_id)
        except MkError:
            return None

    async def _confirmable(self, turn: TurnRecord) -> bool:
        """The speaker has a draft staged and waiting for their yes (only asked when a ``yes`` frame applied)."""
        if turn.speaker is None or not any(f.kind == "reply" and f.name == YES for f in turn.frames):
            return False
        try:
            rows = await self._mk.constructions(turn.bonfire_id)
        except MkError:
            return False
        return has_staged_draft(rows, turn.speaker)

    async def _reply(self, turn: TurnRecord) -> OfferReply | None:
        """The message's yes or no to the open offer on the speaker's previous message turn in the chat."""
        answer = reply_of(turn.frames)
        if answer is None or turn.speaker is None:
            return None
        return offer_reply(await self._store.previous_turn(turn), turn.speaker, answer)

    async def _afforded(self, turn: TurnRecord) -> TurnRecord:
        """A message turn with what its frames afford the model (a child turn affords nothing: it runs the kernel's steps),
        and its answer to an open offer to teach: a yes affords the editor."""
        if turn.parent is not None:
            return turn
        unread = group_of(turn.bonfire_id, turn.platform, turn.group).unread
        reply = await self._reply(turn)
        offered = reply is not None and reply.answer == YES
        catalogue = await self._catalogue(turn)
        names = afford(turn.frames, catalogue, unread, await self._confirmable(turn), offered=offered)
        return turn.model_copy(update={"affordances": names, "native": native_of(names, catalogue), "reply": reply})

    @staticmethod
    def _unread_or_held(turn: TurnRecord, why: str) -> TurnRecord:
        """What a turn the kernel cannot plan now becomes: a licence held stays held with no pending step; a turn with
        none is unread."""
        if turn.licence is not None:
            return turn.model_copy(update={"pending": None, "problems": [why]})
        return turn.model_copy(update={"status": "unread", "pending": None, "problems": [why]})

    def _apply(self, turn: TurnRecord, plan: dict[str, JsonValue], ledger: list[LedgerRow]) -> TurnRecord:
        """``turn`` as the kernel's ``plan`` leaves it (not saved)."""
        kernel_status = plan.get("status")
        licence = licence_of(plan)
        pending: PendingStep | None = None
        problems = diagnostics(plan)
        status: TurnStatus
        if kernel_status == "needs_tools" and turn.parent is None:
            # a message turn issues nothing: the model acts through calls, the reading only affords them
            status, licence = "unread", None
        elif kernel_status == "needs_tools":
            step = step_of(plan)
            if step is None:
                status, problems = "repair", [*problems, "the kernel issued no readable tool step"]
            else:
                tool, args, step_id, needs_confirmation = step
                canon = keys.canonical(args)
                occurrence = sum(
                    1 for r in ledger if r.verdict == "committed" and r.tool == tool and keys.canonical(r.args) == canon
                )
                pending = PendingStep(
                    step_id=step_id,
                    tool=tool,
                    args=args,
                    effect_key=keys.effect_key(turn.turn_id, tool, args, occurrence),
                    needs_confirmation=needs_confirmation,
                )
                status = "licensed"
        elif kernel_status == "answered":
            status = "done" if licence else "answered"
        elif kernel_status == "repair" and turn.parent is None:
            # the kernel cannot run a message turn it was never asked to run ("it" after a direct call has no turn
            # in its window): the reading still affords the calls, the diagnostics stay a note, not a failure
            status, licence = "unread", None
        elif kernel_status == "repair":
            status = "repair"
        else:
            status, licence = "unread", None
        return turn.model_copy(
            update={
                "plan": plan,
                "status": status,
                "licence": licence,
                "pending": pending,
                "phrase": phrase_of(plan) or turn.phrase,
                "issued": _issued(turn.issued, pending),
                "problems": problems,
                "frames": with_offer(frames_of(plan), turn.offer is not None),
            }
        )

    async def _settle(self, before: str, turn: TurnRecord, verdict: Verdict | None) -> TurnRecord:
        """Save ``turn``; a turn that has just finished with a phrase records its one use (once: ``turn.use``)."""
        outcome = self._outcome(turn, verdict) if before not in FINISHED and turn.use is None else None
        if outcome is not None and turn.phrase is not None:
            turn = turn.model_copy(update={"use": outcome})
            await self._store.record_use(
                UseRecord(
                    bonfire_id=turn.bonfire_id,
                    phrase=turn.phrase,
                    speaker=turn.speaker,
                    platform=turn.platform,
                    chat=turn.chat,
                    at=_now(),
                    outcome=outcome,
                )
            )
        await self._store.save_turn(turn)
        return turn

    @staticmethod
    def _outcome(turn: TurnRecord, verdict: Verdict | None) -> UseOutcome | None:
        if turn.status in ("answered", "done"):
            return "ok"
        if turn.status != "repair":
            return None
        kind: JsonValue = verdict if verdict not in (None, "committed") else None
        failure = (turn.plan or {}).get("failure")
        if kind is None and isinstance(failure, dict):
            kind = failure.get("kind")
        if kind == "declined":
            return "declined"
        if kind in ("precondition_failed", "tool_error"):
            return "failed"  # a tool_error is final: no adapter retries it
        return None  # a repair no verdict caused is not a use's outcome

    # -- the view ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _plan_id(turn: TurnRecord) -> str | None:
        plan_id = (turn.plan or {}).get("plan_id")
        return plan_id if isinstance(plan_id, str) else None

    async def _waiting_view(self, turn: TurnRecord, group: Group) -> TurnView:
        """``_view``, and on a message turn the phrase of the speaker waiting at a step that runs in the agent."""
        view = self._view(turn, group)
        if turn.parent is not None or turn.speaker is None:
            return view
        waiting = await self._store.waiting_turn(turn)
        if waiting is None or waiting.pending is None:
            return view
        pending = waiting.pending
        step = pending.tool + (f" {keys.canonical(pending.args)}" if pending.args else "")
        note = (
            f" A phrase of theirs is waiting at its step {step} ({waiting.turn_id}): "
            "continue it (phrase_continue) or drop it."
        )
        return view.model_copy(
            update={"waiting": Waiting(turn_id=waiting.turn_id, step=pending), "context": view.context + note}
        )

    def _view(self, turn: TurnRecord, group: Group) -> TurnView:
        plan = turn.plan or {}
        return TurnView(
            turn_id=turn.turn_id,
            bonfire_id=turn.bonfire_id,
            plan_id=self._plan_id(turn),
            platform=turn.platform,
            group=turn.group,
            chat=turn.chat,
            speaker=turn.speaker,
            status=turn.status,
            unread=group.unread if turn.licence is None else None,
            licence=turn.licence,
            pending=turn.pending,
            answer=plan.get("answer") if turn.status in ("answered", "done") else None,
            repair="; ".join(turn.problems) or "the kernel could not run the turn" if turn.status == "repair" else None,
            problems=turn.problems,
            phrase=turn.phrase,
            frames=turn.frames,
            affordances=turn.affordances,
            native=turn.native,
            parent=turn.parent,
            offer=turn.reply.offer if turn.reply is not None and turn.reply.answer == YES else None,
            context=self._context(turn),
        )

    @staticmethod
    def _context(turn: TurnRecord) -> str:
        note = TurnEngine._reading(turn)
        if turn.reply is not None:
            note = f"{note} {offer_note(turn.reply, CONFIRM in turn.affordances)}"
        if turn.parent is None and turn.affordances:
            others = [n for n in turn.affordances if n not in turn.native]
            if others:
                note = f"{note} Afforded this turn: {', '.join(others)}."
            if turn.native:
                note = f"{note} Tools you hold, afforded this turn: {', '.join(turn.native)} (call them natively)."
        return note

    @staticmethod
    def _reading(turn: TurnRecord) -> str:
        phrase = f" with the phrase {turn.phrase}" if turn.phrase else ""
        steps = ", ".join(turn.licence or []) or "none"
        if turn.status == "unread":
            if turn.frames:
                return f"The kernel read this message as {frames_line(turn.frames)}; it runs nothing itself."
            return UNREAD_CONTEXT
        if turn.status == "licensed":
            pending = turn.pending
            now = (
                f"Pending: {pending.tool} {keys.canonical(pending.args)}."
                if pending is not None
                else "No step is pending until the kernel answers again."
            )
            return f"The kernel read this turn{phrase}. Its steps: {steps}. {now}"
        if turn.status == "done":
            return f"The kernel read this turn{phrase} and its steps ran: {steps}."
        if turn.status == "answered":
            return f"The kernel answered this turn{phrase}."
        return f"The kernel read this turn{phrase} but could not finish it: {'; '.join(turn.problems)}"
