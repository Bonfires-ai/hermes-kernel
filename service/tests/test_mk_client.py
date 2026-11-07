"""The ``mk serve`` runtime client over ``httpx.MockTransport``: status codes, request bodies, the one busy wait."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from kernel_turns.mk_client import KernelBusy, KernelDown, MkClient, NotTeacher, PlanGone, Refused

BONFIRE = "65f0c0ffee0000000000abcd"
Handler = Callable[[httpx.Request], httpx.Response]


def _refusal(reason: str) -> dict[str, object]:
    """The body every ``mk serve`` refusal carries (``serve.runtime.refusal``)."""
    return {"engine": "kernel", "degraded": True, "reason": reason}


class Waits:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def _client(handler: Handler, waits: Waits | None = None) -> MkClient:
    return MkClient("http://kernel-search:8000", transport=httpx.MockTransport(handler), sleep=waits or Waits())


async def test_client_maps_status_codes() -> None:
    seen: list[httpx.Request] = []
    plan_body: dict[str, object] = {
        "text": "brew thé for Kira Dace — café",
        "chat_id": "-100777",
        "speaker": "Kira Dace",
        "message_id": "42",
        "at": "2026-10-22T18:00:00+00:00",
        "participants": ["Kira Dace", "Milo"],
        "committed": [{"tool": "take", "args": {"item": "thé"}, "value": {"id": "t-1"}}],
    }
    plan_json = {"plan_id": "p1", "status": "needs_tools", "tool_steps": [], "licence": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == f"/v1/bonfires/{BONFIRE}/plan":
            return httpx.Response(200, json=plan_json)
        if path == f"/v1/bonfires/{BONFIRE}/plan/p-gone/resume":
            return httpx.Response(404, json=_refusal("no plan 'p-gone': the plan expired"))
        if path == f"/v1/bonfires/{BONFIRE}/plan/p1/resume":
            return httpx.Response(409, json=_refusal("plan p1: step(s) ['x'] are not pending"))
        if path == f"/v1/bonfires/{BONFIRE}/teach/d-1/confirm":
            return httpx.Response(403, json=_refusal("only Kira Dace may confirm draft d-1"))
        raise httpx.ConnectError("connection refused", request=request)

    mk = _client(handler)
    assert await mk.plan(BONFIRE, plan_body) == plan_json
    sent = json.loads(seen[0].content)
    assert sent == plan_body  # exactly the turn fields and committed: nothing added, nothing dropped
    assert set(sent) == {"text", "chat_id", "speaker", "message_id", "at", "participants", "committed"}

    with pytest.raises(PlanGone):
        await mk.resume(
            BONFIRE,
            "p-gone",
            [{"step_id": "p-gone:0:0", "tool": "take", "args": {}, "verdict": "committed", "value": None}],
        )
    with pytest.raises(Refused) as refused:
        await mk.resume(
            BONFIRE, "p1", [{"step_id": "x", "tool": "take", "args": {}, "verdict": "committed", "value": None}]
        )
    assert refused.value.detail == "plan p1: step(s) ['x'] are not pending"
    assert json.loads(seen[-1].content) == {
        "results": [{"step_id": "x", "tool": "take", "args": {}, "verdict": "committed", "value": None}]
    }
    with pytest.raises(NotTeacher):
        await mk.confirm(BONFIRE, "d-1", "Milo")
    assert json.loads(seen[-1].content) == {"by": "Milo"}
    with pytest.raises(KernelDown):
        await mk.constructions(BONFIRE)
    await mk.aclose()


async def test_a_timeout_is_kernel_down_and_a_404_on_the_plan_route_is_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/plan"):
            return httpx.Response(404, json=_refusal(f"unknown bonfire {BONFIRE!r}"))
        raise httpx.ReadTimeout("timed out", request=request)

    mk = _client(handler)
    with pytest.raises(Refused, match="unknown bonfire"):  # an unknown bonfire, never "the plan is gone"
        await mk.plan(BONFIRE, {"text": "look", "chat_id": "c"})
    with pytest.raises(KernelDown):
        await mk.retry(BONFIRE, "p1")
    await mk.aclose()


async def test_busy_waits_once_then_raises() -> None:
    answers = [
        httpx.Response(503, headers={"Retry-After": "1"}, json=_refusal("bonfire is teaching: resume again later")),
        httpx.Response(200, json={"plan_id": "p1", "status": "answered"}),
    ]

    def once_busy(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    waits = Waits()
    mk = _client(once_busy, waits)
    assert await mk.resume(
        BONFIRE, "p1", [{"step_id": "p1:0:0", "tool": "take", "args": {}, "verdict": "committed", "value": None}]
    ) == {
        "plan_id": "p1",
        "status": "answered",
    }
    assert waits.seconds == [1.0]

    def always_busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "30"}, json=_refusal("bonfire is teaching"))

    waits = Waits()
    mk = _client(always_busy, waits)
    with pytest.raises(KernelBusy) as busy:
        await mk.retry(BONFIRE, "p1")
    assert waits.seconds == [5.0]  # Retry-After is honoured once, capped at 5 s
    assert busy.value.retry_after == 5.0
    await mk.aclose()


async def test_reads_return_their_rows() -> None:
    rows = [{"id": "brew.d1.0", "kind": "taught"}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/constructions"):
            return httpx.Response(200, json={"engine": "kernel", "bonfire_id": BONFIRE, "constructions": rows})
        if path.endswith("/catalogue") and request.method == "GET":
            return httpx.Response(200, json={"sha256": "ab", "tools": [{"name": "look"}]})
        if path.endswith("/catalogue") and request.method == "PUT":
            assert json.loads(request.content) == {"schema_version": "kernel-catalogue/v1", "tools": []}
            return httpx.Response(200, json={"sha256": "cd", "tools": []})
        if path.endswith("/context"):
            assert json.loads(request.content) == {"query": "who has the herb?", "speaker": "Ana"}
            return httpx.Response(200, json={"engine": "kernel", "context": "Ana has the herb.", "degraded": False})
        return httpx.Response(500, text="boom")

    mk = _client(handler)
    assert await mk.constructions(BONFIRE) == rows
    assert await mk.get_catalogue(BONFIRE) == {"sha256": "ab", "tools": [{"name": "look"}]}
    assert await mk.put_catalogue(BONFIRE, {"schema_version": "kernel-catalogue/v1", "tools": []}) is None
    assert await mk.context(BONFIRE, "who has the herb?", "Ana") == "Ana has the herb."
    with pytest.raises(KernelDown):
        await mk.teach(BONFIRE, {"forms": ["brew {X}"], "body": ["take water"], "teacher": "Ana", "chat_id": "c"})
    await mk.aclose()
