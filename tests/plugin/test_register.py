import importlib
import logging
import sys
from pathlib import Path

import pytest

import plugin
from plugin.command import status_line

TOOLS = ["run_phrase", "offer_phrase", "teach_phrase", "confirm_phrase", "phrase_continue"]


class GeneralCtx:
    """Looks like Hermes's PluginContext: it can dispatch tools."""

    def __init__(self):
        self.events = []
        self.hooks = []
        self.tools = []
        self.commands = {}
        self.skills = []
        self.providers = []
        self.fail_hook: str | None = None

    def dispatch_tool(self, name, args):
        return "{}"

    def register_hook(self, name, fn):
        if name == self.fail_hook:
            raise RuntimeError("hook refused")
        self.events.append(name)
        self.hooks.append(name)

    def register_tool(self, **kw):
        self.events.append(kw["name"])
        self.tools.append(kw["name"])

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_skill(self, name, path, description=""):
        self.skills.append((name, Path(path)))

    def register_memory_provider(self, provider):
        self.providers.append(provider)


class CollectorCtx:
    """Looks like the memory path's collector: anything but a register_* call raises, as on Hermes."""

    def __init__(self):
        self.providers = []
        self.calls = []

    def register_memory_provider(self, provider):
        self.providers.append(provider)

    def __getattr__(self, name):
        if name.startswith("register_"):

            def recorded(*a, **kw):
                self.calls.append(name)

            return recorded
        raise AttributeError(name)


@pytest.fixture
def impl_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KERNEL_TOKEN", "t")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    plugin._canonical()
    mod = importlib.import_module("hermes_kernel_impl.shared")
    mod.reset_shared()
    yield mod
    mod.reset_shared()


def test_register_on_the_general_path_wires_hooks_and_tools(impl_shared, tmp_path, monkeypatch):
    import hermes_constants  # pyright: ignore[reportMissingImports]

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = GeneralCtx()
    plugin.register(ctx)
    assert ctx.events == ["pre_gateway_dispatch", "pre_llm_call", *TOOLS, "post_tool_call"]
    assert list(ctx.commands) == ["kernel"]
    assert ctx.providers == []
    name, path = ctx.skills[0]
    assert name == "hermes-kernel" and path.name == "SKILL.md" and path.exists()
    # the step ledger is the file under Hermes's home, not an in-memory one
    ledger = impl_shared.get_shared().runs._ledger
    assert not ledger.unsure
    ledger.record("k", "v", "web_search")
    assert (tmp_path / sys.modules["hermes_kernel_impl.runs"].LEDGER_FILENAME).exists()


def test_register_on_the_provider_path_never_needs_dispatch_tool(impl_shared):
    ctx = CollectorCtx()
    plugin.register(ctx)
    assert len(ctx.providers) == 1


def test_the_memory_path_registers_no_hooks_or_tools(impl_shared):
    ctx = CollectorCtx()
    plugin.register(ctx)
    assert ctx.calls == []


def test_register_with_no_token_registers_the_provider_only(impl_shared, monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN")
    impl_shared.reset_shared()
    ctx = GeneralCtx()
    plugin.register(ctx)
    assert ctx.tools == [] and ctx.hooks == [] and ctx.commands == {} and ctx.skills == []
    collector = CollectorCtx()
    plugin.register(collector)
    assert len(collector.providers) == 1


def test_one_hook_failing_costs_only_that_hook(impl_shared, caplog):
    ctx = GeneralCtx()
    ctx.fail_hook = "pre_llm_call"
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)
    assert ctx.hooks == ["pre_gateway_dispatch", "post_tool_call"]
    assert ctx.tools == TOOLS
    assert "kernel" in ctx.commands and ctx.skills


def test_status_line_says_is_it_saving():
    built = {"founded": True, "last_built_at": "2026-11-07T01:32:30Z", "failed": [], "held": None}
    assert "2026-11-07T01:32:30Z" in status_line(built, None)
    failed = {**built, "failed": [{"session_id": "s9", "reason": "boom", "attempts": 2}]}
    line = status_line(failed, None)
    assert "s9" in line and "/kernel retry s9" in line
    held = {**built, "held": "spend ceiling reached"}
    assert "docker compose exec writer mk bonfire ceiling" in status_line(held, None)
    assert "refused the token" in status_line(built, "memory refused the token: check HERMES_KERNEL_TOKEN")
    assert "away" in status_line(None, None)


def test_the_kernel_command_shows_status_and_retries(impl_shared):
    ctx = GeneralCtx()
    plugin.register(ctx)
    sh = impl_shared.get_shared()
    calls = []

    class Client:
        def status(self):
            return {"founded": True, "last_built_at": "T", "failed": [], "held": None}

        def retry_session(self, sid):
            calls.append(sid)

        def close(self):
            pass

    sh.client = Client()
    kernel = ctx.commands["kernel"]
    assert "T" in kernel("")
    assert "s9" in kernel("retry s9", session_id="x") and calls == ["s9"]
    assert "session id" in kernel("retry")

    def away():
        # the impl's own class: the package is loaded once under its canonical name
        raise sys.modules["hermes_kernel_impl.client"].Unavailable("down")

    sh.client.status = away
    assert "away" in kernel("")
