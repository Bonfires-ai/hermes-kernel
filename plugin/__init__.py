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
        from .turns import KernelTurns

        shared = get_shared()
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
            turns, call_log = shared.turns, shared.call_log
        if turns is not None:
            ctx.register_hook("pre_gateway_dispatch", turns.on_gateway_dispatch)
            ctx.register_hook("pre_llm_call", turns.on_pre_llm_call)
            ctx.register_hook("post_tool_call", call_log.on_post_tool_call)
        log.info("hermes-kernel loaded (general path)")  # phrase tools and commands arrive in later tasks
        return
    ctx.register_memory_provider(KernelMemory())
