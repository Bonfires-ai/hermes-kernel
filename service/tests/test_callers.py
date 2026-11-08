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


#: platform-api's answer for every bad key (``api_keys.not_found``, through its AppError handler)
NOT_FOUND = {"error": "api_keys.not_found", "message": "API key not found", "context": {}}


def unknown() -> httpx.Response:
    return httpx.Response(404, json=NOT_FOUND)


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
    answers = iter([ok(), unknown()])
    seam, clock = Seam(lambda _: next(answers)), Clock()
    check = check_over(seam, clock)
    assert await check(KEY) == Caller("b1")
    clock.now += 61
    with pytest.raises(Unauthorized):
        await check(KEY)


async def test_unknown_key_is_not_cached() -> None:
    answers = iter([unknown(), ok()])
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
        unknown(),
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


# --- A 404 is an unknown key only when platform-api says so; every other failure is visible and says one thing ------


async def test_platform_apis_not_found_is_an_unknown_key() -> None:
    for body in (NOT_FOUND, {**NOT_FOUND, "correlation_id": "c-1"}):  # prod adds a correlation id
        with pytest.raises(Unauthorized):
            await check_over(Seam(lambda _, b=body: httpx.Response(404, json=b)))(KEY)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"detail": "Not Found"}),  # FastAPI's own: a wrong path or a deploy-order slip
        httpx.Response(404, text="<html><body>404 Not Found</body></html>"),  # a proxy's page
        httpx.Response(404),
        httpx.Response(404, json={"error": "bonfire.not_found", "message": "x", "context": {}}),
        httpx.Response(404, json=["api_keys.not_found"]),
        httpx.Response(404, json={"detail": "api_keys.not_found"}),
    ],
)
async def test_any_other_404_is_keys_away(answer: httpx.Response) -> None:
    with pytest.raises(KeysAway):
        await check_over(Seam(lambda _: answer))(KEY)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("down"),
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
    ],
)
async def test_keys_away_says_one_fixed_thing_and_never_the_status(answer: httpx.Response | Exception) -> None:
    with pytest.raises(KeysAway) as info:
        await check_over(Seam(lambda _: answer))(KEY)
    assert str(info.value) == "the key check is unavailable"


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (httpx.Response(401), "401"),
        (httpx.Response(403), "403"),
        (httpx.Response(404, json={"detail": "Not Found"}), "404"),
        (httpx.Response(503), "503"),
        (httpx.ConnectError(f"cannot reach with {KEY}"), "ConnectError"),
        (httpx.Response(200, content=b"not json"), "200 (an answer that cannot be read)"),
    ],
)
async def test_a_failed_check_is_one_warning_with_its_status(
    caplog: pytest.LogCaptureFixture, answer: httpx.Response | Exception, status: str
) -> None:
    caplog.set_level(logging.DEBUG, logger="kernel_turns")
    with pytest.raises(KeysAway):
        await check_over(Seam(lambda _: answer))(KEY)
    (record,) = [r for r in caplog.records if "key_check_failed" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"kernel_turns.key_check_failed: status={status}"
    assert KEY not in caplog.text and SEAM_TOKEN not in caplog.text


async def test_an_unknown_key_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="kernel_turns")
    with pytest.raises(Unauthorized):
        await check_over(Seam(lambda _: unknown()))(KEY)
    assert "key_check_failed" not in caplog.text
