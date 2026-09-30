"""Structured logging setup (console or JSON lines on stderr)."""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_mode: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_mode:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
