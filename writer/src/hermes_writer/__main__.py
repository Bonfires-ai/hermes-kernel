"""``python -m hermes_writer``: build closed sessions into the bonfire, forever."""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from pymongo import MongoClient

from hermes_writer.build import build_session, redact, release
from hermes_writer.clean import point_nltk
from hermes_writer.config import Settings
from hermes_writer.loop import run_once
from hermes_writer.queue import Queue

log = logging.getLogger("hermes_writer")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()  # pyright: ignore[reportCallIssue]  # bonfire_id comes from KT_BONFIRE_ID
    point_nltk(settings.data_dir)
    secrets = settings.secrets()
    client: MongoClient[dict[str, Any]] = MongoClient(settings.mongo_uri.get_secret_value())
    queue = Queue(client[settings.mongo_db], stop_backoff_s=settings.stop_backoff_s)
    build = functools.partial(build_session, settings)
    free = functools.partial(release, settings)
    last_held = time.monotonic()
    started = False
    while True:
        slept = False
        try:
            if not started:
                queue.reset_running()
                started = True
            if time.monotonic() - last_held >= settings.held_retry_s:
                last_held = time.monotonic()
                queue.release_held(settings.held_retry_s)
            if not run_once(queue, build, free, settings):
                slept = True
        except Exception as exc:  # noqa: BLE001 - Mongo may be briefly away; the loop goes on
            log.error("pass failed: %s", redact(f"{type(exc).__name__}: {exc}", secrets))
            slept = True
        if slept:
            time.sleep(settings.poll_s)


if __name__ == "__main__":
    main()
