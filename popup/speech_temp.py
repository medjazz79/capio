"""Owned speech playback files, deferred deletion, and crash recovery."""
from __future__ import annotations

import atexit
import logging
from pathlib import Path
import stat
import tempfile
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QLockFile

_ROOT = Path(tempfile.gettempdir()) / "HarmonyTranslator" / "speech"
_LOCK = threading.RLock()
_STOP = threading.Event()
_ACTIVE: set[Path] = set()
_PENDING: set[Path] = set()
_SESSION: Path | None = None
_SESSION_LOCK: QLockFile | None = None
_THREAD: threading.Thread | None = None
_CACHE_CLEANUP: Callable[[], object] | None = None
_CLOSED = False
CLEANUP_INTERVAL_SECONDS = 60


def _session_directory() -> Path:
    global _SESSION, _SESSION_LOCK
    if _CLOSED:
        raise OSError("Speech temporary storage is closing.")
    if _SESSION is None:
        _ROOT.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="session-", dir=_ROOT))
        owner = QLockFile(str(directory / "owner.lock"))
        # A long reading session must not become stale merely due to its age.
        owner.setStaleLockTime(0)
        if not owner.tryLock(0):
            directory.rmdir()
            raise OSError("Could not reserve speech temporary storage.")
        _SESSION, _SESSION_LOCK = directory, owner
    return _SESSION


def create_speech_file(audio: bytes, *, suffix: str = ".mp3") -> str:
    """Create a playback file tracked until its player/worker releases it."""
    if suffix not in (".mp3", ".wav"):
        raise ValueError("Unsupported speech file extension")
    path: Path | None = None
    with _LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                dir=_session_directory(), prefix="audio-", suffix=suffix,
                delete=False,
            ) as handle:
                path = Path(handle.name)
                _ACTIVE.add(path)
                handle.write(audio)
        except BaseException:
            if path is not None:
                release_speech_file(path)
            raise
    return str(path)


def release_speech_file(filename: str | Path) -> bool:
    """Delete an explicitly owned file, retrying later if Windows holds it."""
    path = Path(filename)
    with _LOCK:
        _ACTIVE.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _PENDING.add(path)
            return False
        _PENDING.discard(path)
        return True


def _is_link(path: Path) -> bool:
    attributes = path.lstat()
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _remove_abandoned_sessions() -> None:
    """Only clean our flat session folders after acquiring their owner lock."""
    if not _ROOT.is_dir():
        return
    root = _ROOT.resolve()
    for directory in _ROOT.glob("session-*"):
        try:
            if (directory == _SESSION or _is_link(directory)
                    or not directory.is_dir() or directory.resolve().parent != root
                    or time.time() - directory.stat().st_mtime < CLEANUP_INTERVAL_SECONDS):
                continue
            owner = QLockFile(str(directory / "owner.lock"))
            owner.setStaleLockTime(0)
            if not owner.tryLock(0):
                continue  # Another reader or floating-translator process owns it.
            try:
                for path in directory.glob("audio-*"):
                    if (path.suffix in (".mp3", ".wav") and not _is_link(path)
                            and path.is_file() and path.resolve().parent == directory.resolve()):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
            finally:
                owner.unlock()
            # No recursive deletion: unexpected files/subfolders stay untouched.
            directory.rmdir()
        except OSError:
            continue


def _maintain_storage() -> None:
    with _LOCK:
        for path in tuple(_PENDING):
            release_speech_file(path)
    _remove_abandoned_sessions()
    if _CACHE_CLEANUP is not None:
        _CACHE_CLEANUP()


def _cleanup_loop() -> None:
    while not _STOP.is_set():
        try:
            _maintain_storage()
        except Exception:
            logging.getLogger(__name__).exception("Speech storage cleanup failed")
        if _STOP.wait(CLEANUP_INTERVAL_SECONDS):
            break


def start_speech_cleanup(cache_cleanup: Callable[[], object]) -> None:
    """Start one janitor shared by every speech window in this process."""
    global _THREAD, _CACHE_CLEANUP
    with _LOCK:
        _CACHE_CLEANUP = cache_cleanup
        if _CLOSED or (_THREAD is not None and _THREAD.is_alive()):
            return
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_cleanup_loop, name="capio-speech-cleanup", daemon=True)
        _THREAD.start()


def stop_speech_cleanup() -> None:
    """Release this process's audio on exit; stale sessions cover a hard crash."""
    global _CLOSED, _SESSION_LOCK
    _STOP.set()
    with _LOCK:
        _CLOSED = True
        for path in tuple(_ACTIVE | _PENDING):
            release_speech_file(path)
        if _SESSION_LOCK is not None:
            _SESSION_LOCK.unlock()
            _SESSION_LOCK = None
        if _SESSION is not None:
            try:
                _SESSION.rmdir()
            except OSError:
                pass  # A still-locked file is recovered by a later application run.


atexit.register(stop_speech_cleanup)
