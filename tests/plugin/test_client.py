import importlib.util
import sys
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
        client.begin({})
    assert never_sent(info.value)
    assert not breaker.allow()


def test_a_5xx_trips_and_a_success_resets():
    answers = [httpx.Response(503, json={"detail": "busy"}), httpx.Response(200, json={"ok": True})]
    client, breaker, clock, _ = make(lambda request: answers.pop(0))
    with pytest.raises(Unavailable):
        client.begin({})
    clock.now += 61
    assert client.begin({}) == {"ok": True}
    assert breaker.allow()
    assert breaker.allow()


def test_a_409_is_an_answer_not_a_failure():
    client, breaker, _, seen = make(lambda request: httpx.Response(409, json={"detail": "already reported"}))
    with pytest.raises(Refused) as info:
        client.begin({})
    assert info.value.detail == "already reported"
    assert info.value.status == 409
    assert breaker.allow()
    assert len(seen) == 1


def test_a_wrong_token_is_its_own_error():
    client, breaker, _, _ = make(lambda request: httpx.Response(401, json={"detail": "bad token"}))
    with pytest.raises(Unauthorized):
        client.begin({})
    assert breaker.allow()
    assert not issubclass(Unauthorized, Refused)


def test_a_front_doors_message_is_the_detail():
    # Bonfires' API, in front of the hosted service, answers a key it refuses as {"error", "message"}, not "detail".
    body = {"error": "auth.api_key_invalid", "message": "API key invalid", "context": {}}
    client, breaker, _, _ = make(lambda request: httpx.Response(403, json=body))
    with pytest.raises(Unauthorized) as info:
        client.begin({})
    assert info.value.detail == "API key invalid"
    assert breaker.allow()


def test_a_path_in_the_service_url_is_kept():
    paths: list[str] = []

    def ok(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"versions": {"service": "0.1.0"}})

    client = ServiceClient("https://api.example/kernel/", "tok", transport=httpx.MockTransport(ok))
    client.status()
    assert paths == ["/kernel/v1/status"]


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


def _load_copy(name):
    root = Path(shared_mod.__file__).parent
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def copies():
    sys.modules.pop("hermes_kernel_impl", None)
    yield
    for n in [n for n in sys.modules if n.startswith(("_copy_a", "_copy_b", "hermes_kernel_impl"))]:
        del sys.modules[n]


class FakeCtx:
    def register_memory_provider(self, provider):
        self.provider = provider


def test_two_imports_share_one_state(copies):
    a, b = _load_copy("_copy_a"), _load_copy("_copy_b")
    a.register(FakeCtx())
    b.register(FakeCtx())
    canon = sys.modules["hermes_kernel_impl"]
    assert a._canonical() is canon and b._canonical() is canon
    # the copies themselves hold no client code: one set of classes, so one `Refused`
    assert "client" not in vars(a) and "client" not in vars(b)
    refused = a._canonical().client.Refused
    assert b._canonical().client.Refused is refused
    with pytest.raises(sys.modules["hermes_kernel_impl.client"].Refused):
        raise refused(409, "x")
    assert a._canonical().shared.get_shared() is b._canonical().shared.get_shared()


def test_only_begin_and_context_use_the_breaker():
    client, breaker, _, seen = make(lambda request: httpx.Response(200, json={}))
    breaker.trip()
    client.post_messages([], [])
    client.status()
    assert len(seen) == 2
    with pytest.raises(Unavailable):
        client.begin({})
    assert len(seen) == 2

    def boom(request):
        return httpx.Response(500, json={"detail": "x"})

    client, breaker, _, _ = make(boom)
    with pytest.raises(Unavailable):
        client.post_messages([], [])
    assert breaker.allow()


def test_no_token_means_no_client(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN", raising=False)
    shared_mod.reset_shared()
    try:
        assert shared_mod.get_shared().client is None
    finally:
        shared_mod.reset_shared()
