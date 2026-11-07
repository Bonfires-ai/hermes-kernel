"""Process-wide state. The package is loaded once, under one canonical name (see `__init__`), so a module-level
singleton is one per process."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .capture import Capture
from .client import Breaker, ServiceClient

CONFIG_FILE = "hermes-kernel.json"

DEFAULT_URL = "http://127.0.0.1:8787"


class Shared:
    def __init__(self, client: ServiceClient | None, breaker: Breaker) -> None:
        self.client = client
        self.breaker = breaker
        #: per-session facts, keyed by session id; guard with `lock`
        self.sessions: dict[str, Any] = {}
        self.lock = threading.RLock()
        #: the Capture once `capture()` made it; None before, or with no client
        self.made_capture: Capture | None = None
        #: the turn reader, built by the general path's `register`; None before, or with no client
        self.turns: Any = None
        #: the tool catalogue, its call log and the step editor, built with the turn reader; None before
        self.source: Any = None
        self.call_log: Any = None
        self.editor: Any = None
        #: the phrase runner and the phrase tools, built once by the general path's `register`; None before
        self.runs: Any = None
        self.tools: Any = None

    def capture(self, spool_path: Path) -> Capture | None:
        """The one Capture, made on first call (it starts no thread until first use). None without a client."""
        with self.lock:
            if self.made_capture is None and self.client is not None:
                self.made_capture = Capture(self.client, spool_path, agent_handle="hermes")
            return self.made_capture


_lock = threading.Lock()
_shared: Shared | None = None


def configured_url() -> str:
    """`url` from `<HERMES_HOME>/hermes-kernel.json`, written by `save_config`; "" when absent or unreadable."""
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return ""
    try:
        data = json.loads((Path(home) / CONFIG_FILE).read_text(encoding="utf-8"))
        value = data.get("url") if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def _build() -> Shared:
    breaker = Breaker()
    token = os.environ.get("HERMES_KERNEL_TOKEN", "").strip()
    url = os.environ.get("HERMES_KERNEL_URL", "").strip() or configured_url() or DEFAULT_URL
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
