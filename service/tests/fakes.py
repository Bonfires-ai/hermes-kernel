"""A scripted ``mk serve`` for unit tests: ``FakeMk`` implements ``KernelApi`` over fixed programs.

It keeps the kernel 1.5 contract the turn service relies on (``runtime/api.py``): a program's steps are issued one at
a time as ``<plan_id>:<i>:0``; ``committed=`` resolves a step with the same tool and canonical args (each entry once)
and it is never issued; a resume names the pending step with its own tool and args, else 409; a verdict other than
``committed`` makes the plan a repair carrying ``failure``; ``retry`` re-issues the failed step under its step id;
an unknown or forgotten plan is a 404 (``PlanGone``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from kernel_turns.mk_client import KernelBusy, KernelDown, NotFounded, NotTeacher, PlanGone, Refused
from kernel_turns.models import JsonValue, LedgerRow, TurnRecord
from kernel_turns.store import MemoryTurnStore
from kernel_turns.turns import BeginTurn

Args = dict[str, JsonValue]
AT = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)


def _canon(args: Mapping[str, JsonValue]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Script:
    """One text's program: tool steps in order; the taught phrase it applies; the tools that need confirmation."""

    steps: list[tuple[str, Args]]
    phrase: str | None = None
    confirm: frozenset[str] = frozenset()
    answer: JsonValue = None
    #: the plan's ``frames`` (Frame JSON)
    frames: list[JsonValue] = field(default_factory=list)
    #: the kernel read only parts of the message: status ``unread`` with frames and no program
    partial: bool = False


@dataclass
class _Plan:
    plan_id: str
    script: Script
    turn: dict[str, JsonValue]
    resolved: list[JsonValue] = field(default_factory=list)  # one value per step done so far
    failure: dict[str, JsonValue] | None = None

    @property
    def at(self) -> int:
        return len(self.resolved)


