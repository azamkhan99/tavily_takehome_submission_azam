"""Logging and LangGraph custom-stream helpers."""

from __future__ import annotations

import logging
import sys
from typing import Any

from langgraph.config import get_stream_writer

LOGGER_NAME = "kyc_agent"

_log = logging.getLogger(LOGGER_NAME)


def configure_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(numeric)
    _log.setLevel(numeric)


def log_event(event: dict[str, Any]) -> None:
    """Emit a concise investigation event line to stderr."""
    node = event.get("node") or ""
    message = str(event.get("message") or "").strip()
    kind = str(event.get("kind") or "event")

    if kind == "progress":
        _log.info("  %s: %s", node or "progress", message)
        return
    if kind == "detail":
        preview = message.replace("\n", " ")[:240]
        _log.info("  %s: %s", node or "detail", preview)
        return
    if message:
        _log.info("%s [%s]: %s", kind, node, message)
    else:
        _log.debug("%s [%s]", kind, node)


def emit(event: dict[str, Any]) -> None:
    """Emit a custom stream event from a graph node (main thread only)."""
    log_event(event)
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)
