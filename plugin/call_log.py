"""What the agent called in a session, kept for drafting a phrase.

Outside phrases the agent calls its own tools directly. When someone then says "make a phrase for that", the draft
should come from the calls that ran, not from compressed memory. So the plugin keeps, per session, the calls that
could be phrase steps, from Hermes' `post_tool_call` observer (`status == "ok"` only), recorded by the catalogue row's
name (an MCP tool's short name, not the real name Hermes reports).

Only names are kept, and only names are shown: a tool and the names of the parameters it was given, never their
values. The drafting turn's context is stored with the person's message and replayed, and a value may be a result a
model copied from a web page; the model has its own real calls, values included, in its history.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Mapping
from typing import Protocol

logger = logging.getLogger(__name__)

_CALLS_KEPT = 12
_SESSIONS_KEPT = 256
_MAX_NAME_CHARS = 80
_MAX_BLOCK_CHARS = 2000

HEAD = (
    "Calls you made in this chat, oldest first (tool and parameter names; your own history has the values). To make "
    "a phrase of what you did, draft its steps from these as structured steps, turning the values that should change "
    "into slots of the forms:"
)


class StepSource(Protocol):
    """What the log asks the agent source (`AgentSource`)."""

    def catalogue_name(self, real: str) -> str | None: ...

    def params(self, name: str) -> list[str] | None: ...


def _name(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _MAX_NAME_CHARS else text[: _MAX_NAME_CHARS - 1] + "…"


class CallLog:
    """Per session, the last calls that could be phrase steps."""

    def __init__(self, source: StepSource | None) -> None:
        self._source = source
        self._lock = threading.Lock()
        self._calls: OrderedDict[str, deque[tuple[str, tuple[str, ...]]]] = OrderedDict()

    def observe(self, session_id: str, tool: str, args: object) -> None:
        """A call that ran (`tool`: Hermes' name): kept, by its row's name, when the tool can be a phrase step."""
        if self._source is None:
            return
        name = self._source.catalogue_name(tool)
        params = self._source.params(name) if name is not None else None
        if name is None or params is None:
            return
        given = args if isinstance(args, Mapping) else {}
        self.record(session_id, name, [p for p in params if given.get(p) is not None])

    def record(self, session_id: str, tool: str, params: list[str]) -> None:
        step = (_name(tool), tuple(_name(p) for p in params))
        with self._lock:
            calls = self._calls.pop(session_id, None) or deque(maxlen=_CALLS_KEPT)
            calls.append(step)
            self._calls[session_id] = calls
            while len(self._calls) > _SESSIONS_KEPT:
                self._calls.popitem(last=False)

    def on_post_tool_call(
        self,
        tool_name: object = None,
        args: object = None,
        session_id: object = None,
        status: object = None,
        **_: object,
    ) -> None:
        """`post_tool_call` observer. Never raises, always `None`."""
        try:
            if status != "ok" or not isinstance(tool_name, str) or not isinstance(session_id, str) or not session_id:
                return
            self.observe(session_id, tool_name, args)
        except Exception:  # noqa: BLE001 — logging a call costs a draft at worst, never the call
            logger.warning("hermes-kernel: could not log a tool call", exc_info=True)

    def block(self, session_id: str) -> str:
        """The drafting context's lines for this session ("" when it called nothing that could be a step): the newest
        calls that fit the budget, listed oldest first, as `web_search(query)`."""
        with self._lock:
            calls = list(self._calls.get(session_id) or ())
        lines: list[str] = []
        used = len(HEAD)
        for tool, params in reversed(calls):
            line = f"  {tool}({', '.join(params)})"
            if used + len(line) + 1 > _MAX_BLOCK_CHARS:
                break
            lines.insert(0, line)
            used += len(line) + 1
        return "\n".join([HEAD, *lines]) if lines else ""