class FakeMk:
    def __init__(self, scripts: Mapping[str, Script] | None = None, events: list[str] | None = None) -> None:
        self.scripts = dict(scripts or {})
        self.events = events if events is not None else []
        self.calls: list[tuple[str, JsonValue]] = []
        self.plans: dict[str, _Plan] = {}
        self.down = False
        self.busy = False
        #: the next resume dies with the process (the service stops after its ledger write)
        self.crash_on_resume = False
        self.drafts: dict[str, dict[str, JsonValue]] = {}
        self.teach_answer: dict[str, JsonValue] | None = None
        self.rows: list[dict[str, JsonValue]] = []
        self.catalogues: dict[str, dict[str, JsonValue]] = {}
        #: the bonfire has no first session yet: teach, confirm, catalogue and context answer 404
        self.unborn = False
        self.healthz: dict[str, JsonValue] = {"status": "ok", "version": "2.0.0", "bonfires": {}, "spend": {}}
        self._n = 0

    # -- helpers for tests ---------------------------------------------------------------------------------------------

    def forget(self) -> None:
        """Every plan expires (the kernel restarted, or 15 minutes went by)."""
        self.plans.clear()

    def calls_of(self, name: str) -> list[JsonValue]:
        return [body for call, body in self.calls if call == name]

    def last_committed(self) -> JsonValue:
        """The ``committed`` list the last plan request carried."""
        body = self.calls_of("plan")[-1]
        return body.get("committed") if isinstance(body, dict) else None

    def _check(self, name: str, body: JsonValue) -> None:
        self.calls.append((name, body))
        self.events.append(name)
        if self.down:
            raise KernelDown("mk serve unreachable (ConnectError): connection refused")
        if self.busy:
            raise KernelBusy("bonfire is teaching: call again later", retry_after=2.0)
        if self.unborn and name in ("teach", "confirm", "put_catalogue", "context"):
            raise NotFounded("unknown bonfire")

    # -- turns ---------------------------------------------------------------------------------------------------------

    async def plan(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        self._check("plan", dict(body))
        self._n += 1
        plan_id = f"p{self._n}"
        turn = {k: v for k, v in body.items() if k != "committed"}
        script = self.scripts.get(str(body["text"]))
        if script is None:
            return self._unread(plan_id, turn)
        if script.partial:
            return {**self._unread(plan_id, turn), "frames": list(script.frames)}
        plan = _Plan(plan_id, script, turn)
        committed = body.get("committed")
        rows = committed if isinstance(committed, list) else []
        left = [(c["tool"], _canon(c["args"]), c["value"]) for c in rows if isinstance(c, dict)]
        for tool, args in script.steps:
            n = next((i for i, (t, a, _) in enumerate(left) if t == tool and a == _canon(args)), None)
            if n is None:
                break
            plan.resolved.append(left.pop(n)[2])
        self.plans[plan_id] = plan
        return self._json(plan)

    async def resume(
        self, bonfire: str, plan_id: str, results: Sequence[Mapping[str, JsonValue]]
    ) -> dict[str, JsonValue]:
        self._check("resume", [dict(r) for r in results])
        if self.crash_on_resume:
            self.crash_on_resume = False
            raise RuntimeError("the process died mid-request")
        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanGone(f"no plan {plan_id!r}: the plan expired")
        row = results[0]
        pending = f"{plan_id}:{plan.at}:0"
        if plan.failure is not None or plan.at >= len(plan.script.steps) or row["step_id"] != pending:
            raise Refused(f"plan {plan_id}: step(s) [{row['step_id']!r}] are not pending")
        tool, args = plan.script.steps[plan.at]
        if row["tool"] != tool or _canon(row["args"]) != _canon(args):
            raise Refused(f"plan {plan_id}: the result for step {pending} is not for {tool}(args)")
        if row["verdict"] == "committed":
            plan.resolved.append(row["value"])
        else:
            plan.failure = {"step_id": pending, "tool": tool, "kind": row["verdict"], "detail": row.get("detail")}
        return self._json(plan)

    async def retry(self, bonfire: str, plan_id: str) -> dict[str, JsonValue]:
        self._check("retry", plan_id)
        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanGone(f"no plan {plan_id!r}")
        if plan.failure is None:
            raise Refused(f"plan {plan_id} has no failed step to retry")
        plan.failure = None
        return self._json(plan)

    def _unread(self, plan_id: str, turn: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            "plan_id": plan_id,
            "status": "unread",
            "turn": turn,
            "tool_steps": [],
            "licence": None,
            "answer": None,
            "diagnostics": ["no construction read the turn"],
            "reading": {"applied": []},
            "hand": [],
            "failure": None,
        }

    def _json(self, plan: _Plan) -> dict[str, JsonValue]:
        script = plan.script
        tools: list[str] = []
        for tool, _ in script.steps:
            if tool not in tools:
                tools.append(tool)
        licence: list[JsonValue] = [
            {"tool": t, "side_effect": True, "needs_confirmation": t in script.confirm, "reads": [], "writes": []}
            for t in tools
        ]
        steps: list[JsonValue] = []
        diagnostics: list[JsonValue] = []
        if plan.failure is not None:
            status = "repair"
            f = plan.failure
            diagnostics = [f"tool step {f['step_id']} ({f['tool']}) {f['kind']}: {f['detail']}"]
        elif plan.at < len(script.steps):
            status = "needs_tools"
            tool, args = script.steps[plan.at]
            steps = [
                {
                    "step_id": f"{plan.plan_id}:{plan.at}:0",
                    "tool": tool,
                    "args": args,
                    "side_effect": True,
                    "needs_confirmation": tool in script.confirm,
                    "actor": plan.turn.get("speaker"),
                    "checks": [],
                }
            ]
        else:
            status = "answered"
        hand: list[JsonValue] = [{"id": "base.request", "kind": "base", "rules": []}]
        applied: list[JsonValue] = ["base.request"]
        if script.phrase is not None:
            hand.append({"id": script.phrase, "kind": "taught", "maker": "Kira Dace", "rules": []})
            applied.append(f"{script.phrase}#v2")
        return {
            "plan_id": plan.plan_id,
            "status": status,
            "turn": plan.turn,
            "tool_steps": steps,
            "licence": licence,
            "answer": script.answer if status == "answered" else None,
            "diagnostics": diagnostics,
            "reading": {"applied": applied},
            "hand": hand,
            "failure": plan.failure,
            "frames": list(script.frames),
        }

    # -- teaching and the verb book ------------------------------------------------------------------------------------

    async def teach(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        self._check("teach", dict(body))
        if self.teach_answer is not None:
            return self.teach_answer
        draft_id = f"d{len(self.drafts) + 1}"
        self.drafts[draft_id] = dict(body)
        forms = body["forms"]
        specs = [{"id": f"brew.{draft_id}.{k}", "kind": "taught"} for k in range(len(forms))]
        return {"draft_id": draft_id, "specs": specs, "problems": []}

    async def confirm(self, bonfire: str, draft_id: str, by: str) -> dict[str, JsonValue]:
        self._check("confirm", {"draft_id": draft_id, "by": by})
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise Refused(f"no staged draft {draft_id!r}")
        if draft["teacher"] != by:
            raise NotTeacher(f"only {draft['teacher']} may confirm draft {draft_id}")
        specs: list[JsonValue] = [{"id": f"brew.{draft_id}.{k}"} for k in range(len(draft["forms"]))]
        return {"draft_id": draft_id, "specs": specs}

    async def constructions(self, bonfire: str) -> list[dict[str, JsonValue]]:
        self._check("constructions", bonfire)
        return list(self.rows)

    async def get_catalogue(self, bonfire: str) -> dict[str, JsonValue]:
        self._check("get_catalogue", bonfire)
        return self.catalogues.get(bonfire, {"sha256": None, "tools": []})

    async def put_catalogue(self, bonfire: str, catalogue: Mapping[str, JsonValue]) -> None:
        self._check("put_catalogue", dict(catalogue))
        if "schema_version" not in catalogue:  # the real kernel's parse_catalogue refuses it
            raise Refused("catalogue: schema_version is required")
        self.catalogues[bonfire] = dict(catalogue)

    async def context(self, bonfire: str, query: str, speaker: str | None) -> tuple[str, bool]:
        self._check("context", query)
        return "", False

    async def health(self) -> dict[str, JsonValue]:
        if self.down:
            raise KernelDown("mk serve unreachable (ConnectError): connection refused")
        return self.healthz


# -- a store that logs its writes ----------------------------------------------------------------------------------------


class LoggingStore(MemoryTurnStore):
    """A ``MemoryTurnStore`` that writes ``add_ledger`` into a shared event log (to order it against kernel calls)."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def add_ledger(self, row: LedgerRow) -> None:
        self.events.append("add_ledger")
        await super().add_ledger(row)


class LostWrite(RuntimeError):
    """The turn's own save did not happen (the service stopped, or MongoDB restarted at 22:00)."""


class LosingStore(MemoryTurnStore):
    """A ``MemoryTurnStore`` that, once armed, loses the turn save that follows the next ledger write: ``report``'s
    ledger row is kept, the turn record still holds the step as pending."""

    def __init__(self) -> None:
        super().__init__()
        self._armed = False
        self._lose = False

    def lose_the_save_after_the_next_ledger_write(self) -> None:
        self._armed = True

    async def add_ledger(self, row: LedgerRow) -> None:
        await super().add_ledger(row)
        self._lose, self._armed = self._armed, False

    async def save_turn(self, turn: TurnRecord) -> None:
        if self._lose:
            self._lose = False
            raise LostWrite(f"the save of turn {turn.turn_id} after its ledger write was lost")
        await super().save_turn(turn)


# -- turn requests ------------------------------------------------------------------------------------------------------


def team_turn(text: str, message_id: str = "42", speaker: str | None = "Kira Dace", at: datetime = AT) -> BeginTurn:
    return BeginTurn(
        platform="telegram",
        group="-100777",
        chat="-100777",
        speaker=speaker,
        message_id=message_id,
        text=text,
        at=at,
        participants=["Kira Dace", "Milo"],
    )


def frame(
    name: str, kind: str = "tool", *, complete: bool = False, construction: str | None = None, **roles: str | None
) -> dict[str, JsonValue]:
    """A Frame JSON as the kernel reports it."""
    return {
        "name": name,
        "kind": kind,
        "construction": construction or f"frame.{name}",
        "roles": dict(roles),
        "complete": complete,
        "analysis": 0,
        "composed_with": None,
    }


def cli_turn(text: str, message_id: str = "7") -> BeginTurn:
    return BeginTurn(
        platform="cli",
        group="session-1",
        chat="session-1",
        speaker="Ana",
        message_id=message_id,
        text=text,
        at=AT,
    )


def later(minutes: int) -> datetime:
    return AT + timedelta(minutes=minutes)


def _tool(name: str, *required: str, side_effect: bool = True, consent: str = "speaker_turn") -> dict[str, JsonValue]:
    return {
        "name": name,
        "x-side-effect": side_effect,
        "x-consent": consent,
        "input_schema": {
            "type": "object",
            "properties": {r: {"type": "string"} for r in required},
            "required": list(required),
        },
    }


#: a small catalogue: three verbs that change work (one asks for confirmation), three that only read
CATALOGUE: dict[str, JsonValue] = {
    "schema_version": "kernel-catalogue/v1",
    "tools": [
        _tool("make", "title"),
        _tool("take", "ticket"),
        _tool("archive", "ticket", consent="confirm"),
        _tool("look", "place", side_effect=False),
        _tool("read", "ticket", side_effect=False),
        _tool("search", "query", side_effect=False),
    ],
}


async def afforded_turn(store: MemoryTurnStore, *names: str, speaker: str = "Kira Dace", turn_id: str = "t-aff") -> str:
    """A saved message turn of the team chat that affords ``names`` (for the teaching routes)."""
    await store.save_turn(
        TurnRecord(
            turn_id=turn_id,
            platform="telegram",
            group="-100777",
            chat="-100777",
            message_id=turn_id,
            speaker=speaker,
            text="teach",
            at=AT,
            participants=[],
            bonfire_id="65f0c0ffee0000000000abcd",
            plan=None,
            status="unread",
            licence=None,
            pending=None,
            phrase=None,
            affordances=list(names),
        )
    )
    return turn_id
