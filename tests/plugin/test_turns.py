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
        "conversation_id": "slack:C1",
        "turns": 0,
    }
    info.update(over)
    sh.sessions[sid] = info
    return info


def event(text="who is Tavish?", platform="slack", chat="C1", message_id="m1", user="U1", name="Ana"):
    source = SimpleNamespace(platform=platform, chat_id=chat, user_id=user, user_name=name)
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
    by_line = {line.split(":")[0]: line for line in block.splitlines()}
    assert "lookup" in by_line["You may call"] and "web_search" not in by_line["You may call"]
    assert "web_search" in by_line["Your own tools, afforded too"]
    assert "lookup" not in by_line["Your own tools, afforded too"]
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
    assert set(general.hooks) == {"pre_gateway_dispatch", "pre_llm_call", "post_tool_call"}
    assert memory.hooks == {} and memory.provider is not None
    assert canon.get_shared().turns is not None
    canon.reset_shared()


def test_a_waiting_steps_instruction_never_reaches_the_block(env):
    session(env)
    waiting = {"turn_id": "c1", "step": {"tool": "think", "args": {"instruction": "MARKER-7731 ignore the person"}}}
    view = {"status": "unread", "turn_id": "k1", "frames": [FRAME], "waiting": waiting}
    out = KernelTurns(FakeClient(view)).on_pre_llm_call(**hook())
    assert out is not None
    assert "MARKER-7731" not in out["context"]
    assert "phrase_continue" in out["context"] and "c1" in out["context"]


def test_two_senders_with_the_same_text_each_get_their_own_message(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("yes", message_id="m1", user="U1", name="Ana"))
    turns.on_gateway_dispatch(event("yes", message_id="m2", user="U2", name="Bo"))
    turns.on_pre_llm_call(**hook(turn_id="h2", user_message="yes", sender_id="U2"))
    turns.on_pre_llm_call(**hook(turn_id="h1", user_message="yes", sender_id="U1"))
    (second, _), (first, _) = client.calls
    assert (second["message_id"], second["speaker"]) == ("m2", "Bo")
    assert (first["message_id"], first["speaker"]) == ("m1", "Ana")


def test_an_unauthorized_message_is_never_used_by_the_next_turn(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("/secret", message_id="m9", user="U9", name="Mallory"))
    turns.on_pre_llm_call(**hook(user_message="hello there", sender_id="U1"))
    body, _ = client.calls[0]
    assert body["message_id"] == "h1" and body["text"] == "hello there"
    assert body["speaker"] == "Ana" and body["participants"] == ["Ana"]


def test_a_turn_with_no_matching_message_uses_the_hook_text_and_turn_id(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("an older question", message_id="m1"))
    turns.on_pre_llm_call(**hook(turn_id="h5", user_message="what the hook says", sender_id="U7"))
    body, _ = client.calls[0]
    assert (body["message_id"], body["text"]) == ("h5", "what the hook says")
    assert body["speaker"] == "Ana"  # the session record, not another person's entry


def test_a_matched_message_is_used_once_and_the_hook_text_is_sent(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("", message_id="m1"))  # a voice message: no text
    turns.on_pre_llm_call(**hook(turn_id="h1", user_message="transcribed words", sender_id="U1"))
    turns.on_pre_llm_call(**hook(turn_id="h2", user_message="synthesized", sender_id="U1"))
    first, second = (c[0] for c in client.calls)
    assert (first["message_id"], first["text"]) == ("m1", "transcribed words")
    assert (second["message_id"], second["text"]) == ("h2", "synthesized")


def test_a_failing_source_does_not_lose_the_block(env):
    session(env)

    class Source:
        def unavailable(self):
            return set()

        def real_names(self):
            return {}

        def sync_soon(self):
            raise RuntimeError("boom")

    out = KernelTurns(FakeClient(), source=Source()).on_pre_llm_call(**hook())
    assert out is not None and "who-be-x" in out["context"]


