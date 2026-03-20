"""
Colored console logging for CLI and local runs.

- Uses `colorlog` when installed and stderr is a TTY (unless NO_COLOR or ``color=False``).
- File handlers are always plain text (no ANSI).

Environment:
  NO_COLOR=1  — disable color (https://no-color.org/)

Filtering tips (see README “Logging”):
  rg 'agent_run_|stream_append|session_event' pipeline.log
"""
from __future__ import annotations

import logging
import os
import sys


def _build_color_stream_handler() -> logging.Handler:
    try:
        import colorlog
    except ImportError as e:
        raise ImportError(
            "Colored logs require the 'colorlog' package. "
            "Install: pip install colorlog"
        ) from e

    fmt = (
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s "
        "%(cyan)s[%(name)s]%(reset)s %(message)s"
    )
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        colorlog.ColoredFormatter(
            fmt,
            datefmt="%Y-%m-%dT%H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
            reset=True,
            style="%",
        )
    )
    return h


def _build_plain_stream_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    return h


def _build_file_handler(path: str) -> logging.FileHandler:
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    return fh


def configure_apex_logging(
    level: int | str = logging.INFO,
    *,
    log_file: str | None = None,
    color: bool | None = None,
) -> None:
    """
    Configure root logging for pipeline / agents.

    - ``color=None``: auto (TTY + not NO_COLOR + colorlog installed).
    - ``color=False``: plain stderr.
    - ``color=True``: try colorlog; fall back to plain if import fails.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if color is None:
        color = bool(sys.stderr.isatty()) and os.environ.get("NO_COLOR", "") == ""

    handlers: list[logging.Handler] = []

    if color:
        try:
            handlers.append(_build_color_stream_handler())
        except ImportError:
            handlers.append(_build_plain_stream_handler())
    else:
        handlers.append(_build_plain_stream_handler())

    if log_file:
        handlers.append(_build_file_handler(log_file))

    logging.basicConfig(level=level, handlers=handlers, force=True)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
