from types import SimpleNamespace

import pytest

from plugin import shared
from plugin.client import Unavailable
from plugin.turns import READ_BUDGET_S, KernelTurns

FRAME = {
    "name": "who-be-x",
    "kind": "question",
    "construction": "who-be-x",
    "roles": {"x": "Tavish"},
    "complete": True,
}


class FakeClient:
    def __init__(self, view=None):
        self.calls = []
        self.error: Exception | None = None
        self.view = view if view is not None else {"status": "unread", "turn_id": "k1", "frames": [FRAME]}

    def begin(self, body, *, timeout=10.0):
        self.calls.append((body, timeout))
        if self.error:
            raise self.error
        return self.view


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    shared.reset_shared()
    yield shared.get_shared()
    shared.reset_shared()


def session(sh, sid="s1", **over):
    info = {
        "agent_context": "primary",
        "platform": "slack",
        "user_id": "U1",
        "user_name": "Ana",
        "chat_id": "C1",
        "agent_identity": "hermes",
        "conversation_id": "slack:C1",
        "turns": 0,
    }
    info.update(over)
    sh.sessions[sid] = info
    return info


def event(text="who is Tavish?", platform="slack", chat="C1", message_id="m1"):
    source = SimpleNamespace(platform=platform, chat_id=chat, user_id="U1", user_name="Ana")
    return SimpleNamespace(source=source, message_id=message_id, text=text, timestamp=None)


def hook(**over):
    kw = {"session_id": "s1", "turn_id": "h1", "user_message": "who is Tavish?", "platform": "slack"}
    kw.update(over)
    return kw


def test_gateway_observer_records_any_platform(env):
    turns = KernelTurns(FakeClient())
    assert turns.on_gateway_dispatch(event()) is None
    got = turns.latest("slack:C1")
    assert got is not None
    assert (got.chat, got.message_id, got.speaker) == ("C1", "m1", "Ana")


def test_observer_does_no_io_and_returns_none(env):
    client = FakeClient()
    turns = KernelTurns(client)
    assert turns.on_gateway_dispatch(event(), gateway=object()) is None
    assert client.calls == []


def test_a_turn_begins_and_returns_the_block(env):
    session(env)
    view = {
        "status": "unread",
        "turn_id": "k1",
        "frames": [FRAME],
        "affordances": ["lookup", "web_search"],
        "native": ["web_search"],
    }
    client = FakeClient(view)
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event())
    out = turns.on_pre_llm_call(**hook())
    assert out is not None
    block = out["context"]
    assert "who-be-x" in block and "Tavish" in block
    assert "You may call: lookup (with run_phrase)" in block
    assert "web_search" in block
    body, _ = client.calls[0]
    assert (body["platform"], body["chat"], body["group"]) == ("slack", "slack:C1", "slack:C1")
    assert (body["message_id"], body["speaker"], body["participants"]) == ("m1", "Ana", ["Ana"])
    current = turns.current("s1")
    assert current is not None and current.turn_id == "k1"


def test_cli_turn_needs_no_gateway_record(env):
    session(env, platform="cli", user_name=None, user_id="ana", chat_id=None, conversation_id="cli:s1")
    client = FakeClient()
    turns = KernelTurns(client)
    out = turns.on_pre_llm_call(**hook(platform="cli", sender_id="ana"))
    assert out is not None
    body, _ = client.calls[0]
    assert body["chat"] == "cli:s1"
    assert body["message_id"] == "h1"
    assert body["speaker"] == "ana"


def test_a_subagent_gets_no_block(env):
    session(env, "sub", agent_context="subagent")
    client = FakeClient()
    turns = KernelTurns(client)
    assert turns.on_pre_llm_call(**hook(session_id="sub")) is None
    session(env)
    assert turns.on_pre_llm_call(**hook(parent_session_id="p")) is None
    assert client.calls == []


def test_a_read_past_its_budget_gives_no_block(env):
    session(env)
    client = FakeClient()
    client.error = Unavailable("timed out")
    turns = KernelTurns(client)
    assert turns.on_pre_llm_call(**hook()) is None
    assert READ_BUDGET_S == 10.0
    assert client.calls[0][1] <= 10


def test_frames_give_a_block_whatever_the_status(env):
    session(env)
    view = {
        "status": "unread",
        "turn_id": "k1",
        "frames": [FRAME],
        "native": ["web_search"],
        "affordances": ["web_search"],
    }
    out = KernelTurns(FakeClient(view)).on_pre_llm_call(**hook())
    assert out is not None and "who-be-x" in out["context"]


def test_no_frames_and_nothing_waiting_gives_no_block(env):
    session(env)
    view = {"status": "unread", "turn_id": "k1", "frames": [], "native": ["web_search"], "affordances": ["web_search"]}
    assert KernelTurns(FakeClient(view)).on_pre_llm_call(**hook()) is None


def test_problems_never_reach_the_model(env):
    session(env)
    view = {
        "status": "unread",
        "turn_id": "k1",
        "frames": [FRAME],
        "problems": ["names nobody in the graph", "recall unavailable: no cached context"],
    }
    out = KernelTurns(FakeClient(view)).on_pre_llm_call(**hook())
    assert out is not None
    assert "nobody" not in out["context"] and "recall unavailable" not in out["context"]


def test_second_call_in_a_hermes_turn_reuses_the_first(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    first = turns.on_pre_llm_call(**hook())
    assert turns.on_pre_llm_call(**hook()) == first
    assert len(client.calls) == 1


def test_register_hooks_on_the_general_path_only(monkeypatch):
    import plugin

    class Ctx:
        def __init__(self, general):
            self.hooks: dict[str, object] = {}
            self.provider: object = None
            if general:
                self.dispatch_tool = lambda *a, **k: None

        def register_hook(self, name, fn):
            self.hooks[name] = fn

        def register_memory_provider(self, provider):
            self.provider = provider

    monkeypatch.setenv("HERMES_KERNEL_TOKEN", "t")
    general, memory = Ctx(True), Ctx(False)
    plugin.register(general)
    canon = plugin._canonical().shared  # register delegates to the one canonical copy
    plugin.register(Ctx(True))
    plugin.register(memory)
    assert set(general.hooks) == {"pre_gateway_dispatch", "pre_llm_call"}
    assert memory.hooks == {} and memory.provider is not None
    assert canon.get_shared().turns is not None
    canon.reset_shared()
