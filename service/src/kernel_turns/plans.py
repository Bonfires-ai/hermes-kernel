"""Reading the kernel's ``TurnPlan`` JSON (memory-kernel ``runtime/api.py``): the pending step, the licence, the
taught phrase it applied, its diagnostics; and the ``committed=`` rows a re-plan sends."""

from __future__ import annotations

from pydantic import ValidationError

from kernel_turns.models import Frame, JsonValue, LedgerRow

#: an applied construction's name is ``<spec id>#v<n>`` for an alternate rendering (``constructions.ALTERNATE``)
ALTERNATE = "#v"


def committed_rows(ledger: list[LedgerRow]) -> list[JsonValue]:
    """The ``committed=`` input of a plan: every committed row ``{tool, args, value}`` in ledger (issue) order."""
    return [{"tool": r.tool, "args": r.args, "value": r.value} for r in ledger if r.verdict == "committed"]


def licence_of(plan: dict[str, JsonValue]) -> list[str] | None:
    """The tools the program uses; None for no licence (JSON null)."""
    rows = plan.get("licence")
    if not isinstance(rows, list):
        return None
    tools: list[str] = []
    for row in rows:
        tool = row.get("tool") if isinstance(row, dict) else None
        if isinstance(tool, str) and tool not in tools:
            tools.append(tool)
    return tools


def step_of(plan: dict[str, JsonValue]) -> tuple[str, dict[str, JsonValue], str, bool] | None:
    """The first pending tool step: ``(tool, args, step_id, needs_confirmation)``, or None when there is none."""
    steps = plan.get("tool_steps")
    first = steps[0] if isinstance(steps, list) and steps else None
    if not isinstance(first, dict):
        return None
    tool, args, step_id = first.get("tool"), first.get("args"), first.get("step_id")
    if not isinstance(tool, str) or not isinstance(args, dict) or not isinstance(step_id, str):
        return None
    return tool, args, step_id, first.get("needs_confirmation") is not False


def phrase_of(plan: dict[str, JsonValue]) -> str | None:
    """The taught construction the plan applied: the ``hand`` row of kind ``taught`` whose id is in
    ``reading.applied`` (an applied name may carry its rendering suffix, ``<id>#v<n>``)."""
    reading = plan.get("reading")
    applied = reading.get("applied") if isinstance(reading, dict) else None
    ids = (
        {name.split(ALTERNATE, 1)[0] for name in applied if isinstance(name, str)}
        if isinstance(applied, list)
        else set()
    )
    hand = plan.get("hand")
    for row in hand if isinstance(hand, list) else []:
        if isinstance(row, dict) and row.get("kind") == "taught" and row.get("id") in ids:
            found = row.get("id")
            if isinstance(found, str):
                return found
    return None


def diagnostics(plan: dict[str, JsonValue]) -> list[str]:
    rows = plan.get("diagnostics")
    return [d for d in rows if isinstance(d, str)] if isinstance(rows, list) else []


def frames_of(plan: dict[str, JsonValue]) -> list[Frame]:
    """The plan's ``frames`` (every analysis): a row the shape does not fit is dropped, not guessed at."""
    rows = plan.get("frames")
    frames: list[Frame] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            frames.append(Frame.model_validate(row))
        except ValidationError:
            continue
    return frames
