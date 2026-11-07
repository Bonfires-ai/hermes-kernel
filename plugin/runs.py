"""A taught phrase's run in the agent: Hermes's own tools through `dispatch_tool`, `think` by the model.

A phrase called with `run_phrase` runs as a child turn of the turn service. Every step of it runs in the agent, so
`PhraseRun.drive` takes it one step at a time: it reads the child turn (`GET /v1/turns/{child}`), runs the one step
that is pending and reports it (`POST /v1/turns/{child}/report`), and repeats until no step is pending:

- **a tool Hermes holds** runs through Hermes's `ctx.dispatch_tool(name, args)` (the registry's handler, with this
  session's `task_id` and `session_id`; `name` the real one behind the step's catalogue name, an MCP tool's short
  name: `AgentSource.tool`), and is reported committed with the tool's own text: an untyped result fills a text
  parameter of a later step. Before it runs, in this order, the step must still be a kept row (`AgentSource.tool`: its
  MCP server may be down, its toolset turned off) and the tool must be enabled on this session's platform
  (`AgentSource.enabled_for`, the platform from the session record, lowercased, `cli` by default). When either fails,
  a dispatch raises, or a result is an error (`{"error": ...}`, Hermes's own test), the step is reported `tool_error`
  with the reason: the run stops with a sentence the model can say, and nothing is reported committed. A step left
  pending instead would show as waiting on every later message.
- **`think`** pauses the run: `drive` returns `paused_think` with the step's `instruction` and `input`; the model does
  the step with its own context and answers with `phrase_continue(text)`, which reports the text committed (`resume`)
  and drives on. The pause outlives the Hermes turn: the speaker's next message shows the phrase waiting. A second
  continue of the same step is refused by the service (the step is not pending), and runs nothing; a continue whose
  first report went in but whose answer was lost finds the turn on at a later step and drives on from there without
  sending the text again.
- **drop**: `drop` declines the step the phrase waits at, as the service holds it now; the declined report ends the
  child turn (a repair, a `declined` use), so no later message shows it waiting.
- **once:** each step has the service's effect key. Its result is written to the step ledger (`StepLedger`, a file
  under Hermes's home) right after the dispatch, with the real name of the tool that gave it, and dropped once the
  service took the report, so a step that comes up again with the same key (the report never arrived, it was refused
  under a stale step id, the plugin restarted) is reported with that result and not dispatched again. A refused
  report is not shown or counted as committed.
- **fenced:** a step's value reaches the model in Hermes's untrusted-result fence of its tool (`fence.fenced`:
  `web_search` and `mcp_*` results are someone else's words, and here they come back as `run_phrase`'s answer, which
  Hermes does not fence): the committed line, and every later line whose args or value carry it, told apart by the
  values each child turn's steps gave (`carried`), kept in the step ledger until the child turn ends (so a restart
  keeps them). When the child turn committed steps and what they gave is gone (a ledger that could not be read, or
  was written by an older plugin), every line of it is fenced (`FENCE_ALL`). So is what may echo those args: the
  service's repair question in a child turn that carries a value, and a step's failure (its own tool's fence, as
  Hermes fences an error result, else the fence of a value its args carry), in its line and its stop reason. A think
  step's input is always fenced (`tools.think_lines`). The fence, the scan and the values carried go by the tool's real
  name, never the step's short one: Hermes fences an `mcp_*` result, not a bare `search_knowledge_graph`. The name is
  the one the step was dispatched under, kept with a held result in the step ledger (after a restart no registry read
  may name the short one yet: its MCP server not connected). A held result whose name is unknown fails closed: its line
  in the plugin's own block (`fence.closed`), and the value carried under an `mcp_*` name, so Hermes fences what
  carries it. The service gets the raw text: it fills a later step, and a `tool_error`'s detail is the failure as it
  came.
- **bounded:** two refused reports in a row for the same effect key stop the run, naming the refusal (the service would
  hand the same step back each time); and one Hermes tool call drives for at most `DRIVE_BUDGET_S`, counted from the
  call's start (Hermes abandons a tool call at 420 s): every read and report is held to what is left, no step starts
  with less than `_MIN_LEFT_S` left, and the run stops there resumably.

One drive per child turn at a time (a lock per child turn).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .agent_source import MCP_PREFIX, THINK, AgentSource
from .client import Refused, ServiceClient, ServiceError, Unavailable, never_sent
from .fence import closed, fenced, scan
from .ledger import FENCE_ALL, UNKNOWN, UNKNOWN_SOURCE, Carried, Held, StepLedger  # noqa: F401
from .shared import get_shared

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
JsonValue = Any

#: the step ledger's file under Hermes's home (the session record's `hermes_home`)
LEDGER_FILENAME = "hermes-kernel-steps.json"
#: a result reported to the service (and shown for a think step's input) is cut here
MAX_RESULT_CHARS = 16000
#: what a line shows of a step's args or value
_MAX_VALUE_CHARS = 600
_MAX_AGENT_STEPS = 20
#: the service resumes the kernel after a report (45 s)
_REPORT_TIMEOUT_S = 50.0
_READ_TIMEOUT_S = 30.0
#: one Hermes tool call drives a phrase this long at most, from the call's start: Hermes abandons a tool call at 420 s
#: (`agent/tool_executor.py` `_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`), and one step may still run past the budget
DRIVE_BUDGET_S = 300.0
#: no step starts with less left than this, and a report always gets this long
_MIN_LEFT_S = 15.0
#: refused reports in a row for the same effect key that stop the run
_MAX_REFUSED = 2
NOT_WAITING = "that step is not waiting any more (it went on already), so nothing ran"
#: the detail of the step a dropped phrase declines
DROPPED = "the person dropped the phrase"
#: how a stop opens in a line of the answer
STOPPED_PREFIX = "It stopped: "

#: Hermes's `ctx.dispatch_tool(tool_name, args, **kwargs) -> str` (a handler's JSON string, or a multimodal dict)
Dispatcher = Callable[..., object]


def _probe(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def bounded(value: JsonValue) -> str:
    text = _probe(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 1] + "…"


def fence_carried(text: str, probe: str, carried: Carried) -> str:
    """`text` inside the fence of the first tool whose result `probe` (a JSON text) carries and Hermes fences."""
    for tool, result in carried:
        if tool == UNKNOWN:
            return closed(UNKNOWN_SOURCE, text)
        needle = json.dumps(result, ensure_ascii=False)[1:-1]
        if needle and needle in probe and (out := fenced(tool, text)) != text:
            return out
    return text


def fence_any(text: str, carried: Carried) -> str:
    """`text` inside the fence of the first tool of `carried` that Hermes fences: for the service's own text (a repair
    question), which may echo a step's args in any escaping (a repr, a canonical JSON), so no search for the value in
    it can tell; a child turn that carries a value has it fenced."""
    for tool, _ in carried:
        if tool == UNKNOWN:
            return closed(UNKNOWN_SOURCE, text)
        if (out := fenced(tool, text)) != text:
            return out
    return text


def shown(value: JsonValue, carried: Carried = ()) -> str:
    """`bounded(value)`, inside the fence of the tool whose result it carries, if any (bounded first: the fence's end is
    never cut)."""
    text = bounded(value)
    if not carried:
        return text
    return fence_carried(text, _probe(value), carried)


def shown_failure(tool: str, failure: str, args: JsonObject, carried: Carried = ()) -> str:
    """A step's failure as the model sees it: in its tool's fence (Hermes fences a tool's error like any result of it:
    an MCP `isError` comes back as `{"error": ...}`), else in the fence of a value its args carry (an error may echo
    its arguments)."""
    out = fenced(tool, failure)
    return out if out != failure else fence_carried(failure, _probe(args), carried)


def repair_line(view: JsonObject, carried: Carried = ()) -> str | None:
    """The service's question when the phrase's child turn ended in repair, else `None`; fenced when the child turn
    carries a value (the service builds it from a step's detail or value)."""
    if view.get("status") != "repair":
        return None
    repair = view.get("repair")
    if not isinstance(repair, str) or not repair:
        return "The kernel needs a repair."
    return f"The kernel asks: {fence_any(repair, carried)}"


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _cut(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - 1] + "…"


def think_parts(step: JsonObject) -> tuple[str, str | None]:
    """A think step's instruction, and its input cut where a result is (`None` when it has none)."""
    args = step.get("args")
    given = args if isinstance(args, dict) else {}
    return _text(given.get("instruction") or ""), (_cut(_text(given["input"])) if "input" in given else None)


def _step(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(k), str) for k in ("step_id", "tool", "effect_key")):
        return None
    return value if isinstance(value.get("args"), dict) else None