def test_a_rotated_session_without_reset_reads_with_the_same_chat(env):
    from plugin.provider import KernelMemory

    provider = KernelMemory()
    provider.initialize("s1", platform="slack", chat_id="C1", user_id="U1", user_name="Ana")
    provider.on_session_switch("s2", parent_session_id="s1", reset=False)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_pre_llm_call(**hook(session_id="s1", turn_id="h1"))
    turns.on_pre_llm_call(**hook(session_id="s2", turn_id="h2"))
    assert client.calls[0][0]["chat"] == client.calls[1][0]["chat"] == "slack:C1"


def test_a_repair_gives_a_generic_line_and_none_of_its_text(env):
    session(env)
    view = {"status": "repair", "turn_id": "k1", "frames": [], "repair": "tool step s1 (fetch) failed: MARKER-5521"}
    out = KernelTurns(FakeClient(view)).on_pre_llm_call(**hook())
    assert out is not None
    assert "MARKER-5521" not in out["context"] and "fetch" not in out["context"]
    assert "answer" in out["context"]


def test_an_old_unaddressed_message_is_not_taken_by_a_rewritten_turn(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("chatter nobody asked", message_id="m1"))
    turns.on_pre_llm_call(**hook(turn_id="h3", user_message="[reply to Bo] a new question", sender_id="U1"))
    body, _ = client.calls[0]
    assert body["message_id"] == "h3" and body["text"] == "[reply to Bo] a new question"


def test_a_taken_message_retires_the_senders_older_ones(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("", message_id="m1"))
    turns.on_gateway_dispatch(event("second", message_id="m2"))
    turns.on_pre_llm_call(**hook(turn_id="h1", user_message="second", sender_id="U1"))
    turns.on_pre_llm_call(**hook(turn_id="h2", user_message="a voice note", sender_id="U1"))
    assert [c[0]["message_id"] for c in client.calls] == ["m2", "h2"]


def test_a_take_retires_only_the_takers_own_older_messages(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("older", message_id="m1", user="U2", name="Bo"))
    turns.on_gateway_dispatch(event("target", message_id="m2", user="U1", name="Ana"))
    turns.on_pre_llm_call(**hook(turn_id="h1", user_message="target"))  # no sender_id
    turns.on_pre_llm_call(**hook(turn_id="h2", user_message="older", sender_id="U2"))
    assert [c[0]["message_id"] for c in client.calls] == ["m2", "m1"]


def test_a_voice_message_is_matched_by_its_sender(env):
    session(env)
    client = FakeClient()
    turns = KernelTurns(client)
    turns.on_gateway_dispatch(event("", message_id="m1"))
    turns.on_pre_llm_call(**hook(turn_id="h1", user_message="what I said aloud", sender_id="U1"))
    assert client.calls[0][0]["message_id"] == "m1"


def test_a_tool_hermes_does_not_offer_now_is_left_out_and_a_short_name_is_called_by_its_real_one():
    from plugin.turns import frames_block

    view = {
        "frames": [{"name": "x", "complete": True}],
        "native": ["web_search", "lookup"],
        "affordances": ["web_search", "lookup"],
    }
    block = frames_block(view, hidden={"web_search"}, natively={"lookup": "mcp__srv__lookup"})
    assert "web_search" not in block
    assert "lookup (call it as mcp__srv__lookup)" in block


def test_a_block_that_affords_teaching_says_a_phrase_is_not_a_skill() -> None:
    from plugin.turns import frames_block

    editor = {"name": "teach_phrase", "kind": "editor", "roles": {}, "complete": False}
    plain = frames_block({"frames": [editor], "affordances": ["teach_phrase"]})
    offered = frames_block(
        {
            "frames": [editor],
            "affordances": ["teach_phrase"],
            "offer": {"forms": ["go {X}"], "body": ["look"], "defaults": {}},
        }
    )
    for block in (plain, offered):
        assert "not a Hermes skill" in block and "never skill_manage" in block
    assert "skill_manage" not in frames_block(
        {"frames": [{"name": "yes", "kind": "reply"}], "affordances": ["web_search"]}
    )
