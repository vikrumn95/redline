"""Structured JSON logging (JSON lines to stdout)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str = "") -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": self._run_id,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if extra:
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base)


def get_logger(run_id: str = "", level: str = "INFO") -> logging.Logger:
    name = f"redline.{run_id}" if run_id else "redline"
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter(run_id))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


class RunLogger:
    """Convenience wrapper that injects extra fields into every log call."""

    def __init__(self, run_id: str, level: str = "INFO") -> None:
        self._logger = get_logger(run_id, level)
        self._run_id = run_id

    def _log(self, lvl: int, event: str, **kwargs: Any) -> None:
        record = self._logger.makeRecord(self._logger.name, lvl, "", 0, event, (), None)
        record.extra = kwargs  # type: ignore[attr-defined]
        self._logger.handle(record)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)
