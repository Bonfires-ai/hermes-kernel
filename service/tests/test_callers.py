"""Who is calling: the local check, one token for the one configured bonfire."""

from __future__ import annotations

import pytest

from kernel_turns.callers import Caller, Unauthorized, token_check


async def test_token_check_gives_the_configured_bonfire() -> None:
    check = token_check("s3cret-token", "b1")
    assert await check("s3cret-token") == Caller(bonfire_id="b1")
    for wrong in ("not-the-token", "s3cret-token ", "", "thé"):  # a non-ASCII bearer is wrong, never a TypeError
        with pytest.raises(Unauthorized):
            await check(wrong)


async def test_empty_token_refuses_everything() -> None:
    check = token_check("", "b1")
    for given in ("", "anything", "b1"):
        with pytest.raises(Unauthorized):
            await check(given)
