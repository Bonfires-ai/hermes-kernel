"""``GET /v1/status``: whether memory is working, in one answer that never fails because ``mk serve`` is away.

``founded`` is whether the bonfire is a key in ``mk serve``'s ``/healthz`` ``bonfires`` (it exists once its first
session is built). With ``mk serve`` unreachable the answer still comes: ``founded`` false, ``spend`` and
``versions.mk`` null. The session numbers come from the shared store, so they hold either way.
"""

from __future__ import annotations

import importlib.metadata
from datetime import datetime

from pydantic import BaseModel

from kernel_turns.mk_client import KernelApi, MkError
from kernel_turns.models import JsonValue
from kernel_turns.sessions import SessionStore


class FailedSession(BaseModel):
    session_id: str
    conversation_id: str
    reason: str | None
    attempts: int


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


async def build_status(mk: KernelApi, sessions: SessionStore, bonfire_id: str) -> StatusView:
    try:
        health = await mk.health()
    except MkError:  # mk serve away: say so by what it cannot tell us
        health = {}
    version = health.get("version")
    waiting = await sessions.waiting(bonfire_id)
    return StatusView(
        bonfire_id=bonfire_id,
        founded=is_founded(health, bonfire_id),
        open_messages=sum(s.message_count for s in waiting if s.state == "open"),
        sessions_waiting=sum(1 for s in waiting if s.state in ("closed", "running", "held")),
        last_built_at=await sessions.last_built_at(bonfire_id),
        failed=[
            FailedSession(session_id=s.id, conversation_id=s.conversation_id, reason=s.reason, attempts=s.attempts)
            for s in waiting
            if s.state == "failed"
        ],
        held=next((s.reason for s in waiting if s.state == "held" and s.reason), None),
        spend=_spend(health, bonfire_id),
        versions=Versions(service=_service_version(), mk=version if isinstance(version, str) else None),
    )
