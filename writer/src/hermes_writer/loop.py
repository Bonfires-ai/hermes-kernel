"""One pass of the writer: claim a session, build it, record what came of it."""

from __future__ import annotations

import logging
from collections.abc import Callable

from hermes_writer.build import BuildResult, redact
from hermes_writer.config import Settings
from hermes_writer.queue import Claimed, Queue

log = logging.getLogger(__name__)


def run_once(
    queue: Queue,
    build: Callable[[Claimed], BuildResult],
    release: Callable[[], None],
    settings: Settings,
) -> bool:
    """Build the oldest closed session. False when there was nothing to do, or the bonfire was busy (wait, then retry)."""
    claimed = queue.claim()
    if claimed is None:
        return False
    sid, secrets = claimed["id"], settings.secrets()
    try:
        result = build(claimed)
    except Exception as exc:  # noqa: BLE001 - one session's failure must not stop the writer
        result = BuildResult("stopped", redact(f"{type(exc).__name__}: {exc}", secrets))
    reason = redact(result.reason or "", secrets) or None
    log.info("session %s: %s%s", sid, result.kind, f" ({reason})" if reason else "")
    match result.kind:
        case "built":
            queue.built(sid)
        case "stopped":
            attempts = queue.stopped(sid, reason or "stopped")
            if attempts >= settings.max_tries:
                note = reason or "stopped"
                try:
                    release()
                except Exception as exc:  # noqa: BLE001 - the lease may still look live
                    note += f"; release refused: {redact(f'{type(exc).__name__}: {exc}', secrets)}"
                queue.failed(sid, note)
        case "held":
            queue.held(sid, reason or "held")
        case "busy":
            queue.requeue(sid, reason or "busy")
            return False
        case "refused":
            queue.failed(sid, reason or "refused")
    return True
