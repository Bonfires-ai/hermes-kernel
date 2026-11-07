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
            queue.built(sid, reason)
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
            note = reason or "busy"
            if result.pending:
                # A failed session whose release was refused leaves a lease naming an uncommitted run: without a
                # release every later session would be busy for ever.
                failed = queue.last_failed()
                note += f"; waiting on failed session {failed}" if failed else ""
                if queue.pending_release_due(settings.held_retry_s):
                    try:
                        release()
                        log.warning("released the lease behind a pending session (%s)", sid)
                    except Exception as exc:  # noqa: BLE001 - the lease may still look live
                        log.warning(
                            "release for pending session refused: %s", redact(f"{type(exc).__name__}: {exc}", secrets)
                        )
            queue.requeue(sid, note)
            return False
        case "refused":
            queue.failed(sid, reason or "refused")
    return True
