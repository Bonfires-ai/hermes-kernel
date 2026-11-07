"""The `/kernel` command: is memory saving, and a retry for a session that failed to build."""

from __future__ import annotations

import logging
from typing import Any

from .client import STATUS_TIMEOUT_S, Refused, ServiceError, Unauthorized, Unavailable
from .shared import get_shared

log = logging.getLogger(__name__)

USAGE = "Use /kernel for the status, or /kernel retry <session id> to retry a session that failed to build."
NOT_SELECTED = "memory.provider is not hermes-kernel: set it with `hermes config set memory.provider hermes-kernel`"


def _money(value: Any) -> str:
    return f"${float(value):.2f}"


def _held_lines(status: dict[str, Any], held: str) -> list[str]:
    if "spend ceiling" not in held:
        return [f"Held: {held}"]
    spend = status.get("spend")
    lines = [f"Held: {held}"]
    if isinstance(spend, dict):
        lines.append(f"Spent {_money(spend.get('charged_usd'))} of {_money(spend.get('ceiling_usd'))}.")
    lines.append(
        "The ceiling is a running total: pick a new one above what is spent. One close to the total can refuse a long session."
    )
    lines.append(
        "To raise it, from the hermes-kernel directory: "
        f"docker compose exec writer mk bonfire ceiling {status.get('bonfire_id', '<bonfire id>')} "
        "<new total in USD>"
    )
    return lines


def status_line(status: dict[str, Any] | None, problem: str | None, *, saving_known: bool = True) -> str:
    """Is it saving? `status` is the service's `/v1/status` answer, `None` when the service is away; `problem` is
    why capture is off (a capture error, or the provider not selected); `saving_known` is False when that could not
    be checked."""
    lines: list[str] = []
    if problem:
        lines.append(f"Not saving: {problem}")
    elif not saving_known:
        lines.append("Could not read Hermes's config to check that memory.provider is hermes-kernel.")
    if status is None:
        if problem:
            lines.append("The memory service is also away.")
        else:
            lines.append(
                "The memory service is away. Recent conversations are kept for it and sent when it is back, "
                "up to a limit."
            )
        return "\n".join(lines)
    versions = status.get("versions")
    if isinstance(versions, dict) and versions.get("mk", True) is None:
        lines.append("Recall is off: the memory engine behind the service is away.")
    failed = status.get("failed") or []
    for item in failed:
        sid = str(item.get("session_id", ""))
        lines.append(f"Session {sid} failed to build ({item.get('reason') or 'no reason given'}): /kernel retry {sid}")
    held = status.get("held")
    if held:
        lines.extend(_held_lines(status, str(held)))
    waiting = status.get("sessions_waiting") or 0
    if waiting:
        lines.append(f"{waiting} closed session(s) waiting to be built.")
    built = status.get("last_built_at")
    if built:
        lines.append(f"Last built: {built}")
    elif not status.get("founded") and not waiting:
        lines.append("Nothing built yet: memory starts once the first session has closed.")
    if saving_known and not (problem or failed or held):
        lines.insert(0, "Saving.")
    return "\n".join(lines)


def _provider_state() -> tuple[bool, str | None]:
    """(known, problem): whether Hermes's own config selects this provider, and why not if it does not."""
    try:
        from hermes_cli.config import load_config_readonly  # pyright: ignore[reportMissingImports]

        memory = load_config_readonly().get("memory")
        provider = memory.get("provider") if isinstance(memory, dict) else None
    except Exception:  # noqa: BLE001  Hermes's config cannot be read: say nothing is known
        return False, None
    return (True, None) if provider == "hermes-kernel" else (True, NOT_SELECTED)


def kernel_command(raw_args: str = "", **session_kwargs: Any) -> str:
    """`/kernel` shows status; `/kernel retry <session_id>` retries a session that failed to build."""
    try:
        shared = get_shared()
        client = shared.client
        if client is None:
            return "Memory is not set up: HERMES_KERNEL_TOKEN is missing."
        parts = (raw_args or "").split()
        if parts and parts[0] == "retry":
            if len(parts) != 2:
                return "Give the session id: /kernel retry <session id>"
            client.retry_session(parts[1], timeout=STATUS_TIMEOUT_S)
            return f"Retrying session {parts[1]}."
        if parts:
            return f"Unknown: /kernel {' '.join(parts)}. {USAGE}"
        capture = shared.made_capture
        known, problem = _provider_state()
        problem = problem or (capture.last_error if capture is not None else None)
        try:
            return status_line(client.status(), problem, saving_known=known)
        except Unavailable:
            return status_line(None, problem, saving_known=known)
    except Unauthorized:
        return "The memory service refused the token: check HERMES_KERNEL_TOKEN."
    except Unavailable:
        return "The memory service is away; try again shortly."
    except (Refused, ServiceError) as exc:
        return f"The memory service said: {exc}"
    except Exception:  # noqa: BLE001  a command never breaks the chat
        log.warning("hermes-kernel: /kernel failed", exc_info=True)
        return "/kernel failed; see the Hermes log."
