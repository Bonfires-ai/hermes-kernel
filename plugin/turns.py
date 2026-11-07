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
session id, matched to the turn by the sender's id and the text, each message used once. The observer does no network and no disk (Hermes puts no time limit on that hook) and always returns
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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .client import ServiceError
from .provider import conversation_id
from .shared import get_shared

logger = logging.getLogger(__name__)

#: the whole read, passed to `begin` as its timeout
READ_BUDGET_S = 10.0

#: the block stays well under Hermes' 10 000-char spill: role values and the number of lines are capped
_MAX_BLOCK_CHARS = 6000
_MAX_ROLE_CHARS = 120
_MAX_FRAME_LINES = 12
#: the accepted offer's forms, body and defaults in the never-cut lines (still bounded)
_MAX_OFFER_CHARS = 1500
#: a phrase's name, id and step tool are shown up to this long
_MAX_NAME_CHARS = 80
#: a recorded message's text is kept up to this long
_MAX_INBOUND_CHARS = 4000
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
    """The tool catalogue (`AgentSource`)."""

    def sync_soon(self) -> None: ...

    def unavailable(self) -> set[str]: ...

    def real_names(self) -> dict[str, str]: ...


class Log(Protocol):
    def block(self, session_id: str) -> str: ...


@dataclass
class Inbound:
    """A message as the gateway dispatched it (before it was authorised: it may never become a turn)."""

    message_id: str
    text: str
    speaker: str | None
    at: str
    #: the platform's chat id
    chat: str
    #: the sender's platform id, to match the hook's `sender_id`
    user_id: str | None = None
    #: a turn took this message: no other turn takes it
    used: bool = False


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
    #: what the reading afforded the model, and the afforded tools it calls natively (`None`: not read)
    affordances: list[str] | None = None
    native: set[str] = field(default_factory=set)


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


def _short(value: object, limit: int = _MAX_NAME_CHARS) -> str:
    """One line of at most `limit` characters."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    hidden: Collection[str] = (),
    names: Mapping[str, str] | None = None,
    natively: Mapping[str, str] | None = None,
) -> str:
    """What the model may run and what the kernel read in the message, one short block ("" when it read nothing:
    no frames, no waiting phrase, no offer). The lines naming the calls and the teaching tools come first and are
    never cut; the frame lines get the budget that is left (`_MAX_BLOCK_CHARS`), whole lines only. `hidden`: the agent's
    own tools Hermes does not offer now (left out of the lines); `names`: phrase names by child turn id, for the
    waiting line; `natively`: a short-named row's real name (`AgentSource.real_names`), to call it by."""
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
    afford = [a for a in afford if not (a in own and a in hidden)]
    lines: list[str] = []
    native = [a for a in afford if a in own]
    calls = [a for a in afford if a not in OWN_TOOLS and a not in native]
    if calls:
        lines.append(
            f"You may call: {', '.join(calls)} (with run_phrase). A call changes the work once; ask the person for "
            "a role that is not given instead of guessing it."
        )
    if native:
        real = natively or {}
        said = [f"{a} (call it as {real[a]})" if a in real else a for a in native]
        lines.append(f"Your own tools, afforded too: {', '.join(said)}. Call them directly, not with run_phrase.")
    if isinstance(waiting, dict):
        child = str(waiting.get("turn_id"))
        lines.append(waiting_line(waiting, (names or {}).get(child)))
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


def waiting_line(waiting: JsonObject, name: str | None = None) -> str:
    """The speaker's phrase waiting at a step that runs in the agent (`TurnView.waiting`): the phrase (`name` when
    known, and its id) and the step's tool. Never the step's input or args: they may be bound from an earlier result
    (a web page's text), and this block is stored with the person's message and replayed. `phrase_continue` with
    empty text hands the step back, fenced as an untrusted result."""
    step = waiting.get("step")
    step = step if isinstance(step, dict) else {}
    child = _short(waiting.get("turn_id"))
    phrase = f'"{_short(name)}" ({child})' if name else child
    tool = step.get("tool")
    at = f"its {_short(tool)} step" if isinstance(tool, str) and tool else "its next step"
    return (
        f"A phrase this person started, {phrase}, is waiting at {at}: read the step with the phrase_continue tool "
        f'(phrase "{child}", empty text; that shows it and runs nothing), then continue it with phrase_continue (its '
        "text is your result of that step), or drop it when they do not want it any more "
        '(phrase_continue with "drop": true).'
    )


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


