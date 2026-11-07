"""The turn service's data: turns, the ledger, uses and groups (JSON values as the kernel speaks them)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

#: a JSON value (what the kernel and the platforms exchange); a PEP 695 alias so pydantic resolves the recursion
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

#: a phrase body entry: a sentence the grammar reads, or a structured step ``{"call": tool, "args": {param: value}}``
#: which the kernel compiles; kernel-turns passes it on unchanged
BodyStep = str | dict[str, JsonValue]

#: how a tool step ended (the kernel's ``VERDICTS``)
Verdict = Literal["committed", "precondition_failed", "tool_error", "declined"]
#: what a group does with a turn the kernel did not read: ``open`` leaves it untouched; ``talk_only`` affords only
#: tools whose catalogue ``x-side-effect`` is false
UnreadSetting = Literal["open", "talk_only"]
#: a turn's state. ``licensed``: a program holds the turn (a pending step, or none while the kernel is out of reach);
#: ``answered``: the program ended without naming a tool (an answer from memory, licence ``[]``); ``done``: a program
#: that names tools ran to its end; ``repair``: the kernel could not run it, or a step's verdict ended it
TurnStatus = Literal["unread", "licensed", "answered", "repair", "done"]
UseOutcome = Literal["ok", "failed", "declined"]
#: where a catalogue tool runs, and which catalogue source a row came from: always the agent
Runner = Literal["agent"]


def _utc(value: datetime) -> datetime:
    """A naive time is UTC (MongoDB hands datetimes back naive unless the client is ``tz_aware``)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_utc)]


class PendingStep(BaseModel):
    """The one step the kernel issued and waits on: only this call, with exactly these args, may run."""

    step_id: str
    tool: str
    args: dict[str, JsonValue]
    #: the platform's dedupe key for this effect (``keys.effect_key``): stable across re-plans
    effect_key: str
    needs_confirmation: bool
    #: a step the model's call admitted (``TurnEngine.call``), not one the kernel issued: its report writes the ledger
    #: row and never resumes the kernel
    direct: bool = False
    #: who runs the step: the agent, whose tool the step names (or ``think``)
    runner: Runner = "agent"


class Waiting(BaseModel):
    """A phrase of this speaker, in this chat, paused at a step that runs in the agent."""

    turn_id: str
    step: PendingStep


#: what a frame stands for: a tool verb, a taught phrase, the teaching editor, a reply ("yes"), a question
FrameKind = Literal["tool", "phrase", "editor", "reply", "question"]


class Frame(BaseModel):
    """One reading of part of a message (the kernel's plan ``frames``): a construction that applied, the roles it
    bound (None = open) and whether it read completely. ``analysis`` numbers the competing analyses;
    ``composed_with`` names the open frame of the previous turn this reading answers."""

    name: str
    kind: FrameKind
    construction: str
    roles: dict[str, str | None] = Field(default_factory=dict)
    complete: bool
    analysis: int = 0
    composed_with: str | None = None


#: a reply frame's name: the kernel's base rows read a message as a yes or a no
ReplyName = Literal["yes", "no"]


class TeachOffer(BaseModel):
    """The agent's offer to teach a phrase, recorded on the turn it was made on: no side effect.
    The speaker's yes on their next message turn in the chat affords ``teach_phrase``; a no closes it."""

    forms: list[str] = Field(min_length=1)
    body: list[BodyStep] = Field(min_length=1)
    defaults: dict[str, str] = Field(default_factory=dict)
    #: the speaker of the turn it was made on: only their answer composes with it
    to: str
    at: UtcDatetime
    #: the draft ``teach_phrase`` returned for it on a later turn of that speaker: a drafted offer is context no more
    drafted: str | None = None


class OfferReply(BaseModel):
    """A message turn's yes or no to the open offer on the speaker's previous message turn."""

    #: the turn the offer was made on
    turn_id: str
    offer: TeachOffer
    answer: ReplyName


class ChildCall(BaseModel):
    """A phrase the model called: the child turn that runs its steps on the kernel's own loop."""

    tool: str
    args: dict[str, JsonValue]
    turn_id: str


class TurnRecord(BaseModel):
    """One platform message and what the kernel made of it. ``plan`` is the kernel's latest ``TurnPlan`` JSON."""

    turn_id: str
    platform: str
    group: str
    chat: str
    message_id: str
    speaker: str | None
    text: str
    at: UtcDatetime
    participants: list[str]
    bonfire_id: str
    plan: dict[str, JsonValue] | None
    status: TurnStatus
    #: the tools the program uses; None = no licence (an unread turn, or a repair whose program names no tool)
    licence: list[str] | None
    pending: PendingStep | None
    #: the taught construction the plan applied (a spec id), if any
    phrase: str | None
    #: every step issued for this turn, in issue order (a step issued again under a new step id replaces its entry)
    issued: list[PendingStep] = Field(default_factory=list)
    #: why the turn is what it is when the kernel did not say (the kernel's diagnostics otherwise)
    problems: list[str] = Field(default_factory=list)
    #: the use of a taught phrase this turn recorded, if any: one per turn (a turn retried after a tool_error that was
    #: recorded ``failed`` does not record again)
    use: UseOutcome | None = None
    #: the readings of the message the kernel reported (every analysis), and the tools and phrases they afford
    frames: list[Frame] = Field(default_factory=list)
    affordances: list[str] = Field(default_factory=list)
    #: the afforded tools that run in the agent (the model calls them natively)
    native: list[str] = Field(default_factory=list)
    #: the steps the model's calls were admitted as (``PendingStep.direct``), and the child turns its phrase calls began
    calls: list[PendingStep] = Field(default_factory=list)
    children: list[ChildCall] = Field(default_factory=list)
    #: the turn whose phrase call began this one; None for a platform message
    parent: str | None = None
    #: the offer to teach a phrase the agent made on this turn (``POST …/offers``), and this turn's answer to the open
    #: offer of the speaker's previous turn
    offer: TeachOffer | None = None
    reply: OfferReply | None = None

    def child(self, turn_id: str, message_id: str, text: str) -> TurnRecord:
        """The turn a phrase call of this one begins: the same speaker, chat and time, ``text`` its message."""
        return TurnRecord(
            turn_id=turn_id,
            platform=self.platform,
            group=self.group,
            chat=self.chat,
            message_id=message_id,
            speaker=self.speaker,
            text=text,
            at=self.at,
            participants=list(self.participants),
            bonfire_id=self.bonfire_id,
            plan=None,
            status="unread",
            licence=None,
            pending=None,
            phrase=None,
            parent=self.turn_id,
        )


class LedgerRow(BaseModel):
    """What a step did, written BEFORE the kernel hears of it. One row per ``(turn_id, step_id, verdict)``."""

    turn_id: str
    step_id: str
    tool: str
    args: dict[str, JsonValue]
    #: the tool's result; None when it returned nothing (a committed row always carries this key)
    value: JsonValue = None
    verdict: Verdict
    detail: str | None = None
    at: UtcDatetime


class UseRecord(BaseModel):
    """One finished turn that applied a taught phrase: the verb book's usage and retention numbers."""

    bonfire_id: str
    phrase: str
    speaker: str | None
    platform: str
    chat: str
    at: UtcDatetime
    outcome: UseOutcome


class Group(BaseModel):
    """A group owns one bonfire; its unread setting says what an unread turn may do."""

    platform: str = Field(min_length=1)
    group: str = Field(min_length=1)
    bonfire_id: str = Field(min_length=1)
    unread: UnreadSetting
