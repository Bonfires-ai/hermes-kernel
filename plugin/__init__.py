"""hermes-kernel: memory-kernel as this Hermes's memory and turn reader."""

import logging

log = logging.getLogger(__name__)


def register(ctx) -> None:
    log.info("hermes-kernel loaded")