def run_of(view: JsonObject) -> JsonObject:
    """A child turn's view as the run `drive` goes on from: the turn is the view, and the step to run is its pending
    one while the turn is licensed (no pending step: it ended)."""
    pending = view.get("pending") if view.get("status") == "licensed" else None
    return {"agent_step": pending, "turn": view}


def _result(result: object) -> tuple[str, str | None]:
    """A dispatch's result as the step's value (the tool's own text), and its error when it is one (Hermes's
    `_tool_result_observer_fields`: a JSON object with an `error`)."""
    if isinstance(result, dict) and isinstance(result.get("content"), list):  # Hermes's multimodal envelope
        parts = [p.get("text") for p in result["content"] if isinstance(p, dict)]
        text = "\n".join(p for p in parts if isinstance(p, str))
    else:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        return text, str(parsed["error"])
    return _cut(text), None


@dataclass
class RunOutcome:
    """Where a drive left the phrase: `done` (no step is left: the turn finished), `paused_think` (a think step waits for
    the model: `instruction`, `input`, `step`), or `stopped` (`reason`; `resumable` when `phrase_continue` can take it
    on from `step`; `later` when the step surely still waits in the agent, so the speaker's next message shows it
    waiting too)."""

    kind: Literal["done", "paused_think", "stopped"]
    turn_id: str
    lines: list[str] = field(default_factory=list)
    committed: int = 0
    instruction: str | None = None
    input: str | None = None
    step: JsonObject | None = None
    reason: str | None = None
    resumable: bool = False
    later: bool = False


