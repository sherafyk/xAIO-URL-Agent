from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

_LOGGING_CONFIGURED = False


def setup_logging(name: str) -> logging.Logger:
    global _LOGGING_CONFIGURED
    if not _LOGGING_CONFIGURED:
        level_name = os.getenv("XAIO_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        log_path = Path(os.getenv("XAIO_LOG_FILE", ".runtime/logs/xaio.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handlers = [logging.StreamHandler()]
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
        _LOGGING_CONFIGURED = True
    return logging.getLogger(name)


def elapsed_ms(start_time: float) -> int:
    return int((time.monotonic() - start_time) * 1000)


def log_event(
    logger: logging.Logger,
    *,
    stage: str,
    item_id: Optional[str] = None,
    row: Optional[int] = None,
    url: Optional[str] = None,
    elapsed_ms_value: Optional[int] = None,
    message: Optional[str] = None,
    level: int = logging.INFO,
    **extra: Any,
) -> None:
    parts = [f"stage={stage}"]
    if item_id is not None:
        parts.append(f"item_id={item_id}")
    if row is not None:
        parts.append(f"row={row}")
    if url:
        parts.append(f"url={url}")
    if elapsed_ms_value is not None:
        parts.append(f"elapsed_ms={elapsed_ms_value}")
    if message:
        parts.append(f"msg={message}")
    for key, value in extra.items():
        if value is not None:
            parts.append(f"{key}={value}")
    logger.log(level, " ".join(parts))
