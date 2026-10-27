"""Structured logging: JSON lines to ``$PAPI_HOME/logs/papi.log`` (rotated) + human console output.

Use the standard library: ``log = logging.getLogger("papi.<slice>")`` and pass structured fields via
``extra={"data": {...}}`` (or any extra keys; they are serialised into the JSON line).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from typing import Any

from papi.core.config import Settings, get_settings

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
_HANDLER_TAG = "_papi_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(
    settings: Settings | None = None, *, level: str | None = None, console: bool | None = None
) -> None:
    """Configure the ``papi`` logger tree (idempotent: previous papi handlers are replaced)."""
    s = settings or get_settings()
    root = logging.getLogger("papi")
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()
    root.setLevel((level or s.logging.level).upper())
    root.propagate = False

    s.paths.logs.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        s.paths.logs / "papi.log",
        maxBytes=s.logging.max_bytes,
        backupCount=s.logging.backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    setattr(file_handler, _HANDLER_TAG, True)
    root.addHandler(file_handler)

    if console if console is not None else s.logging.console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        setattr(stream, _HANDLER_TAG, True)
        root.addHandler(stream)


def get_logger(name: str) -> logging.Logger:
    """``logging.getLogger`` under the ``papi`` namespace (``get_logger("kap")`` → ``papi.kap``)."""
    return logging.getLogger(name if name.startswith("papi") else f"papi.{name}")
