import importlib.util
from pathlib import Path

import httpx
import pytest

from plugin import shared as shared_mod
from plugin.client import Breaker, Refused, ServiceClient, Unauthorized, Unavailable, never_sent


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(handler, clock=None):
    clock = clock or Clock()
    breaker = Breaker(clock=clock)
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = ServiceClient("http://svc", "tok", breaker=breaker, transport=httpx.MockTransport(wrapped))
    return client, breaker, clock, seen


def test_a_hung_stack_costs_one_slow_call():
    def hang(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, breaker, clock, seen = make(hang)
    with pytest.raises(Unavailable):
        client.context("q", None)
    assert len(seen) == 1
    assert not breaker.allow()
    clock.now += 30
    with pytest.raises(Unavailable):
        client.context("q", None)
    assert len(seen) == 1  # no request sent while open
    clock.now += 30.1
    with pytest.raises(Unavailable):
        client.context("q", None)
    assert len(seen) == 2  # one let through after 60 s, and it re-opened the breaker
    with pytest.raises(Unavailable):
        client.context("q", None)
    assert len(seen) == 2


def test_a_refused_connection_trips_the_breaker_too():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client, breaker, _, _ = make(refuse)
    with pytest.raises(Unavailable) as info:
        client.status()
    assert never_sent(info.value)
    assert not breaker.allow()


def test_a_5xx_trips_and_a_success_resets():
    answers = [httpx.Response(503, json={"detail": "busy"}), httpx.Response(200, json={"ok": True})]
    client, breaker, clock, _ = make(lambda request: answers.pop(0))
    with pytest.raises(Unavailable):
        client.status()
    clock.now += 61
    assert client.status() == {"ok": True}
    assert breaker.allow()
    assert breaker.allow()


def test_a_409_is_an_answer_not_a_failure():
    client, breaker, _, seen = make(lambda request: httpx.Response(409, json={"detail": "already reported"}))
    with pytest.raises(Refused) as info:
        client.report("t1", "s1", "ok")
    assert info.value.detail == "already reported"
    assert info.value.status == 409
    assert breaker.allow()
    assert len(seen) == 1


def test_a_wrong_token_is_its_own_error():
    client, breaker, _, _ = make(lambda request: httpx.Response(401, json={"detail": "bad token"}))
    with pytest.raises(Unauthorized):
        client.status()
    assert breaker.allow()
    assert not issubclass(Unauthorized, Refused)


def test_the_bearer_and_the_routes():
    def ok(request):
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path == "/v1/context":
            return httpx.Response(200, json={"context": "hello", "degraded": False})
        return httpx.Response(200, json={})

    client, _, _, seen = make(ok)
    assert client.context("q", "ann") == "hello"
    client.calls("t 1", "tool", {"a": 1})
    assert [r.url.raw_path.decode() for r in seen] == ["/v1/context", "/v1/turns/t%201/calls"]


def test_two_imports_share_one_state(monkeypatch):
    monkeypatch.setenv("HERMES_KERNEL_TOKEN", "tok")
    shared_mod.reset_shared()
    first = shared_mod.get_shared()
    path = Path(shared_mod.__file__).parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("_other_name", path, submodule_search_locations=[str(path.parent)])
    assert spec and spec.loader
    other = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["_other_name"] = other
    try:
        spec.loader.exec_module(other)
        from _other_name import shared as other_shared  # type: ignore[import-not-found]

        assert other_shared is not shared_mod
        assert other_shared.get_shared() is first
    finally:
        for name in [n for n in sys.modules if n == "_other_name" or n.startswith("_other_name.")]:
            del sys.modules[name]
        shared_mod.reset_shared()


def test_no_token_means_no_client(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN", raising=False)
    shared_mod.reset_shared()
    try:
        assert shared_mod.get_shared().client is None
    finally:
        shared_mod.reset_shared()
