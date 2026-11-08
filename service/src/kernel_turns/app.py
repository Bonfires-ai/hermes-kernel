"""The turn service's HTTP surface.

Every route but ``/healthz`` needs ``Authorization: Bearer <key>``. The ``caller`` dependency turns the bearer into a
``Caller`` and every ``/v1`` route takes its bonfire from that and nowhere else, so a turn, session or draft of another
bonfire is unknown to it: 404, never 403. With no ``authenticate`` injected the key is ``KT_TOKEN`` for the one
configured bonfire (``callers.token_check``: an empty ``KT_TOKEN`` refuses every request), warmed at start; otherwise a
bonfire is warmed when its caller first comes. With a ``handoff`` (``KT_HANDOFF_URL``) a loop beside the quiet sweep hands
closed sessions over, and no writer runs; without one there is no loop and closed sessions wait for the writer.

Routes delegate to ``TurnEngine`` and ``Teaching``; the domain exceptions they raise are mapped here: ``Unauthorized``
401, ``ReadOnlyKey`` 403, ``KeysAway`` 503 with ``Retry-After``, ``UnknownTurn`` 404, ``Refused`` 409, ``NotTeacher``
403, ``KernelBusy`` 503 with ``Retry-After``, ``KernelDown`` 502, an invalid body 422.
"""

# No `from __future__ import annotations` in this file: FastAPI resolves a string annotation against the module's
# globals, and `Depends(caller)` names a local of `create_app`, so `who` would silently become a query parameter.

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import AsyncMongoClient

from kernel_turns.callers import (
    KEYS_AWAY,
    Authenticate,
    Caller,
    KeyCheck,
    KeysAway,
    ReadOnlyKey,
    Unauthorized,
    WrongBonfire,
    bonfire_guard,
    token_check,
)
from kernel_turns.calls import CallView
from kernel_turns.config import Settings
from kernel_turns.handoff import HandoffApi, HandoffClient, hand_over_forever
from kernel_turns.mk_client import KernelApi, KernelBusy, KernelDown, MkClient, MkError, NotTeacher, Refused
from kernel_turns.models import BodyStep, JsonValue, LedgerRow, Runner, TurnRecord, Verdict
from kernel_turns.sessions import (
    AddRequest,
    AddResult,
    CloseRequest,
    CloseResult,
    MemorySessionStore,
    MongoSessionStore,
    SessionStore,
    SessionTake,
)
from kernel_turns.status import StatusView, build_status
from kernel_turns.store import Doc, MongoTurnStore, TurnStore
from kernel_turns.teaching import ConfirmRequest, ConfirmView, DraftView, TeachDraft, Teaching
from kernel_turns.turns import BeginTurn, TurnEngine, TurnView, UnknownTurn
from kernel_turns.verbs import Verb
from kernel_turns.warm import WarmPool

log = logging.getLogger(__name__)

#: how long a caller waits when keys cannot be checked
KEYS_RETRY_AFTER_S = 5


class CallRequest(BaseModel):
    tool: str = Field(min_length=1)
    args: dict[str, JsonValue] = Field(default_factory=dict)


class OfferRequest(BaseModel):
    """The agent's offer to teach a phrase, as it would draft it."""

    forms: list[str] = Field(min_length=1)
    body: list[BodyStep] = Field(min_length=1)
    defaults: dict[str, str] = Field(default_factory=dict)


class ReportRequest(BaseModel):
    step_id: str = Field(min_length=1)
    verdict: Verdict
    #: the tool's result (null when it returned nothing)
    value: JsonValue = None
    detail: str | None = None


class ContextRequest(BaseModel):
    query: str = Field(min_length=1)
    speaker: str | None = None


class ContextView(BaseModel):
    context: str
    #: true when memory could not be read (no bonfire yet, or mk away): the plugin treats it as no recall
    degraded: bool


class TurnTrace(TurnRecord):
    """A turn with its ledger rows: the trace."""

    ledger: list[LedgerRow]


