"""
Shared logging configuration for PRECISE Scheduler.

Call ``setup_logging()`` once at process start (the CLI entry point does this).
After that, any module can use ``logging.getLogger(__name__)`` normally.

Log levels
----------
- File   : DEBUG and above  (rotating, 5 × 10 MB)
- stderr : WARNING and above
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path


_DEFAULT_LOG_DIR = Path.home() / ".local" / "share" / "precise_scheduler" / "logs"
_LOG_FILENAME = "precise_scheduler.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5
_FMT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def setup_logging(log_dir: Path | None = None, level: int = logging.DEBUG) -> None:
    """
    Configure the root logger.  Safe to call multiple times (handlers are not
    duplicated if already attached).

    Parameters
    ----------
    log_dir:
        Directory for the rotating log file.  Defaults to
        ``~/.local/share/precise_scheduler/logs/``.  The directory is created
        if it does not exist.
    level:
        Minimum level written to the log file.  stderr always receives WARNING+.
    """
    root = logging.getLogger()
    if root.handlers:
        # Already configured — do not add duplicate handlers.
        return

    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(fmt=_FMT, datefmt=_DATE_FMT)
    formatter.converter = __import__("time").gmtime  # UTC timestamps

    # --- rotating file handler -------------------------------------------
    resolved_dir = log_dir or Path(
        os.environ.get("PRECISE_LOG_DIR", str(_DEFAULT_LOG_DIR))
    )
    resolved_dir.mkdir(parents=True, exist_ok=True)
    log_path = resolved_dir / _LOG_FILENAME

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # --- stderr handler --------------------------------------------------
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    logging.getLogger(__name__).debug("Logging initialised (file=%s)", log_path)
