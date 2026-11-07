"""Process-wide state. The package is loaded once, under one canonical name (see `__init__`), so a module-level
singleton is one per process."""

from __future__ import annotations

import os
import threading
from typing import Any

from .client import Breaker, ServiceClient

DEFAULT_URL = "http://127.0.0.1:8787"


class Shared:
    def __init__(self, client: ServiceClient | None, breaker: Breaker) -> None:
        self.client = client
        self.breaker = breaker
        #: per-session facts, keyed by session id; guard with `lock`
        self.sessions: dict[str, Any] = {}
        self.lock = threading.RLock()


_lock = threading.Lock()
_shared: Shared | None = None


def _build() -> Shared:
    breaker = Breaker()
    token = os.environ.get("HERMES_KERNEL_TOKEN", "").strip()
    url = os.environ.get("HERMES_KERNEL_URL", "").strip() or DEFAULT_URL
    client = ServiceClient(url, token, breaker=breaker) if token else None
    return Shared(client, breaker)


def get_shared() -> Shared:
    global _shared
    with _lock:
        if _shared is None:
            _shared = _build()
        return _shared


def reset_shared() -> None:
    """For tests only."""
    global _shared
    with _lock:
        shared, _shared = _shared, None
    if shared is not None and shared.client is not None:
        shared.client.close()
