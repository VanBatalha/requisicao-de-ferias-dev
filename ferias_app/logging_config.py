from __future__ import annotations

import logging
import os
from logging.config import dictConfig

def setup_logging() -> None:
    """Configura logging estruturado simples (stdout) — ideal para Render."""
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"

    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s - %(message)s"
            }
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            }
        },
        "root": {
            "level": level,
            "handlers": ["wsgi"]
        }
    })

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
