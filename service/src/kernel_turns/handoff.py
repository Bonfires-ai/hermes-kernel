"""Hosted: closed sessions leave for a URL, where something else builds them.

With ``KT_HANDOFF_URL`` set no writer runs. Every ``KT_HANDOFF_EVERY_S`` a pass takes each bonfire's oldest closed session
and posts it to ``{url}/bonfires/{bonfire}/sessions``, with ``Authorization: Bearer <KT_HANDOFF_TOKEN>``:

* a 2xx is ``accepted``: the session is ``handed`` and its text goes, because the receiver holds it now;
* 400, 409, 413 or 422 is ``refused``: the session is ``failed`` with the answer's message and keeps its text, for
  ``/v1/sessions/{id}/retry``, and it stops holding up the sessions behind it;
* anything else is ``later``, so a broken seam never fails a session: a wrong seam token, a timeout, and a 404, which the
  route never answers and so means a wrong URL or a deploy-order slip. It stays ``closed`` and waits out ``retry_after``,
  and the sessions behind it wait too.

A session that raises (a row that cannot be read, a database blip) waits the same way, with the error's class as its
reason. A message that cannot be read stops its session until the row is mended: skipping it, as the writer does, would
send fewer ids, and that is a new job.

None is lost and none is built twice because a resend is the same job. The receiver keys a job on the sorted message ids,
and a resend sends the same ids: a closed session never gains a message (see ``sessions``) and keeps its text until it is
``handed``. So a crash between the answer and the mark only sends the same body again.

Logs carry ids, counts and this module's own words. Never a ``refused`` reason, which is the receiver's and can quote what
the person said; never an exception's text, which can quote what it was sent; never the token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from kernel_turns.sessions import Session, SessionStore, StoredMessage

log = logging.getLogger(__name__)

Body = dict[str, Any]

#: what the agent is called in memory (the plugin's ``capture.AGENT_HANDLE``): a hosted bonfire is created with it, and a
#: body with any other handle is refused for good, so a session's stored handles are not what is sent
AGENT_HANDLE = "hermes"
REFUSED_STATUSES = frozenset({400, 409, 413, 422})
REASON_MAX_CHARS = 500
#: the status read is inside the plugin's 5 s for ``/v1/status``, and a first-seen bonfire's guard inside its 3 s
STATUS_TIMEOUT_S = 2.0
BACKOFF_MAX_S = 600.0
#: past this the doubling is well over ``BACKOFF_MAX_S``, and ``2**attempts`` must stay small enough to be a float
_BACKOFF_MAX_DOUBLINGS = 32


@dataclass(frozen=True)
class Outcome:
    """What became of a send. A ``later`` reason is logged, so it holds only this service's own words."""

    kind: Literal["accepted", "refused", "later"]
    reason: str | None = None


class WorkerFailure(BaseModel):
    """A session the worker holds that failed to build. ``key`` is the worker's own name for it."""

    key: str
    conversation_id: str | None = None
    reason: str | None = None
    attempts: int


class WorkerStatus(BaseModel):
    """What the worker says of a bonfire (``GET {url}/bonfires/{id}/status``): its engine, the handles it was created
    with, how many sessions it holds unbuilt, those that failed, why it is held, and when it last built."""

    engine: str
    agent_handles: list[str]
    waiting: int
    failed: list[WorkerFailure]
    held: str | None = None
    last_built_at: datetime | None = None


class WorkerUnreadable(Exception):
    """The worker's status could not be read: away, an answer that is not one, or a bonfire it does not know."""


class HandoffApi(Protocol):
    async def send(self, bonfire_id: str, body: Body) -> Outcome: ...

    async def worker_status(self, bonfire_id: str) -> WorkerStatus:
        """Raises ``WorkerUnreadable`` when it cannot be read."""
        ...


def _refusal(resp: httpx.Response) -> str:
    """The answer's own words when they are one plain string, ``message`` or ``detail``. FastAPI's 422 ``detail`` is a list
    whose entries quote their ``input``, which can be the text, so it is never kept."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - not JSON: a proxy's page
        body = None
    if isinstance(body, dict):
        for field in ("message", "detail"):
            said = body.get(field)
            if isinstance(said, str) and said:
                return said[:REASON_MAX_CHARS]
    return f"the hand-off refused it with {resp.status_code}"


class HandoffClient:
    """``HandoffApi`` over HTTP. ``send`` never raises for what the seam does, and takes only the class of a transport
    error: its words can quote what it was sent."""

    def __init__(
        self, url: str, token: str, *, client: httpx.AsyncClient | None = None, timeout_s: float = 30.0
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._timeout_s = timeout_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, bonfire_id: str, body: Body) -> Outcome:
        url = f"{self._url}/bonfires/{quote(bonfire_id, safe='')}/sessions"
        try:
            resp = await self._client.post(url, json=body, headers=self._headers, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - transport, timeout, anything: the seam is away
            return Outcome("later", f"the hand-off did not answer: {type(exc).__name__}")
        if resp.is_success:
            return Outcome("accepted")
        if resp.status_code in REFUSED_STATUSES:
            return Outcome("refused", _refusal(resp))
        return Outcome("later", f"the hand-off answered {resp.status_code}")

    async def worker_status(self, bonfire_id: str) -> WorkerStatus:
        url = f"{self._url}/bonfires/{quote(bonfire_id, safe='')}/status"
        # The raise sits outside the `except` blocks: inside, the original error rides along, and a transport's or a
        # parser's words can quote what was sent or read.
        problem: str | None = None
        status: WorkerStatus | None = None
        try:
            resp = await self._client.get(url, headers=self._headers, timeout=STATUS_TIMEOUT_S)
            if resp.status_code != 200:
                problem = f"the hand-off's status answered {resp.status_code}"
            else:
                status = WorkerStatus.model_validate(resp.json())
                for row in status.failed:  # the receiver's words can quote what a person said: cut as a refusal is
                    row.reason = row.reason[:REASON_MAX_CHARS] if row.reason else row.reason
        except ValidationError:
            problem = "the hand-off's status was not what is expected"
        except ValueError:  # not JSON
            problem = "the hand-off's status was not what is expected"
        except Exception as exc:  # noqa: BLE001 - transport, timeout, anything: only the class is kept
            problem = f"the hand-off's status did not answer: {type(exc).__name__}"
        if status is None:
            raise WorkerUnreadable(problem or "the hand-off's status could not be read")
        return status


def _utc(when: datetime) -> datetime:
    # A client that is not tz-aware hands back naive UTC.
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _rising(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``messages`` in order, each one at least 1 ms after the one before: the writer's ``queue._rising`` (the service
    does not depend on the writer). A crash between storing a message and counting it leaves an orphan whose ``seq`` the
    next message takes, so a session can hold ties, and a tie must not make it unbuildable. Only what the kernel is handed
    changes: the stored time stays as it was sent."""
    last: datetime | None = None
    for message in messages:
        at = datetime.fromisoformat(message["timestamp"])
        if last is not None and at <= last:
            at = last + timedelta(milliseconds=1)
            message["timestamp"] = at.isoformat()
        last = at
    return messages


