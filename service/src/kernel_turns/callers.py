"""Who is calling. A request's bonfire comes from here and nowhere else: ``platform`` and ``group`` only select within it.

An ``Authenticate`` takes the bearer and answers with the ``Caller``, or raises one of the exceptions below; ``app.py``
maps them to 401, 403 and 503. ``token_check`` is the local one: one token, for the one configured bonfire.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from kernel_turns.config import BONFIRE_PATTERN


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


_BONFIRE = re.compile(BONFIRE_PATTERN)


class KeyCheck:
    """The hosted check: ``POST url`` with ``{"key": key}`` and ``Authorization: Bearer <token>``.

    ``200 {bonfire_id, permission}`` is an answer; ``404`` is an unknown or revoked key. Anything else, and any answer
    that is not well formed, is the seam failing: ``KeysAway``, never ``Unauthorized``, so a person is not told their
    key is bad because the seam is. Only ``write`` and ``admin`` answers are kept, for ``ttl_s``, under the key's
    sha256: a revoked key stops within a minute, an unknown or read-only one is asked again at once. The key is never
    logged, kept, or put in an error message.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = 3.0,
    ) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._ttl_s = ttl_s
        self._clock = clock
        self._timeout_s = timeout_s
        self._good: dict[str, tuple[float, Caller]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __call__(self, key: str) -> Caller:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = self._clock()
        held = self._good.get(digest)
        if held is not None and held[0] > now:
            return held[1]
        caller, permission = await self._ask(key)
        if permission == "read":
            raise ReadOnlyKey("the key may not write")
        if len(self._good) >= 1024:  # expired entries go when the table grows; it never grows past what is live
            self._good = {d: v for d, v in self._good.items() if v[0] > now}
        self._good[digest] = (now + self._ttl_s, caller)
        return caller

    async def _ask(self, key: str) -> tuple[Caller, str]:
        # The raises sit outside the `except` blocks: inside, the original error rides along as `__context__`, and an
        # error from a transport can quote what it was sent.
        try:
            resp = await self._client.post(self._url, json={"key": key}, headers=self._headers, timeout=self._timeout_s)
        except Exception:  # noqa: BLE001 - transport, timeout, anything: the seam is away
            resp = None
        if resp is None:
            raise KeysAway("the key check did not answer")
        if resp.status_code == 404:
            raise Unauthorized("unknown or revoked key")
        if resp.status_code != 200:
            raise KeysAway(f"the key check answered {resp.status_code}")
        bonfire: object = None
        permission: object = None
        try:
            body = resp.json()
            bonfire, permission = body["bonfire_id"], body["permission"]
        except Exception:  # noqa: BLE001 - not JSON, not an object, a field missing
            pass
        if (
            not isinstance(bonfire, str)
            or _BONFIRE.fullmatch(bonfire) is None  # \x1f joins the store's keys: one would break isolation
            or permission not in ("read", "write", "admin")
        ):
            raise KeysAway("the key check answered with something unreadable")
        return Caller(bonfire), permission
