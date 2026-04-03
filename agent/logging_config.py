"""
logging_config.py — Centralized logging setup for Atom Agent.
================================================================
Configures Python's logging module with:
  - Console handler  (WARNING+)  — keeps the REPL clean
  - Rotating file handler (DEBUG+) — full operational trace

Log directory (in priority order):
    1. $ATOM_LOG_DIR (if set)
    2. ~/atom-agentic-ai/logs/ (default)

Fails fast if directory is not writable.

Usage in any module:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("something useful")

Call ``setup_logging()`` once at startup (idempotent).
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Log directory resolution
# ---------------------------------------------------------------------------

def _resolve_log_dir() -> Path:
    """Resolve log directory.
    
    Priority:
        1. $ATOM_LOG_DIR environment variable
        2. ~/atom-agentic-ai/logs/ (default)
    
    Raises:
        PermissionError: If log directory is not writable.
    """
    # 1. Environment variable override
    env_dir = os.environ.get("ATOM_LOG_DIR", "").strip()
    if env_dir:
        log_dir = Path(env_dir)
    else:
        # 2. Default: ~/atom-agentic-ai/logs/
        log_dir = Path.home() / "atom-agentic-ai" / "logs"
    
    # Fast fail: ensure directory exists and is writable
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        test_file = log_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (PermissionError, OSError) as e:
        raise PermissionError(
            f"\n\n❌ Cannot write to log directory: {log_dir}\n"
            f"   Fix with: mkdir -p {log_dir} && chmod 755 {log_dir}\n"
            f"   Or set ATOM_LOG_DIR to a writable path.\n"
        ) from e
    
    return log_dir


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG_DIR = _resolve_log_dir()
LOG_DIR = _LOG_DIR  # public alias for importers
_LOG_FILE = _LOG_DIR / "atom.log"
LOG_FILE_PATH = _LOG_FILE  # public alias for importers
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB per file
_BACKUP_COUNT = 4              # keep atom.log.1 … .4  (total cap: 100 MB)
_ROOT_LOGGER_NAME = "atom"    # all atom loggers live under this namespace

_FILE_FMT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_FILE_DATE_FMT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_FMT = "%(levelname)-8s %(message)s"

_setup_done = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_logging(*, console_level: int = logging.WARNING, file_level: int = logging.DEBUG) -> None:
    """Configure logging for the Atom Agent process (idempotent).

    Parameters
    ----------
    console_level:
        Minimum level for stderr output (default: WARNING).
        Keeps the interactive REPL clean.
    file_level:
        Minimum level for the log file (default: DEBUG).
        Captures everything for post-mortem analysis.
    """
    global _setup_done  # noqa: PLW0603
    if _setup_done:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG)  # let handlers decide what to keep

    # --- Rotating file handler (DEBUG+) ---
    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(file_level)
    fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT))
    root.addHandler(fh)

    # --- Console handler (WARNING+ by default) ---
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(ch)

    _setup_done = True
    root.debug("Logging initialised — file: %s", _LOG_FILE)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``atom.*`` namespace.

    Example::

        logger = get_logger(__name__)   # → atom.gcs_audit_logger
        logger.info("flush complete")
    """
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
