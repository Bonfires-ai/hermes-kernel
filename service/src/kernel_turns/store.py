"""Turns, the ledger, uses and catalogues: ``TurnStore`` and its two implementations.

``MongoTurnStore`` keeps four collections in its database (``KT_MONGO_DB``, default ``kernel_turns``):

* ``turns``: one document per turn (``_id`` = the turn id);
* ``ledger``: one row per ``(turn_id, step_id, verdict)`` (a unique index makes ``add_ledger`` idempotent), read back
  in insertion order (``_id``: the driver's ObjectIds grow within the one service process, and across restarts);
* ``uses``: one row per finished use of a taught phrase;
* ``catalogues``: the merged catalogue last installed for each group, and each source's rows as last PUT
  (``agent``).

What the kernel or a tool hands back unchanged (a ``TurnPlan``, a tool's value, a catalogue) is stored as JSON text:
its keys are not ours (``$``-prefixed or dotted names are legal JSON) and it is never queried, only returned whole.
``MemoryTurnStore`` is the in-process implementation every unit test runs on.
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import TypeAdapter
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from kernel_turns.models import JsonValue, LedgerRow, Runner, TurnRecord, UseRecord

Doc = dict[str, object]


class TurnStore(Protocol):
    async def get_turn(self, turn_id: str) -> TurnRecord | None: ...

    async def save_turn(self, turn: TurnRecord) -> None: ...

    async def add_ledger(self, row: LedgerRow) -> None:
        """Idempotent on ``(turn_id, step_id, verdict)``: the same report twice is one row."""

    async def ledger(self, turn_id: str) -> list[LedgerRow]:
        """The turn's rows in the order they were written (issue order)."""

    async def record_use(self, use: UseRecord) -> None: ...

    async def uses(self, bonfire_id: str) -> list[UseRecord]:
        """The bonfire's uses, oldest first."""

    async def recent_turns(self, bonfire_id: str, limit: int) -> list[TurnRecord]:
        """The bonfire's latest turns by message time, newest first."""

    async def previous_turn(self, turn: TurnRecord) -> TurnRecord | None:
        """The speaker's latest earlier message turn in the same chat (a child turn is never one), or None."""

    async def save_catalogue(self, platform: str, group: str, catalogue: dict[str, JsonValue]) -> None: ...

    async def catalogue(self, platform: str, group: str) -> dict[str, JsonValue] | None:
        """The merged catalogue installed for the group (what the turn engine reads)."""

    async def save_source(self, platform: str, group: str, source: Runner, catalogue: dict[str, JsonValue]) -> None:
        """One catalogue source's rows as last PUT (the agent's): the merge is rebuilt from these."""

    async def source(self, platform: str, group: str, source: Runner) -> dict[str, JsonValue] | None: ...

    async def kept_groups(self, source: Runner) -> list[tuple[str, str]]:
        """The (platform, group) pairs that have a stored ``source``."""
        ...

    async def waiting_turn(self, turn: TurnRecord) -> TurnRecord | None:
        """The latest phrase child turn of ``turn``'s speaker in its chat that is licensed with a pending step that runs
        in the agent, other than ``turn``'s own children, begun no later than ``turn``; or None."""


def _ledger_key(row: LedgerRow) -> tuple[str, str, str]:
    return (row.turn_id, row.step_id, row.verdict)


