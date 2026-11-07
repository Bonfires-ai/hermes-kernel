"""Teaching a phrase, the verb book and each group's catalogue.

Teaching is draft → plain read-back → the teacher's yes → confirm. The read-back is built here from the request, not
from the kernel: ``"{forms joined ' / '}: {body joined ' → '}"`` plus ``" (if left out, {slot} is {default})"`` per
default. A draft the kernel refuses comes back with ``draft_id == ""`` and the kernel's problems verbatim. Only the
draft's teacher may confirm it (the kernel's 403, ``NotTeacher``). A draft marks the open offer to teach on the
teacher's previous turn as followed by it (``TeachOffer.drafted``): that offer is context no more.

A group's catalogue is the agent's tools: installed in ``mk serve`` (``PUT …/catalogue``) and stored once the kernel
accepts it. The turn engine reads the stored copy for what a turn affords.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field, ValidationError

from kernel_turns.affordances import CONFIRM, TEACH
from kernel_turns.groups import GroupFinder
from kernel_turns.mk_client import KernelApi, NotFounded, Refused
from kernel_turns.models import BodyStep, Frame, JsonValue, Runner
from kernel_turns.store import TurnStore
from kernel_turns.turns import UnknownTurn
from kernel_turns.verbs import Verb, tool_names, verb_book

log = logging.getLogger(__name__)


class TeachDraft(BaseModel):
    #: the turn whose reading afforded the editor (``teach_phrase``)
    turn_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    group: str = Field(min_length=1)
    chat: str = Field(min_length=1)
    teacher: str = Field(min_length=1)
    forms: list[str] = Field(min_length=1)
    body: list[BodyStep] = Field(min_length=1)
    defaults: dict[str, str] = Field(default_factory=dict)
    replace: bool = False


class DraftView(BaseModel):
    #: "" when the kernel refused the draft
    draft_id: str
    read_back: str
    problems: list[str]
    #: form text -> the frames the live grammar already reads that form's own renderings as (the kernel's ``competes``)
    competes: dict[str, list[Frame]] = Field(default_factory=dict)


class ConfirmView(BaseModel):
    draft_id: str
    #: the ids of the phrases that went live (one per form)
    phrases: list[str]


class ConfirmRequest(BaseModel):
    platform: str = Field(min_length=1)
    group: str = Field(min_length=1)
    by: str = Field(min_length=1)
    #: the turn whose reading afforded the confirmation (``confirm_phrase``)
    turn_id: str = Field(min_length=1)


def step_text(step: BodyStep) -> str:
    """A body entry as the teacher reads it: a sentence as written, a structured call as ``tool(param=value, …)``."""
    if isinstance(step, str):
        return step
    args = step.get("args")
    given = ", ".join(f"{k}={v}" for k, v in args.items()) if isinstance(args, dict) else ""
    return f"{step.get('call')}({given})"


def read_back(forms: list[str], body: list[BodyStep], defaults: dict[str, str]) -> str:
    text = f"{' / '.join(forms)}: {' → '.join(step_text(s) for s in body)}"
    return text + "".join(f" (if left out, {slot} is {default})" for slot, default in defaults.items())


def merge_sources(agent: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    """The catalogue the kernel installs: the agent's tools, each stamped ``x-runner: agent`` (the kernel's contract
    for a tool the agent runs). A tool whose name an earlier row has is skipped, logged. With no agent source, no
    tools."""
    out: dict[str, JsonValue] = {k: v for k, v in (agent or {}).items() if k not in ("sha256", "tools")}
    out.setdefault("schema_version", "kernel-catalogue/v1")
    taken: set[str] = set()
    merged: list[JsonValue] = []
    for row in _rows(agent):
        name = str(row["name"])
        if name in taken:
            log.warning("kernel_turns.catalogue: tool %s skipped: an earlier row has that name", name)
            continue
        taken.add(name)
        merged.append({**row, "x-runner": "agent"})
    out["tools"] = merged
    return out


def _rows(catalogue: dict[str, JsonValue] | None) -> list[dict[str, JsonValue]]:
    rows = catalogue.get("tools") if catalogue is not None else None
    return [
        r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and isinstance(r.get("name"), str)
    ]


def _strings(value: JsonValue) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _competes(value: JsonValue) -> dict[str, list[Frame]]:
    """The kernel's ``competes`` report; a frame the shape does not fit is dropped."""
    out: dict[str, list[Frame]] = {}
    for form, rows in value.items() if isinstance(value, dict) else []:
        frames: list[Frame] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                frames.append(Frame.model_validate(row))
            except ValidationError:
                continue
        if frames:
            out[form] = frames
    return out


#: what a person is told when they teach before the bonfire exists
NOT_YET = "I can start learning phrases once the first conversation has been saved."


