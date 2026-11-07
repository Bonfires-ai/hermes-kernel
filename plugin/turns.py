"""Reading a turn: before the model answers, ask the turn service what the message means and give the model a short
block: the kernel's reading (frames), the names it may run, tools of its own afforded too, a phrase waiting.

## What Hermes hands the hooks

`pre_llm_call` runs once per turn, before the first model call, with `session_id`, `task_id`, `turn_id`,
`user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`, `parent_session_id` and `sender_id`.
There is no agent context, no chat id, no message id and no speaker name. The returned `{"context": ...}` is appended
to this turn's user message, never the system prompt, and stored with it, so every API call of the turn sends the same
bytes and later turns replay it. A context over Hermes' output spill (10 000 characters) is spilled to a file, so the
block is bounded (`_MAX_BLOCK_CHARS`). The hook is held to `READ_BUDGET_S`: a late result is dropped.

So the chat, the message id and the speaker come from `pre_gateway_dispatch` (`event`, a `MessageEvent`), recorded
per conversation (the session record's `conversation_id`, the same string capture uses) and found again from the
session id. The observer does no network and no disk (Hermes puts no time limit on that hook) and always returns
`None`. With no gateway (the CLI) no such hook fires: the message id is Hermes' `turn_id` and the speaker is the
session record's `user_name`, then `sender_id`, then "user".

## The turn

- Only a `primary` session reads: a delegated subagent (`agent_context` in the session record, or a
  `parent_session_id`) gets no call and no block. A session with no record yet is primary.
- The decision is on frames, not status: a plain question comes back `unread` with real frames, because the plan
  cannot answer from memory (recall is the provider's `prefetch`). Any frames, a waiting phrase, an offer or a repair
  give a block; nothing of those gives none. `native` alone is no reason for a block. `problems` go to the debug log,
  never into the block: they would mislead the model.
- Once per Hermes turn: the result is kept by `turn_id`; a second `pre_llm_call` of the same turn returns it.

Every hook catches everything: on any failure there is no block and the turn goes on.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .client import ServiceError
from .provider import conversation_id
from .shared import get_shared

logger = logging.getLogger(__name__)

#: the whole read, passed to `begin` as its timeout
READ_BUDGET_S = 10.0
#: the step tool a phrase hands back to the model
THINK = "think"
#: agent values a phrase's child turn carried: `(tool, result)` pairs
Carried = Collection[tuple[str, object]]

#: the block stays well under Hermes' 10 000-char spill: role values and the number of lines are capped
_MAX_BLOCK_CHARS = 6000
_MAX_ROLE_CHARS = 120
_MAX_FRAME_LINES = 12
#: the accepted offer's forms, body and defaults in the never-cut lines (still bounded)
_MAX_OFFER_CHARS = 1500
#: a waiting think step's instruction is shown up to this long (a longer one may be a result)
_MAX_INSTRUCTION_CHARS = 200
#: the two affordances that are tools of their own, not phrases to run
OWN_TOOLS = ("teach_phrase", "confirm_phrase")

# Bounded: a long-lived process must not keep one entry per turn forever.
_TURNS_KEPT = 256
_SESSIONS_KEPT = 512
_EVENTS_PER_CHAT = 8
_PARTICIPANTS_KEPT = 32

JsonObject = dict[str, Any]


class TurnsApi(Protocol):
    """What the plugin calls on the turn service."""

    def begin(self, body: JsonObject, *, timeout: float = ...) -> JsonObject: ...


class Source(Protocol):
    def sync_soon(self) -> None: ...


class Log(Protocol):
    def block(self, session_id: str) -> str: ...


@dataclass(frozen=True)
class Inbound:
    """A message as the gateway dispatched it."""

    message_id: str
    text: str
    speaker: str | None
    at: str
    #: the platform's chat id
    chat: str


@dataclass
class LocalTurn:
    """The plugin's copy of one Hermes turn."""

    hermes_turn: str
    session_id: str
    platform: str
    group: str
    chat: str
    speaker: str | None
    #: the turn service's id for this message's turn; `None` while it did not answer
    turn_id: str | None = None
    context: dict[str, str] | None = None
    #: the speaker's phrases waiting at a step that runs in the agent, `{turn_id, step}` by child turn id
    waiting: dict[str, JsonObject] = field(default_factory=dict)
    #: the phrases continued in this turn (an unnamed continue never goes to another one)
    continued: set[str] = field(default_factory=set)