def _error(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def _install_errors(app: FastAPI) -> None:
    async def unauthorized(_: Request, exc: Exception) -> JSONResponse:
        return _error(401, str(exc), {"WWW-Authenticate": "Bearer"})

    async def read_only(_: Request, exc: Exception) -> JSONResponse:
        return _error(403, str(exc))

    async def wrong_bonfire(_: Request, exc: Exception) -> JSONResponse:
        return _error(403, str(exc))

    async def keys_away(_: Request, __: Exception) -> JSONResponse:
        # Always the one sentence: what failed, and its status, is in the warning `callers` logged.
        return _error(503, KEYS_AWAY, {"Retry-After": str(KEYS_RETRY_AFTER_S)})

    async def not_found(_: Request, exc: Exception) -> JSONResponse:
        return _error(404, str(exc))

    async def refused(_: Request, exc: Exception) -> JSONResponse:
        return _error(409, exc.detail if isinstance(exc, MkError) else str(exc))

    async def not_teacher(_: Request, exc: Exception) -> JSONResponse:
        return _error(403, exc.detail if isinstance(exc, MkError) else str(exc))

    async def busy(_: Request, exc: Exception) -> JSONResponse:
        wait = exc.retry_after if isinstance(exc, KernelBusy) else 1.0
        detail = exc.detail if isinstance(exc, MkError) else str(exc)
        return _error(503, detail, {"Retry-After": str(max(1, math.ceil(wait)))})

    async def down(_: Request, exc: Exception) -> JSONResponse:
        log.warning("kernel_turns.kernel_down: %s", exc)
        return _error(502, exc.detail if isinstance(exc, MkError) else str(exc))

    async def other_kernel_error(_: Request, exc: Exception) -> JSONResponse:
        return _error(409, exc.detail if isinstance(exc, MkError) else str(exc))  # PlanGone, should one surface

    app.add_exception_handler(Unauthorized, unauthorized)
    app.add_exception_handler(ReadOnlyKey, read_only)
    app.add_exception_handler(WrongBonfire, wrong_bonfire)
    app.add_exception_handler(KeysAway, keys_away)
    app.add_exception_handler(UnknownTurn, not_found)
    app.add_exception_handler(Refused, refused)
    app.add_exception_handler(NotTeacher, not_teacher)
    app.add_exception_handler(KernelBusy, busy)
    app.add_exception_handler(KernelDown, down)
    app.add_exception_handler(MkError, other_kernel_error)


SWEEP_EVERY_S = 30


async def _close_quiet_sessions(take: SessionTake) -> None:
    while True:
        await asyncio.sleep(SWEEP_EVERY_S)
        try:
            await take.close_idle()
        except Exception:  # a database blip must not end the sweep
            log.exception("kernel_turns.close_idle_failed")


def create_app(
    settings: Settings,
    *,
    mk: KernelApi | None = None,
    store: TurnStore | None = None,
    sessions: SessionStore | None = None,
    clock: Callable[[], datetime] | None = None,
    authenticate: Authenticate | None = None,
    handoff: HandoffApi | None = None,
) -> FastAPI:
    """The app over ``settings``; ``mk`` and ``store`` are injected by tests (default: ``MkClient`` and
    ``MongoTurnStore``, owned and closed by the app). ``authenticate`` says who a bearer is (default: ``token_check``
    over ``KT_TOKEN`` and ``KT_BONFIRE_ID``). ``handoff`` takes closed sessions (default: a ``HandoffClient`` when
    ``KT_HANDOFF_URL`` is set, owned and closed by the app; otherwise none, and no loop runs)."""
    client: MkClient | None = None
    kernel: KernelApi
    if mk is None:
        client = MkClient(
            settings.mk_url,
            plan_timeout_s=settings.plan_timeout_s,
            resume_timeout_s=settings.resume_timeout_s,
            teach_timeout_s=settings.teach_timeout_s,
        )
        kernel = client
    else:
        kernel = mk
    mongo: AsyncMongoClient[Doc] | None = None
    mongo_store: MongoTurnStore | None = None
    mongo_sessions: MongoSessionStore | None = None
    turns_store: TurnStore
    session_store: SessionStore
    if store is None:
        mongo = AsyncMongoClient(settings.mongo_uri.get_secret_value(), tz_aware=True)
        mongo_store = MongoTurnStore(mongo, settings.mongo_db, retention_days=settings.retention_days)
        turns_store = mongo_store
        mongo_sessions = MongoSessionStore(mongo, settings.mongo_db)
        session_store = sessions or mongo_sessions
    else:  # tests inject the stores; sessions then default to memory, never to a database
        turns_store = store
        session_store = sessions or MemorySessionStore()
    now = clock or (lambda: datetime.now(UTC))
    take = SessionTake(session_store, settings.session_idle_s, settings.session_max_messages, now)
    engine = TurnEngine(kernel, turns_store)
    teaching = Teaching(kernel, turns_store)
    warm = WarmPool(kernel, teaching)
    handoff_client: HandoffClient | None = None
    if handoff is None and settings.handoff_url is not None:
        handoff = handoff_client = HandoffClient(settings.handoff_url, settings.handoff_token.get_secret_value())

    key_check: KeyCheck | None = None
    check: Authenticate
    if authenticate is not None:
        check = authenticate
    elif settings.auth_url is not None:
        assert handoff is not None  # Settings: the two hosted URLs come together
        key_check = KeyCheck(settings.auth_url, settings.auth_token.get_secret_value())
        check = bonfire_guard(key_check, handoff)
    else:
        assert settings.bonfire_id is not None  # Settings: local means a bonfire
        check = token_check(settings.token.get_secret_value(), settings.bonfire_id)

    async def caller(authorization: Annotated[str | None, Header()] = None) -> Caller:
        scheme, _, bearer = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            raise Unauthorized("a bearer token is required")
        who = await check(bearer)
        warm.ensure(who.bonfire_id)
        return who

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if mongo_store is not None:
            await mongo_store.ensure_indexes()  # the ledger's unique key is what makes a report idempotent
        if mongo_sessions is not None and session_store is mongo_sessions:
            await mongo_sessions.ensure_indexes()  # one open session per chat
        loops = [asyncio.create_task(_close_quiet_sessions(take))]
        if handoff is not None:
            loops.append(asyncio.create_task(hand_over_forever(session_store, handoff, now, settings.handoff_every_s)))
        if settings.bonfire_id is not None and authenticate is None:  # the one bonfire is known now
            warm.ensure(settings.bonfire_id)
        try:
            yield
        finally:
            await warm.stop()
            for loop in loops:
                loop.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop
            if client is not None:
                await client.aclose()
            if key_check is not None:
                await key_check.aclose()
            if handoff_client is not None:
                await handoff_client.aclose()
            if mongo is not None:
                await mongo.close()

    # No generated docs or schema: only /healthz is open without the bearer.
    app = FastAPI(title="kernel-turns", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.caller = caller
    _install_errors(app)
    # On the router too, so a route that forgets `who` is still closed. It runs once per request either way.
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(caller)])

    @v1.post("/turns")
    async def begin_turn(who: Annotated[Caller, Depends(caller)], req: BeginTurn) -> TurnView:
        return await engine.begin(who.bonfire_id, req)

    @v1.get("/turns/{turn_id}")
    async def get_turn(who: Annotated[Caller, Depends(caller)], turn_id: str) -> TurnView:
        return await engine.get(who.bonfire_id, turn_id)

    @v1.post("/turns/{turn_id}/calls")
    async def call_tool(who: Annotated[Caller, Depends(caller)], turn_id: str, req: CallRequest) -> CallView:
        try:
            return await engine.call(who.bonfire_id, turn_id, req.tool, req.args)
        except MkError as exc:
            # The class and how many arguments, so a failure can be read from the logs: never `detail`, which can quote
            # what the person said.
            why = exc.why if isinstance(exc, Refused) else type(exc).__name__
            log.warning(
                "kernel_turns.call_refused: reason=%s tool=%s args=%d turn=%s", why, req.tool, len(req.args), turn_id
            )
            raise

    @v1.post("/turns/{turn_id}/offers")
    async def offer_phrase(who: Annotated[Caller, Depends(caller)], turn_id: str, req: OfferRequest) -> TurnView:
        return await engine.offer(who.bonfire_id, turn_id, req.forms, req.body, req.defaults)

    @v1.post("/turns/{turn_id}/report")
    async def report_step(who: Annotated[Caller, Depends(caller)], turn_id: str, req: ReportRequest) -> TurnView:
        return await engine.report(who.bonfire_id, turn_id, req.step_id, req.verdict, req.value, req.detail)

    @v1.post("/turns/{turn_id}/retry")
    async def retry_step(who: Annotated[Caller, Depends(caller)], turn_id: str) -> TurnView:
        return await engine.retry(who.bonfire_id, turn_id)

    @v1.post("/teaching")
    async def teach(who: Annotated[Caller, Depends(caller)], req: TeachDraft) -> DraftView:
        return await teaching.begin(who.bonfire_id, req)

    @v1.post("/teaching/{draft_id}/confirm")
    async def confirm(who: Annotated[Caller, Depends(caller)], draft_id: str, req: ConfirmRequest) -> ConfirmView:
        return await teaching.confirm(who.bonfire_id, draft_id, req)

    @v1.put("/groups/{platform}/{group}/catalogue/{source}", status_code=204)
    async def put_catalogue(
        who: Annotated[Caller, Depends(caller)],
        platform: str,
        group: str,
        source: Runner,
        catalogue: dict[str, JsonValue],
    ) -> Response:
        await teaching.put_catalogue(who.bonfire_id, platform, group, catalogue, source)
        return Response(status_code=204)

    @v1.get("/groups/{platform}/{group}/verbs")
    async def verbs(who: Annotated[Caller, Depends(caller)], platform: str, group: str) -> list[Verb]:
        return await teaching.verbs(who.bonfire_id, platform, group)

    @v1.get("/groups/{platform}/{group}/turns")
    async def recent_turns(
        who: Annotated[Caller, Depends(caller)],
        platform: str,
        group: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> list[TurnTrace]:
        return [
            TurnTrace(**turn.model_dump(), ledger=ledger)
            for turn, ledger in await engine.recent(who.bonfire_id, platform, group, limit)
        ]

    @v1.post("/messages")
    async def add_messages(who: Annotated[Caller, Depends(caller)], req: AddRequest) -> AddResult:
        return await take.add(who.bonfire_id, req.messages, req.agent_handles)

    @v1.post("/sessions/close")
    async def close_session(who: Annotated[Caller, Depends(caller)], req: CloseRequest) -> CloseResult:
        return CloseResult(closed=await take.close(who.bonfire_id, req.conversation_id))

    @v1.post("/sessions/{session_id}/retry")
    async def retry_session(who: Annotated[Caller, Depends(caller)], session_id: str) -> Response:
        outcome = await take.retry(who.bonfire_id, session_id)
        if outcome == "unknown":
            raise HTTPException(404, f"no session {session_id!r}")
        if outcome == "not_failed":
            raise HTTPException(409, "only a failed session can be retried")
        return Response(status_code=204)

    @v1.post("/context")
    async def recall(who: Annotated[Caller, Depends(caller)], req: ContextRequest) -> ContextView:
        try:
            text, degraded = await kernel.context(who.bonfire_id, req.query, req.speaker)
        except MkError as exc:  # no bonfire yet, busy or away: never an error for the plugin
            log.info("kernel_turns.context_unavailable: %s", exc)
            return ContextView(context="", degraded=True)
        return ContextView(context=text, degraded=degraded)

    @v1.get("/status")
    async def status(who: Annotated[Caller, Depends(caller)]) -> StatusView:
        return await build_status(kernel, session_store, who.bonfire_id, worker=handoff)

    app.include_router(v1)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
