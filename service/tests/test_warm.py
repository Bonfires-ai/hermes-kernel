"""Warming: once the bonfire exists, the kept catalogue is installed and one throwaway plan is sent; then it is done."""

from __future__ import annotations

import asyncio

from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import Teaching
from kernel_turns.warm import Warmer, warm_until_done
from tests.fakes import FakeMk
from tests.test_sources import AGENT, BONFIRE


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


async def test_boot_warms_a_bonfire_that_already_exists() -> None:
    mk, teaching, _ = _setup()
    _found(mk)
    await teaching.put_catalogue(BONFIRE, "hermes", "main", AGENT)  # installed and stored before the restart
    mk.calls.clear()
    del mk.catalogues[BONFIRE]  # the kernel no longer holds it
    warmer = Warmer(mk, teaching, BONFIRE)  # the restarted service
    assert await warmer.tick() is True
    assert "web_search" in str(mk.catalogues[BONFIRE])
    assert len(mk.calls_of("plan")) == 1


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
