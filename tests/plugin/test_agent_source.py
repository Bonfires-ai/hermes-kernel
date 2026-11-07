"""The catalogue a person's Hermes offers the kernel: its own read-only tools, as rows."""

import threading
import time
from types import SimpleNamespace

import pytest

from plugin.agent_source import AgentSource, HermesRegistry, ToolView, load_read_only
from plugin.call_log import CallLog

TEXT = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


def tool(name, toolset="web", parameters=TEXT, available=True):
    return ToolView(name, toolset, f"{name} does a thing", parameters, available)


class Registry:
    def __init__(self, *tools):
        self.tools = list(tools)

    def offered(self):
        return list(self.tools)

    def enabled_tools(self, platform):
        return {"telegram": {"web_search"}}[platform]


class Client:
    def __init__(self):
        self.puts = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.threads = []

    def put_source(self, platform, group, source, catalogue, *, timeout=None):
        self.threads.append(threading.current_thread())
        self.puts.append((platform, group, source, catalogue))
        self.entered.set()
        assert self.release.wait(5)


def entry(name, toolset):
    return SimpleNamespace(name=name, toolset=toolset, schema={"description": "d", "parameters": TEXT}, check_fn=None)


def test_only_listed_read_only_tools_enter_the_catalogue():
    client = Client()
    registry = Registry(tool("web_search"), tool("terminal", "terminal"), tool("write_file", "file"))
    AgentSource(client, registry, {"web_search"}).sync()
    names = [row["name"] for row in client.puts[0][3]["tools"]]
    assert names == ["web_search", "think"]
    assert client.puts[0][:3] == ("hermes", "all", "agent")


def test_disabled_toolsets_stay_out_even_when_listed():
    entries = [entry("web_search", "web"), entry("session_search", "session_search"), entry("vision_analyze", "vision")]
    registry = HermesRegistry(
        entries=lambda: entries,
        disabled=lambda: {"web", "composite"},
        disabled_tools=lambda names: {"vision_analyze"} if "composite" in names else set(),
        check=lambda fn, memo: True,
    )
    assert [t.name for t in registry.offered()] == ["session_search"]
    client = Client()
    AgentSource(client, registry, {"web_search", "session_search", "vision_analyze"}).sync()
    assert [r["name"] for r in client.puts[0][3]["tools"]] == ["session_search", "think"]


def test_every_row_is_agent_read_only_speaker_turn():
    client = Client()
    AgentSource(
        client, Registry(tool("web_search"), tool("mcp__srv__lookup", "mcp-srv")), {"web_search", "mcp__srv__lookup"}
    ).sync()
    rows = client.puts[0][3]["tools"]
    assert {r["name"] for r in rows} == {"web_search", "lookup", "think"}
    for row in rows:
        assert row["x-runner"] == "agent"
        assert row["x-side-effect"] is False
        assert row["x-consent"] == "speaker_turn"
    # with no tool at all, think is still there
    empty = Client()
    AgentSource(empty, Registry(), set()).sync()
    assert [r["name"] for r in empty.puts[0][3]["tools"]] == ["think"]


def test_a_short_name_that_clashes_with_another_tool_keeps_the_long_name():
    client = Client()
    registry = Registry(tool("lookup", "x"), tool("mcp__srv__lookup", "mcp-srv"))
    AgentSource(client, registry, {"lookup", "mcp__srv__lookup"}).sync()
    assert {r["name"] for r in client.puts[0][3]["tools"]} == {"lookup", "mcp__srv__lookup", "think"}


def test_sync_soon_never_runs_on_the_callers_thread():
    client = Client()
    client.release.clear()
    source = AgentSource(client, Registry(tool("web_search")), {"web_search"})
    started = time.monotonic()
    source.sync_soon()
    source.sync_soon()  # one at a time: a second call while one runs is a no-op
    assert client.entered.wait(5)
    assert time.monotonic() - started < 1  # the caller was not held by the blocked put
    assert client.threads[0] is not threading.current_thread()
    assert client.threads[0].daemon
    client.release.set()
    time.sleep(0.2)
    assert len(client.puts) == 1


