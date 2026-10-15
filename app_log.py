"""One rotating log file for every entry point.

main.py used to import `logging` and never configure it, so a frozen build
crashed in silence. This is the single place that turns logging on, and it
works the same whether the app runs from source or from a packaged .exe.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_NAME = "harmony_translator"
_MAX_BYTES = 1_000_000
_BACKUPS = 3


def log_path() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    folder = Path(base) / "HarmonyTranslator"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to temp if LOCALAPPDATA is not writable
        import tempfile
        folder = Path(tempfile.gettempdir()) / "HarmonyTranslator"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path(tempfile.gettempdir()) / "harmony_translator.log"
    return folder / "harmony_translator.log"


def _verbose() -> bool:
    val = os.getenv("CAPIO_DEBUG", os.getenv("HARMONY_DEBUG", "")).strip().lower()
    return val not in ("", "0", "false", "no", "off", "disable", "disabled")


def setup_logging(stream: bool = False) -> logging.Logger:
    """Idempotent: safe to call from main(), main_02.py and the popup worker."""
    logger = logging.getLogger(LOG_NAME)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    verbose = _verbose()
    # Always update level to reflect current env (fix: old code returned early
    # with stale level if HARMONY_DEBUG changed after first call)
    desired_level = logging.DEBUG if verbose else logging.INFO
    if getattr(logger, "_ht_configured", False):
        if logger.level != desired_level:
            logger.setLevel(desired_level)
        if (stream or verbose) and not any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            for handler in logger.handlers
        ):
            console = logging.StreamHandler(sys.stderr)
            console.setFormatter(fmt)
            logger.addHandler(console)
        return logger
    logger.setLevel(desired_level)
    logger.propagate = False

    # Use WatchedFileHandler-like approach: RotatingFileHandler is not
    # safe for multi-process. Use delay + try to avoid PermissionError on Windows.
    try:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(
            log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        handler = None
        # Ensure errors are still visible
        sys.stderr.write("Warning: Could not create log file, logging to stderr only\n")
    if handler is not None:
        handler.setFormatter(fmt)
        # Handle rotation failures gracefully in multi-process: wrap emit
        _orig_emit = handler.emit
        def _safe_emit(record):
            try:
                _orig_emit(record)
            except (OSError, PermissionError, ValueError):
                pass  # Another process holds the file; skip this record
        handler.emit = _safe_emit  # type: ignore[method-assign]
        logger.addHandler(handler)

    if stream or verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        logger.addHandler(console)

    # Use module-level flag instead of monkey-patching Logger
    logger._ht_configured = True  # type: ignore[attr-defined]
    return logger


def get_logger(name: str = "") -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"{LOG_NAME}.{name}" if name else LOG_NAME)