class MemoryTurnStore:
    """``TurnStore`` in process memory. Everything handed out is a copy, as a database's would be."""

    def __init__(self) -> None:
        self._turns: dict[str, TurnRecord] = {}
        self._ledger: list[LedgerRow] = []
        self._uses: list[UseRecord] = []
        self._catalogues: dict[tuple[str, str], dict[str, JsonValue]] = {}
        self._sources: dict[tuple[str, str, str], dict[str, JsonValue]] = {}

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        turn = self._turns.get(turn_id)
        return turn.model_copy(deep=True) if turn is not None else None

    async def save_turn(self, turn: TurnRecord) -> None:
        self._turns[turn.turn_id] = turn.model_copy(deep=True)

    async def add_ledger(self, row: LedgerRow) -> None:
        if all(_ledger_key(r) != _ledger_key(row) for r in self._ledger):
            self._ledger.append(row.model_copy(deep=True))

    async def ledger(self, turn_id: str) -> list[LedgerRow]:
        return [r.model_copy(deep=True) for r in self._ledger if r.turn_id == turn_id]

    async def record_use(self, use: UseRecord) -> None:
        self._uses.append(use.model_copy(deep=True))

    async def uses(self, bonfire_id: str) -> list[UseRecord]:
        return [u.model_copy(deep=True) for u in self._uses if u.bonfire_id == bonfire_id]

    async def recent_turns(self, bonfire_id: str, limit: int) -> list[TurnRecord]:
        turns = [t for t in self._turns.values() if t.bonfire_id == bonfire_id]
        turns.sort(key=lambda t: (t.at, t.turn_id), reverse=True)
        return [t.model_copy(deep=True) for t in turns[: max(limit, 0)]]

    async def previous_turn(self, turn: TurnRecord) -> TurnRecord | None:
        earlier = [
            t
            for t in self._turns.values()
            if t.turn_id != turn.turn_id
            and t.bonfire_id == turn.bonfire_id
            and t.chat == turn.chat
            and t.speaker == turn.speaker
            and t.parent is None
            and t.at < turn.at
        ]
        earlier.sort(key=lambda t: (t.at, t.turn_id), reverse=True)
        return earlier[0].model_copy(deep=True) if earlier else None

    async def save_catalogue(self, platform: str, group: str, catalogue: dict[str, JsonValue]) -> None:
        self._catalogues[(platform, group)] = json.loads(json.dumps(catalogue))

    async def catalogue(self, platform: str, group: str) -> dict[str, JsonValue] | None:
        found = self._catalogues.get((platform, group))
        return json.loads(json.dumps(found)) if found is not None else None

    async def save_source(self, platform: str, group: str, source: Runner, catalogue: dict[str, JsonValue]) -> None:
        self._sources[(platform, group, source)] = json.loads(json.dumps(catalogue))

    async def source(self, platform: str, group: str, source: Runner) -> dict[str, JsonValue] | None:
        found = self._sources.get((platform, group, source))
        return json.loads(json.dumps(found)) if found is not None else None

    async def kept_groups(self, source: Runner) -> list[tuple[str, str]]:
        return sorted((p, g) for p, g, s in self._sources if s == source)

    async def waiting_turn(self, turn: TurnRecord) -> TurnRecord | None:
        waiting = [
            t
            for t in self._turns.values()
            if t.parent is not None
            and t.parent != turn.turn_id
            and t.bonfire_id == turn.bonfire_id
            and t.chat == turn.chat
            and t.speaker == turn.speaker
            and t.status == "licensed"
            and t.pending is not None
            and t.pending.runner == "agent"
            and t.at <= turn.at
        ]
        waiting.sort(key=lambda t: (t.at, t.turn_id), reverse=True)
        return waiting[0].model_copy(deep=True) if waiting else None


_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _decode(raw: object) -> dict[str, JsonValue] | None:
    """A stored catalogue's JSON text as an object, or None when there is none."""
    if not isinstance(raw, str):
        return None
    found = json.loads(raw)
    return _OBJECT.validate_python(found) if isinstance(found, dict) else None


