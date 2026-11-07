"""The client of ``mk serve``'s runtime routes (memory-kernel, ``memory_kernel/serve/http.py``).

This service is the only caller of plan, resume, retry, teach, confirm, constructions and catalogue. ``mk serve`` has
no auth and no host port: it is reached on the stack's own network only.

Status codes become domain exceptions (the app maps them to HTTP):

* ``PlanGone``: a 404 on a plan's own route (``…/plan/{plan_id}/resume`` or ``…/retry``): the plan expired (15 min
  after its last use), was evicted (256 per bonfire), or the bonfire's catalogue was reloaded. The caller re-plans with
  ``committed=``. A 404 anywhere else (``POST …/plan`` included) names an unknown bonfire: that is a ``Refused``, never
  "the plan is gone", so a re-plan can never loop on it.
* ``Refused(detail)``: 400, 409 or 422, carrying the kernel's ``reason``.
* ``NotFounded`` (a ``Refused``): a 404 off the plan routes: no such bonfire yet (it is founded by the first session).
* ``NotTeacher``: 403 (a confirm by someone other than the draft's teacher).
* ``KernelBusy``: a 503 again after waiting its ``Retry-After`` once (capped at 5 s; 1 s when it names none).
* ``KernelDown``: a connect error, a timeout, any other status, or a body that is not JSON of the expected shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol
from urllib.parse import quote

import httpx

from kernel_turns.models import JsonValue

#: the longest wait a 503's Retry-After buys before the one retry
MAX_BUSY_WAIT_S = 5.0
#: the wait when a 503 names no Retry-After (a teach or confirm 503 carries none)
DEFAULT_BUSY_WAIT_S = 1.0

Sleep = Callable[[float], Awaitable[None]]


class MkError(Exception):
    """Something ``mk serve`` refused or could not do."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class PlanGone(MkError):
    """The plan named in the route is unknown or expired on the kernel (404 on resume or retry)."""


class Refused(MkError):
    """The kernel refused the request (400, 409, 422); ``detail`` is its reason."""


#: how a ``NotFounded`` detail opens: a turn's problems carry it when its plan failed for want of a bonfire
NOT_FOUNDED_SAID = "no conversation has been saved yet, so there is no memory"


class NotFounded(Refused):
    """The bonfire does not exist yet: it is founded when the first session is built, which can be many minutes after
    install (mk answers 404). ``detail`` is neutral and factual; the kernel's own words follow in brackets."""

    def __init__(self, kernel_detail: str = "") -> None:
        said = NOT_FOUNDED_SAID
        super().__init__(f"{said} [{kernel_detail}]" if kernel_detail else said)


class NotTeacher(MkError):
    """A confirm by someone other than the draft's teacher (403)."""


class KernelBusy(MkError):
    """The bonfire is busy (teaching, reloading) even after waiting once; try again after ``retry_after`` seconds."""

    def __init__(self, detail: str, retry_after: float) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class KernelDown(MkError):
    """``mk serve`` cannot be reached, timed out, or answered something this client cannot read."""


class KernelApi(Protocol):
    """What the turn service needs from ``mk serve`` (``MkClient``; tests inject a fake)."""

    async def plan(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]: ...

    async def resume(
        self, bonfire: str, plan_id: str, results: Sequence[Mapping[str, JsonValue]]
    ) -> dict[str, JsonValue]: ...

    async def retry(self, bonfire: str, plan_id: str) -> dict[str, JsonValue]: ...

    async def teach(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]: ...

    async def confirm(self, bonfire: str, draft_id: str, by: str) -> dict[str, JsonValue]: ...

    async def constructions(self, bonfire: str) -> list[dict[str, JsonValue]]: ...

    async def get_catalogue(self, bonfire: str) -> dict[str, JsonValue]: ...

    async def put_catalogue(self, bonfire: str, catalogue: Mapping[str, JsonValue]) -> None: ...

    async def context(self, bonfire: str, query: str, speaker: str | None) -> tuple[str, bool]: ...

    async def health(self) -> dict[str, JsonValue]: ...