def repair_block() -> str:
    """A fixed line: the kernel's repair text may quote a step's outcome (a tool's result), so none of it is shown."""
    return "For this message only, the kernel could not finish reading it: answer the person normally."


class KernelTurns:
    """The `pre_gateway_dispatch` observer and the `pre_llm_call` hook."""

    def __init__(
        self,
        client: TurnsApi,
        *,
        source: Source | None = None,
        call_log: Log | None = None,
    ) -> None:
        self._client = client
        self._source = source
        self._call_log = call_log
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
            text=_as_str(getattr(event, "text", None))[:_MAX_INBOUND_CHARS],
            speaker=speaker_of(event),
            at=_iso(getattr(event, "timestamp", None)),
            chat=chat,
            user_id=_as_str(getattr(source, "user_id", None)) or None,
        )
        with self._lock:
            events = self._inbound.pop(key, None) or []
            events = [*events, inbound][-_EVENTS_PER_CHAT:]
            self._inbound[key] = events
            while len(self._inbound) > _SESSIONS_KEPT:
                self._inbound.popitem(last=False)

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

    def _take(self, conversation: str, user_text: str, sender_id: str) -> Inbound | None:
        """The message this turn answers, marked used: among the sender's unused messages (all unused ones when the
        sender or the messages carry no id) the one with equal text, else, for a known sender, their oldest one with no text (a voice
        message). Taking one retires the sender's older ones. `None`: no message of this person's is waiting, so never another person's."""
        with self._lock:
            events = [e for e in self._inbound.get(conversation) or () if not e.used]
            if sender_id:
                events = [e for e in events if e.user_id == sender_id]
            wanted = user_text.strip()
            found = next((e for e in events if e.text.strip() == wanted), None)
            if found is None and sender_id:
                found = next((e for e in events if not e.text.strip()), None)  # a voice message: no text
            if found is not None:
                found.used = True
                if found.user_id:  # this person's older messages were never addressed: they match no later turn
                    for e in events[: events.index(found)]:
                        e.used = e.used or e.user_id == found.user_id
            return found

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
        sender_id = _as_str(kwargs.get("sender_id"))
        inbound = self._take(conversation, user_text, sender_id)
        if inbound is not None:
            message_id, at, speaker = inbound.message_id, inbound.at, inbound.speaker
        else:
            message_id, at, speaker = hermes_turn or uuid.uuid4().hex, _iso(None), None
        text = user_text
        speaker = speaker or info.get("user_name") or sender_id or "user"
        speaker = _as_str(speaker)
        participants = self._note_speaker(conversation, speaker)
        local = LocalTurn(hermes_turn, session_id, platform, conversation, conversation, speaker)
        self._remember(local)
        if self._source is not None:
            try:
                self._source.sync_soon()
            except Exception:  # noqa: BLE001 — the catalogue sync costs a later turn at worst, never this block
                self._warn("source", "could not start the tool sync", exc_info=True)
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
        raw_native = view.get("native")
        local.affordances = [a for a in affordances if isinstance(a, str)] if isinstance(affordances, list) else None
        local.native = {a for a in raw_native if isinstance(a, str)} if isinstance(raw_native, list) else set()
        drafting = isinstance(affordances, list) and "teach_phrase" in affordances
        calls = self._call_log.block(local.session_id) if drafting and self._call_log is not None else ""
        with self._lock:
            names = dict(self._phrases)
        hidden, natively = self._tool_names()
        block = frames_block(view, hidden=hidden, names=names, natively=natively)
        lines = [block, calls] if block else []
        repair = view.get("repair")
        if view.get("status") == "repair" and isinstance(repair, str) and repair:
            lines.insert(0, repair_block())
        context = "\n".join(line for line in lines if line)
        return {"context": context} if context else None

    def _tool_names(self) -> tuple[set[str], dict[str, str]]:
        """The agent's tools Hermes does not offer now, and the short-named rows' real names (as of the last read of
        the registry); never costs the turn."""
        if self._source is None:
            return set(), {}
        try:
            return self._source.unavailable(), self._source.real_names()
        except Exception:  # noqa: BLE001 — the block then lists what the service afforded
            self._warn("tools", "could not tell which tools Hermes offers now", exc_info=True)
            return set(), {}

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