def _as_str(value: Any) -> str:
    """A Hermes field as a plain string (`source.platform` is an enum: its `.value`)."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def speaker_of(event: Any) -> str | None:
    """`source.user_name`, else the platform's own handle (`raw_message.from_user.username`) when present, else
    `source.user_id`."""
    source = getattr(event, "source", None)
    sender = getattr(getattr(event, "raw_message", None), "from_user", None)
    for candidate in (
        getattr(source, "user_name", None),
        getattr(sender, "username", None),
        getattr(source, "user_id", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo is None else value).isoformat()
    return datetime.now(UTC).isoformat()


def message_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = [p.get("text") for p in user_message if isinstance(p, dict)]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def carries(text: str, carried: Carried) -> bool:
    """Whether `text` carries one of the agent values `carried`."""
    probe = json.dumps(text, ensure_ascii=False)
    for _tool, result in carried:
        needle = json.dumps(result, ensure_ascii=False)[1:-1]
        if needle and needle in probe:
            return True
    return False


def _role(role: str, value: object) -> str:
    if isinstance(value, str) and value:
        shown = value if len(value) <= _MAX_ROLE_CHARS else value[: _MAX_ROLE_CHARS - 1] + "…"
        return f"{role}: {shown}"
    return f"{role}: not given"


def _frame_line(frame: JsonObject) -> str:
    roles = frame.get("roles")
    given = [_role(str(k), v) for k, v in roles.items()] if isinstance(roles, dict) else []
    line = f"{frame.get('name')} ({', '.join(given)})" if given else str(frame.get("name"))
    line += " — complete" if frame.get("complete") else " — partial"
    kind = frame.get("kind")
    if kind in ("phrase", "editor", "reply", "question"):
        line += f" [{kind}]"
    follows = frame.get("composed_with")
    if isinstance(follows, str) and follows:
        line += f", continues your earlier {follows}"
    return line


def reading_text(frame: JsonObject) -> str:
    """``who-be-x(x: Tavish)``: what a frame reads as, without its completeness."""
    roles = frame.get("roles")
    given = [_role(str(k), v) for k, v in roles.items()] if isinstance(roles, dict) else []
    return f"{frame.get('name')}({', '.join(given)})" if given else str(frame.get("name"))


def frames_block(
    view: JsonObject,
    *,
    names: Mapping[str, str] | None = None,
    carried: Callable[[str], Carried] | None = None,
) -> str:
    """What the model may run and what the kernel read in the message, one short block ("" when it read nothing:
    no frames, no waiting phrase, no offer). The lines naming the calls and the teaching tools come first and are
    never cut; the frame lines get the budget that is left (`_MAX_BLOCK_CHARS`), whole lines only. `names`: phrase
    names by child turn id, and `carried`: a child turn's agent values, for the waiting line."""
    raw = view.get("frames")
    frames = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
    waiting = view.get("waiting")
    offer = view.get("offer")
    if not frames and not isinstance(waiting, dict) and not isinstance(offer, dict):
        return ""
    raw_native = view.get("native")
    own = {a for a in raw_native if isinstance(a, str)} if isinstance(raw_native, list) else set()
    raw_afford = view.get("affordances")
    afford = [a for a in raw_afford if isinstance(a, str)] if isinstance(raw_afford, list) else []
    lines: list[str] = []
    native = [a for a in afford if a in own]
    calls = [a for a in afford if a not in OWN_TOOLS and a not in native]
    if calls:
        lines.append(
            f"You may call: {', '.join(calls)} (with run_phrase). A call changes the work once; ask the person for "
            "a role that is not given instead of guessing it."
        )
    if native:
        lines.append(f"Your own tools, afforded too: {', '.join(native)}. Call them directly, not with run_phrase.")
    if isinstance(waiting, dict):
        child = str(waiting.get("turn_id"))
        lines.append(waiting_line(waiting, (names or {}).get(child), carried(child) if carried is not None else ()))
    if "teach_phrase" in afford and isinstance(offer, dict):
        lines.append(offer_line(offer, also_confirm="confirm_phrase" in afford))
    elif "teach_phrase" in afford:
        lines.append(
            "This message reads as teaching: draft it with the teach_phrase tool (not run_phrase), and read the "
            "read-back to the teacher."
        )
    if "confirm_phrase" in afford:
        lines.append(
            "The teacher said yes: keep the draft with the confirm_phrase tool, confirm_phrase(draft_id) "
            "(not run_phrase)."
        )
    if frames:
        lines += _reading_lines(frames, _MAX_BLOCK_CHARS - sum(len(line) + 1 for line in lines))
    return "\n".join(lines)


