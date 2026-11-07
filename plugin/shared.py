"""Process-wide state, one per process.

Hermes loads this package under two different module names (the memory path and the general path), so a module
global would give each its own copy. The state lives on an object kept under a fixed key in `sys.modules`.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from typing import Any, cast

from .client import Breaker, ServiceClient

_KEY = "hermes_kernel_shared"
DEFAULT_URL = "http://127.0.0.1:8787"


class Shared:
    def __init__(self, client: ServiceClient | None, breaker: Breaker) -> None:
        self.client = client
        self.breaker = breaker
        #: per-session facts, keyed by session id; guard with `lock`
        self.sessions: dict[str, Any] = {}
        self.lock = threading.RLock()


class _Holder(types.ModuleType):
    def __init__(self) -> None:
        super().__init__(_KEY)
        self.build_lock = threading.Lock()
        self.shared: Shared | None = None


def _build() -> Shared:
    breaker = Breaker()
    token = os.environ.get("HERMES_KERNEL_TOKEN", "").strip()
    url = os.environ.get("HERMES_KERNEL_URL", "").strip() or DEFAULT_URL
    client = ServiceClient(url, token, breaker=breaker) if token else None
    return Shared(client, breaker)


def get_shared() -> Shared:
    # `setdefault` on the dict is atomic, so two racing threads (or module copies) end up with one holder
    holder = cast(_Holder, sys.modules.setdefault(_KEY, _Holder()))
    with holder.build_lock:
        if holder.shared is None:
            holder.shared = _build()
        return holder.shared


def reset_shared() -> None:
    """For tests only."""
    holder = cast(_Holder | None, sys.modules.pop(_KEY, None))
    shared = holder.shared if holder is not None else None
    if shared is not None and shared.client is not None:
        shared.client.close()
