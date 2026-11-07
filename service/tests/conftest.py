"""Shared fixtures: the one bonfire every conversation speaks to."""

from __future__ import annotations

import pytest

from kernel_turns.groups import SingleBonfire

BONFIRE = "65f0c0ffee0000000000abcd"


@pytest.fixture
def groups() -> SingleBonfire:
    return SingleBonfire(BONFIRE)
