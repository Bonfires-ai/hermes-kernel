"""Warming: once the bonfire exists, the kept catalogue is installed and one throwaway plan is sent; then it is done."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from fastapi.testclient import TestClient
from pydantic import SecretStr

from kernel_turns.app import create_app
from kernel_turns.callers import Caller
from kernel_turns.config import Settings
from kernel_turns.models import JsonValue
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import Teaching
from kernel_turns.warm import Warmer, WarmPool, warm_until_done
from tests.fakes import FakeMk
from tests.test_sources import AGENT, BONFIRE

TOKEN = "s3cret-token"


def _setup() -> tuple[FakeMk, Teaching, Warmer]:
    mk = FakeMk()
    mk.unborn = True
    teaching = Teaching(mk, MemoryTurnStore())
    return mk, teaching, Warmer(mk, teaching, BONFIRE)


def _found(mk: FakeMk) -> None:
    mk.unborn = False
    mk.calls.clear()  # what the service did before the bonfire existed does not count
    mk.healthz = {"status": "ok", "version": "2.0.0", "bonfires": {BONFIRE: {}}, "spend": {}}


async def test_founding_installs_the_kept_catalogue_and_warms() -> None:
    mk, teaching, warmer = _setup()
    await teaching.put_catalogue(BONFIRE, "hermes", "main", AGENT)  # before founding: kept, installed nowhere
    assert await warmer.tick() is False
    assert mk.calls_of("plan") == []
    _found(mk)
    assert await warmer.tick() is True
    puts = mk.calls_of("put_catalogue")
    assert len(puts) == 1 and "web_search" in str(puts[0])
    plans = mk.calls_of("plan")
    assert len(plans) == 1 and isinstance(plans[0], dict) and plans[0]["chat_id"] == "__warm__"


async def test_tick_does_it_once() -> None:
    mk, teaching, warmer = _setup()
    await teaching.put_catalogue(BONFIRE, "hermes", "main", AGENT)
    _found(mk)
    assert await warmer.tick() is True
    assert await warmer.tick() is True
    assert len(mk.calls_of("put_catalogue")) == 1
    assert len(mk.calls_of("plan")) == 1


async def test_a_refused_install_retries_next_tick() -> None:
    mk, teaching, warmer = _setup()
    await teaching.put_catalogue(BONFIRE, "hermes", "main", AGENT)
    _found(mk)
    mk.busy = True  # PUT refused with 503
    assert await warmer.tick() is False
    mk.busy = False
    assert await warmer.tick() is True
    assert len(mk.calls_of("plan")) == 1


async def test_an_unborn_bonfire_with_nothing_kept_is_not_warmed() -> None:
    mk, _, warmer = _setup()
    assert await warmer.tick() is False
    assert mk.calls_of("plan") == []


async def test_a_second_warm_with_nothing_changed_makes_no_put() -> None:
    """An install drops the bonfire's runtime and its staged drafts, and the kernel keeps its catalogue in the graph: a
    restarted service whose stored catalogue is what it would install warms the bonfire without installing again."""
    mk, store = FakeMk(), MemoryTurnStore()
    _found(mk)
    await Teaching(mk, store).put_catalogue(BONFIRE, "hermes", "main", AGENT)  # installed and stored before the restart
    installed = mk.catalogues[BONFIRE]
    mk.calls.clear()
    warmer = Warmer(mk, Teaching(mk, store), BONFIRE)  # the restarted service, on the same store
    assert await warmer.tick() is True
    assert mk.calls_of("put_catalogue") == []
    assert mk.catalogues[BONFIRE] == installed
    assert len(mk.calls_of("plan")) == 1


async def test_boot_installs_a_kept_catalogue_the_kernel_never_took() -> None:
    mk, store = FakeMk(), MemoryTurnStore()
    mk.unborn = True  # the catalogue is kept and installed nowhere
    await Teaching(mk, store).put_catalogue(BONFIRE, "hermes", "main", AGENT)
    _found(mk)
    warmer = Warmer(mk, Teaching(mk, store), BONFIRE)  # the restarted service
    assert await warmer.tick() is True
    assert len(mk.calls_of("put_catalogue")) == 1 and "web_search" in str(mk.catalogues[BONFIRE])


async def test_boot_installs_again_when_the_stored_catalogue_is_not_what_it_would_install() -> None:
    mk, store = FakeMk(), MemoryTurnStore()
    _found(mk)
    await Teaching(mk, store).put_catalogue(BONFIRE, "hermes", "main", AGENT)
    stale: dict[str, JsonValue] = {
        "schema_version": "kernel-catalogue/v1",
        "tools": [],
    }  # e.g. merged by an older service
    await store.save_catalogue(BONFIRE, "hermes", "main", stale)
    mk.calls.clear()
    warmer = Warmer(mk, Teaching(mk, store), BONFIRE)
    assert await warmer.tick() is True
    assert len(mk.calls_of("put_catalogue")) == 1 and "web_search" in str(mk.catalogues[BONFIRE])
    assert await store.catalogue(BONFIRE, "hermes", "main") == mk.catalogues[BONFIRE]


async def test_a_tick_that_raises_does_not_end_the_loop() -> None:
    class Flaky:
        n = 0

        async def tick(self) -> bool:
            self.n += 1
            if self.n == 1:
                raise RuntimeError("a database blip")
            return True

    flaky = Flaky()
    await asyncio.wait_for(warm_until_done(flaky, every_s=0.001), 2)  # type: ignore[arg-type]
    assert flaky.n == 2


def _founded(*bonfires: str) -> dict[str, JsonValue]:
    return {"status": "ok", "version": "2.0.0", "bonfires": {b: {} for b in bonfires}, "spend": {}}


def _warm_plans(mk: FakeMk) -> list[str]:
    return sorted(bonfire for call, bonfire in mk.seen if call == "plan")


def _settle(done: Callable[[], bool]) -> None:
    """The warmer runs on the test client's loop, in another thread: wait for it, briefly."""
    deadline = time.monotonic() + 2
    while not done() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert done()