def waiting_line(waiting: JsonObject, name: str | None = None, carried: Carried = ()) -> str:
    """The speaker's phrase waiting at a step that runs in the agent (`TurnView.waiting`): the phrase (`name` when
    known, and its id), the step's tool and a think step's short instruction (not one bound from an agent value this
    process saw, `carried`), never its input or args (an earlier step's result: someone else's words, here as the
    person's). `phrase_continue` with empty text hands back a think step whole, fenced."""
    step = waiting.get("step")
    step = step if isinstance(step, dict) else {}
    child = waiting.get("turn_id")
    phrase = f'"{name}" ({child})' if name else str(child)
    tool = step.get("tool")
    at = f"its {tool} step" if isinstance(tool, str) and tool else "its next step"
    args = step.get("args")
    instruction = args.get("instruction") if isinstance(args, dict) and tool == THINK else None
    short = isinstance(instruction, str) and instruction.strip() and len(instruction) <= _MAX_INSTRUCTION_CHARS
    if isinstance(instruction, str) and short and not carries(instruction, carried):
        at += f' ("{" ".join(instruction.split())}")'
    line = (
        f"A phrase this person started, {phrase}, is waiting at {at}: continue it with the phrase_continue tool "
        f'(phrase "{child}"; at a think step, its text is your result of that step), or drop it when they do not want '
        'it any more (phrase_continue with "drop": true).'
    )
    if tool == THINK:
        line += " phrase_continue with empty text shows the whole step first and runs nothing."
    return line


def offer_line(offer: JsonObject, *, also_confirm: bool) -> str:
    """The person said yes to an offer to teach (`TurnView.offer`): what was offered, and that `teach_phrase` drafts
    it now; with a draft of theirs also waiting for their yes, that their yes may be for either."""
    raw_forms = offer.get("forms")
    forms = [f for f in raw_forms if isinstance(f, str)] if isinstance(raw_forms, list) else []
    offered = json.dumps(
        {"forms": forms, "body": offer.get("body"), "defaults": offer.get("defaults") or {}}, ensure_ascii=False
    )
    if len(offered) > _MAX_OFFER_CHARS:
        offered = offered[: _MAX_OFFER_CHARS - 1] + "…"
    first = forms[0] if forms else "the phrase"
    line = (
        f'You offered to teach "{first}" and they said yes: draft it now with the teach_phrase tool (not run_phrase) '
        f"from your offer {offered}, and read the read-back to the teacher."
    )
    if also_confirm:
        line += " They also have a draft waiting for their yes: if it is not clear which one their yes is for, ask."
    return line


def _reading_lines(frames: list[JsonObject], budget: int) -> list[str]:
    """The heading and one line per frame (competing analyses numbered) that fit in `budget` characters, at most
    `_MAX_FRAME_LINES` frames, whole lines in reading order; the frames left out are counted, "(and N more)". Nothing
    when not even the heading, one frame and that count fit."""
    analyses = sorted({int(f.get("analysis") or 0) for f in frames})
    if len(analyses) > 1:
        head = f"The kernel read this message {len(analyses)} ways (they compete; pick the one that fits):"
    else:
        head = "The kernel read this message as:"
    frame_lines: list[str] = []
    for n in analyses:
        frame_lines += [
            (f"  reading {n + 1}: " if len(analyses) > 1 else "  ") + _frame_line(f)
            for f in frames
            if int(f.get("analysis") or 0) == n
        ]
    shown = frame_lines[:_MAX_FRAME_LINES]
    while shown:
        left = len(frame_lines) - len(shown)
        out = [head, *shown, *([f"  (and {left} more)"] if left else [])]
        if sum(len(line) + 1 for line in out) <= budget:
            return out
        shown = shown[:-1]
    return []


def repair_block(repair: str) -> str:
    return f"For this message only, the kernel could not run it as asked: {repair}"