def _dumps(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MongoTurnStore:
    """``TurnStore`` over MongoDB (``AsyncMongoClient``, pymongo>=4.13). Call ``ensure_indexes`` once at start."""

    def __init__(self, client: AsyncMongoClient[Doc], db: str) -> None:
        database = client[db]
        self._turns = database["turns"]
        self._ledger = database["ledger"]
        self._uses = database["uses"]
        self._catalogues = database["catalogues"]

    async def ensure_indexes(self) -> None:
        await self._ledger.create_index(
            [("turn_id", ASCENDING), ("step_id", ASCENDING), ("verdict", ASCENDING)], unique=True, name="ledger_key"
        )
        await self._turns.create_index([("bonfire_id", ASCENDING), ("at", DESCENDING)], name="bonfire_recent")
        await self._uses.create_index([("bonfire_id", ASCENDING)], name="bonfire_uses")
        await self._turns.create_index(
            [
                ("bonfire_id", ASCENDING),
                ("chat", ASCENDING),
                ("speaker", ASCENDING),
                ("status", ASCENDING),
                ("at", DESCENDING),
            ],
            name="speaker_waiting",
        )

    # -- turns -------------------------------------------------------------------------------------------------------

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        doc = await self._turns.find_one({"_id": turn_id})
        return self._turn(doc) if doc is not None else None

    async def save_turn(self, turn: TurnRecord) -> None:
        doc: Doc = turn.model_dump(mode="python", exclude={"plan"})
        doc["_id"] = turn.turn_id
        doc["plan_json"] = _dumps(turn.plan) if turn.plan is not None else None
        await self._turns.replace_one({"_id": turn.turn_id}, doc, upsert=True)

    async def recent_turns(self, bonfire_id: str, limit: int) -> list[TurnRecord]:
        if limit <= 0:
            return []
        cursor = self._turns.find({"bonfire_id": bonfire_id}).sort([("at", DESCENDING), ("_id", DESCENDING)])
        return [self._turn(doc) async for doc in cursor.limit(limit)]

    async def previous_turn(self, turn: TurnRecord) -> TurnRecord | None:
        query: Doc = {
            "bonfire_id": turn.bonfire_id,
            "chat": turn.chat,
            "speaker": turn.speaker,
            "parent": None,
            "at": {"$lt": turn.at},
            "_id": {"$ne": turn.turn_id},
        }
        cursor = self._turns.find(query).sort([("at", DESCENDING), ("_id", DESCENDING)]).limit(1)
        async for doc in cursor:
            return self._turn(doc)
        return None

    @staticmethod
    def _turn(doc: Doc) -> TurnRecord:
        fields = {k: v for k, v in doc.items() if k not in ("_id", "plan_json")}
        raw = doc.get("plan_json")
        fields["plan"] = json.loads(raw) if isinstance(raw, str) else None
        return TurnRecord.model_validate(fields)

    # -- the ledger ----------------------------------------------------------------------------------------------------

    async def add_ledger(self, row: LedgerRow) -> None:
        doc: Doc = row.model_dump(mode="python", exclude={"value"})
        doc["value_json"] = _dumps(row.value)
        try:
            await self._ledger.insert_one(doc)
        except DuplicateKeyError:
            return  # the same report again: the row is already there

    async def ledger(self, turn_id: str) -> list[LedgerRow]:
        cursor = self._ledger.find({"turn_id": turn_id}).sort([("_id", ASCENDING)])
        rows: list[LedgerRow] = []
        async for doc in cursor:
            fields = {k: v for k, v in doc.items() if k not in ("_id", "value_json")}
            raw = doc.get("value_json")
            fields["value"] = json.loads(raw) if isinstance(raw, str) else None
            rows.append(LedgerRow.model_validate(fields))
        return rows

    # -- uses ----------------------------------------------------------------------------------------------------------

    async def record_use(self, use: UseRecord) -> None:
        await self._uses.insert_one(use.model_dump(mode="python"))

    async def uses(self, bonfire_id: str) -> list[UseRecord]:
        cursor = self._uses.find({"bonfire_id": bonfire_id}).sort([("_id", ASCENDING)])
        return [UseRecord.model_validate({k: v for k, v in doc.items() if k != "_id"}) async for doc in cursor]

    # -- catalogues ----------------------------------------------------------------------------------------------------

    async def save_catalogue(self, platform: str, group: str, catalogue: dict[str, JsonValue]) -> None:
        key = f"{platform}\x1f{group}"
        doc: Doc = {"_id": key, "platform": platform, "group": group, "catalogue_json": _dumps(catalogue)}
        await self._catalogues.replace_one({"_id": key}, doc, upsert=True)

    async def catalogue(self, platform: str, group: str) -> dict[str, JsonValue] | None:
        doc = await self._catalogues.find_one({"_id": f"{platform}\x1f{group}"})
        return _decode(doc.get("catalogue_json") if doc is not None else None)

    async def save_source(self, platform: str, group: str, source: Runner, catalogue: dict[str, JsonValue]) -> None:
        key = f"{platform}\x1f{group}\x1f{source}"
        doc: Doc = {
            "_id": key,
            "platform": platform,
            "group": group,
            "source": source,
            "catalogue_json": _dumps(catalogue),
        }
        await self._catalogues.replace_one({"_id": key}, doc, upsert=True)

    async def source(self, platform: str, group: str, source: Runner) -> dict[str, JsonValue] | None:
        doc = await self._catalogues.find_one({"_id": f"{platform}\x1f{group}\x1f{source}"})
        return _decode(doc.get("catalogue_json") if doc is not None else None)

    async def kept_groups(self, source: Runner) -> list[tuple[str, str]]:
        docs = self._catalogues.find({"source": source})
        return sorted([(str(d["platform"]), str(d["group"])) async for d in docs])

    async def waiting_turn(self, turn: TurnRecord) -> TurnRecord | None:
        query: Doc = {
            "bonfire_id": turn.bonfire_id,
            "chat": turn.chat,
            "speaker": turn.speaker,
            "parent": {"$nin": [None, turn.turn_id]},
            "status": "licensed",
            "pending.runner": "agent",
            "at": {"$lte": turn.at},
        }
        cursor = self._turns.find(query).sort([("at", DESCENDING), ("_id", DESCENDING)]).limit(1)
        async for doc in cursor:
            return self._turn(doc)
        return None