async def test_warm_pool_starts_one_warmer_per_bonfire() -> None:
    mk = FakeMk()
    mk.healthz = _founded("a", "b")
    pool = WarmPool(mk, Teaching(mk, MemoryTurnStore()), every_s=0.001)
    before = asyncio.all_tasks()
    pool.ensure("a")
    pool.ensure("a")
    pool.ensure("b")
    started = asyncio.all_tasks() - before
    assert len(started) == 2
    await asyncio.wait(started)
    assert _warm_plans(mk) == ["a", "b"]
    pool.ensure("a")  # once: a bonfire that is warm is not warmed again
    assert asyncio.all_tasks() <= before
    await pool.stop()


async def test_stopping_the_pool_ends_a_warmer_still_waiting() -> None:
    mk = FakeMk()  # no bonfire founded: the warmer waits
    pool = WarmPool(mk, Teaching(mk, MemoryTurnStore()), every_s=60)
    before = asyncio.all_tasks()
    pool.ensure("a")
    started = asyncio.all_tasks() - before
    assert len(started) == 1
    await pool.stop()
    assert all(task.done() for task in started)


def test_local_warms_at_start_as_before() -> None:
    mk = FakeMk()
    mk.healthz = _founded(BONFIRE)
    settings = Settings(token=SecretStr(TOKEN), bonfire_id=BONFIRE)
    with TestClient(create_app(settings, mk=mk, store=MemoryTurnStore())):  # no request is made
        _settle(lambda: bool(_warm_plans(mk)))
    assert _warm_plans(mk) == [BONFIRE]


def test_with_callers_of_its_own_a_service_warms_only_the_bonfires_that_call() -> None:
    mk = FakeMk()
    mk.healthz = _founded(BONFIRE, "a", "b")

    async def authenticate(_: str) -> Caller:
        return Caller(bonfire_id="a")

    settings = Settings(token=SecretStr(TOKEN), bonfire_id=BONFIRE)
    app = create_app(settings, mk=mk, store=MemoryTurnStore(), authenticate=authenticate)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200  # the loop has turned: a warmer at start would have planned
        assert _warm_plans(mk) == []
        for _ in range(2):
            answer = client.get("/v1/status", headers={"Authorization": "Bearer a-key"})
            assert answer.status_code == 200, answer.text
        _settle(lambda: bool(_warm_plans(mk)))
    assert _warm_plans(mk) == ["a"]
