"""hermes-kernel: memory-kernel as this Hermes's memory and turn reader.

Hermes imports this directory under different module names on its memory path and its general path. Module state
and classes (`Refused`, the shared singleton) would be duplicated per name, so the package is loaded exactly once,
under `_CANON`, and every copy's `register` delegates to that one. The top level stays standard-library only: it
runs on every path, the memory path once per turn.
"""

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType

log = logging.getLogger(__name__)

_CANON = "hermes_kernel_impl"
# Hermes already serialises `register`; this only guards a double load if two threads reach it together.
_load_lock = threading.Lock()


def _canonical() -> ModuleType:
    mod = sys.modules.get(_CANON)
    if mod is not None:
        return mod
    with _load_lock:
        mod = sys.modules.get(_CANON)
        if mod is not None:
            return mod
        here = Path(__file__).resolve()
        spec = importlib.util.spec_from_file_location(_CANON, here, submodule_search_locations=[str(here.parent)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {here}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_CANON] = mod  # before executing, so relative imports resolve
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(_CANON, None)
            raise
        return mod


def _ledger_path() -> Path | None:
    """The step ledger's file under Hermes's home; `None` when Hermes cannot say (the ledger then fences everything)."""
    try:
        from hermes_constants import get_hermes_home  # pyright: ignore[reportMissingImports]

        from .runs import LEDGER_FILENAME

        return Path(str(get_hermes_home())) / LEDGER_FILENAME
    except Exception:  # noqa: BLE001 — no home: the ledger has no file and fails closed
        log.warning("hermes-kernel: Hermes's home is unknown; phrase results are not kept across a restart")
        return None


def _fenced(what: str, fn, *args) -> None:
    """Run one registration; a failure costs only that registration."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001  plain Hermes still works without it
        log.exception("hermes-kernel: %s did not register", what)


def _register_command(ctx) -> None:
    from .command import kernel_command

    ctx.register_command(
        "kernel",
        kernel_command,
        description="Is memory saving? '/kernel retry <session id>' retries a session that failed to build.",
        args_hint="[retry <session id>]",
    )


def _register_skill(ctx) -> None:
    ctx.register_skill(
        "hermes-kernel",
        Path(__file__).resolve().parent / "skills" / "hermes-kernel" / "SKILL.md",
        description="Reading the kernel's block, running and teaching phrases, and when memory arrives.",
    )


def register(ctx) -> None:
    if __name__ != _CANON:
        return _canonical().register(ctx)
    from .provider import KernelMemory
    from .shared import get_shared

    get_shared()
    if hasattr(ctx, "dispatch_tool"):
        from . import guide
        from .agent_source import AgentSource, HermesRegistry, load_read_only
        from .call_log import CallLog
        from .ledger import StepLedger
        from .runs import PhraseRun
        from .tools import KernelTools, register_tools
        from .turns import KernelTurns

        shared = get_shared()
        new_tools = None
        with shared.lock:
            if shared.turns is None and shared.client is not None:
                try:
                    read_only, params = load_read_only()
                except (OSError, ValueError):
                    log.exception("hermes-kernel: the read-only tools file is unreadable; phrases have only think")
                    read_only, params = set(), {}
                source = AgentSource(shared.client, HermesRegistry(), read_only, params=params)
                call_log = CallLog(source)
                shared.source = source
                shared.call_log = call_log
                shared.editor = guide.Editor(source, guide.load())
                shared.turns = KernelTurns(shared.client, source=source, call_log=call_log)
            if shared.tools is None and shared.turns is not None and shared.client is not None:
                shared.runs = PhraseRun(shared.client, shared.source, ctx.dispatch_tool, StepLedger(_ledger_path()))
                shared.tools = new_tools = KernelTools(
                    shared.client, shared.turns, shared.runs, shared.source, shared.editor
                )
            turns, call_log = shared.turns, shared.call_log
        if turns is not None:
            _fenced("the gateway hook", ctx.register_hook, "pre_gateway_dispatch", turns.on_gateway_dispatch)
            _fenced("the prompt hook", ctx.register_hook, "pre_llm_call", turns.on_pre_llm_call)
        if new_tools is not None:
            register_tools(ctx, new_tools)  # once: Hermes defers them behind `tool_search` (v0.23.0)
        if call_log is not None:
            _fenced("the tool-call hook", ctx.register_hook, "post_tool_call", call_log.on_post_tool_call)
        if turns is not None:  # no token: the provider alone says so, visibly
            _fenced("the /kernel command", _register_command, ctx)
            _fenced("the skill", _register_skill, ctx)
        log.info("hermes-kernel loaded (general path)")
        return
    ctx.register_memory_provider(KernelMemory())
