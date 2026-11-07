"""``GET /v1/status``: whether memory is working, in one answer that never fails because ``mk serve`` is away.

``founded`` is whether the bonfire is a key in ``mk serve``'s ``/healthz`` ``bonfires`` (it exists once its first
session is built). With ``mk serve`` unreachable the answer still comes: ``founded`` false, ``spend`` and
``versions.mk`` null. The session numbers come from the shared store, so they hold either way.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from datetime import datetime

from pydantic import BaseModel

from kernel_turns.handoff import REASON_MAX_CHARS, STATUS_TIMEOUT_S, HandoffApi, WorkerStatus
from kernel_turns.mk_client import KernelApi, MkError
from kernel_turns.models import JsonValue
from kernel_turns.sessions import Session, SessionStore


class FailedSession(BaseModel):
    session_id: str
    conversation_id: str
    reason: str | None
    attempts: int
    #: False for a session the worker holds: it cannot be retried from here
    retryable: bool = True


class Spend(BaseModel):
    charged_usd: float
    ceiling_usd: float


class Versions(BaseModel):
    service: str
    mk: str | None


class StatusView(BaseModel):
    bonfire_id: str
    founded: bool
    #: messages in sessions still open
    open_messages: int
    #: sessions closed, running or held: taken in, not yet built
    sessions_waiting: int
    last_built_at: datetime | None
    failed: list[FailedSession]
    #: why a session is held, if one is
    held: str | None
    spend: Spend | None
    versions: Versions
    #: the numbers came partly from a hosted worker
    hosted: bool = False


def _service_version() -> str:
    try:
        return importlib.metadata.version("kernel-turns")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _number(value: JsonValue) -> float | None:
    """A JSON number, or a numeric string: ``mk serve`` sends its Decimal amounts as strings ("0.030284058")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _spend(health: dict[str, JsonValue], bonfire_id: str) -> Spend | None:
    table = health.get("spend")
    row = table.get(bonfire_id) if isinstance(table, dict) else None
    if not isinstance(row, dict):
        return None
    charged, ceiling = _number(row.get("charged_usd")), _number(row.get("ceiling_usd"))
    return Spend(charged_usd=charged, ceiling_usd=ceiling) if charged is not None and ceiling is not None else None


def is_founded(health: dict[str, JsonValue], bonfire_id: str) -> bool:
    """Whether the bonfire is a key in ``/healthz``'s ``bonfires``: it exists once its first session is built."""
    bonfires = health.get("bonfires")
    return isinstance(bonfires, dict) and bonfire_id in bonfires


QUEUE_UNREADABLE = "the build queue can't be read right now"


def _put_off_held(waiting: list[Session]) -> str | None:
    """Hosted: the oldest of the bonfire's sessions that the hand-off put off, as a sentence."""
    off = sorted(
        (s for s in waiting if s.state == "closed" and s.reason), key=lambda s: (s.closed_at or s.opened_at, s.id)
    )
    if not off:
        return None
    return f"A session is waiting to be handed over: {off[0].reason}"


async def build_status(
    mk: KernelApi, sessions: SessionStore, bonfire_id: str, *, worker: HandoffApi | None = None
) -> StatusView:
    async def read_health() -> dict[str, JsonValue]:
        try:
            return await mk.health()
        except MkError:  # mk serve away: say so by what it cannot tell us
            return {}

    async def read_worker() -> WorkerStatus | None:
        if worker is None:
            return None
        try:  # a hung read is cut short, so the whole answer stays inside the plugin's time
            return await asyncio.wait_for(worker.worker_status(bonfire_id), STATUS_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - whatever the reason, the queue is unknown
            return None

    health, read = await asyncio.gather(read_health(), read_worker())
    version = health.get("version")
    waiting = await sessions.waiting(bonfire_id)
    own_waiting = sum(1 for s in waiting if s.state in ("closed", "running", "held"))
    failed = [
        FailedSession(session_id=s.id, conversation_id=s.conversation_id, reason=s.reason, attempts=s.attempts)
        for s in waiting
        if s.state == "failed"
    ]
    held = next((s.reason for s in waiting if s.state == "held" and s.reason), None)
    built = await sessions.last_built_at(bonfire_id)
    if worker is not None:
        if read is None:
            held = QUEUE_UNREADABLE
        else:
            own_waiting += read.waiting
            built = read.last_built_at
            failed += [
                FailedSession(
                    session_id=f.key.rpartition(":")[2],  # the key holds the chat's id; its last part is enough
                    conversation_id="",  # another chat's id is not for this one to see
                    reason=f.reason[:REASON_MAX_CHARS] if f.reason else f.reason,
                    attempts=f.attempts,
                    retryable=False,
                )
                for f in read.failed
            ]
            held = read.held or _put_off_held(waiting) or held
    return StatusView(
        bonfire_id=bonfire_id,
        founded=is_founded(health, bonfire_id),
        open_messages=sum(s.message_count for s in waiting if s.state == "open"),
        sessions_waiting=own_waiting,
        last_built_at=built,
        failed=failed,
        held=held,
        spend=_spend(health, bonfire_id),
        versions=Versions(service=_service_version(), mk=version if isinstance(version, str) else None),
        hosted=worker is not None,
    )
