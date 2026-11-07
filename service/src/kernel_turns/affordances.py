"""What a turn affords the model: pure rules over the kernel's frames and the group's catalogue.

* a frame of kind ``tool`` or ``phrase`` affords the tool or phrase it names;
* a read-only tool (catalogue ``x-side-effect: false``) is always afforded; every catalogue tool when the group is
  ``open``;
* an ``editor`` frame affords ``teach_phrase``; a ``yes`` reply frame affords ``confirm_phrase`` when the speaker has a
  staged draft waiting (``confirmable``);
* the agent's offer to teach is an open editor frame on its own turn (``offer_frame``, construction ``offer``) that
  affords nothing there; the same speaker's ``yes`` on their next message turn composes with it (``offer_reply``) and
  affords ``teach_phrase`` (``offered``), a ``no`` closes it.

``open_frames`` is the context the next turn of the same speaker carries: the previous turn's tool and phrase
frames that nothing has run for yet. ``render_form`` fills a taught phrase's form for a phrase call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from kernel_turns import keys
from kernel_turns.models import (
    ChildCall,
    Frame,
    JsonValue,
    LedgerRow,
    OfferReply,
    ReplyName,
    Runner,
    TurnRecord,
    UnreadSetting,
)

TEACH = "teach_phrase"
CONFIRM = "confirm_phrase"
#: the catalogue tool that is a pause for the agent's own work (answered with phrase_continue), never a native tool
THINK = "think"
#: the reply frames a "yes" and a "no" read as (the kernel's base rows)
YES = "yes"
NO = "no"
#: the construction of the open editor frame an offer to teach adds to its turn
OFFER = "offer"
#: the most context frames the kernel's plan route takes
MAX_CONTEXT = 8
_SLOT = re.compile(r"\{([^{}]*)\}")


def catalogue_rows(catalogue: Mapping[str, JsonValue] | None) -> list[dict[str, JsonValue]]:
    rows = catalogue.get("tools") if catalogue is not None else None
    return [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict) and "name" in row]


def tool_row(catalogue: Mapping[str, JsonValue] | None, name: str) -> dict[str, JsonValue] | None:
    return next((row for row in catalogue_rows(catalogue) if row.get("name") == name), None)


def runner_of(row: Mapping[str, JsonValue] | None) -> Runner:
    """Where a catalogue tool runs: always the agent."""
    return "agent"


def native_of(names: Iterable[str], catalogue: Mapping[str, JsonValue] | None) -> list[str]:
    """The ``names`` that are catalogue tools: every one runs in the agent, which calls them natively."""
    return [n for n in names if n != THINK and tool_row(catalogue, n) is not None]


def afford(
    frames: list[Frame],
    catalogue: Mapping[str, JsonValue] | None,
    unread: UnreadSetting,
    confirmable: bool,
    *,
    offered: bool = False,
) -> list[str]:
    """The names the turn affords, in a stable order: the frames' tools and phrases, the catalogue's read-only (or, when
    open, every) tools, then the editor (an editor frame the kernel read, or ``offered``: the speaker's yes to an open
    offer; an offer's own frame affords nothing on its turn) and the confirmation."""
    names: list[str] = []

    def add(name: str) -> None:
        if name not in names:
            names.append(name)

    for frame in frames:
        if frame.kind in ("tool", "phrase"):
            add(frame.name)
    for row in catalogue_rows(catalogue):
        if row["name"] != THINK and (unread == "open" or row.get("x-side-effect") is False):
            add(str(row["name"]))  # think is a pause inside a phrase (phrase_continue), never offered as a call
    if offered or any(frame.kind == "editor" and frame.construction != OFFER for frame in frames):
        add(TEACH)
    if confirmable and any(frame.kind == "reply" and frame.name == YES for frame in frames):
        add(CONFIRM)
    return names


def open_frames(frames: Iterable[Frame], ledger: Iterable[LedgerRow], called: Iterable[ChildCall]) -> list[Frame]:
    """The tool and phrase frames of a finished turn that nothing has run for: a tool frame with no committed ledger row
    of that tool (a call that failed, was declined or is in flight leaves it open: the speaker's next words may answer
    it), a phrase frame not called as a child turn. Duplicates across analyses (same name and roles) keep the first;
    at most ``MAX_CONTEXT``, in reading order (the kernel refuses more)."""
    rows = list(ledger)
    names = {c.tool for c in called}
    out: list[Frame] = []
    seen: set[tuple[str, tuple[tuple[str, str | None], ...]]] = set()
    for f in frames:
        if f.kind not in ("tool", "phrase") or f.name in names:
            continue
        if any(r.verdict == "committed" and r.tool == f.name for r in rows):
            continue
        ident = (f.name, tuple(sorted(f.roles.items())))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(f)
    return out[:MAX_CONTEXT]


def offer_frame() -> Frame:
    """The open editor frame an offer to teach adds to the turn it was made on."""
    return Frame(name=TEACH, kind="editor", construction=OFFER, roles={}, complete=False)


def with_offer(frames: Iterable[Frame], offered: bool) -> list[Frame]:
    """``frames`` with exactly one offer frame when the turn holds an offer, none otherwise."""
    kept = [f for f in frames if f.construction != OFFER or f.kind != "editor"]
    return [*kept, offer_frame()] if offered else kept


def reply_of(frames: Iterable[Frame]) -> ReplyName | None:
    """The message's answer by its reply frames: ``yes`` or ``no``; None when it has neither, or reads as both."""
    names = {f.name for f in frames if f.kind == "reply" and f.name in (YES, NO)}
    if names == {YES}:
        return "yes"
    if names == {NO}:
        return "no"
    return None


def offer_reply(previous: TurnRecord | None, speaker: str | None, answer: ReplyName | None) -> OfferReply | None:
    """A turn's answer to the offer on the speaker's previous message turn: only an offer made to ``speaker`` and not
    followed by a draft yet is answered."""
    offer = previous.offer if previous is not None else None
    if previous is None or offer is None or answer is None or speaker is None:
        return None
    if offer.to != speaker or offer.drafted is not None:
        return None
    return OfferReply(turn_id=previous.turn_id, offer=offer, answer=answer)


def offer_note(reply: OfferReply, confirmable: bool) -> str:
    """What the model is told about the speaker's answer to its offer to teach."""
    form = reply.offer.forms[0]
    if reply.answer == NO:
        return f"You offered to teach {form}; they said no, so the offer is declined: do not draft it."
    note = f"You offered to teach {form}; they said yes: draft it with teach_phrase."
    if confirmable:
        note += " They also have a draft waiting for their yes (confirm_phrase): if it is not clear which, ask."
    return note


def has_staged_draft(rows: Iterable[Mapping[str, JsonValue]], speaker: str) -> bool:
    """A kernel construction row of a draft staged by ``speaker``, waiting for their yes."""
    for row in rows:
        provenance = row.get("provenance")
        teacher = provenance.get("teacher") if isinstance(provenance, dict) else None
        if row.get("status") == "staged" and teacher == speaker:
            return True
    return False


def missing_args(row: Mapping[str, JsonValue], args: Mapping[str, JsonValue]) -> str | None:
    """Why ``args`` do not type-check against a catalogue tool's ``input_schema`` (an argument its ``properties`` do
    not declare, a required argument that is missing or not text), or None. An undeclared argument is refused because
    the effect key covers every argument: the verb would ignore it, and the same call would be a second effect."""
    schema = row.get("input_schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict):
        declared = list(properties)
        if any(name not in properties for name in args):
            return f"{row['name']} takes {', '.join(declared) or 'no arguments'}"
    required = schema.get("required") if isinstance(schema, dict) else None
    for name in required if isinstance(required, list) else []:
        if not isinstance(name, str):
            continue
        if name not in args:
            return f"{row['name']} needs {name}"
        if not isinstance(args[name], str):
            return f"{row['name']}: {name} must be text"
    return None


def needs_confirmation(row: Mapping[str, JsonValue]) -> bool:
    """The kernel's rule: confirmation unless the row says the speaker's own turn is consent enough."""
    return row.get("x-consent") != "speaker_turn"


def slot_names(form: str) -> list[str]:
    return [n for raw in _SLOT.findall(form) if (n := raw.strip())]


def render_form(forms: list[str], args: Mapping[str, JsonValue], defaults: Mapping[str, str]) -> tuple[str | None, str]:
    """``(text, why)``: the first form whose slots all have a value (the call's argument, else the teacher's default),
    filled in; ``text`` is None, with ``why`` saying which slot has none, when no form can be filled."""
    why = "the phrase has no form"
    lowered = {k.lower(): v for k, v in args.items()}
    for form in forms:
        values: dict[str, str] = {}
        missing: str | None = None
        for name in slot_names(form):
            given = args.get(name, lowered.get(name.lower()))
            value = given if isinstance(given, str) and given.strip() else defaults.get(name)
            if value is None:
                missing = name
                break
            values[name] = value
        if missing is None:
            return _SLOT.sub(lambda m, v=values: v[m.group(1).strip()], form), ""
        why = f"{missing} has no value and no default"
    return None, why


def canon_key(tool: str, args: Mapping[str, JsonValue]) -> str:
    return f"{tool}\x1f{keys.canonical(dict(args))}"


def frames_line(frames: list[Frame]) -> str:
    """``take(ticket=JUJ-3); make (open: title) [partial]``: what read which words, bound and open roles."""
    parts: list[str] = []
    for frame in frames:
        bound = ", ".join(f"{k}={v}" for k, v in frame.roles.items() if v is not None)
        free = ", ".join(k for k, v in frame.roles.items() if v is None)
        text = f"{frame.name}({bound})" if bound else frame.name
        if free:
            text += f" (open: {free})"
        if not frame.complete:
            text += " [partial]"
        if frame.composed_with is not None:
            text += f" [answers {frame.composed_with}]"
        parts.append(text)
    return "; ".join(parts)