def _body(session: Session, messages: list[StoredMessage]) -> Body:
    """The session as the worker takes it; the same session always gives an equal body."""
    return {
        "conversation_id": session.conversation_id,
        "messages": _rising(
            [
                {
                    "message_id": m.message_id,
                    "speaker": m.speaker,
                    "text": m.text,
                    "timestamp": _utc(m.timestamp).isoformat(),
                    "addressees": list(m.addressees),
                }
                for m in messages
            ]
        ),
        "agent_handles": [AGENT_HANDLE],
    }


def _backoff_s(attempts: int, backoff_s: float) -> float:
    return min(backoff_s * 2 ** min(attempts, _BACKOFF_MAX_DOUBLINGS), BACKOFF_MAX_S)


async def _put_off(store: SessionStore, session: Session, reason: str, now: datetime, backoff_s: float) -> None:
    until = now + timedelta(seconds=_backoff_s(session.attempts, backoff_s))
    await store.put_off(session.bonfire_id, session.id, reason, until)


async def _hand_over(
    store: SessionStore, api: HandoffApi, session: Session, clock: Callable[[], datetime], backoff_s: float
) -> bool:
    """Send one session and mark it by the answer; whether it was handed over."""
    bonfire_id, session_id = session.bonfire_id, session.id
    messages = [m for m in await store.messages_of(bonfire_id, session_id) if m.text]
    if not messages:  # nothing to build, and the receiver takes no empty session
        await store.mark_handed(bonfire_id, session_id, clock())
        return True
    outcome = await api.send(bonfire_id, _body(session, messages))
    now = clock()
    if outcome.kind == "accepted":
        await store.mark_handed(bonfire_id, session_id, now)
        log.info("kernel_turns.handed_over: bonfire=%s session=%s messages=%d", bonfire_id, session_id, len(messages))
        return True
    if outcome.kind == "refused":
        await store.mark_refused(bonfire_id, session_id, outcome.reason or "the hand-off refused it", now)
        log.warning(
            "kernel_turns.handoff_refused: bonfire=%s session=%s messages=%d", bonfire_id, session_id, len(messages)
        )
        return False
    reason = outcome.reason or "the hand-off said later"
    await _put_off(store, session, reason, now, backoff_s)
    log.warning(
        "kernel_turns.handoff_later: bonfire=%s session=%s attempts=%d reason=%s",
        bonfire_id,
        session_id,
        session.attempts + 1,
        reason,
    )
    return False


async def _put_off_failed(
    store: SessionStore, session: Session, error: str, clock: Callable[[], datetime], backoff_s: float
) -> None:
    """A session that raised waits as a ``later`` one does; otherwise it goes again every pass, with no reason given."""
    bonfire_id, session_id = session.bonfire_id, session.id
    log.warning("kernel_turns.handoff_failed: bonfire=%s session=%s error=%s", bonfire_id, session_id, error)
    try:
        await _put_off(store, session, f"the hand-off failed: {error}", clock(), backoff_s)
    except Exception as exc:  # noqa: BLE001 - the database is away too: the next pass tries it again
        log.warning(
            "kernel_turns.handoff_put_off_failed: bonfire=%s session=%s error=%s",
            bonfire_id,
            session_id,
            type(exc).__name__,
        )


async def hand_over_once(
    store: SessionStore, api: HandoffApi, clock: Callable[[], datetime], backoff_s: float = 30.0
) -> int:
    """One pass: at most one session of each bonfire, the oldest, so a bonfire's sessions go in order and one bonfire's
    trouble stays its own. How many were handed over."""
    handed = 0
    for session in await store.closed_heads(clock()):
        error: str | None = None
        try:
            handed += await _hand_over(store, api, session, clock, backoff_s)
        except Exception as exc:  # noqa: BLE001 - only the class: its words can quote what it read
            error = type(exc).__name__
        if error is not None:
            await _put_off_failed(store, session, error, clock, backoff_s)
    return handed


async def hand_over_forever(
    store: SessionStore, api: HandoffApi, clock: Callable[[], datetime], every_s: float = 15.0, backoff_s: float = 30.0
) -> None:
    while True:
        await asyncio.sleep(every_s)
        try:
            await hand_over_once(store, api, clock, backoff_s)
        except Exception as exc:  # noqa: BLE001 - a database blip must not end the loop
            log.warning("kernel_turns.handoff_pass_failed: error=%s", type(exc).__name__)
