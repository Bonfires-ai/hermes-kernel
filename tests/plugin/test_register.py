import importlib
import logging
import os
import sys
from pathlib import Path

import pytest

import plugin
from plugin.command import status_line

TOOLS = ["run_phrase", "offer_phrase", "teach_phrase", "confirm_phrase", "phrase_continue"]
#: the call log's `post_tool_call` and the card's
HOOKS = ["post_tool_call", "post_tool_call", "pre_gateway_dispatch", "pre_llm_call"]


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
    import hermes_constants  # pyright: ignore[reportMissingImports]

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)  # never a developer's real home
    plugin._canonical()
    mod = importlib.import_module("hermes_kernel_impl.shared")
    mod.reset_shared()
    yield mod
    mod.reset_shared()


def test_register_on_the_general_path_wires_hooks_and_tools(impl_shared, tmp_path, monkeypatch):
    ctx = GeneralCtx()
    plugin.register(ctx)
    assert ctx.events == ["pre_gateway_dispatch", "pre_llm_call", *TOOLS, "post_tool_call", "post_tool_call"]
    assert list(ctx.commands) == ["kernel"]
    assert ctx.providers == []
    name, path = ctx.skills[0]
    assert name == "hermes-kernel" and path.name == "SKILL.md" and path.exists()
    # the step ledger is the file under Hermes's home, not an in-memory one
    ledger = impl_shared.get_shared().runs._ledger
    assert not ledger.unsure
    ledger.record("k", "v", "web_search")
    assert (tmp_path / sys.modules["hermes_kernel_impl.runs"].LEDGER_FILENAME).exists()


class HookCtx(GeneralCtx):
    """Keeps the callbacks too."""

    def __init__(self):
        super().__init__()
        self.callbacks = []

    def register_hook(self, name, fn):
        super().register_hook(name, fn)
        self.callbacks.append((name, fn))


def test_the_cards_hook_is_the_book_the_tools_hold_on_every_load(impl_shared):
    for _ in range(2):  # a reload unloads the registrations, and the module and its state stay
        ctx = HookCtx()
        plugin.register(ctx)
        shared = impl_shared.get_shared()
        assert shared.cards is not None and shared.tools._cards is shared.cards
        assert ("post_tool_call", shared.cards.on_post_tool_call) in ctx.callbacks
        assert ("post_tool_call", shared.call_log.on_post_tool_call) in ctx.callbacks


def test_register_on_the_provider_path_never_needs_dispatch_tool(impl_shared):
    ctx = CollectorCtx()
    plugin.register(ctx)
    assert len(ctx.providers) == 1


def test_the_memory_path_registers_no_hooks_or_tools(impl_shared):
    ctx = CollectorCtx()
    plugin.register(ctx)
    assert ctx.calls == []


def test_register_with_no_token_registers_the_provider_and_the_command_only(impl_shared, monkeypatch, caplog):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN")
    impl_shared.reset_shared()
    ctx = GeneralCtx()
    with caplog.at_level(logging.INFO):
        plugin.register(ctx)
    assert ctx.tools == [] and ctx.hooks == [] and ctx.skills == []
    assert list(ctx.commands) == ["kernel"]
    assert "not set up" in ctx.commands["kernel"]("")
    assert any(r.levelno == logging.ERROR and "HERMES_KERNEL_TOKEN" in r.getMessage() for r in caplog.records)
    collector = CollectorCtx()
    plugin.register(collector)
    assert len(collector.providers) == 1


STATUS = {"founded": True, "last_built_at": "2026-11-07T10:00:00Z", "sessions_waiting": 0}


def _late_token(impl_shared, monkeypatch, own_hermes_home):
    """A Hermes that registered with no token; then `hermes memory setup`, in another process, wrote its .env."""
    monkeypatch.delenv("HERMES_KERNEL_TOKEN")
    impl_shared.reset_shared()
    ctx = GeneralCtx()
    plugin.register(ctx)
    assert "not set up" in ctx.commands["kernel"]("")
    (own_hermes_home / ".env").write_text("HERMES_KERNEL_TOKEN=late\n", encoding="utf-8")
    return ctx


