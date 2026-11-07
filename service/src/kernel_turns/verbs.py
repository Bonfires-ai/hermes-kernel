"""The verb book: the group's taught phrases (the kernel's ``GET …/constructions`` rows of kind ``taught``) joined with
the store's uses.

One ``Verb`` per taught draft: a draft taught with several forms ("brew {X} for {R}" / "brew {X}") is stored as one
row per form (``<name>.<draft_id>.<k>``), all with the same ``provenance.draft_id``; the book shows them as one phrase
with its forms, and its uses are those of every row. ``id`` is the first row's id. ``teacher`` and ``chat`` come from
``provenance``; ``slots`` are the slot names as the teacher wrote them (``provenance.form``; the row's ``form`` holds
them lower-cased); ``steps`` are the tool names of ``meaning`` in order (an op that is no catalogue tool, such as
``answer``, is not a step); ``standing`` sums the rows' uses (``confirmed`` is the most any row has).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel

from kernel_turns.models import JsonValue, UseRecord

#: a ``{Name}`` slot in a written form (memory-kernel ``teach._SLOT``)
_SLOT = re.compile(r"\{([^{}]*)\}")
_STANDING_SUMS = ("uses_ok", "uses_failed", "uses_world")

Row = dict[str, JsonValue]


class Verb(BaseModel):
    id: str
    #: the id of every row of the draft (a frame's ``construction`` names one of them)
    ids: list[str] = []
    forms: list[str]
    #: the teacher's defaults for slots left out (``provenance.request.defaults``)
    defaults: dict[str, str] = {}
    slots: list[str]
    steps: list[str]
    teacher: str | None
    chat: str | None
    status: str
    standing: dict[str, JsonValue]
    #: newest first
    uses: list[UseRecord]


def _provenance(row: Row) -> dict[str, JsonValue]:
    found = row.get("provenance")
    return found if isinstance(found, dict) else {}


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _form_text(row: Row) -> str:
    """The form as the teacher wrote it; rebuilt from the row's parts when provenance has none."""
    written = _text(_provenance(row).get("form"))
    if written is not None:
        return written
    parts = row.get("form")
    out: list[str] = []
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("slot"), str):
            out.append(f"{{{part['slot']}}}")
    return " ".join(out)


def _slots(row: Row) -> list[str]:
    return [name.strip() for name in _SLOT.findall(_form_text(row)) if name.strip()]


def _steps(row: Row, tools: set[str]) -> list[str]:
    meaning = row.get("meaning")
    return [
        p[0]
        for p in (meaning if isinstance(meaning, list) else [])
        if isinstance(p, list) and p and isinstance(p[0], str) and p[0] in tools
    ]


def _defaults(row: Row) -> dict[str, str]:
    request = _provenance(row).get("request")
    found = request.get("defaults") if isinstance(request, dict) else None
    return {k: v for k, v in found.items() if isinstance(v, str)} if isinstance(found, dict) else {}


def _standing(rows: list[Row]) -> dict[str, JsonValue]:
    standings = [s for s in (row.get("standing") for row in rows) if isinstance(s, dict)]

    def ints(key: str) -> list[int]:
        return [v for s in standings if isinstance(v := s.get(key), int) and not isinstance(v, bool)]

    out: dict[str, JsonValue] = {"confirmed": max(ints("confirmed"), default=0)}
    for key in _STANDING_SUMS:
        out[key] = sum(ints(key))
    return out


def verb_book(rows: Iterable[Row], tools: set[str], uses: Iterable[UseRecord]) -> list[Verb]:
    """The verbs of ``rows`` (taught rows only, grouped by draft, in the order the kernel lists them)."""
    drafts: dict[str, list[Row]] = {}
    for row in rows:
        row_id = _text(row.get("id"))
        if row.get("kind") != "taught" or row_id is None:
            continue
        drafts.setdefault(_text(_provenance(row).get("draft_id")) or row_id, []).append(row)
    every_use = list(uses)
    verbs: list[Verb] = []
    for group in drafts.values():
        first = group[0]
        ids = {_text(row.get("id")) for row in group}
        slots: list[str] = []
        for row in group:
            slots += [s for s in _slots(row) if s not in slots]
        mine = sorted((u for u in every_use if u.phrase in ids), key=lambda u: u.at, reverse=True)
        provenance = _provenance(first)
        verbs.append(
            Verb(
                id=str(first["id"]),
                ids=[str(row["id"]) for row in group],
                defaults=_defaults(first),
                forms=[_form_text(row) for row in group],
                slots=slots,
                steps=_steps(first, tools),
                teacher=_text(provenance.get("teacher")),
                chat=_text(provenance.get("chat_id")),
                status=_text(first.get("status")) or "unknown",
                standing=_standing(group),
                uses=mine,
            )
        )
    return verbs


def tool_names(catalogue: dict[str, JsonValue] | None) -> set[str]:
    """The tool names of a catalogue (the file's JSON, or ``GET …/catalogue``'s ``{sha256, tools}``)."""
    rows = catalogue.get("tools") if catalogue is not None else None
    return {
        name
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and isinstance(name := row.get("name"), str)
    }
