"""Structured logging setup using structlog.

Every component gets its own bound logger with contextual fields:
    - component: module/component name
    - correlation_id: request/trade correlation ID
    - symbol: trading pair (when applicable)

Output format: JSON (production) or console (development).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import get_config


def setup_logging() -> None:
    """Initialize structlog with config-driven settings. Call once at startup."""
    cfg = get_config()

    log_level = getattr(logging, cfg.logging.level.upper(), logging.INFO)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if cfg.logging.format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(colors=True, exception_formatter=structlog.dev.plain_traceback)
        )

    # PrintLoggerFactory (file=sys.stdout) creates a PrintLogger — no .name attribute
    # so we skip add_logger_name. For stdlib loggers with .name, use LoggerFactory().
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict[str, Any],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(processor=structlog.processors.JSONRenderer()))
    root_logger.addHandler(handler)


def get_logger(name: str, **kwargs: Any) -> structlog.stdlib.BoundLogger:
    """Get a component logger with default context fields.

    Args:
        name: Logger name (usually __name__ of the module).
        **kwargs: Additional context fields bound to every log event.

    Returns:
        Bound structlog logger.
    """
    logger = structlog.get_logger(name)
    if kwargs:
        logger = logger.bind(**kwargs)
    return logger