class Teaching:
    def __init__(self, mk: KernelApi, store: TurnStore, groups: GroupFinder) -> None:
        self._mk = mk
        self._store = store
        self._groups = groups
        self._installing: dict[tuple[str, str], asyncio.Lock] = {}

    async def _afforded(
        self, turn_id: str, platform: str, group: str, name: str, speaker: str, chat: str | None = None
    ) -> None:
        """Refuse (409) unless ``name`` is afforded by that turn of this group, spoken by ``speaker`` (in ``chat``)."""
        turn = await self._store.get_turn(turn_id)
        if turn is None:
            raise UnknownTurn(f"no turn {turn_id!r}")
        mine = turn.speaker == speaker and (chat is None or turn.chat == chat)
        if (turn.platform, turn.group) != (platform, group) or not mine or name not in turn.affordances:
            afforded = ", ".join(turn.affordances) or "nothing"
            raise Refused(f"{name} is not afforded by this turn (afforded: {afforded})")

    async def begin(self, req: TeachDraft) -> DraftView:
        group = await self._groups.find(req.platform, req.group)
        await self._afforded(req.turn_id, req.platform, req.group, TEACH, req.teacher, req.chat)
        body: dict[str, JsonValue] = {
            "forms": list(req.forms),
            "body": list(req.body),
            "defaults": dict(req.defaults),
            "teacher": req.teacher,
            "chat_id": req.chat,
            "replace": req.replace,
        }
        try:
            out = await self._mk.teach(group.bonfire_id, body)
        except NotFounded as exc:
            raise Refused(NOT_YET) from exc
        draft_id = out.get("draft_id")
        if isinstance(draft_id, str) and draft_id:
            await self._follow_offer(req.turn_id, draft_id)
        return DraftView(
            draft_id=draft_id if isinstance(draft_id, str) else "",
            read_back=read_back(req.forms, req.body, req.defaults),
            problems=_strings(out.get("problems")),
            competes=_competes(out.get("competes")),
        )

    async def _follow_offer(self, turn_id: str, draft_id: str) -> None:
        """Mark the open offer on the teacher's previous message turn as followed by this draft. Written without
        the turn engine's lock on that turn: the offer's turn is over by the time its answer is drafted, so only a
        redelivery of it races, and a lost mark costs at most a second draft offered, never an effect."""
        turn = await self._store.get_turn(turn_id)
        previous = await self._store.previous_turn(turn) if turn is not None else None
        offer = previous.offer if previous is not None else None
        if turn is None or previous is None or offer is None or offer.to != turn.speaker or offer.drafted is not None:
            return
        await self._store.save_turn(
            previous.model_copy(update={"offer": offer.model_copy(update={"drafted": draft_id})})
        )

    async def confirm(self, draft_id: str, req: ConfirmRequest) -> ConfirmView:
        found = await self._groups.find(req.platform, req.group)
        await self._afforded(req.turn_id, req.platform, req.group, CONFIRM, req.by)
        try:
            out = await self._mk.confirm(found.bonfire_id, draft_id, req.by)  # NotTeacher for anyone but the teacher
        except NotFounded as exc:
            raise Refused(NOT_YET) from exc
        specs = out.get("specs")
        phrases = [
            spec_id
            for spec in (specs if isinstance(specs, list) else [])
            if isinstance(spec, dict) and isinstance(spec_id := spec.get("id"), str)
        ]
        return ConfirmView(draft_id=draft_id, phrases=phrases)

    async def verbs(self, platform: str, group: str) -> list[Verb]:
        found = await self._groups.find(platform, group)
        rows = await self._mk.constructions(found.bonfire_id)
        catalogue = await self._store.catalogue(platform, group)
        if catalogue is None:  # never PUT through this service: the catalogue the kernel has installed
            catalogue = await self._mk.get_catalogue(found.bonfire_id)
        uses = await self._store.uses(found.bonfire_id)
        return verb_book(rows, tool_names(catalogue), uses)

    async def put_catalogue(
        self, platform: str, group: str, catalogue: dict[str, JsonValue], source: Runner = "agent"
    ) -> None:
        """Install the agent's catalogue in the kernel, then store it as the agent source and as the merged catalogue.
        Stored only after the kernel accepts it, so a failed install is retried. A catalogue identical to the one the
        kernel last accepted installs nothing: every install drops the bonfire's in-flight plans. A bonfire that does
        not exist yet (``NotFounded``) keeps the agent source and answers as if installed; the merged catalogue is
        stored only once the kernel has it, so the same catalogue is installed after founding."""
        found = await self._groups.find(platform, group)
        async with self._installing.setdefault((platform, group), asyncio.Lock()):
            held = await self._store.source(platform, group, source)
            merged = merge_sources(catalogue)
            installed = await self._store.catalogue(platform, group)  # stored only once the kernel has taken it
            if held is not None and _by_name(held) == _by_name(catalogue) and installed is not None:
                return
            try:
                await self._mk.put_catalogue(found.bonfire_id, merged)
            except NotFounded:
                await self._store.save_source(platform, group, source, catalogue)
                return
            await self._store.save_source(platform, group, source, catalogue)
            await self._store.save_catalogue(platform, group, merged)


def _by_name(catalogue: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The catalogue with its rows sorted by name: the same tool set in another order is no change."""
    rows = sorted(_rows(catalogue), key=lambda r: str(r.get("name")))
    return {**catalogue, "tools": list(rows)}
