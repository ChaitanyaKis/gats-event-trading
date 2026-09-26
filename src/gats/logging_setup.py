"""Logging setup: console plus a size-rotated file, UTC timestamps."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)sZ %(levelname)-7s %(name)s | %(message)s"


class _UTCFormatter(logging.Formatter):
    # typeshed types `converter` narrower than time.gmtime's real signature.
    converter = time.gmtime  # type: ignore[assignment]


def configure_logging(level: str = "INFO", logs_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _UTCFormatter(_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "gats.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # httpx logs every request at INFO; that is noise for a 24/7 poller.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def kv(**fields: object) -> str:
    """Render ``key=value`` pairs for grep-friendly log lines."""
    return " ".join(
        f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}"
        for key, value in fields.items()
    )
