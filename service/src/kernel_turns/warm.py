"""Warm the engine: ``mk serve`` starts a bonfire's engine on its first plan, which takes a few seconds. This service
sends one throwaway plan itself so no person's turn pays for it.

A bonfire exists only once its first conversation is built, so the catalogue the agent sent before then is kept, not
installed. ``tick`` waits for the bonfire, installs the kept catalogue, warms once, and says it is done.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from kernel_turns.mk_client import KernelApi, MkError
from kernel_turns.models import JsonValue
from kernel_turns.status import is_founded
from kernel_turns.teaching import Teaching

log = logging.getLogger(__name__)

WARM_EVERY_S = 30
WARM_CHAT = "__warm__"


class Warmer:
    def __init__(self, mk: KernelApi, teaching: Teaching, bonfire_id: str) -> None:
        self._mk = mk
        self._teaching = teaching
        self._bonfire_id = bonfire_id
        self._installed = False
        self._warmed = False

    async def warm(self) -> None:
        """One throwaway plan. Never raises: an ``unread`` answer counts as sent."""
        body: dict[str, JsonValue] = {
            "text": "hello",
            "chat_id": WARM_CHAT,
            "speaker": "warm",
            "at": datetime.now(UTC).isoformat(),
        }
        try:
            plan = await self._mk.plan(self._bonfire_id, body)
            log.info("kernel_turns.warm: engine warmed (%s)", plan.get("status"))
        except MkError as exc:
            log.info("kernel_turns.warm: warm plan failed: %s", exc)

    async def tick(self) -> bool:
        """Do the work once the bonfire exists; True when it is all done."""
        if self._warmed:
            return True
        try:
            health = await self._mk.health()
        except MkError:
            return False
        if not is_founded(health, self._bonfire_id):
            return False
        if not self._installed:
            self._installed = await self._teaching.install_kept()
            if not self._installed:
                return False
        await self.warm()
        self._warmed = True
        return True


async def warm_until_done(warmer: Warmer, every_s: float = WARM_EVERY_S) -> None:
    """Tick now and every ``every_s`` until done; an exception in a tick never ends the loop."""
    while True:
        try:
            if await warmer.tick():
                return
        except Exception:
            log.exception("kernel_turns.warm_failed")
        await asyncio.sleep(every_s)