def test_a_failed_background_sync_is_tried_again_on_a_later_sync_soon():
    class Flaky(Client):
        def put_source(self, *args, **kwargs):
            super().put_source(*args, **kwargs)
            if len(self.puts) == 1:
                raise RuntimeError("down")

    client = Flaky()
    source = AgentSource(client, Registry(tool("web_search")), {"web_search"})
    source.sync_soon()
    for _ in range(100):
        if client.puts:
            break
        time.sleep(0.02)
    time.sleep(0.2)
    source.sync_soon()
    for _ in range(100):
        if len(client.puts) == 2:
            break
        time.sleep(0.02)
    assert len(client.puts) == 2


def test_an_unchanged_set_sends_nothing():
    client = Client()
    registry = Registry(tool("web_search"), tool("session_search"))
    source = AgentSource(client, registry, {"web_search", "session_search", "vision_analyze"})
    source.sync()
    registry.tools.reverse()  # the same tools in another order are no change
    source.sync()
    assert len(client.puts) == 1
    registry.tools.append(tool("vision_analyze", "vision"))
    source.sync()
    assert len(client.puts) == 2


def test_a_tool_hermes_does_not_offer_now_is_unavailable_and_no_step():
    client = Client()
    registry = Registry(tool("web_search", available=False), tool("session_search"))
    source = AgentSource(client, registry, {"web_search", "session_search"})
    source.sync()
    assert source.unavailable() == {"web_search"}
    assert source.tool("web_search") is None
    assert source.tool("session_search") is not None
    assert [name for name, _ in source.steps()] == ["session_search", "think"]


def test_the_call_log_block_shows_names_and_parameter_names_only():
    source = AgentSource(Client(), Registry(tool("web_search")), {"web_search"})
    log = CallLog(source)
    log.on_post_tool_call(
        tool_name="web_search", args={"query": "SECRET-VALUE", "limit": 5}, session_id="s1", status="ok"
    )
    log.on_post_tool_call(tool_name="terminal", args={"command": "x"}, session_id="s1", status="ok")
    log.on_post_tool_call(tool_name="web_search", args={"query": "FAILED-ONE"}, session_id="s1", status="error")
    block = log.block("s1")
    assert "web_search(query)" in block
    assert "SECRET-VALUE" not in block and "FAILED-ONE" not in block and "terminal" not in block
    assert log.block("other") == ""


def test_load_read_only_lists_only_tools_hermes_registers():
    names, _ = load_read_only()
    assert names == {"web_search"}


def test_side_effecting_names_a_write_tool_hermes_offers_and_nothing_else():
    registry = Registry(tool("web_search"), tool("write_file", "file"), tool("mcp__srv__poke", "mcp-srv"))
    source = AgentSource(Client(), registry, {"web_search"})
    assert source.side_effecting(["web_search", "write_file", "nope", "think"]) == {"write_file"}

    class Broken(Registry):
        def offered(self):
            raise RuntimeError("registry unreadable")

    with pytest.raises(RuntimeError):
        AgentSource(Client(), Broken(), {"web_search"}).side_effecting(["write_file"])


def test_enabled_for_follows_the_platforms_enabled_toolsets_and_fails_closed():
    source = AgentSource(Client(), Registry(tool("web_search"), tool("session_x", "x")), {"web_search"})
    assert source.enabled_for("telegram", "web_search") is True
    assert source.enabled_for("telegram", "session_x") is False

    class Raising(Registry):
        def enabled_tools(self, platform):
            raise RuntimeError("no resolver")

    assert AgentSource(Client(), Raising(), {"web_search"}).enabled_for("telegram", "web_search") is False


def test_hermes_registry_enabled_tools_is_enabled_set_less_disabled():
    entries = [entry("web_search", "web"), entry("terminal", "terminal"), entry("vision_analyze", "vision")]
    registry = HermesRegistry(
        entries=lambda: entries,
        disabled=lambda: {"vision"},
        disabled_tools=lambda names: set(),
        platform_toolsets=lambda platform: {"web", "vision"},
        resolve=lambda toolset: {"web": ["web_search"], "vision": ["vision_analyze"]}[toolset],
        check=lambda fn, memo: True,
    )
    assert registry.enabled_tools("telegram") == {"web_search"}


def test_a_tool_that_was_a_row_and_is_gone_is_unavailable():
    registry = Registry(tool("web_search"))
    source = AgentSource(Client(), registry, {"web_search"})
    source.sync()
    assert source.unavailable() == set()
    registry.tools.clear()
    source.sync()
    assert source.unavailable() == {"web_search"}
