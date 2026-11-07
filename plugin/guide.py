"""The editor teaches the structured step at the point of failure.

A small model teaching a phrase tends to write sentence steps ("web_search {X}", "search the web for {X}") that the
live grammar does not read, and each refusal only says the step "does not read". So when the kernel's problems say a
body clause does not read (`body clause {clause!r}: ... does not read ...`; the marks are data:
`data/step_guide.json`), `teach_phrase`'s answer appends a short guide built from what the agent has right now:

- the shape of a structured step, and one line per tool a step can call: the composable tools by their catalogue
  names with their parameters (`AgentSource.steps`: the ones Hermes offers now, then `think`);
- the failing request's body as calls, when every step that does not read starts with a tool's name (its catalogue
  name, or its real name: `mcp__srv__web_search {X}` is `web_search`) and takes one value, or is `think: ...` after
  an earlier step; the steps that read stay as they are. No guess otherwise: "search the web for {X}" gets the
  tools, not a body.

The guide carries no value the model did not just send in that same tool call: the tool lines are names and parameter
names, the worked body is the model's own body rewritten, and the kernel's problems are only matched, never quoted.

`Editor.short` also writes a structured step's real tool name as its catalogue name (`mcp__srv__web_search` ->
`web_search`): a model's history and older instructions use the long name, which the kernel no longer holds.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .agent_source import THINK

JsonValue = Any
JsonObject = dict[str, Any]

logger = logging.getLogger(__name__)

GUIDE_FILE = Path(__file__).resolve().parent / "data" / "step_guide.json"
#: how the kernel names a body clause in a problem (`teach.py` `_program`: the clause as the body gave it)
_CLAUSE = "body clause {!r}: "
#: the guide stays short: it goes to a small model
_MAX_TOOLS = 24
_LEAD = re.compile(r"\s*([A-Za-z0-9_.-]+)")
_SLOT = re.compile(r"\{[^{}]*\}")
SHAPE = (
    'A step the grammar does not read can be a call: {"call": "<tool>", "args": {"<param>": "<value>"}}, each value '
    'a slot "{X}", "it" (the result of the step before) or literal text.'
)
TOOLS_HEAD = "Tools a step can call (name them exactly so):"

Tools = Sequence[tuple[str, Sequence[str]]]


@dataclass(frozen=True)
class GuideData:
    """The data file: what a problem says when a step does not read."""

    unread_marks: tuple[str, ...] = ()


def load(path: Path | None = None) -> GuideData:
    """The data file; unreadable: no guide (logged), the kernel's problems as before."""
    try:
        raw = json.loads((path or GUIDE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("hermes-kernel: the step guide's data file is unreadable; no guide on a refused draft")
        return GuideData()

    def texts(key: str) -> tuple[str, ...]:
        value = raw.get(key) if isinstance(raw, dict) else None
        return tuple(v for v in value if isinstance(v, str) and v) if isinstance(value, list) else ()

    return GuideData(texts("unread_marks"))


def unread(body: Sequence[JsonValue], problems: Sequence[str], marks: Sequence[str]) -> list[int]:
    """The indexes of the body's sentences a problem says the live grammar does not read."""
    out: list[int] = []
    for i, step in enumerate(body):
        if not isinstance(step, str):
            continue
        head = _CLAUSE.format(step)
        if any(p.startswith(head) and any(mark in p[len(head) :] for mark in marks) for p in problems):
            out.append(i)
    return out


def _value(rest: str, first: bool) -> str | None:
    """A one-value step's value from the text after the tool's name: a lone slot, literal text, or "it" when nothing
    follows the name of a later step. Text around a slot is no exact value: `None`."""
    slots = _SLOT.findall(rest)
    if slots:
        return slots[0] if len(slots) == 1 and rest == slots[0] else None
    return rest or (None if first else "it")


def _call(
    sentence: str, first: bool, tools: Mapping[str, Sequence[str]], aliases: Mapping[str, str]
) -> JsonObject | None:
    """`sentence` as a structured step, when it starts with a tool's name (or a real name, written as the catalogue's)
    and its value can be read off: `name {X}`, `name({X})`, `name: text`; `think: instruction` after an earlier step."""
    lead = _LEAD.match(sentence)
    if lead is None:
        return None
    by_lower = {name.lower(): name for name in tools} | {real.lower(): name for real, name in aliases.items()}
    tool = by_lower.get(lead.group(1).lower())
    if tool is None:
        return None
    rest = sentence[lead.end() :].strip()
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()
    elif rest.startswith(":"):
        rest = rest[1:].strip()
    params = list(tools[tool])
    if tool == THINK:
        return None if first or not rest else {"call": THINK, "args": {"instruction": rest, "input": "it"}}
    value = _value(rest, first)
    if len(params) != 1 or value is None:
        return None
    return {"call": tool, "args": {params[0]: value}}


def worked_body(
    body: Sequence[JsonValue], failing: Sequence[int], tools: Mapping[str, Sequence[str]], aliases: Mapping[str, str]
) -> list[JsonValue] | None:
    """The body with each failing sentence written as a call; `None` when one of them cannot be read off."""
    out: list[JsonValue] = []
    for i, step in enumerate(body):
        if i in failing and isinstance(step, str):
            call = _call(step, i == 0, tools, aliases)
            if call is None:
                return None
            out.append(call)
        else:
            out.append(step)
    return out


def guide_lines(
    tool: str,
    body: Sequence[JsonValue],
    problems: Sequence[str],
    *,
    steps: Tools,
    aliases: Mapping[str, str],
    data: GuideData,
) -> list[str]:
    """The guide appended to `tool`'s answer (`[]` when no problem says a step does not read). `steps`: the agent's
    composable tools and `think`; `aliases`: real name -> catalogue name."""
    failing = unread(body, problems, data.unread_marks)
    if not failing:
        return []
    lines = [SHAPE, TOOLS_HEAD, *(f"{name}({', '.join(params)})" for name, params in steps[:_MAX_TOOLS])]
    tools = dict(steps)
    worked = worked_body(body, failing, tools, aliases)
    if worked is None:
        lines.append(f"Write the steps that do not read as calls of these, then call {tool} again.")
    else:
        lines.append(f"For this phrase: {json.dumps(worked, ensure_ascii=False)} — call {tool} again with that body.")
    return lines


class StepSource(Protocol):
    """What the guide asks the agent source (`AgentSource`)."""

    def steps(self) -> list[tuple[str, list[str]]]: ...

    def real_names(self) -> dict[str, str]: ...


class Editor:
    """The guide bound to the agent's tools as they are now."""

    def __init__(self, source: StepSource | None, data: GuideData) -> None:
        self._source = source
        self._data = data

    def _aliases(self) -> dict[str, str]:
        """Real name -> catalogue name, for the rows whose names differ (an MCP tool's short name)."""
        if self._source is None:
            return {}
        return {real: name for name, real in self._source.real_names().items()}

    def short(self, body: Sequence[JsonValue]) -> list[JsonValue]:
        """`body` with each structured step's real tool name written as its catalogue name."""
        try:
            aliases = self._aliases()
        except Exception:  # noqa: BLE001 — the registry unreadable: the body as the agent wrote it
            logger.warning("hermes-kernel: could not read the agent's tool names", exc_info=True)
            return list(body)
        out: list[JsonValue] = []
        for step in body:
            call = step.get("call") if isinstance(step, dict) else None
            if isinstance(step, dict) and isinstance(call, str) and call in aliases:
                out.append({**step, "call": aliases[call]})
            else:
                out.append(step)
        return out

    def lines(self, tool: str, body: Sequence[JsonValue], problems: Sequence[str]) -> list[str]:
        """The guide for `tool`'s answer; never costs the answer (a failure: no guide, logged)."""
        if not unread(body, problems, self._data.unread_marks):
            return []
        try:
            steps = self._source.steps() if self._source is not None else []
            aliases = self._aliases()
        except Exception:  # noqa: BLE001 — the guide is help, never the answer's failure
            logger.warning("hermes-kernel: could not build the step guide", exc_info=True)
            return []
        return guide_lines(tool, body, problems, steps=steps, aliases=aliases, data=self._data)