class KernelTurns:
    """The `pre_gateway_dispatch` observer and the `pre_llm_call` hook."""

    def __init__(
        self,
        client: TurnsApi,
        *,
        source: Source | None = None,
        call_log: Log | None = None,
        carried: Callable[[str], Carried] | None = None,
    ) -> None:
        self._client = client
        self._source = source
        self._call_log = call_log
        #: a phrase child turn's agent values, so the waiting line never shows one
        self._carried = carried
        self._lock = threading.Lock()
        self._inbound: OrderedDict[str, list[Inbound]] = OrderedDict()
        self._participants: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
        self._by_turn: OrderedDict[str, LocalTurn] = OrderedDict()
        self._by_session: OrderedDict[str, LocalTurn] = OrderedDict()
        #: phrase names by child turn id (`name_phrase`)
        self._phrases: OrderedDict[str, str] = OrderedDict()
        self._logged: set[str] = set()

    def _warn(self, kind: str, message: str, *args: object, exc_info: bool = False) -> None:
        """One warning per failure kind; the same failure again goes to debug."""
        with self._lock:
            first = kind not in self._logged
            self._logged.add(kind)
        (logger.warning if first else logger.debug)("hermes-kernel: " + message, *args, exc_info=exc_info)

    # -- pre_gateway_dispatch ----------------------------------------------------------------------------------------

    def on_gateway_dispatch(self, event: Any = None, **kwargs: Any) -> None:
        """Record a message by its conversation. An observer: no network, no disk, always `None`, never a verdict."""
        try:
            self._record(event)
        except Exception:  # noqa: BLE001 — recording costs the block at worst, never the dispatch
            self._warn("record", "could not record an inbound message", exc_info=True)
        return None

    def _record(self, event: Any) -> None:
        source = getattr(event, "source", None)
        if source is None:
            return
        platform = _as_str(getattr(source, "platform", None)).lower()
        chat = _as_str(getattr(source, "chat_id", None))
        message_id = _as_str(getattr(event, "message_id", None))
        if not platform or not chat or not message_id:
            return
        key = conversation_id(platform, chat, "")
        inbound = Inbound(
            message_id=message_id,
            text=_as_str(getattr(event, "text", None)),
            speaker=speaker_of(event),
            at=_iso(getattr(event, "timestamp", None)),
            chat=chat,
        )
        with self._lock:
            events = self._inbound.pop(key, None) or []
            events = [*events, inbound][-_EVENTS_PER_CHAT:]
            self._inbound[key] = events
            while len(self._inbound) > _SESSIONS_KEPT:
                self._inbound.popitem(last=False)
        self._note_speaker(key, inbound.speaker)

    def latest(self, conversation: str) -> Inbound | None:
        """The newest message recorded for `conversation`."""
        with self._lock:
            events = self._inbound.get(conversation)
            return events[-1] if events else None

    def _note_speaker(self, conversation: str, speaker: str | None) -> list[str]:
        """Add `speaker` to the chat's ordered set of the last `_PARTICIPANTS_KEPT`; the set, oldest first."""
        with self._lock:
            people = self._participants.pop(conversation, None) or OrderedDict()
            if speaker:
                people.pop(speaker, None)
                people[speaker] = None
                while len(people) > _PARTICIPANTS_KEPT:
                    people.popitem(last=False)
            self._participants[conversation] = people
            while len(self._participants) > _SESSIONS_KEPT:
                self._participants.popitem(last=False)
            return list(people)

    def _inbound_for(self, conversation: str, user_text: str) -> Inbound | None:
        """The message this turn answers: the newest recorded one whose text the turn carries, else the newest."""
        with self._lock:
            events = list(self._inbound.get(conversation) or ())
        for inbound in reversed(events):
            if inbound.text and inbound.text in user_text:
                return inbound
        return events[-1] if events else None

    # -- pre_llm_call ------------------------------------------------------------------------------------------------

    def on_pre_llm_call(self, **kwargs: Any) -> dict[str, str] | None:
        try:
            return self._pre_llm_call(kwargs)
        except Exception:  # noqa: BLE001 — a hook failure costs the block, never the reply
            self._warn("hook", "the turn hook failed; answering without a reading", exc_info=True)
            return None

    def _pre_llm_call(self, kwargs: dict[str, Any]) -> dict[str, str] | None:
        if kwargs.get("parent_session_id"):
            return None  # a delegated subagent: its parent's turn already began
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        shared = get_shared()
        with shared.lock:
            record = shared.sessions.get(session_id)
            info: dict[str, Any] = dict(record) if isinstance(record, dict) else {}
        if info.get("agent_context", "primary") != "primary":
            return None
        hermes_turn = kwargs.get("turn_id")
        hermes_turn = hermes_turn if isinstance(hermes_turn, str) else ""
        with self._lock:
            known = self._by_turn.get(hermes_turn) if hermes_turn else None
            if known is not None and known.session_id == session_id:
                return known.context  # the same Hermes turn again: nothing begins twice
        platform = _as_str(kwargs.get("platform")).lower() or _as_str(info.get("platform")).lower() or "cli"
        conversation = str(info.get("conversation_id") or conversation_id(platform, "", session_id))
        user_text = message_text(kwargs.get("user_message"))
        inbound = self._inbound_for(conversation, user_text)
        if inbound is not None:
            message_id, text, at, speaker = inbound.message_id, inbound.text or user_text, inbound.at, inbound.speaker
        else:
            message_id, text, at, speaker = hermes_turn or uuid.uuid4().hex, user_text, _iso(None), None
        speaker = speaker or info.get("user_name") or kwargs.get("sender_id") or "user"
        speaker = _as_str(speaker)
        participants = self._note_speaker(conversation, speaker)
        local = LocalTurn(hermes_turn, session_id, platform, conversation, conversation, speaker)
        self._remember(local)
        if self._source is not None:
            self._source.sync_soon()
        try:
            local.context = self._read(local, message_id, text, at, participants)
        except ServiceError as exc:
            self._warn("service", "the turn service did not answer (%s); answering without a reading", exc.detail)
            local.context = None
        return local.context

    def _read(
        self, local: LocalTurn, message_id: str, text: str, at: str, participants: list[str]
    ) -> dict[str, str] | None:
        view = self._client.begin(
            {
                "platform": local.platform,
                "group": local.group,
                "chat": local.chat,
                "speaker": local.speaker,
                "message_id": message_id,
                "text": text,
                "at": at,
                "participants": participants,
            },
            timeout=READ_BUDGET_S,
        )
        turn_id = view.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            local.turn_id = turn_id
        problems = view.get("problems")
        if problems:
            logger.debug("hermes-kernel: the read had problems: %s", problems)
        waiting = view.get("waiting")
        child = waiting.get("turn_id") if isinstance(waiting, dict) else None
        local.waiting = {child: waiting} if isinstance(waiting, dict) and isinstance(child, str) and child else {}
        affordances = view.get("affordances")
        drafting = isinstance(affordances, list) and "teach_phrase" in affordances
        calls = self._call_log.block(local.session_id) if drafting and self._call_log is not None else ""
        with self._lock:
            names = dict(self._phrases)
        block = frames_block(view, names=names, carried=self._carried)
        lines = [block, calls] if block else []
        repair = view.get("repair")
        if view.get("status") == "repair" and isinstance(repair, str) and repair:
            lines.insert(0, repair_block(repair))
        context = "\n".join(line for line in lines if line)
        return {"context": context} if context else None

    def name_phrase(self, child_turn_id: str, phrase: str) -> None:
        """The phrase `phrase` runs as `child_turn_id` (its name in a later message's waiting line)."""
        with self._lock:
            self._phrases.pop(child_turn_id, None)
            self._phrases[child_turn_id] = phrase
            while len(self._phrases) > _TURNS_KEPT:
                self._phrases.popitem(last=False)

    def _remember(self, local: LocalTurn) -> None:
        with self._lock:
            if local.hermes_turn:
                self._by_turn[local.hermes_turn] = local
                while len(self._by_turn) > _TURNS_KEPT:
                    self._by_turn.popitem(last=False)
            self._by_session.pop(local.session_id, None)
            self._by_session[local.session_id] = local
            while len(self._by_session) > _SESSIONS_KEPT:
                self._by_session.popitem(last=False)

    def current(self, session_id: str) -> LocalTurn | None:
        """The session's current turn (`None` before the first, or for a session that never read)."""
        with self._lock:
            return self._by_session.get(session_id)
