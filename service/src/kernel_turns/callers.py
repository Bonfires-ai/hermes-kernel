"""Who is calling. A request's bonfire comes from here and nowhere else: ``platform`` and ``group`` only select within it.

An ``Authenticate`` takes the bearer and answers with the ``Caller``, or raises one of the exceptions below; ``app.py``
maps them to 401, 403 and 503. ``token_check`` is the local one: one token, for the one configured bonfire.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    bonfire_id: str


Authenticate = Callable[[str], Awaitable[Caller]]


class Unauthorized(Exception):
    """No bearer, a wrong one, or no ``KT_TOKEN`` configured."""


class ReadOnlyKey(Exception):
    """The key is good and may not write."""


class KeysAway(Exception):
    """The key could not be checked just now: nothing is known about it, so it is not refused."""


def token_check(token: str, bonfire_id: str) -> Authenticate:
    """The bearer must be ``token`` (compared in constant time); an empty ``token`` refuses everything."""
    expected = token.encode("utf-8")
    caller = Caller(bonfire_id)

    async def check(bearer: str) -> Caller:
        if not expected or not hmac.compare_digest(bearer.encode("utf-8"), expected):
            raise Unauthorized("a bearer token is required")
        return caller

    return check
