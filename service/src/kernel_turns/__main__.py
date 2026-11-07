"""``python -m kernel_turns``: the turn service on 0.0.0.0:8000 (the compose service ``kernel-turns``)."""

from __future__ import annotations

import logging

import uvicorn

from kernel_turns.app import create_app
from kernel_turns.config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()  # pyright: ignore[reportCallIssue] - the fields come from KT_* in the environment
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
