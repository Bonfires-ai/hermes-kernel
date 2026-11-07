"""The turn service's client against a real `mk serve`: the calls it relies on, in the order a conversation makes them.

Run by hand or on a release, never on every push (it needs a running stack and teaches one phrase into it):

    KT_CONTRACT_URL=http://mk-serve:8000 pytest service/tests/contract -v

`mk serve` has no host port, so run it from a container on the stack's network (see the README's release notes). The
bonfire (`KT_CONTRACT_BONFIRE`, default `hermes`) must already hold a built session. Each run teaches one new phrase,
`ktcontract<letters> {X}`, and leaves it in the bonfire.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kernel_turns.mk_client import MkClient, PlanGone, Refused
from kernel_turns.models import JsonValue
from kernel_turns.teaching import merge_sources

URL = os.environ.get("KT_CONTRACT_URL", "")
BONFIRE = os.environ.get("KT_CONTRACT_BONFIRE", "hermes")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not URL, reason="KT_CONTRACT_URL is not set: no mk serve to talk to"),
]

AGENT_ROWS: list[JsonValue] = [
    {
        "name": "web_search",
        "description": "search the web",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "x-args": {"query": {"role": "query"}},
        "x-side-effect": False,
        "x-consent": "speaker_turn",
    },
    {
        "name": "think",
        "description": "the agent does this step itself",
        "input_schema": {
            "type": "object",
            "properties": {"instruction": {"type": "string"}, "input": {"type": "string"}},
            "required": ["instruction", "input"],
        },
        "x-args": {"instruction": {"role": "theme"}, "input": {"role": "source"}},
        "x-side-effect": False,
        "x-consent": "speaker_turn",
    },
]
# a word no other run used (letters only: a form is words): a phrase taught by an earlier run stays in the bonfire
WORD = "ktcontract" + uuid4().hex[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
TEACHER = "Contract"
CHAT = "contract:1"
BODY: list[JsonValue] = [
    {"call": "web_search", "args": {"query": "{X}"}},
    {"call": "think", "args": {"instruction": "say it in one line", "input": "it"}},
]


@pytest.fixture
async def mk() -> AsyncIterator[MkClient]:
    client = MkClient(URL)
    yield client
    await client.aclose()


def plan_body(text: str) -> dict[str, JsonValue]:
    return {
        "text": text,
        "chat_id": CHAT,
        "speaker": TEACHER,
        "message_id": uuid4().hex,
        "at": datetime.now(UTC).isoformat(),
        "participants": [TEACHER],
        "committed": [],
    }


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def first_step(plan: dict[str, JsonValue]) -> dict[str, JsonValue]:
    steps = plan.get("tool_steps")
    assert isinstance(steps, list) and steps, f"no pending step in {plan.get('status')!r}: {plan.get('diagnostics')}"
    return as_dict(steps[0])


def result(
    step: dict[str, JsonValue], verdict: str, value: JsonValue, detail: str | None = None
) -> dict[str, JsonValue]:
    return {
        "step_id": step["step_id"],
        "tool": step["tool"],
        "args": step["args"],
        "verdict": verdict,
        "value": value,
        "detail": detail,
    }


async def test_the_walk(mk: MkClient) -> None:
    # health: the bonfire is built and mk serve names it
    health = await mk.health()
    bonfires = as_dict(health["bonfires"])
    assert BONFIRE in bonfires, f"{BONFIRE!r} is not built in this mk serve: {sorted(bonfires)}"

    # the catalogue: read, put (the agent's tools, as the service puts them), read back
    before = await mk.get_catalogue(BONFIRE)
    old = before.get("tools")
    names = {str(r["name"]) for r in old if isinstance(r, dict)} if isinstance(old, list) else set[str]()
    wanted: list[JsonValue] = [r for r in AGENT_ROWS if as_dict(r)["name"] not in names]
    merged = merge_sources({"tools": [*(old if isinstance(old, list) else []), *wanted]})
    await mk.put_catalogue(BONFIRE, merged)
    after = await mk.get_catalogue(BONFIRE)
    tools = after.get("tools")
    assert isinstance(tools, list)
    assert {"web_search", "think"} <= {str(r["name"]) for r in tools if isinstance(r, dict)}
    assert all(as_dict(r).get("x-runner") == "agent" for r in tools)

    # context: recall of a built bonfire answers, not degraded
    text, degraded = await mk.context(BONFIRE, "what do we know about the people here", None)
    assert degraded is False
    assert text.strip()

    # teach a phrase, see its draft staged, confirm it as the teacher
    draft = await mk.teach(
        BONFIRE,
        {
            "forms": [f"{WORD} {{X}}"],
            "body": BODY,
            "defaults": {},
            "teacher": TEACHER,
            "chat_id": CHAT,
            "replace": True,
        },
    )
    draft_id = draft["draft_id"]
    assert isinstance(draft_id, str) and draft_id, f"the draft was refused: {draft.get('problems')}"
    assert draft.get("problems") == []
    rows = await mk.constructions(BONFIRE)
    staged = [r for r in rows if r.get("status") == "staged" or draft_id in str(r.get("id", ""))]
    assert staged, "the new draft is not among the constructions"
    confirmed = await mk.confirm(BONFIRE, draft_id, TEACHER)
    assert confirmed["draft_id"] == draft_id
    assert confirmed.get("specs") or confirmed.get("phrases")

    # plan a message that reads as the phrase: its first step is the search
    plan = await mk.plan(BONFIRE, plan_body(f"{WORD} sourdough"))
    assert plan["status"] == "needs_tools", plan.get("diagnostics")
    plan_id = plan["plan_id"]
    assert isinstance(plan_id, str)
    search = first_step(plan)
    assert search["tool"] == "web_search"

    # resume with the search's result: the next step is the model's think
    plan = await mk.resume(BONFIRE, plan_id, [result(search, "committed", "Sourdough is a leavened bread.")])
    think = first_step(plan)
    assert think["tool"] == "think"

    # a probe step no plan issues is refused while the plan lives (409), and changes nothing
    with pytest.raises(Refused):
        await mk.resume(
            BONFIRE, plan_id, [result({**think, "step_id": f"{plan_id}:probe"}, "tool_error", None, "probe")]
        )

    # the think step done: the plan is answered
    plan = await mk.resume(BONFIRE, plan_id, [result(think, "committed", "Sourdough is bread raised with a starter.")])
    assert plan["status"] == "answered"

    # a failed step: repair, then retry brings back the same step
    plan = await mk.plan(BONFIRE, plan_body(f"{WORD} rye"))
    retry_id = plan["plan_id"]
    assert isinstance(retry_id, str)
    step = first_step(plan)
    plan = await mk.resume(BONFIRE, retry_id, [result(step, "tool_error", None, "search was down")])
    assert plan["status"] == "repair"
    plan = await mk.retry(BONFIRE, retry_id)
    assert plan["status"] == "needs_tools"
    assert first_step(plan)["step_id"] == step["step_id"]


async def test_a_plan_that_is_gone_is_plan_gone(mk: MkClient) -> None:
    row: dict[str, JsonValue] = {
        "step_id": "no-such-plan:0",
        "tool": "web_search",
        "args": {"query": "x"},
        "verdict": "committed",
        "value": "x",
        "detail": None,
    }
    with pytest.raises(PlanGone):
        await mk.resume(BONFIRE, "no-such-plan", [row])
    with pytest.raises(PlanGone):
        await mk.retry(BONFIRE, "no-such-plan")