def _seg(value: str) -> str:
    return quote(value, safe="")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("reason", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text[:500]


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return DEFAULT_BUSY_WAIT_S
    return min(max(seconds, 0.0), MAX_BUSY_WAIT_S)


def _object(body: JsonValue, what: str) -> dict[str, JsonValue]:
    if not isinstance(body, dict):
        raise KernelDown(f"mk serve answered {what} with a {type(body).__name__}, not an object")
    return body


class MkClient:
    """One ``httpx.AsyncClient`` per client; each route has its own timeout (``Settings.*_timeout_s``)."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        plan_timeout_s: float = 30,
        resume_timeout_s: float = 45,
        teach_timeout_s: float = 30,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=plan_timeout_s)
        self._plan_timeout = plan_timeout_s
        self._resume_timeout = resume_timeout_s
        self._teach_timeout = teach_timeout_s
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- turns ----------------------------------------------------------------------------------------------------

    async def plan(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        out = await self._call("POST", f"{self._root(bonfire)}/plan", body=dict(body), timeout=self._plan_timeout)
        return _object(out, "a plan")

    async def resume(
        self, bonfire: str, plan_id: str, results: Sequence[Mapping[str, JsonValue]]
    ) -> dict[str, JsonValue]:
        path = f"{self._root(bonfire)}/plan/{_seg(plan_id)}/resume"
        body: dict[str, JsonValue] = {"results": [dict(r) for r in results]}
        out = await self._call("POST", path, body=body, timeout=self._resume_timeout, plan_route=True)
        return _object(out, "a resume")

    async def retry(self, bonfire: str, plan_id: str) -> dict[str, JsonValue]:
        path = f"{self._root(bonfire)}/plan/{_seg(plan_id)}/retry"
        out = await self._call("POST", path, timeout=self._resume_timeout, plan_route=True)
        return _object(out, "a retry")

    # -- teaching -------------------------------------------------------------------------------------------------

    async def teach(self, bonfire: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        out = await self._call("POST", f"{self._root(bonfire)}/teach", body=dict(body), timeout=self._teach_timeout)
        return _object(out, "a teach")

    async def confirm(self, bonfire: str, draft_id: str, by: str) -> dict[str, JsonValue]:
        path = f"{self._root(bonfire)}/teach/{_seg(draft_id)}/confirm"
        out = await self._call("POST", path, body={"by": by}, timeout=self._teach_timeout)
        return _object(out, "a confirm")

    async def constructions(self, bonfire: str) -> list[dict[str, JsonValue]]:
        out = _object(await self._call("GET", f"{self._root(bonfire)}/constructions"), "constructions")
        rows = out.get("constructions")
        if not isinstance(rows, list):
            raise KernelDown("mk serve answered constructions without a constructions list")
        return [_object(row, "a construction row") for row in rows]

    # -- the catalogue ----------------------------------------------------------------------------------------------

    async def get_catalogue(self, bonfire: str) -> dict[str, JsonValue]:
        return _object(await self._call("GET", f"{self._root(bonfire)}/catalogue"), "the catalogue")

    async def put_catalogue(self, bonfire: str, catalogue: Mapping[str, JsonValue]) -> None:
        # the reload waits for the plans in flight (up to MK_SERVE_TEACH_WAIT_MS): as long as a teach
        await self._call("PUT", f"{self._root(bonfire)}/catalogue", body=dict(catalogue), timeout=self._teach_timeout)

    # -- memory -----------------------------------------------------------------------------------------------------

    async def context(self, bonfire: str, query: str, speaker: str | None) -> tuple[str, bool]:
        """The recalled text and whether mk could not read (``degraded``)."""
        body: dict[str, JsonValue] = {"query": query}
        if speaker is not None:
            body["speaker"] = speaker
        out = _object(await self._call("POST", f"{self._root(bonfire)}/context", body=body), "a context")
        text = out.get("context")
        return (text if isinstance(text, str) else ""), out.get("degraded") is True

    async def health(self) -> dict[str, JsonValue]:
        """``GET /healthz``. A 503 (mk is warming) with an object body is an answer, not an error; nothing is retried."""
        response = await self._send("GET", "/healthz", None, 5.0)
        try:
            body: JsonValue = response.json()
        except ValueError:
            body = None
        if response.status_code in (200, 503) and isinstance(body, dict):
            return body
        raise KernelDown(f"mk serve answered GET /healthz with {response.status_code}")

    # -- transport --------------------------------------------------------------------------------------------------

    @staticmethod
    def _root(bonfire: str) -> str:
        return f"/v1/bonfires/{_seg(bonfire)}"

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: JsonValue | None = None,
        timeout: float | None = None,
        plan_route: bool = False,
    ) -> JsonValue:
        response = await self._send(method, path, body, timeout)
        if response.status_code == 503:
            wait = _retry_after(response)
            await self._sleep(wait)
            response = await self._send(method, path, body, timeout)
            if response.status_code == 503:
                raise KernelBusy(_detail(response), retry_after=wait)
        status = response.status_code
        if 200 <= status < 300:
            try:
                return response.json()
            except ValueError as exc:
                raise KernelDown(f"mk serve answered {method} {path} with a body that is not JSON") from exc
        detail = _detail(response)
        if status == 404 and plan_route:
            raise PlanGone(detail)
        if status == 404:
            raise NotFounded(detail)
        if status == 403:
            raise NotTeacher(detail)
        if status in (400, 404, 409, 422):
            raise Refused(detail)
        raise KernelDown(f"mk serve answered {method} {path} with {status}: {detail}")

    async def _send(self, method: str, path: str, body: JsonValue | None, timeout: float | None) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                path,
                json=body,
                timeout=timeout if timeout is not None else self._plan_timeout,
            )
        except httpx.TransportError as exc:  # connect errors and every timeout
            raise KernelDown(f"mk serve unreachable ({type(exc).__name__}): {exc}") from exc
