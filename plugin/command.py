"""The `/kernel` command: is memory saving, and a retry for a session that failed to build."""

from __future__ import annotations

import logging
from typing import Any

from .client import Refused, ServiceError, Unauthorized, Unavailable
from .shared import get_shared

log = logging.getLogger(__name__)

CEILING_COMMAND = "docker compose exec writer mk bonfire ceiling"


def status_line(status: dict[str, Any] | None, capture_error: str | None) -> str:
    """Is it saving? `status` is the service's `/v1/status` answer, `None` when the service is away."""
    lines: list[str] = []
    if capture_error:
        lines.append(f"Not saving: {capture_error}")
    if status is None:
        lines.append("The memory service is away. What you say is kept on this machine and sent when it is back.")
        return "\n".join(lines)
    failed = status.get("failed") or []
    for item in failed:
        sid = str(item.get("session_id", ""))
        lines.append(f"Session {sid} failed to build ({item.get('reason', 'no reason given')}): /kernel retry {sid}")
    held = status.get("held")
    if held:
        lines.append(f"Held: {held}. To raise the spend ceiling: {CEILING_COMMAND}")
    waiting = status.get("sessions_waiting") or 0
    if waiting:
        lines.append(f"{waiting} closed session(s) waiting to be built.")
    built = status.get("last_built_at")
    if built:
        lines.append(f"Last built: {built}")
    elif not status.get("founded"):
        lines.append("Nothing built yet: memory starts once the first session has closed.")
    if not lines:
        lines.append("Saving.")
    elif not (capture_error or failed or held):
        lines.insert(0, "Saving.")
    return "\n".join(lines)


def kernel_command(raw_args: str = "", **session_kwargs: Any) -> str:
    """`/kernel` shows status; `/kernel retry <session_id>` retries a session that failed to build."""
    try:
        shared = get_shared()
        client = shared.client
        if client is None:
            return "Memory is not set up: HERMES_KERNEL_TOKEN is missing."
        capture = shared.made_capture
        capture_error = capture.last_error if capture is not None else None
        parts = (raw_args or "").split()
        if parts and parts[0] == "retry":
            if len(parts) != 2:
                return "Give the session id: /kernel retry <session id>"
            client.retry_session(parts[1])
            return f"Retrying session {parts[1]}."
        try:
            return status_line(client.status(), capture_error)
        except Unavailable:
            return status_line(None, capture_error)
    except Unauthorized:
        return "The memory service refused the token: check HERMES_KERNEL_TOKEN."
    except Unavailable:
        return "The memory service is away; try again shortly."
    except (Refused, ServiceError) as exc:
        return f"The memory service said: {exc}"
    except Exception:  # noqa: BLE001  a command never breaks the chat
        log.warning("hermes-kernel: /kernel failed", exc_info=True)
        return "/kernel failed; see the Hermes log."
