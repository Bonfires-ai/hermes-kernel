"""The memory provider: recall before each turn, capture after it, and a `search_memory` tool.

Hermes builds providers often (an identity check every turn, then the agent's own), so the constructor does nothing
and `is_available` reads config only. Everything real lives on `get_shared()`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

from .client import Refused, Unauthorized, Unavailable
from .shared import CONFIG_FILE, DEFAULT_URL, get_shared

log = logging.getLogger(__name__)

# A safety bound only: mk bounds its own context (about 10 KB), and a tighter cut could drop the relevant line.
MAX_KERNEL_CHARS = 20000
CONTEXT_TIMEOUT_S = 6.0
SPOOL_NAME = "hermes-kernel-spool.jsonl"

SEARCH_SCHEMA = {
    "name": "search_memory",
    "description": "Search long-term memory of past conversations for facts, decisions and people.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look up, in plain words."}},
        "required": ["query"],
    },
}


def cut_kernel_block(block: str) -> str:
    """The block, or its start up to the last line boundary before `MAX_KERNEL_CHARS`."""
    if len(block) <= MAX_KERNEL_CHARS:
        return block
    head = block[:MAX_KERNEL_CHARS]
    # A cut that lands exactly on a line end keeps that line whole.
    keep = MAX_KERNEL_CHARS if block[MAX_KERNEL_CHARS] == "\n" else head.rfind("\n")
    return head[:keep] if keep > 0 else head


def conversation_id(platform: str, chat_id: str, session_id: str) -> str:
    return f"{platform or 'cli'}:{chat_id or session_id}"


class KernelMemory(MemoryProvider):
    def __init__(self) -> None:
        self._author: str | None = None  # last on_turn_start author; the provider is per agent, not per session
        self._session_id = ""

    @property
    def name(self) -> str:
        return "hermes-kernel"

    # availability

    def is_available(self) -> bool:
        return get_shared().client is not None

    def unavailable_reason(self) -> str:
        shared = get_shared()
        if shared.client is None:
            return "HERMES_KERNEL_TOKEN is not set: add it to the Hermes .env file"
        capture = shared.made_capture
        return (capture.last_error or "") if capture is not None else ""

    # lifecycle

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        shared = get_shared()
        self._session_id = session_id
        info: dict[str, Any] = {
            "agent_context": kwargs.get("agent_context") or "primary",
            "platform": kwargs.get("platform") or "cli",
            "user_id": kwargs.get("user_id"),
            "user_name": kwargs.get("user_name"),
            "chat_id": kwargs.get("chat_id"),
            "agent_identity": kwargs.get("agent_identity"),
            "turns": 0,
        }
        info["conversation_id"] = self._derive(info, session_id)
        with shared.lock:
            shared.sessions[session_id] = info
        home = kwargs.get("hermes_home")
        if home:
            capture = shared.capture(Path(home) / SPOOL_NAME)
            if capture is not None:
                capture.set_agent_handle(str(info["agent_identity"] or "hermes"))

    def shutdown(self) -> None:
        pass  # durability does not depend on it; the shared Capture outlives any one provider

    def _session(self, session_id: str) -> dict[str, Any] | None:
        info = get_shared().sessions.get(session_id)
        return info if isinstance(info, dict) else None

    def _primary(self, session_id: str) -> dict[str, Any] | None:
        info = self._session(session_id)
        return info if info is not None and info["agent_context"] == "primary" else None

    def _conversation(self, info: dict[str, Any], session_id: str) -> str:
        return str(info.get("conversation_id") or self._derive(info, session_id))

    @staticmethod
    def _derive(info: dict[str, Any], session_id: str) -> str:
        chat = info.get("chat_id")
        return conversation_id(info["platform"], str(chat) if chat else "", session_id)

    def _speaker(self, info: dict[str, Any]) -> str | None:
        return self._author or info.get("user_name") or info.get("user_id") or None

    # recall

    def system_prompt_block(self) -> str:
        return (
            "Long-term memory from past conversations is added to your context automatically when it is relevant. "
            "The search_memory tool looks further."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        info = self._primary(session_id)
        client = get_shared().client
        if info is None or client is None or not (query or "").strip():
            return ""
        try:
            block = cut_kernel_block(client.context(query, self._speaker(info), timeout=CONTEXT_TIMEOUT_S))
            log.info("hermes-kernel: recalled %d characters for session %s", len(block), session_id)
            return block
        except (Unavailable, Unauthorized, Refused) as exc:
            log.warning("hermes-kernel: no recall this turn: %s", exc)
        except Exception:  # noqa: BLE001  nothing may raise out of a memory call
            log.warning("hermes-kernel: recall failed", exc_info=True)
        return ""

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        name = kwargs.get("author_name") or kwargs.get("author_id")
        self._author = str(name) if name else None

    # capture

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        turn_author: dict[str, Any] | None = None,
    ) -> None:
        shared = get_shared()
        info = self._primary(session_id)
        capture = shared.made_capture
        if info is None or capture is None:
            return
        author = (turn_author or {}).get("name") or (turn_author or {}).get("id")
        speaker = author or self._speaker(info) or "user"
        with shared.lock:
            info["turns"] += 1
            turn_number = info["turns"]
        capture.turn(
            self._conversation(info, session_id), str(speaker), user_content, assistant_content, session_id, turn_number
        )

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        # Hermes gives no session id here: it is the one this instance was initialized with
        self._close(self._session_id)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        shared = get_shared()
        old = self._session_id
        self._session_id = new_session_id
        with shared.lock:
            source = shared.sessions.get(old) or shared.sessions.get(parent_session_id)
            if not isinstance(source, dict):
                return
            info = dict(source)
            if reset:
                info["turns"] = 0
                info["conversation_id"] = self._derive(info, new_session_id)
            shared.sessions.setdefault(new_session_id, info)
        if reset:
            self._close(parent_session_id or old)

    def _close(self, session_id: str) -> None:
        info = self._primary(session_id)
        capture = get_shared().made_capture
        if info is not None and capture is not None:
            capture.close(self._conversation(info, session_id))

    # tools

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name != "search_memory":
            return json.dumps({"error": f"unknown tool {tool_name}"})
        query = str((args or {}).get("query") or "").strip()
        client = get_shared().client
        if not query:
            return json.dumps({"error": "give a query to search for"})
        if client is None:
            return json.dumps({"error": "memory is not set up: HERMES_KERNEL_TOKEN is missing"})
        info = self._session(self._session_id)
        speaker = self._speaker(info) if info is not None else self._author
        try:
            return json.dumps({"results": cut_kernel_block(client.context(query, speaker, timeout=CONTEXT_TIMEOUT_S))})
        except Unauthorized:
            return json.dumps({"error": "memory refused the token: check HERMES_KERNEL_TOKEN"})
        except (Unavailable, Refused):
            return json.dumps({"error": "memory is not reachable right now"})
        except Exception:  # noqa: BLE001  nothing may raise out of a memory call
            log.warning("hermes-kernel: search failed", exc_info=True)
            return json.dumps({"error": "memory search failed"})

    # config

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "url", "description": "Turn service URL", "default": DEFAULT_URL},
            {
                "key": "token",
                "description": "Turn service bearer token",
                "secret": True,
                "required": True,
                "env_var": "HERMES_KERNEL_TOKEN",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        url = str((values or {}).get("url") or "").strip()
        if not url:
            return
        path = Path(hermes_home) / CONFIG_FILE
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"url": url}, fh)
        os.chmod(path, 0o600)
