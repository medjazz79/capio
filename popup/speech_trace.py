"""Opt-in timing trace for speech synthesis and playback."""
from __future__ import annotations

import os
import secrets
import threading
import time

from config.app_log import get_logger


class SpeechTrace:
    """Record speech timing events; each stage is logged once by default."""

    def __init__(self, label: str):
        self.enabled = os.environ.get(
            "CAPIO_TTS_TRACE", os.environ.get("HARMONY_TTS_TRACE")
        ) == "1"
        self.trace_id = secrets.token_hex(4)
        self._started = time.perf_counter_ns()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._logger = get_logger("tts_trace") if self.enabled else None
        self.mark("trace_started", label=label)

    def mark(
        self, stage: str, *, once: bool = True, **details: object
    ) -> None:
        if not self.enabled or self._logger is None:
            return
        with self._lock:
            if once:
                if stage in self._seen:
                    return
                self._seen.add(stage)
            elapsed_ms = (time.perf_counter_ns() - self._started) / 1_000_000
        detail_text = " ".join(
            f"{key}={value!r}" if isinstance(value, str)
            else f"{key}={value}"
            for key, value in details.items()
        )
        suffix = f" {detail_text}" if detail_text else ""
        self._logger.info(
            "TTS_TRACE id=%s +%.1fms stage=%s%s",
            self.trace_id, elapsed_ms, stage, suffix,
        )
