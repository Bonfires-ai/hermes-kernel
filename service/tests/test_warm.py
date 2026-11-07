"""Warming: once the bonfire exists, the kept catalogue is installed and one throwaway plan is sent; then it is done."""

from __future__ import annotations

from kernel_turns.groups import SingleBonfire
from kernel_turns.store import MemoryTurnStore
from kernel_turns.teaching import Teaching
from kernel_turns.warm import Warmer
from tests.fakes import FakeMk
from tests.test_sources import AGENT, BONFIRE


def _setup(groups: SingleBonfire) -> tuple[FakeMk, Teaching, Warmer]:
    mk = FakeMk()
    mk.unborn = True
    teaching = Teaching(mk, MemoryTurnStore(), groups)
    return mk, teaching, Warmer(mk, teaching, BONFIRE)


def _found(mk: FakeMk) -> None:
    mk.unborn = False
    mk.calls.clear()  # what the service did before the bonfire existed does not count
    mk.healthz = {"status": "ok", "version": "2.0.0", "bonfires": {BONFIRE: {}}, "spend": {}}


async def test_founding_installs_the_kept_catalogue_and_warms(groups: SingleBonfire) -> None:
    mk, teaching, warmer = _setup(groups)
    await teaching.put_catalogue("hermes", "main", AGENT)  # before founding: kept, installed nowhere
    assert await warmer.tick() is False
    assert mk.calls_of("plan") == []
    _found(mk)
    assert await warmer.tick() is True
    puts = mk.calls_of("put_catalogue")
    assert len(puts) == 1 and "web_search" in str(puts[0])
    plans = mk.calls_of("plan")
    assert len(plans) == 1 and isinstance(plans[0], dict) and plans[0]["chat_id"] == "__warm__"


async def test_tick_does_it_once(groups: SingleBonfire) -> None:
    mk, teaching, warmer = _setup(groups)
    await teaching.put_catalogue("hermes", "main", AGENT)
    _found(mk)
    assert await warmer.tick() is True
    assert await warmer.tick() is True
    assert len(mk.calls_of("put_catalogue")) == 1
    assert len(mk.calls_of("plan")) == 1


async def test_a_refused_install_retries_next_tick(groups: SingleBonfire) -> None:
    mk, teaching, warmer = _setup(groups)
    await teaching.put_catalogue("hermes", "main", AGENT)
    _found(mk)
    mk.busy = True  # PUT refused with 503
    assert await warmer.tick() is False
    mk.busy = False
    assert await warmer.tick() is True
    assert len(mk.calls_of("plan")) == 1


async def test_boot_warms_a_bonfire_that_already_exists(groups: SingleBonfire) -> None:
    mk, teaching, warmer = _setup(groups)
    await teaching.put_catalogue("hermes", "main", AGENT)  # kept before a restart
    _found(mk)
    assert await warmer.tick() is True
    assert len(mk.calls_of("put_catalogue")) == 1
    assert len(mk.calls_of("plan")) == 1
