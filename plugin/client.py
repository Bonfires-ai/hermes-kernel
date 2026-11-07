"""Sync HTTP client for the turn service, with a circuit breaker.

Hermes runs hooks and tool handlers on worker threads; `httpx.Client` is thread-safe and `Breaker` takes a lock.
Only connection failures, timeouts and 5xx answers trip the breaker. A 4xx never does: the service answered.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_TIMEOUT_S = 30.0
BEGIN_TIMEOUT_S = 10.0
CONTEXT_TIMEOUT_S = 6.0
STATUS_TIMEOUT_S = 5.0


class ServiceError(Exception):
    """A call to the turn service did not give its answer."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Unavailable(ServiceError):
    """The service is not there to answer: a connect error, a timeout, a 5xx, a body that is not JSON, or an open
    breaker (then no request was sent)."""

    #: set when the breaker refused the call before any request
    short_circuited = False


class Refused(ServiceError):
    """The service answered with a 4xx; `detail` is its own words."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class Unauthorized(ServiceError):
    """A 401 or 403: the service is up and the token is wrong. Not an outage, so the breaker stays closed."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


def never_sent(exc: Unavailable) -> bool:
    """The request never reached the service: an open breaker, a refused connect or a connect timeout."""
    return exc.short_circuited or isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))


class Breaker:
    """Closed until `trip()`; then `allow()` is False for `open_s` seconds, after which one call is let through.
    That call's `reset()` closes it, its `trip()` re-opens it for another `open_s`."""

    def __init__(self, open_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._open_s = open_s
        self._clock = clock
        self._lock = threading.Lock()
        self._opened_at: float | None = None

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            now = self._clock()
            if now - self._opened_at >= self._open_s:
                self._opened_at = now  # the probe; everyone else waits another open_s unless it resets
                return True
            return False

    def trip(self) -> None:
        with self._lock:
            self._opened_at = self._clock()

    def reset(self) -> None:
        with self._lock:
            self._opened_at = None


def _seg(value: str) -> str:
    return quote(value, safe="")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list) and detail:  # FastAPI's 422: a list of problems
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return response.text[:500] or f"HTTP {response.status_code}"


class ServiceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        breaker: Breaker | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.breaker = breaker if breaker is not None else Breaker()
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, body: Any = None, *, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
        if not self.breaker.allow():
            exc = Unavailable("turn service: breaker open, call skipped")
            exc.short_circuited = True
            raise exc
        try:
            response = self._http.request(method, path, json=body, timeout=timeout)
        except httpx.HTTPError as exc:  # connect errors, timeouts, protocol errors
            self.breaker.trip()
            raise Unavailable(f"turn service: {type(exc).__name__}: {exc}") from exc
        status = response.status_code
        if status >= 500:
            self.breaker.trip()
            raise Unavailable(f"turn service answered {status}: {_detail(response)}")
        self.breaker.reset()  # it answered, so it is up
        if status in (401, 403):
            raise Unauthorized(status, _detail(response))
        if status >= 400:
            raise Refused(status, _detail(response))
        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            self.breaker.trip()
            raise Unavailable(f"turn service answered {status} with a body that is not JSON") from exc

    def _object(self, value: Any, what: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise Unavailable(f"turn service answered {what} with a {type(value).__name__}, not an object")
        return value

    # turn calls

    def begin(self, body: dict[str, Any], *, timeout: float = BEGIN_TIMEOUT_S) -> dict[str, Any]:
        return self._object(self._call("POST", "/v1/turns", body, timeout=timeout), "a turn")

    def get(self, turn_id: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        return self._object(self._call("GET", f"/v1/turns/{_seg(turn_id)}", timeout=timeout), "a turn")

    def offer(self, turn_id: str, body: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        path = f"/v1/turns/{_seg(turn_id)}/offers"
        return self._object(self._call("POST", path, body, timeout=timeout), "a turn")

    def calls(
        self, turn_id: str, tool: str, args: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        path = f"/v1/turns/{_seg(turn_id)}/calls"
        return self._object(self._call("POST", path, {"tool": tool, "args": args}, timeout=timeout), "a call")

    def report(
        self,
        turn_id: str,
        step_id: str,
        verdict: str,
        value: Any = None,
        detail: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        path = f"/v1/turns/{_seg(turn_id)}/report"
        body = {"step_id": step_id, "verdict": verdict, "value": value, "detail": detail}
        return self._object(self._call("POST", path, body, timeout=timeout), "a turn")

    def teach(self, body: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        return self._object(self._call("POST", "/v1/teaching", body, timeout=timeout), "a draft")

    def confirm(self, draft_id: str, body: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        path = f"/v1/teaching/{_seg(draft_id)}/confirm"
        return self._object(self._call("POST", path, body, timeout=timeout), "a confirmation")

    def put_source(
        self,
        platform: str,
        group: str,
        source: str,
        catalogue: dict[str, Any],
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        path = f"/v1/groups/{_seg(platform)}/{_seg(group)}/catalogue/{_seg(source)}"
        self._call("PUT", path, catalogue, timeout=timeout)

    # memory calls

    def post_messages(
        self, batch: list[dict[str, Any]], agent_handles: list[str], *, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        body = {"messages": batch, "agent_handles": agent_handles}
        return self._object(self._call("POST", "/v1/messages", body, timeout=timeout), "an add result")

    def close_session(self, conversation_id: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        body = {"conversation_id": conversation_id}
        return self._object(self._call("POST", "/v1/sessions/close", body, timeout=timeout), "a close result")

    def context(self, query: str, speaker: str | None, timeout: float = CONTEXT_TIMEOUT_S) -> str:
        """The recalled context, or "" when the service could not read memory (degraded)."""
        body = {"query": query, "speaker": speaker}
        view = self._object(self._call("POST", "/v1/context", body, timeout=timeout), "a context")
        text = view.get("context")
        return text if isinstance(text, str) else ""

    def status(self, timeout: float = STATUS_TIMEOUT_S) -> dict[str, Any]:
        return self._object(self._call("GET", "/v1/status", timeout=timeout), "the status")

    def retry_session(self, session_id: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._call("POST", f"/v1/sessions/{_seg(session_id)}/retry", timeout=timeout)
