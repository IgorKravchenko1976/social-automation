"""Centralized logging with 3-day circular rotation.

Writes to ``{data_dir}/app.log`` with daily rotation, keeping at most
3 days of history.  Older files are deleted automatically.
A ``LOG_SEPARATOR`` line is written at the start of each new day so
the file is easy to navigate.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def setup_logging(data_dir: str = "/data", level: int = logging.INFO) -> Path:
    """Configure root logger: stdout + rotating file handler.

    Returns the path to the active log file.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return _log_path(data_dir)
    _CONFIGURED = True

    log_path = _log_path(data_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FMT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=2,       # current day + 2 backups = 3 days
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    return log_path


def _log_path(data_dir: str) -> Path:
    return Path(data_dir) / "app.log"


def get_log_path() -> Path:
    """Return the path of the active log file (useful for API endpoints)."""
    from config.settings import settings
    return _log_path(settings.data_dir)


def _read_log(tail_lines: int | None = None) -> str:
    """Read log file content. If tail_lines is set, return only last N lines."""
    path = get_log_path()
    if not path.exists():
        return "(log file not found)"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if tail_lines is not None:
            return "\n".join(content.splitlines()[-tail_lines:])
        return content
    except Exception as exc:
        return f"(error reading log: {exc})"


def read_log_tail(lines: int = 200) -> str:
    """Return last *lines* lines from the active log file."""
    return _read_log(tail_lines=lines)


def read_full_log() -> str:
    """Return the entire active log file content."""
    return _read_log()
