"""Who is calling: the local check, one token for the one configured bonfire."""

from __future__ import annotations

import json

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


# --- KeyCheck: the hosted way, a key posted to a URL -----------------------------------------------------------------

import hashlib  # noqa: E402
import logging  # noqa: E402
from collections.abc import Callable  # noqa: E402

import httpx  # noqa: E402

from kernel_turns.callers import KeyCheck, KeysAway, ReadOnlyKey  # noqa: E402

KEY = "bf_" + "Ab3dE6gH" + "_" + "Zy9xW8vU7tS6rQ5pO4nM3lK2jI1hG0fE"
SEAM_TOKEN = "seam-" + "token-xyz"


class Seam:
    """A fake auth URL: answers come from ``respond``; every request is kept."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response | Exception]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        out = self.respond(request)
        if isinstance(out, Exception):
            raise out
        return out


def ok(permission: str = "write", bonfire: str = "b1") -> httpx.Response:
    return httpx.Response(200, json={"bonfire_id": bonfire, "permission": permission})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def check_over(seam: Seam, clock: Clock | None = None) -> KeyCheck:
    return KeyCheck(
        "http://seam.test/check",
        SEAM_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(seam)),
        clock=clock or Clock(),
    )


async def test_the_request_is_the_contract() -> None:
    seam = Seam(lambda _: ok())
    assert await check_over(seam)(KEY) == Caller("b1")
    (req,) = seam.requests
    assert req.method == "POST" and str(req.url) == "http://seam.test/check"
    assert req.headers["authorization"] == f"Bearer {SEAM_TOKEN}"
    assert json.loads(req.content) == {"key": KEY}


async def test_good_key_is_cached_for_60_s() -> None:
    seam, clock = Seam(lambda _: ok()), Clock()
    check = check_over(seam, clock)
    await check(KEY)
    await check(KEY)
    assert len(seam.requests) == 1
    clock.now += 59
    await check(KEY)
    assert len(seam.requests) == 1
    clock.now += 2  # t+61
    await check(KEY)
    assert len(seam.requests) == 2


async def test_admin_is_cached_too() -> None:
    seam = Seam(lambda _: ok("admin"))
    check = check_over(seam)
    await check(KEY)
    await check(KEY)
    assert len(seam.requests) == 1


async def test_revoked_key_stops_within_a_minute() -> None:
    answers = iter([ok(), httpx.Response(404)])
    seam, clock = Seam(lambda _: next(answers)), Clock()
    check = check_over(seam, clock)
    assert await check(KEY) == Caller("b1")
    clock.now += 61
    with pytest.raises(Unauthorized):
        await check(KEY)


async def test_unknown_key_is_not_cached() -> None:
    answers = iter([httpx.Response(404), ok()])
    seam = Seam(lambda _: next(answers))
    check = check_over(seam)
    with pytest.raises(Unauthorized):
        await check(KEY)
    assert await check(KEY) == Caller("b1")


async def test_read_only_key_is_read_only_key_and_not_cached() -> None:
    answers = iter([ok("read"), ok("write")])
    seam = Seam(lambda _: next(answers))
    check = check_over(seam)
    with pytest.raises(ReadOnlyKey):
        await check(KEY)
    assert await check(KEY) == Caller("b1")  # the second answer was asked for: the first was not kept


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(429),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "b1"}),
        httpx.Response(200, json={"bonfire_id": "b1", "permission": "owner"}),
        httpx.Response(200, json={"bonfire_id": 7, "permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "", "permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "a\x1fb", "permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "a\x00b", "permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "ab\n", "permission": "write"}),
        httpx.Response(200, json={"bonfire_id": "a\x85b", "permission": "write"}),
    ],
)
async def test_seam_down_or_wrong_seam_token_is_keys_away(answer: httpx.Response | Exception) -> None:
    check = check_over(Seam(lambda _: answer))
    with pytest.raises(KeysAway):
        await check(KEY)


async def test_a_failed_seam_is_not_cached() -> None:
    answers = iter([httpx.Response(500), ok()])
    check = check_over(Seam(lambda _: next(answers)))
    with pytest.raises(KeysAway):
        await check(KEY)
    assert await check(KEY) == Caller("b1")


async def test_the_key_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    failures: list[httpx.Response | Exception] = [
        ok(),
        httpx.Response(404),
        httpx.Response(500),
        httpx.ConnectError(f"cannot reach with {KEY}"),
        httpx.Response(200, content=b"not json"),
    ]
    caught: list[str] = []
    for answer in failures:
        check = check_over(Seam(lambda _, a=answer: a))
        try:
            await check(KEY)
        except Exception as exc:  # noqa: BLE001 - every outcome is looked at
            caught.append(f"{exc!r} {exc.__cause__!r} {exc.__context__!r}")
    assert KEY not in caplog.text and SEAM_TOKEN not in caplog.text
    assert all(KEY not in c for c in caught)


async def test_the_cache_holds_a_hash_and_not_the_key() -> None:
    check = check_over(Seam(lambda _: ok()))
    await check(KEY)
    held = repr(vars(check))
    assert KEY not in held
    assert hashlib.sha256(KEY.encode()).hexdigest() in held


async def test_a_good_answer_for_one_key_is_not_another_keys() -> None:
    seam = Seam(lambda r: ok(bonfire="b-" + json.loads(r.content)["key"][-1]))
    check = check_over(seam)
    assert await check("key-1") == Caller("b-1")
    assert await check("key-2") == Caller("b-2")
    assert len(seam.requests) == 2


async def test_read_only_key_says_what_it_needs() -> None:
    with pytest.raises(ReadOnlyKey, match="needs write permission"):
        await check_over(Seam(lambda _: ok("read")))(KEY)
