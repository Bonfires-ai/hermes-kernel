"""Which bonfire a conversation speaks to: always the one this service is configured for.

Every (platform, group) pair maps to the same bonfire (``KT_BONFIRE_ID``) and is always ``open``: a turn the kernel did
not read leaves the model free to call any tool of the catalogue.
"""

from __future__ import annotations

from typing import Protocol

from kernel_turns.models import Group


class GroupFinder(Protocol):
    async def find(self, platform: str, group: str) -> Group: ...


class SingleBonfire:
    def __init__(self, bonfire_id: str) -> None:
        self._bonfire_id = bonfire_id

    async def find(self, platform: str, group: str) -> Group:
        return Group(platform=platform, group=group, bonfire_id=self._bonfire_id, unread="open")