def test_a_token_saved_after_register_reaches_a_running_hermes_without_a_restart(
    impl_shared, monkeypatch, own_hermes_home
):
    ctx = _late_token(impl_shared, monkeypatch, own_hermes_home)
    sh = impl_shared.get_shared()
    assert sh.client is not None  # no process restarted, and this process's environment still has no token
    assert "HERMES_KERNEL_TOKEN" not in os.environ
    monkeypatch.setattr(sh.client, "status", lambda **_: STATUS)
    said = ctx.commands["kernel"]("")
    assert "not set up" not in said and "Last built" in said
    assert (
        "Phrases are off" in said and "hermes gateway restart --detach" in said
    )  # hooks and tools came with no register
    assert ctx.hooks == [] and ctx.tools == []


def test_a_reload_after_the_token_brings_the_hooks_and_tools_and_ends_the_restart_line(
    impl_shared, monkeypatch, own_hermes_home
):
    _late_token(impl_shared, monkeypatch, own_hermes_home)
    reloaded = GeneralCtx()  # `hermes plugins enable`: the running gateway rediscovers its plugins
    plugin.register(reloaded)
    assert reloaded.tools == TOOLS
    assert sorted(reloaded.hooks) == HOOKS
    assert reloaded.skills
    monkeypatch.setattr(impl_shared.get_shared().client, "status", lambda **_: STATUS)
    assert "Phrases are off" not in reloaded.commands["kernel"]("")


def test_no_token_anywhere_stays_not_set_up_and_says_what_to_run(impl_shared, monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_TOKEN")
    impl_shared.reset_shared()
    ctx = GeneralCtx()
    plugin.register(ctx)
    assert impl_shared.get_shared().client is None
    assert "hermes memory setup" in ctx.commands["kernel"]("")


def test_a_second_register_after_a_reload_still_has_the_five_tools(impl_shared):
    first, second = GeneralCtx(), GeneralCtx()
    plugin.register(first)
    plugin.register(second)  # Hermes unloaded the first registrations; the module and its state stayed
    for ctx in (first, second):
        assert ctx.tools == TOOLS
        assert sorted(ctx.hooks) == HOOKS


def test_one_hook_failing_costs_only_that_hook(impl_shared, caplog):
    ctx = GeneralCtx()
    ctx.fail_hook = "pre_llm_call"
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)
    assert ctx.hooks == ["pre_gateway_dispatch", "post_tool_call", "post_tool_call"]
    assert ctx.tools == TOOLS
    assert "kernel" in ctx.commands and ctx.skills


def test_status_line_says_is_it_saving():
    built = {"founded": True, "last_built_at": "2026-11-07T01:32:30Z", "failed": [], "held": None}
    assert "Saving." in status_line(built, None)
    assert "2026-11-07T01:32:30Z" in status_line(built, None)
    failed = {**built, "failed": [{"session_id": "s9", "reason": None, "attempts": 2}]}
    line = status_line(failed, None)
    assert "s9" in line and "/kernel retry s9" in line and "no reason given" in line
    assert "refused the token" in status_line(built, "memory refused the token: check HERMES_KERNEL_TOKEN")
    assert "Saving." not in status_line(built, None, saving_known=False)
    # service away: one true thing, never both "not saving" and "kept"
    assert "away" in status_line(None, None) and "limit" in status_line(None, None)
    both = status_line(None, "the spool could not be read")
    assert "Not saving" in both and "kept" not in both
    # mk serve away: recall is off
    assert "Recall is off" in status_line({**built, "versions": {"service": "1", "mk": None}}, None)
    # a long quiet waiting list is not "nothing built yet"
    waiting = {"founded": False, "last_built_at": None, "sessions_waiting": 2, "failed": [], "held": None}
    assert "memory starts once" not in status_line(waiting, None)


def test_the_ceiling_hold_gives_a_command_that_runs():
    held = {
        "bonfire_id": "hermes",
        "founded": True,
        "last_built_at": None,
        "failed": [],
        "held": "bonfire spend ceiling reached: bonfire 'hermes' has spent $19.9 of $20; this run's estimate is $1",
        "spend": {"charged_usd": 19.9, "ceiling_usd": 20.0},
    }
    line = status_line(held, None)
    assert "docker compose restart mk-serve" in line and line.index("restart") > line.index("mk bonfire ceiling")
    assert "docker compose exec writer mk bonfire ceiling hermes <new total in USD>" in line
    assert "hermes-kernel directory" in line and "Spent $19.90 of $20.00" in line and "long session" in line
    assert "Spent $" not in status_line({**held, "spend": None}, None)
    other = status_line({**held, "held": "cannot found bonfire hermes: OPENROUTER_API_KEY is unset"}, None)
    assert "OPENROUTER_API_KEY is unset" in other and "mk bonfire ceiling" not in other