@dataclass
class _Ran:
    line: str
    reason: str | None = None
    resumable: bool = False
    later: bool = False
    #: the service took the step's report (it counts in `RunOutcome.committed`)
    committed: bool = False
    #: the service refused the report: its sentence
    refused: str | None = None


def _platform(session_id: str | None) -> str:
    """The session record's platform, lowercased; `cli` when there is none."""
    info = get_shared().sessions.get(session_id) if session_id else None
    value = info.get("platform") if isinstance(info, dict) else None
    return str(getattr(value, "value", value) or "cli").lower()


class PhraseRun:
    """Drives phrase child turns in the agent."""

    def __init__(
        self,
        client: ServiceClient,
        source: AgentSource,
        dispatch: Dispatcher | None,
        ledger: StepLedger,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._source = source
        self._dispatch = dispatch
        self._ledger = ledger
        self._clock = clock
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self._guard = threading.Lock()

    @property
    def can_run(self) -> bool:
        """Whether this Hermes gave the plugin a `dispatch_tool` (only the general path has one)."""
        return self._dispatch is not None

    def bind(self, session_id: str | None) -> None:
        """Give the ledger its file under the session's Hermes home, when the session record has one."""
        info = get_shared().sessions.get(session_id) if session_id else None
        home = info.get("hermes_home") if isinstance(info, dict) else None
        if home:
            self._ledger.bind(Path(str(home)) / LEDGER_FILENAME)

    def budget_end(self) -> float:
        """When a drive that starts now (a Hermes tool call's start) must stop: `DRIVE_BUDGET_S` from now."""
        return self._clock() + DRIVE_BUDGET_S

    def carried(self, child_turn_id: str) -> Carried:
        """What `child_turn_id`'s lines are fenced by, `(tool, value)`: its steps' values; `FENCE_ALL` when they are not
        all known."""
        return self._ledger.carried(child_turn_id)

    def _left(self, deadline: float, cap: float) -> float:
        """A call's timeout: what is left of the budget, at least `_MIN_LEFT_S`, at most `cap`."""
        return min(cap, max(deadline - self._clock(), _MIN_LEFT_S))

    @contextmanager
    def _hold(self, turn_id: str) -> Iterator[None]:
        with self._guard:
            lock, users = self._locks.get(turn_id) or (threading.Lock(), 0)
            self._locks[turn_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._guard:
                _, users = self._locks[turn_id]
                if users <= 1:
                    del self._locks[turn_id]
                else:
                    self._locks[turn_id] = (lock, users - 1)

    def drive(
        self,
        child_turn_id: str,
        *,
        first: JsonObject | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        deadline: float | None = None,
    ) -> RunOutcome:
        """Run the child turn on from where it is (`first`: the run the call already gave, `run_of` its view; none: the
        turn is read), until `deadline` (`budget_end()` of the tool call's start; now's when not given)."""
        end = self.budget_end() if deadline is None else deadline
        with self._hold(child_turn_id):
            return self._drive(child_turn_id, first, RunOutcome("done", child_turn_id), task_id, session_id, end)

    def resume(
        self,
        child_turn_id: str,
        step: JsonObject | None,
        text: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        deadline: float | None = None,
    ) -> RunOutcome:
        """`phrase_continue`: a think `step` is reported committed with `text`, then the run goes on; any other step
        (or none) is simply driven on (the step runs, or its held result is reported)."""
        end = self.budget_end() if deadline is None else deadline
        with self._hold(child_turn_id):
            out = RunOutcome("done", child_turn_id)
            if step is not None and step.get("tool") == THINK:
                reported = self._report_think(child_turn_id, step, text, self._left(end, _REPORT_TIMEOUT_S))
                if reported == "gone":
                    self._ledger.end(child_turn_id)
                    return RunOutcome("stopped", child_turn_id, reason=NOT_WAITING)
                if reported == "taken":
                    out.lines.append("1. think: its text went in already, on an earlier try (not sent again)")
                else:
                    out.lines.append(f"1. think: committed → {bounded(text)}")
                    out.committed = 1
            return self._drive(child_turn_id, None, out, task_id, session_id, end)

    def drop(self, child_turn_id: str) -> bool:
        """Drop the phrase: the step it waits at, as the service holds it now (a re-plan issues it again under a new id),
        is reported `declined`, which ends the child turn. `False` when no step of it is pending any more (it went on,
        or ended): nothing is reported."""
        with self._hold(child_turn_id):
            view = self._client.get(child_turn_id, timeout=_REPORT_TIMEOUT_S)
            pending = _step(view.get("pending"))
            if view.get("status") != "licensed" or pending is None:
                return False
            step_id, key = str(pending["step_id"]), str(pending["effect_key"])
            try:
                self._client.report(child_turn_id, step_id, "declined", None, DROPPED, timeout=_REPORT_TIMEOUT_S)
            except Refused:
                return False
            self._ledger.forget(key)  # a result held for it is never reported now
            self._ledger.end(child_turn_id)
            return True

    def _report_think(
        self, child: str, step: JsonObject, text: str, timeout: float
    ) -> Literal["reported", "taken", "gone"]:
        """Report the think `step` committed with `text`. Refused (the step is not pending under that id), the turn is
        read again:

        - a re-plan issues the same step under a new id and the same effect key, and only that step takes the text
          (`reported`);
        - still licensed with another step pending, the turn took the text already, on a report whose answer was lost
          (`taken`): the run goes on from that step, and the text is not sent again;
        - nothing pending, or the turn ended: it takes nothing (`gone`)."""
        try:
            self._client.report(child, str(step["step_id"]), "committed", _cut(text), None, timeout=timeout)
            return "reported"
        except Refused:
            view = self._client.get(child, timeout=timeout)
        pending = _step(view.get("pending"))
        if pending is None or view.get("status") != "licensed":
            return "gone"
        if pending["effect_key"] != step.get("effect_key"):
            return "taken"
        if pending["tool"] != THINK:
            return "gone"
        try:
            self._client.report(child, pending["step_id"], "committed", _cut(text), None, timeout=timeout)
        except Refused:
            return "gone"
        return "reported"

    def _drive(
        self,
        child: str,
        run: JsonObject | None,
        out: RunOutcome,
        task_id: str | None,
        session_id: str | None,
        deadline: float,
    ) -> RunOutcome:
        n = len(out.lines)
        refused: tuple[str | None, int] = (None, 0)  # the effect key last refused, and how many times in a row
        for _ in range(_MAX_AGENT_STEPS + 1):
            if run is None:
                if deadline - self._clock() < _MIN_LEFT_S:
                    return _out_of_time(out, None)
                try:
                    run = run_of(self._client.get(child, timeout=min(_READ_TIMEOUT_S, deadline - self._clock())))
                except ServiceError as exc:
                    logger.warning("hermes-kernel: reading phrase turn %s failed: %s", child, exc.detail)
                    out.kind, out.resumable = "stopped", True
                    out.reason = "the turn service did not answer, so the phrase's next step has not run"
                    return out
            step = _step(run.get("agent_step"))
            if step is None:
                turn = run.get("turn")
                view: JsonObject = turn if isinstance(turn, dict) else {}
                out = self._ended(view, out, self.carried(child))
                if view.get("status") != "licensed":
                    self._ledger.end(child)  # the child turn ended: its values are no longer kept
                return out
            if step["tool"] == THINK:
                out.kind, out.step = "paused_think", step
                out.instruction, out.input = think_parts(step)
                return out
            if deadline - self._clock() < _MIN_LEFT_S:
                return _out_of_time(out, step)  # nothing dispatched: the step surely still waits in the agent
            n += 1
            ran = self._agent_step(child, step, n, task_id, session_id, deadline)
            out.lines.append(ran.line)
            if ran.reason is not None:
                out.kind, out.step, out.reason = "stopped", step, ran.reason
                out.resumable, out.later = ran.resumable, ran.later
                return out
            key = str(step["effect_key"])
            refused = (key, refused[1] + 1 if refused[0] == key else 1) if ran.refused is not None else (None, 0)
            if refused[1] >= _MAX_REFUSED:
                out.kind, out.step = "stopped", step
                out.reason = (
                    f"the turn service refused the result of {step['tool']} {refused[1]} times ({ran.refused}), so "
                    "the phrase stopped there; nothing after it ran"
                )
                return out
            out.committed += int(ran.committed)
            run = None
        out.kind, out.reason = "stopped", f"the phrase ran more than {_MAX_AGENT_STEPS} steps in the agent"
        return out

    @staticmethod
    def _ended(view: JsonObject, out: RunOutcome, carried: Carried = ()) -> RunOutcome:
        asked = repair_line(view, carried)
        status = view.get("status")
        if status in ("done", "answered") and not asked:
            return out
        out.kind = "stopped"
        out.reason = (
            f"{asked}; nothing more ran. Ask the person, then call again"
            if asked
            else "the kernel has not issued the phrase's next step yet"
        )
        # Licensed with nothing pending: the next read re-plans it.
        out.resumable = status == "licensed" and not asked
        return out

    def _agent_step(
        self, child: str, step: JsonObject, n: int, task_id: str | None, session_id: str | None, deadline: float
    ) -> _Ran:
        tool, key, step_id = str(step["tool"]), str(step["effect_key"]), str(step["step_id"])
        raw = step["args"]
        args: JsonObject = {str(k): _text(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        carried = self.carried(child)
        head = f"{n}. {tool} {shown(args, carried)}"
        held = self._ledger.get(key)
        real: str | None  # Hermes's name: the fence decides by it
        if held is None:
            value, failure, real = self._dispatched(tool, args, task_id, session_id)
            if failure is not None:
                said = shown_failure(real, failure, args, carried)
                return self._failed(child, step_id, failure, said, head, deadline)
            self._ledger.record(key, value, real)
        else:
            value, real = held[0], held[1] or self._source.real_name(tool)
        # Unknown (an older entry no read maps): fail closed, the line in the plugin's own block, the value carried
        # under an `mcp_*` name so Hermes fences any line that carries it.
        safe = fenced(real, bounded(value)) if real is not None else closed(tool, bounded(value))
        self._ledger.carry(child, real if real is not None else MCP_PREFIX + tool, value)
        try:
            self._client.report(
                child, step_id, "committed", value, None, timeout=self._left(deadline, _REPORT_TIMEOUT_S)
            )
        except Refused as exc:
            # The step id went stale (a re-plan issues the step again under a new id and the same effect key), or it
            # was reported already (an answer that was lost). The result stays held for a step issued again with this
            # key (the ledger is bounded, and a later report of the key drops it); the next read finds where it is.
            logger.info("hermes-kernel: the report of %s was refused (%s); going on", step_id, exc.detail)
            return _Ran(f"{head}: ran; the turn service had moved on", refused=exc.detail)
        except Unavailable as exc:
            logger.warning("hermes-kernel: the report of %s did not arrive: %s", step_id, exc.detail)
            return _Ran(
                f"{head}: ran, not yet reported",
                reason=(
                    f"I ran {tool}, but the turn service did not take its result ({exc.detail}). The result is kept: "
                    f"continuing the phrase (phrase_continue) reports it without running {tool} again"
                ),
                resumable=True,
                later=never_sent(exc),  # surely not taken: the step still waits in the agent
            )
        self._ledger.forget(key)
        return _Ran(f"{head}: committed → {safe}", committed=True)

    def _dispatched(
        self, tool: str, args: JsonObject, task_id: str | None, session_id: str | None
    ) -> tuple[str, str | None, str]:
        """The step's value, its failure (or `None`), and the name it ran under (the real one; when nothing ran, the
        step's own: then the failure is the plugin's sentence)."""
        if self._dispatch is None:
            return "", f"this Hermes cannot run {tool} inside a phrase", tool
        try:
            held = self._source.tool(tool)
        except Exception:  # noqa: BLE001 — Hermes's registry or config unreadable: the step's failure, never Hermes's
            logger.warning("hermes-kernel: could not read the agent's tools to run %s", tool, exc_info=True)
            return "", f"I could not read my own tools to run {tool}", tool
        if held is None:
            missing = (
                f"{tool} is not one of my tools right now (its server may be down, what it needs is missing, or it "
                "was turned off)"
            )
            return "", missing, tool
        platform = _platform(session_id)
        if not self._source.enabled_for(platform, held.name):
            return "", f"{tool} is not enabled on {platform} here, so a phrase may not run it", tool
        kwargs = {k: v for k, v in (("task_id", task_id), ("session_id", session_id)) if v}
        try:
            result = self._dispatch(held.name, dict(args), **kwargs)
        except Exception as exc:  # noqa: BLE001 — any failure of the tool is the step's, never an error into Hermes
            logger.warning("hermes-kernel: %s failed inside a phrase", held.name, exc_info=True)
            return "", f"{tool} could not run ({type(exc).__name__}: {exc})", held.name
        value, error = _result(result)
        if error is None:
            scan(held.name, value)
            return value, None, held.name
        return "", f"{tool} did not work: {error}", held.name

    def _failed(self, child: str, step_id: str, failure: str, said: str, head: str, deadline: float) -> _Ran:
        """Report the step's `failure` raw (`tool_error`); the line and the stop reason show it as `said` (fenced)."""
        try:
            self._client.report(
                child, step_id, "tool_error", None, failure, timeout=self._left(deadline, _REPORT_TIMEOUT_S)
            )
        except ServiceError as exc:
            logger.warning("hermes-kernel: the failure of %s could not be reported: %s", step_id, exc.detail)
        return _Ran(f"{head}: tool_error — {said}", reason=f"{said}, so the phrase stopped there; nothing after it ran")


def _out_of_time(out: RunOutcome, step: JsonObject | None) -> RunOutcome:
    """The drive's budget is spent before the next step (`step`: a step not dispatched, which surely still waits in the
    agent; `None`: the next read): stopped, resumably, nothing half-run."""
    out.kind, out.step, out.resumable, out.later = "stopped", step, True, step is not None
    out.reason = (
        f"this call ran out of time (one tool call drives a phrase for {int(DRIVE_BUDGET_S)} s), so the phrase's next "
        "step has not run"
    )
    return out
