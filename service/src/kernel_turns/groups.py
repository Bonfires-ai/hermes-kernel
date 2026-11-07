"""A conversation's group within a bonfire.

The bonfire is the caller's; ``platform`` and ``group`` only select within it. A group is always ``open``: a turn the
kernel did not read leaves the model free to call any tool of the catalogue.
"""

from __future__ import annotations

from kernel_turns.models import Group


def group_of(bonfire_id: str, platform: str, group: str) -> Group:
    return Group(platform=platform, group=group, bonfire_id=bonfire_id, unread="open")