@pytest.fixture
def provider_config(monkeypatch):
    import hermes_cli.config  # pyright: ignore[reportMissingImports]

    state = {"cfg": {"memory": {"provider": "hermes-kernel"}}}

    def load():
        if state["cfg"] is None:
            raise OSError("unreadable")
        return state["cfg"]

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", load)
    return state


def test_the_kernel_command_shows_status_and_retries(impl_shared, provider_config):
    ctx = GeneralCtx()
    plugin.register(ctx)
    sh = impl_shared.get_shared()
    calls = []

    class Client:
        def status(self):
            return {"founded": True, "last_built_at": "T", "failed": [], "held": None}

        def retry_session(self, sid, timeout=30.0):
            calls.append((sid, timeout))

        def close(self):
            pass

    sh.client = Client()
    kernel = ctx.commands["kernel"]
    assert "T" in kernel("")
    assert "s9" in kernel("retry s9", session_id="x") and calls == [("s9", 5.0)]
    assert "session id" in kernel("retry")
    assert "retry <session id>" in kernel("retyr s1") and "Last built" not in kernel("retyr s1")

    def away():
        raise sys.modules["hermes_kernel_impl.client"].Unavailable("down")

    sh.client.status = away
    assert "away" in kernel("")


def test_the_command_does_not_say_saving_when_the_provider_is_not_selected(impl_shared, provider_config):
    ctx = GeneralCtx()
    plugin.register(ctx)
    sh = impl_shared.get_shared()

    class Client:
        def status(self):
            return {"founded": True, "last_built_at": "T", "failed": [], "held": None}

        def close(self):
            pass

    sh.client = Client()
    kernel = ctx.commands["kernel"]
    provider_config["cfg"] = {"memory": {"provider": ""}}
    answer = kernel("")
    assert "Saving." not in answer and "memory.provider is not hermes-kernel" in answer
    assert "hermes config set memory.provider hermes-kernel" in answer
    provider_config["cfg"] = None  # Hermes's config cannot be read: do not claim it
    assert "Saving." not in kernel("")


def test_money_small_amounts_do_not_read_as_zero():
    from plugin.command import _money

    assert _money(0.0034) == "$0.0034"
    assert _money(0.0004) == "$0.0004"
    assert _money(0.00001234) == "$0.00001234"  # under $0.00005 four decimals would still read as $0.0000
    assert _money(0.1) == "$0.10"
    assert _money(19.9) == "$19.90"
    assert _money(0) == "$0.00"
    assert _money(20.0) == "$20.00"


def test_status_line_hosted_ceiling_names_no_docker():
    held = {
        "bonfire_id": "hermes",
        "hosted": True,
        "founded": True,
        "last_built_at": None,
        "failed": [],
        "held": "bonfire spend ceiling reached: bonfire 'hermes' has spent $4.9 of $5",
        "spend": {"charged_usd": 4.9, "ceiling_usd": 5.0},
    }
    line = status_line(held, None)
    assert "docker" not in line and "mk bonfire ceiling" not in line
    assert "set by Bonfires" in line and "ask them" in line and "Spent $4.90 of $5.00" in line and "Saving." not in line


def test_status_line_unretryable_session_offers_no_retry():
    row = {"session_id": "k9", "reason": "mk said no", "attempts": 2, "retryable": False}
    status = {"founded": True, "last_built_at": None, "failed": [row], "held": None}
    line = status_line(status, None)
    assert "k9" in line and "mk said no" in line and "/kernel retry" not in line
    assert "ask Bonfires to look at it" in line
    assert "/kernel retry s1" in status_line(
        {**status, "failed": [{**row, "session_id": "s1", "retryable": True}]}, None
    )


def test_status_line_unreadable_queue_never_says_saving():
    status = {
        "hosted": True,
        "founded": True,
        "last_built_at": None,
        "failed": [],
        "held": "the build queue can't be read right now",
    }
    line = status_line(status, None)
    assert "Saving." not in line and "can't be read" in line
