import json

import pytest

from plugin import shared
from plugin.client import Unavailable
from plugin.provider import MAX_KERNEL_CHARS, KernelMemory, cut_kernel_block


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None
        self.answer = "Kira's cat is Biscuit."

    def context(self, query, speaker, timeout=6.0):
        self.calls.append((query, speaker, timeout))
        if self.error:
            raise self.error
        return self.answer

    def close(self):
        pass


class FakeCapture:
    def __init__(self):
        self.turns = []
        self.closed = []
        self.last_error = None
        self.handle = None

    def turn(self, *args):
        self.turns.append(args)

    def close(self, conversation_id):
        self.closed.append(conversation_id)

    def set_agent_handle(self, handle):
        self.handle = handle


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KERNEL_TOKEN", "t")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    shared.reset_shared()
    sh = shared.get_shared()
    sh.client = FakeClient()  # type: ignore[assignment]
    sh.made_capture = FakeCapture()  # type: ignore[assignment]
    yield sh
    shared.reset_shared()


def start(sid="s1", **kw):
    provider = KernelMemory()
    kw.setdefault("hermes_home", "/nonexistent")
    provider.initialize(sid, **kw)
    return provider


def test_prefetch_returns_the_context_cut_at_a_line(env):
    env.client.answer = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(200))
    out = start().prefetch("what is my cat called", session_id="s1")
    assert 0 < len(out) <= MAX_KERNEL_CHARS
    assert env.client.answer.startswith(out) and out.endswith("x")
    assert cut_kernel_block("short") == "short"
    assert start().prefetch("   ", session_id="s1") == ""
    assert len(env.client.calls) == 1


def test_prefetch_gives_up_at_six_seconds_and_returns_nothing(env):
    env.client.error = Unavailable("down")
    assert start().prefetch("hello", session_id="s1") == ""
    assert env.client.calls[0][2] == 6.0
    env.client.error = RuntimeError("boom")
    assert start().prefetch("hello", session_id="s1") == ""


def test_sync_turn_skips_subagents_cron_and_flush(env):
    for ctx in ("subagent", "cron", "flush"):
        provider = start(f"s-{ctx}", agent_context=ctx)
        provider.sync_turn("hi", "hello", session_id=f"s-{ctx}")
        assert provider.prefetch("hello", session_id=f"s-{ctx}") == ""
    assert env.made_capture.turns == [] and env.client.calls == []
    assert json.loads(start("s9", agent_context="cron").handle_tool_call("search_memory", {"query": "cat"}))["results"]


def test_sync_turn_uses_the_author_then_user_name_then_user(env):
    provider = start(user_id="u1", user_name="Kira")
    provider.sync_turn("a", "b", session_id="s1", turn_author={"id": "u2", "name": "Mo"})
    provider.sync_turn("a", "b", session_id="s1", turn_author={"id": "u2", "name": None})
    provider.sync_turn("a", "b", session_id="s1")
    provider.on_turn_start(1, "hi", author_name="Lee")
    assert provider.prefetch("q", session_id="s1") == env.client.answer and env.client.calls[-1][1] == "Lee"
    assert [t[1] for t in env.made_capture.turns] == ["Mo", "u2", "Kira"]
    plain = start("s2", user_id="uid:1")
    plain.sync_turn("a", "b", session_id="s2")
    assert env.made_capture.turns[-1][1] == "uid:1"


def test_conversation_id_is_platform_and_chat(env):
    start("s1", platform="telegram", chat_id="12").sync_turn("a", "b", session_id="s1")
    start("s2", platform="cli").sync_turn("a", "b", session_id="s2")
    assert [t[0] for t in env.made_capture.turns] == ["telegram:12", "cli:s2"]


def test_on_session_end_and_reset_close_the_session(env):
    provider = start("s1", platform="cli")
    provider.on_session_end([])
    assert env.made_capture.closed == ["cli:s1"]
    provider.on_session_switch("s2", parent_session_id="s1", reset=True)  # Hermes ends, then switches: once
    assert env.made_capture.closed == ["cli:s1"]
    provider.initialize("s2", platform="cli")
    provider.on_session_switch("s2", parent_session_id="s2", rewound=True)
    assert env.made_capture.closed == ["cli:s1"]
    provider.on_session_switch("s3", parent_session_id="s2", reset=True)
    assert env.made_capture.closed == ["cli:s1", "cli:s2"]


def test_not_configured_is_visible(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    shared.reset_shared()
    try:
        provider = KernelMemory()
        assert provider.is_available() is False
        assert "HERMES_KERNEL_TOKEN" in provider.unavailable_reason()
    finally:
        shared.reset_shared()


def test_a_wrong_token_shows_in_system_status(env):
    provider = KernelMemory()
    assert provider.is_available() and provider.unavailable_reason() == ""
    env.made_capture.last_error = "the turn service rejected the token: check HERMES_KERNEL_TOKEN"
    assert "rejected the token" in provider.unavailable_reason()
