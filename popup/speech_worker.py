"""Background TTS synthesis for translated text."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextvars import ContextVar
import difflib
import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import NamedTuple, Protocol

from translators import http_client as requests

try:
    import edge_tts
    import edge_tts.communicate as edge_tts_communicate
    from edge_tts.constants import TRUSTED_CLIENT_TOKEN
except ImportError:
    # Optional dependency: the app must start and translate without it.
    # The stub keeps `edge_tts.Communicate` an importable patch target for tests.
    class _MissingEdgeTTS:
        Communicate = None

    edge_tts = _MissingEdgeTTS()  # type: ignore[no-redef]
else:
    # Microsoft moved Edge's synthesis WebSocket to this endpoint. The old
    # speech.platform.bing.com endpoint still lists voices, but now rejects
    # synthesis handshakes with HTTP 403.
    edge_tts_communicate.WSS_URL = (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
    )
from PySide6.QtCore import QThread, Signal

from .audio_stream import AudioStreamServer


class SpeechTraceLike(Protocol):
    def mark(
        self, stage: str, *, once: bool = True, **details: object
    ) -> None: ...


_EDGE_TTS_LANGUAGE: ContextVar[str | None] = ContextVar(
    "harmony_edge_tts_language", default=None)


def _install_language_aware_ssml() -> None:
    """Allow each synthesis request to set the language in Edge TTS SSML."""
    module = globals().get("edge_tts_communicate")
    if module is None or getattr(module, "_harmony_language_aware", False):
        return
    original_mkssml = module.mkssml

    def mkssml_with_language(tts_config, escaped_text) -> str:
        ssml = original_mkssml(tts_config, escaped_text)
        language = _EDGE_TTS_LANGUAGE.get()
        if language is None:
            return ssml
        ssml, root_replacements = re.subn(
            r"(<speak\b[^>]*\bxml:lang=['\"])[^'\"]+(['\"])",
            rf"\g<1>{language}\2",
            ssml,
            count=1,
        )
        if root_replacements != 1:
            raise RuntimeError(
                "Installed edge-tts produced an unexpected SSML envelope; "
                "Arabic pronunciation cannot be guaranteed."
            )
        return ssml

    module.mkssml = mkssml_with_language
    module._harmony_language_aware = True


_install_language_aware_ssml()


def _safe_speech_error(error: Exception) -> str:
    return _EDGE_TTS_SUBSCRIPTION_KEY.sub(
        r"\1[redacted]", str(error))


async def _edge_tts_stream_with_retry(
    text: str,
    voice: str,
    rate: str,
):
    """Retry a rate-limited Edge handshake before any stream data is received."""
    for attempt in range(len(EDGE_TTS_429_RETRY_DELAYS) + 1):
        communicate = edge_tts.Communicate(
            text, voice, rate=rate, boundary="WordBoundary")
        yielded_data = False
        try:
            async for chunk in communicate.stream():
                yielded_data = True
                yield chunk
            return
        except Exception as error:
            if getattr(error, "status", None) != 429 or yielded_data:
                raise
            if attempt == len(EDGE_TTS_429_RETRY_DELAYS):
                raise RuntimeError(
                    "Microsoft Edge Read Aloud is temporarily rate-limiting "
                    "requests (HTTP 429). Wait a little and try again."
                ) from error
            await asyncio.sleep(EDGE_TTS_429_RETRY_DELAYS[attempt])


ARABIC_VOICE = "fr-FR-VivienneMultilingualNeural"
AUTOMATIC_SPEECH_VOICES = {
    "ar": "ar-SA-ZariyahNeural",
    "fr": "fr-FR-DeniseNeural",
    "en": "en-US-AriaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "it": "it-IT-ElsaNeural",
    "es": "es-ES-ElviraNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "hi": "hi-IN-SwaraNeural",
}
EDGE_TTS_PREFERRED_LANGUAGE_CODES = frozenset(
    {"ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "ru", "zh"}
)
SPEECH_RATE = "+15%"
# Classic playback keeps only the first request short for a quick start.
# Continuous reads bound long requests while preserving sentence punctuation.
FIRST_CHUNK_LENGTH = 90
# Retained for compatibility with callers that used a longer first chunk.
CONTINUOUS_FIRST_CHUNK_LENGTH = 240
MAX_CHUNK_LENGTH = 1200
MAX_PARALLEL_SEGMENTS = 3
CONTINUOUS_PREFETCH_SEGMENTS = 2
EDGE_TTS_429_RETRY_DELAYS = (1.0, 3.0)
_MP3_BITRATE_BPS = 48_000
_TICKS_PER_SECOND = 10_000_000
_EDGE_TTS_SUBSCRIPTION_KEY = re.compile(
    r"(?i)(Ocp-Apim-Subscription-Key=)[^&\s'\"]+")

DEFAULT_SPEECH_RATE = 1.0
MIN_SPEECH_RATE = 0.25
MAX_SPEECH_RATE = 2.0
SPEECH_RATE_PRECISION = 2
SPEED_SNAP_TOLERANCE = 0.025

# Disk cache for synthesized segments: key = sha1(segment + voice + rate).
# Repeat clicks and "restart from word" re-synthesized everything from
# scratch; now they replay from disk in ~0s. Override the location in tests
# with HARMONY_TTS_CACHE. Boundaries ride along in a .json sidecar so the
# word highlight keeps working on cache hits.
TTS_CACHE_MAX_FILES = 200
TTS_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_WESTERN_TO_ARABIC_DIGITS = str.maketrans(
    "0123456789",
    "٠١٢٣٤٥٦٧٨٩",
)
_ARABIC_DIGITS_TO_WESTERN = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_NON_PAUSING_SPEECH_WRAPPERS = frozenset("()[]{}\"“”‘’_")
# Strong sentence endings keep their own prosody in continuous playback. Every
# other punctuation or symbol still becomes a comma pause; without that cleanup,
# Edge TTS can spell out or stumble over markup-like characters from PDFs.
_SENTENCE_FINAL_PUNCTUATION = frozenset({".", "!", "?", "؟", "…"})
def _speech_text_for_synthesis(text: str, target_language: str) -> str:
    if target_language.casefold().split("-", 1)[0] == "ar":
        return text.translate(_WESTERN_TO_ARABIC_DIGITS)
    return text


def _normalize_arabic_pronunciation_token(token: str) -> str:
    token = unicodedata.normalize("NFC", token).translate(str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
    }))
    return "".join(
        character for character in token
        if not (unicodedata.category(character).startswith("M")
                and unicodedata.name(character, "").startswith("ARABIC"))
    )


def _cache_dir() -> Path:
    base = os.environ.get("HARMONY_TTS_CACHE") or str(
        Path(tempfile.gettempdir()) / "harmony_tts_cache")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(
    segment: str,
    voice: str,
    rate: str,
    *,
    ssml_language: str | None = None,
    engine: str = "edge",
) -> str:
    parts = ["v18", engine, voice, rate]
    if ssml_language is not None:
        parts.append(f"ssml-language:{ssml_language}")
    parts.append(segment)
    return hashlib.sha1(
        "\x00".join(parts).encode("utf-8")
    ).hexdigest()


def _prune_cache() -> None:
    """Drop cache entries older than a week and enforce the entry-count cap."""
    try:
        cache_dir = _cache_dir()
        files = sorted(
            (*cache_dir.glob("*.mp3"), *cache_dir.glob("*.wav")),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return

    now = time.time()
    expired = []
    retained = []
    for path in files:
        try:
            (expired if now - path.stat().st_mtime >=
             TTS_CACHE_MAX_AGE_SECONDS else retained).append(path)
        except OSError:
            retained.append(path)

    expired.extend(
        retained[:max(0, len(retained) - TTS_CACHE_MAX_FILES)])
    for stale in expired:
        try:
            stale.unlink(missing_ok=True)
            stale.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            continue

    audio_keys = {path.stem for path in files if path not in expired}
    try:
        for metadata in cache_dir.glob("*.json"):
            if metadata.stem not in audio_keys:
                metadata.unlink(missing_ok=True)
    except OSError:
        return


def rate_string(multiplier: float) -> str:
    """Convert a speed multiplier into the percentage edge-tts expects.

    edge-tts wants "+25%" / "-50%". Getting the sign wrong would make "slower"
    come out faster, so it is kept as one tested function.
    """
    pct = round((float(multiplier) - 1.0) * 100)
    return f"{pct:+d}%"


def normalize_speed(value) -> float:
    """Keep a free-form speech speed within range at hundredth precision."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SPEECH_RATE
    if not math.isfinite(v):
        return DEFAULT_SPEECH_RATE
    return round(
        min(MAX_SPEECH_RATE, max(MIN_SPEECH_RATE, v)),
        SPEECH_RATE_PRECISION,
    )


def snap_speed_to_quarter(value: float) -> float:
    """Lightly attract slider values that land very close to quarter steps."""
    speed = normalize_speed(value)
    quarter_step = 0.25
    nearest_step = round(speed / quarter_step) * quarter_step
    if abs(speed - nearest_step) <= SPEED_SNAP_TOLERANCE:
        return normalize_speed(nearest_step)
    return speed

MISSING_SPEECH_MESSAGE = (
    "Read aloud needs an internet connection and the edge-tts package."
)
GEMINI_TTS_ENGINE = "gemini"
GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"
GEMINI_TTS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_TTS_VOICES = (
    ("Zephyr", "Bright"),
    ("Puck", "Upbeat"),
    ("Charon", "Informative"),
    ("Kore", "Firm"),
    ("Fenrir", "Excitable"),
    ("Leda", "Youthful"),
    ("Orus", "Firm"),
    ("Aoede", "Breezy"),
    ("Callirrhoe", "Easy-going"),
    ("Autonoe", "Bright"),
    ("Enceladus", "Breathy"),
    ("Iapetus", "Clear"),
    ("Umbriel", "Easy-going"),
    ("Algieba", "Smooth"),
    ("Despina", "Smooth"),
    ("Erinome", "Clear"),
    ("Algenib", "Gravelly"),
    ("Rasalgethi", "Informative"),
    ("Laomedeia", "Upbeat"),
    ("Achernar", "Soft"),
    ("Alnilam", "Firm"),
    ("Schedar", "Even"),
    ("Gacrux", "Mature"),
    ("Pulcherrima", "Forward"),
    ("Achird", "Friendly"),
    ("Zubenelgenubi", "Casual"),
    ("Vindemiatrix", "Gentle"),
    ("Sadachbia", "Lively"),
    ("Sadaltager", "Knowledgeable"),
    ("Sulafat", "Warm"),
)


def speech_missing_message(engine: str = "edge") -> str:
    if engine == GEMINI_TTS_ENGINE:
        return "Gemini TTS needs a Gemini API key. Add one in Settings → API Keys."
    if engine != "edge":
        return f"Unsupported read-aloud engine: {engine!r}"
    return MISSING_SPEECH_MESSAGE


def speech_available(engine: str = "edge", api_key: str = "") -> bool:
    """True when the selected TTS engine can be used."""
    if engine == GEMINI_TTS_ENGINE:
        return bool(api_key.strip())
    return engine == "edge" and getattr(edge_tts, "Communicate", None) is not None


def _gemini_tts_audio(text: str, voice: str, api_key: str) -> bytes:
    if not api_key.strip():
        raise RuntimeError(speech_missing_message(GEMINI_TTS_ENGINE))
    payload = {
        "model": GEMINI_TTS_MODEL,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "Read the text verbatim in a clear, natural, steady voice.",
                }],
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    response = requests.post(
        GEMINI_TTS_ENDPOINT,
        headers={
            "x-goog-api-key": api_key.strip(),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(10, 90),
    )
    response.raise_for_status()
    data = response.json()
    audio_data = data.get("output_audio", {}).get("data")
    if not isinstance(audio_data, str):
        for step in data.get("steps", []):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content", [])
            if not isinstance(content, list):
                continue
            audio_item = next(
                (
                    item for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "audio"
                    and isinstance(item.get("data"), str)
                ),
                None,
            )
            if audio_item is not None:
                audio_data = audio_item["data"]
                break
    if not isinstance(audio_data, str):
        raise RuntimeError("Gemini TTS response did not contain audio data.")
    try:
        audio = base64.b64decode(audio_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Gemini TTS returned invalid audio data.") from exc
    if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
        raise RuntimeError("Gemini TTS returned audio in an unsupported format.")
    return audio


def split_speech_text(
    text: str,
    max_length: int = MAX_CHUNK_LENGTH,
    first_chunk_length: int = FIRST_CHUNK_LENGTH,
    preserve_sentence_punctuation: bool = False,
) -> list[str]:
    """Normalize whitespace and punctuation for speech.

    Punctuation other than sentence-final marks becomes a comma pause, and
    whitespace is normalized, because Edge TTS can otherwise spell out or
    stumble over markup-like characters from PDFs. Continuous playback passes
    ``preserve_sentence_punctuation=True`` so one synthesis request keeps the
    source's own sentence prosody instead of flattening every stop into a
    comma.
    """
    speech_characters = []
    for index, character in enumerate(text):
        is_word_apostrophe = (
            character in ("'", "’")
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isalpha()
            and text[index + 1].isalpha()
        )
        is_word_hyphen = (
            character in ("-", "\u2010", "\u2011")
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isalpha()
            and text[index + 1].isalpha()
        )
        if is_word_apostrophe or is_word_hyphen:
            speech_characters.append(
                "'" if is_word_apostrophe else character)
        elif (
            preserve_sentence_punctuation
            and character in _SENTENCE_FINAL_PUNCTUATION
        ):
            speech_characters.append(character)
        elif (
            character in _NON_PAUSING_SPEECH_WRAPPERS
        ):
            speech_characters.append(" ")
        else:
            speech_characters.append(
                "," if unicodedata.category(character).startswith(("P", "S"))
                else character
            )
    speech_text = "".join(speech_characters)
    speech_text = re.sub(r"(?:\s*,\s*)+", ", ", speech_text).strip(" ,")
    speech_text = " ".join(speech_text.split())
    words = speech_text.split()
    chunks: list[str] = []
    current = ""
    limit = min(max(1, first_chunk_length), max_length)
    for word in words:
        while word:
            separator = 1 if current else 0
            available = limit - len(current) - separator
            if len(word) <= available:
                current = f"{current} {word}".strip()
                word = ""
                continue
            if current:
                chunks.append(current)
                current = ""
                limit = max_length
                continue
            if len(word) > limit:
                chunks.append(word[:limit])
                word = word[limit:]
                limit = max_length
                continue
            current = word
            word = ""

    if current:
        chunks.append(current)
    return chunks


def _split_continuous_speech_text(text: str) -> list[str]:
    """Normalize a read into bounded requests while preserving punctuation."""
    return split_speech_text(
        text,
        max_length=MAX_CHUNK_LENGTH,
        first_chunk_length=MAX_CHUNK_LENGTH,
        preserve_sentence_punctuation=True,
    )


def speech_start_offset(text: str, position: int) -> int:
    """Return the clicked word's start, or the nearest word around the caret."""
    words = _word_spans(text)
    if not words:
        return len(text)

    position = max(0, min(position, len(text)))
    for start, end in words:
        if start <= position < end:
            return start
        if start >= position:
            return start
    return words[-1][0]


def speech_word_span(text: str, position: int) -> tuple[int, int]:
    """Return the displayed-text range for the word at or after a position."""
    words = _word_spans(text)
    if not words:
        return len(text), len(text)
    position = max(0, min(position, len(text)))
    for start, end in words:
        if start <= position < end or start >= position:
            return start, end
    return words[-1]


_IDEOGRAPHIC_RANGES = (
    (0x3005, 0x3007),    # 々 〆 〇
    (0x303B, 0x303B),    # 〻
    (0x3040, 0x30FF),    # Hiragana and Katakana
    (0x31F0, 0x31FF),    # Katakana phonetic extensions
    (0x3400, 0x4DBF),    # CJK extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFF66, 0xFF9F),    # Halfwidth Katakana
    (0x20000, 0x2FA1F),  # CJK extensions B-F
)


def _is_ideographic_character(character: str) -> bool:
    """True for scripts written without spaces between words."""
    code = ord(character)
    return any(start <= code <= end for start, end in _IDEOGRAPHIC_RANGES)


def _word_spans_with(text: str, *, underscore: bool) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = None
    for index, character in enumerate(text):
        if _is_ideographic_character(character):
            # Japanese and Chinese have no spaces: each character is its own
            # word, and a boundary such as 音楽 matches two consecutive spans.
            if start is not None:
                spans.append((start, index))
                start = None
            spans.append((index, index + 1))
            continue
        category = unicodedata.category(character)
        is_mark = category.startswith("M")
        if is_mark and start is None and spans and spans[-1][1] == index:
            # A combining mark on the ideograph just emitted belongs to it.
            spans[-1] = (spans[-1][0], index + 1)
            continue
        is_word = (
            character.isalnum() or is_mark
            or (underscore and character == "_")
        )
        if is_word:
            if start is None and not is_mark:
                start = index
        elif start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Return word ranges while keeping combining marks attached to their letters."""
    return _word_spans_with(text, underscore=True)


def python_to_qt_position(text: str, position: int) -> int:
    """Convert a Python code-point offset to QTextCursor's UTF-16 offset."""
    position = max(0, min(position, len(text)))
    return len(text[:position].encode("utf-16-le")) // 2


def qt_to_python_position(text: str, position: int) -> int:
    """Convert QTextCursor's UTF-16 offset to a Python code-point offset."""
    remaining = max(0, position)
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if remaining < width:
            return index
        remaining -= width
    return len(text)


class SpeechWorker(QThread):
    segment_stream_ready = Signal(int, str)
    word_boundary_ready = Signal(int, object)
    segment_ready = Signal(int, str)
    word_boundaries = Signal(int, object)
    failed = Signal(str)

    def __init__(
        self,
        text: str,
        parent=None,
        rate: float = DEFAULT_SPEECH_RATE,
        source_offset: int = 0,
        voice: str = ARABIC_VOICE,
        automatic_voice: bool = False,
        target_language: str = "ar",
        fast: bool = True,
        stream_server: AudioStreamServer | None = None,
        prefetch_only: bool = False,
        trace: SpeechTraceLike | None = None,
        stream_audio: bool = True,
        continuous_stream: bool = False,
        read_latin_words: bool = False,
        engine: str = "edge",
        gemini_api_key: str = "",
        gemini_voice: str = "Kore",
        latin_only: bool = False,
        glossary: dict[str, str] | None = None,
    ):
        super().__init__(parent)
        continuous = bool(continuous_stream and engine == "edge")
        (
            self._segments,
            self._segment_voices,
            self._segment_word_spans,
        ) = _speech_segments_with_voices(
            text,
            voice,
            automatic_voice=automatic_voice,
            target_language=target_language,
            read_latin_words=read_latin_words,
            first_chunk_length=FIRST_CHUNK_LENGTH,
            continuous=continuous,
            latin_only=latin_only,
            glossary=dict(glossary or {}),
        )
        self._source_text = text
        self._source_offset = source_offset
        self.source_end_offset = source_offset + max(
            (
                end
                for spans in self._segment_word_spans
                for _, end in spans
            ),
            default=0,
        )
        self._voice = voice
        self._engine = engine
        self._gemini_api_key = gemini_api_key
        self._gemini_voice = gemini_voice
        self._target_language = target_language
        self._read_latin_words = bool(read_latin_words)
        self._latin_only = bool(latin_only)
        self._automatic_voice = bool(automatic_voice)
        self._foreign_pauses = (
            str(target_language or "").casefold().split("-", 1)[0] == "ar"
            and (self._latin_only or self._read_latin_words)
        )
        self._output_paths: list[Path] = []
        self._rate = rate_string(rate)
        # fast=False: classic behavior — no disk cache, strictly sequential.
        self._fast = bool(fast)
        self._stream_server = stream_server
        self._owns_stream_server = stream_server is None
        self._stream_audio = bool(stream_audio)
        self._continuous_stream = continuous and len(self._segments) == 1
        self._prefetch_only = prefetch_only
        self._trace = trace
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._synthesis_task: asyncio.Task[None] | None = None

    def run(self) -> None:
        if self._trace is not None:
            self._trace.mark(
                "worker_started",
                segment_count=len(self._segments),
                continuous=self._continuous_stream,
                cached_mode=self._fast,
            )
        try:
            if not speech_available(self._engine, self._gemini_api_key):
                if self._trace is not None:
                    self._trace.mark("speech_engine_unavailable")
                self.failed.emit(speech_missing_message(self._engine))
                return
            from translators.http_client import cancellation_scope
            with cancellation_scope(self.isInterruptionRequested):
                asyncio.run(self._run_synthesis())
        except asyncio.CancelledError:
            if self._trace is not None:
                self._trace.mark("worker_cancelled")
            if not self.isInterruptionRequested():
                self.failed.emit("Speech synthesis was cancelled unexpectedly.")
        except Exception as exc:
            if self._trace is not None:
                self._trace.mark(
                    "worker_failed", error_type=type(exc).__name__)
            if not self.isInterruptionRequested():
                self.failed.emit(_safe_speech_error(exc))
        finally:
            if self._fast:
                _prune_cache()
            if self._trace is not None:
                self._trace.mark(
                    "worker_finished",
                    interrupted=self.isInterruptionRequested(),
                )
            if self._owns_stream_server and self._stream_server is not None:
                self._stream_server.close()
            if self.isInterruptionRequested() or self._continuous_stream:
                self.cleanup_output()

    async def _synthesize(self) -> None:
        total = len(self._segments)
        if not total:
            if self._trace is not None:
                self._trace.mark("no_speakable_segments")
            return
        if self._prefetch_only:
            await self._make_segment(0)
            return
        if self._continuous_stream:
            await self._synthesize_continuous(total)
            return
        if not self._fast:
            for index in range(total):
                if self.isInterruptionRequested():
                    return
                path, boundaries = await self._make_segment(index)
                self.word_boundaries.emit(index, boundaries)
                self.segment_ready.emit(index, path)
            return
        # Stream segment 0 first so playback can begin before synthesis completes.
        first = await self._make_segment(0)
        if self.isInterruptionRequested():
            self.cleanup_output()
            return
        self.word_boundaries.emit(0, first[1])
        self.segment_ready.emit(0, first[0])
        if total == 1:
            return
        semaphore = asyncio.Semaphore(MAX_PARALLEL_SEGMENTS)

        async def synthesize_limited(index: int) -> tuple[str, list]:
            async with semaphore:
                return await self._make_segment(index)

        tasks = [
            asyncio.create_task(synthesize_limited(index))
            for index in range(1, total)
        ]
        try:
            for index, task in enumerate(tasks, start=1):
                if self.isInterruptionRequested():
                    break
                seg_path, seg_bounds = await task
                if self.isInterruptionRequested():
                    break
                self.word_boundaries.emit(index, seg_bounds)
                self.segment_ready.emit(index, seg_path)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.isInterruptionRequested():
            self.cleanup_output()

    def requestInterruption(self) -> None:
        super().requestInterruption()
        loop = self._event_loop
        task = self._synthesis_task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The loop finished between the state check and cancellation.

    async def _run_synthesis(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._synthesis_task = asyncio.current_task()
        try:
            if not self.isInterruptionRequested():
                await self._synthesize()
        finally:
            self._synthesis_task = None
            self._event_loop = None

    async def _synthesize_continuous(self, total: int) -> None:
        # Keep one MP3 response open across chunks to avoid player source gaps.
        if self._stream_server is None:
            self._stream_server = AudioStreamServer()
            self._owns_stream_server = True
        stream_server = self._stream_server
        token, url = stream_server.create_stream()
        if self._trace is not None:
            self._trace.mark(
                "continuous_stream_created", segment_count=total)
        self.segment_stream_ready.emit(0, url)
        all_boundaries: list[tuple[int, int, int, int]] = []
        offset_ticks = 0
        first_audio_ready = asyncio.Event()
        prefetch_limit = asyncio.Semaphore(CONTINUOUS_PREFETCH_SEGMENTS)
        prefetch_tasks: dict[int, asyncio.Task[tuple[str, list]]] = {}

        async def synthesize_first() -> tuple[str, list]:
            try:
                return await self._make_segment(
                    0,
                    continuous_token=token,
                    continuous_offset_ticks=offset_ticks,
                    first_audio_ready=first_audio_ready,
                )
            finally:
                first_audio_ready.set()

        async def prefetch(index: int) -> tuple[str, list]:
            started_ns = time.perf_counter_ns()
            async with prefetch_limit:
                result = await self._make_segment(
                    index, emit_word_boundaries=False)
            if self._trace is not None:
                self._trace.mark(
                    "continuous_prefetch_ready",
                    once=False,
                    segment=index,
                    elapsed_ms=round(
                        (time.perf_counter_ns() - started_ns) / 1_000_000,
                        1,
                    ),
                )
            return result

        def schedule_prefetch(index: int) -> None:
            prefetch_tasks[index] = asyncio.create_task(prefetch(index))
            if self._trace is not None:
                self._trace.mark(
                    "continuous_prefetch_started",
                    once=False,
                    segment=index,
                )

        async def handoff_prefetched_segment(
            index: int, result: tuple[str, list], wait_ms: float
        ) -> None:
            nonlocal offset_ticks
            path, boundaries = result
            audio_path = Path(path)
            audio_bytes = audio_path.stat().st_size
            segment_boundaries = [
                (
                    start + offset_ticks,
                    duration,
                    source_start,
                    source_end,
                )
                for start, duration, source_start, source_end in boundaries
            ]
            all_boundaries.extend(segment_boundaries)
            for boundary in segment_boundaries:
                self.word_boundary_ready.emit(0, boundary)
            await asyncio.to_thread(
                stream_server.write,
                token,
                audio_path.read_bytes(),
            )
            if self._trace is not None:
                self._trace.mark(
                    "continuous_segment_handoff",
                    segment=index,
                    audio_bytes=audio_bytes,
                    word_boundaries=len(boundaries),
                    handoff_wait_ms=round(wait_ms, 1),
                )
            offset_ticks += (
                audio_bytes * 8 * _TICKS_PER_SECOND // _MP3_BITRATE_BPS
            )
            self._cleanup_output_path(audio_path)

        for index in range(
            1, min(total, CONTINUOUS_PREFETCH_SEGMENTS + 1)
        ):
            schedule_prefetch(index)
        first_task = asyncio.create_task(synthesize_first())
        try:
            await first_audio_ready.wait()
            first_path, first_boundaries = await first_task
            if self.isInterruptionRequested():
                return
            all_boundaries.extend(first_boundaries)
            self.word_boundaries.emit(0, list(all_boundaries))
            first_audio_bytes = Path(first_path).stat().st_size
            if self._trace is not None:
                self._trace.mark(
                    "continuous_segment_handoff",
                    segment=0,
                    audio_bytes=first_audio_bytes,
                    word_boundaries=len(first_boundaries),
                    handoff_wait_ms=0.0,
                )
            offset_ticks += (
                first_audio_bytes * 8 * _TICKS_PER_SECOND
                // _MP3_BITRATE_BPS
            )
            self._cleanup_output_path(Path(first_path))

            for index in range(1, total):
                if self.isInterruptionRequested():
                    return
                handoff_started_ns = time.perf_counter_ns()
                result = await prefetch_tasks.pop(index)
                handoff_wait_ms = (
                    time.perf_counter_ns() - handoff_started_ns
                ) / 1_000_000
                next_index = index + CONTINUOUS_PREFETCH_SEGMENTS
                if next_index < total:
                    schedule_prefetch(next_index)
                await handoff_prefetched_segment(
                    index, result, handoff_wait_ms)
                self.word_boundaries.emit(0, list(all_boundaries))
        finally:
            for task in prefetch_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                first_task, *prefetch_tasks.values(), return_exceptions=True
            )
            await asyncio.to_thread(stream_server.finish, token)
            if self._trace is not None:
                self._trace.mark("continuous_stream_finished")

    def _synthesis_text(self, text: str) -> str:
        """The exact text sent to the voice for one segment.

        The prefetch and the cache probes must call this too, or they hash a
        different request from the one the reader will play.
        """
        synthesis_text = _speech_text_for_synthesis(
            text, self._target_language)
        if self._foreign_pauses:
            synthesis_text = _with_foreign_pauses(synthesis_text)
        return synthesis_text

    async def _make_segment(
        self,
        index: int,
        *,
        continuous_token: str | None = None,
        continuous_offset_ticks: int = 0,
        first_audio_ready: asyncio.Event | None = None,
        emit_word_boundaries: bool = True,
    ) -> tuple[str, list]:
        """Synthesize one segment, cache its audio, and map word boundaries."""
        segment_started_ns = time.perf_counter_ns()
        text = self._segments[index]
        synthesis_text = self._synthesis_text(text)
        voice = (
            self._gemini_voice
            if self._engine == GEMINI_TTS_ENGINE
            else self._segment_voices[index]
        )
        spans = self._segment_word_spans[index]
        ssml_language = self._ssml_language_for_segment(
            synthesis_text, voice)
        suffix = ".wav" if self._engine == GEMINI_TTS_ENGINE else ".mp3"
        key = _cache_key(
            synthesis_text,
            voice,
            self._rate,
            ssml_language=ssml_language,
            engine=self._engine,
        )
        if self._trace is not None:
            self._trace.mark(
                "segment_started",
                once=False,
                segment=index,
                characters=len(synthesis_text),
                voice=voice,
                ssml_language=ssml_language,
            )
        if self._fast:
            hit = (
                self._read_cache(key, suffix=suffix)
                if suffix != ".mp3"
                else self._read_cache(key)
            )
            if hit is not None:
                audio_bytes, cached_boundaries = hit
                if all(
                    0 <= boundary[2] < boundary[3] <= len(spans)
                    for boundary in cached_boundaries
                ):
                    boundaries = [
                        (
                            start,
                            duration,
                            spans[first_word][0] + self._source_offset,
                            spans[end_word - 1][1] + self._source_offset,
                        )
                        for start, duration, first_word, end_word
                        in cached_boundaries
                    ]
                    if continuous_token is not None:
                        for boundary in boundaries:
                            self.word_boundary_ready.emit(
                                0,
                                (
                                    boundary[0] + continuous_offset_ticks,
                                    boundary[1],
                                    boundary[2],
                                    boundary[3],
                                ),
                            )
                        await asyncio.to_thread(
                            self._stream_server.write,
                            continuous_token,
                            audio_bytes,
                        )
                    if first_audio_ready is not None:
                        first_audio_ready.set()
                    if self._trace is not None:
                        self._trace.mark(
                            "segment_synthesized_from_cache",
                            once=False,
                            segment=index,
                            elapsed_ms=round(
                                (time.perf_counter_ns() - segment_started_ns)
                                / 1_000_000,
                                1,
                            ),
                            audio_bytes=len(audio_bytes),
                            word_boundaries=len(boundaries),
                        )
                    if self._prefetch_only:
                        return "", boundaries
                    if suffix == ".mp3":
                        return self._store_temp(audio_bytes, boundaries)
                    return self._store_temp(
                        audio_bytes, boundaries, suffix=suffix)
            if self._trace is not None:
                self._trace.mark(
                    "segment_cache_miss",
                    once=False,
                    segment=index,
                )

        if self._engine == GEMINI_TTS_ENGINE:
            return await self._make_gemini_segment(
                index,
                key=key,
                text=synthesis_text,
                suffix=suffix,
            )

        boundaries: list[tuple[int, int, int, int]] = []
        boundary_word_ranges: list[tuple[int, int]] = []
        chunks: list[bytes] = []
        stream: tuple[AudioStreamServer, str] | None = None
        word_index = 0
        language_token = None
        first_audio = True
        first_boundary = True
        audio_write_total_ms = 0.0
        audio_write_max_ms = 0.0

        async def write_audio(
            server: AudioStreamServer, stream_token: str, audio_data: bytes
        ) -> None:
            nonlocal audio_write_total_ms, audio_write_max_ms
            write_started_ns = time.perf_counter_ns()
            await asyncio.to_thread(server.write, stream_token, audio_data)
            write_ms = (
                time.perf_counter_ns() - write_started_ns
            ) / 1_000_000
            audio_write_total_ms += write_ms
            audio_write_max_ms = max(audio_write_max_ms, write_ms)

        try:
            if getattr(edge_tts, "Communicate", None) is None:
                raise RuntimeError("edge-tts is not installed")
            if self._trace is not None:
                self._trace.mark(
                    "edge_request_started",
                    once=False,
                    segment=index,
                    voice=voice,
                )
            if ssml_language is not None:
                language_token = _EDGE_TTS_LANGUAGE.set(ssml_language)
            stream_iterator = _edge_tts_stream_with_retry(
                synthesis_text, voice, self._rate).__aiter__()
            while True:
                if self.isInterruptionRequested():
                    raise asyncio.CancelledError()
                try:
                    chunk = await stream_iterator.__anext__()
                except StopAsyncIteration:
                    break
                if chunk["type"] == "audio":
                    audio = chunk["data"]
                    chunks.append(audio)
                    if first_audio:
                        first_audio = False
                        if first_audio_ready is not None:
                            first_audio_ready.set()
                        if self._trace is not None:
                            self._trace.mark(
                                "edge_first_audio",
                                once=False,
                                segment=index,
                                elapsed_ms=round(
                                    (time.perf_counter_ns()
                                     - segment_started_ns) / 1_000_000,
                                    1,
                                ),
                                chunk_bytes=len(audio),
                            )
                    if continuous_token is not None:
                        await write_audio(
                            self._stream_server, continuous_token, audio)
                    elif (
                        self._stream_audio
                        and not self._prefetch_only
                        and not self._continuous_stream
                        and stream is None
                    ):
                        if self._stream_server is None:
                            self._stream_server = AudioStreamServer()
                        token, url = self._stream_server.create_stream()
                        stream = (self._stream_server, token)
                        self.segment_stream_ready.emit(index, url)
                    if stream is not None:
                        await write_audio(stream[0], stream[1], audio)
                elif chunk["type"] == "WordBoundary":
                    if first_boundary:
                        first_boundary = False
                        if self._trace is not None:
                            self._trace.mark(
                                "edge_first_word_boundary",
                                once=False,
                                segment=index,
                                elapsed_ms=round(
                                    (time.perf_counter_ns()
                                     - segment_started_ns) / 1_000_000,
                                    1,
                                ),
                            )
                    first_word = word_index
                    matched = _match_synthesis_boundary_span(
                        chunk.get("text", ""), synthesis_text,
                        self._source_text, spans, word_index)
                    if matched is not None:
                        start, end, word_index = matched
                        boundary_word_ranges.append((first_word, word_index))
                        boundary = (
                            int(chunk["offset"]), int(chunk["duration"]),
                            start + self._source_offset,
                            end + self._source_offset,
                        )
                        boundaries.append(boundary)
                        if continuous_token is not None:
                            self.word_boundary_ready.emit(
                                0,
                                (
                                    boundary[0] + continuous_offset_ticks,
                                    boundary[1],
                                    boundary[2],
                                    boundary[3],
                                ),
                            )
                        elif emit_word_boundaries:
                            self.word_boundary_ready.emit(index, boundary)
            if not chunks:
                raise RuntimeError("Edge TTS returned no audio for a speech segment.")
            audio = b"".join(chunks)
        finally:
            if language_token is not None:
                _EDGE_TTS_LANGUAGE.reset(language_token)
            if stream is not None:
                await asyncio.to_thread(stream[0].finish, stream[1])
        if self._fast:
            cached_boundaries = [
                (
                    boundary[0],
                    boundary[1],
                    first_word,
                    end_word,
                )
                for boundary, (first_word, end_word)
                in zip(boundaries, boundary_word_ranges)
            ]
            self._write_cache(key, audio, cached_boundaries)
        if self._trace is not None:
            self._trace.mark(
                "segment_synthesized",
                once=False,
                segment=index,
                elapsed_ms=round(
                    (time.perf_counter_ns() - segment_started_ns) / 1_000_000,
                    1,
                ),
                audio_bytes=len(audio),
                audio_chunks=len(chunks),
                word_boundaries=len(boundaries),
                audio_write_total_ms=round(audio_write_total_ms, 1),
                audio_write_max_ms=round(audio_write_max_ms, 1),
                prefetch_only=self._prefetch_only,
            )
        if self._prefetch_only:
            return "", boundaries
        return self._store_temp(audio, boundaries)

    async def _make_gemini_segment(
        self,
        index: int,
        *,
        key: str,
        text: str,
        suffix: str,
    ) -> tuple[str, list]:
        if self._trace is not None:
            self._trace.mark(
                "gemini_tts_request_started",
                once=False,
                segment=index,
                voice=self._gemini_voice,
            )
        audio = await asyncio.to_thread(
            _gemini_tts_audio,
            text,
            self._gemini_voice,
            self._gemini_api_key,
        )
        if self.isInterruptionRequested():
            raise asyncio.CancelledError()
        if self._fast:
            self._write_cache(key, audio, [], suffix=suffix)
        if self._trace is not None:
            self._trace.mark(
                "gemini_tts_segment_synthesized",
                once=False,
                segment=index,
                audio_bytes=len(audio),
            )
        if self._prefetch_only:
            return "", []
        return self._store_temp(audio, [], suffix=suffix)

    def _ssml_language_for_segment(
        self, text: str, voice: str
    ) -> str | None:
        if self._target_language.casefold().split("-", 1)[0] != "ar":
            return None
        return "ar-SA"

    def _store_temp(
        self, audio: bytes, boundaries: list, *, suffix: str = ".mp3"
    ) -> tuple[str, list]:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            path = Path(tmp.name)
        self._output_paths.append(path)
        return str(path), boundaries

    @staticmethod
    def _read_cache(
        key: str, *, suffix: str = ".mp3"
    ) -> tuple[bytes, list] | None:
        try:
            cache = _cache_dir()
            mp3 = cache / f"{key}{suffix}"
            meta = cache / f"{key}.json"
            if not (mp3.is_file() and meta.is_file()):
                return None
            audio = mp3.read_bytes()
            metadata = json.loads(meta.read_text("utf-8"))
            if isinstance(metadata, dict):
                if metadata.get("audio_size") != len(audio):
                    return None
                boundary_data = metadata.get("boundaries", [])
            else:
                boundary_data = metadata
            boundaries = [tuple(b) for b in boundary_data]
        except (OSError, TypeError, ValueError):
            return None
        if not audio:
            return None
        now = time.time()
        try:
            os.utime(mp3, (now, now))
            os.utime(meta, (now, now))
        except OSError:
            pass
        return audio, boundaries

    @staticmethod
    def _write_cache(
        key: str,
        audio: bytes,
        boundaries: list,
        *,
        suffix: str = ".mp3",
    ) -> None:
        try:
            cache = _cache_dir()
            (cache / f"{key}{suffix}").write_bytes(audio)
            (cache / f"{key}.json").write_text(
                json.dumps({
                    "audio_size": len(audio),
                    "boundaries": [list(b) for b in boundaries],
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

    def cleanup_output(self) -> None:
        """Delete temp mp3s; keep locked ones for a later cleanup retry."""
        remaining = []
        for path in self._output_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                remaining.append(path)
        self._output_paths = remaining

    def _cleanup_output_path(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
        self._output_paths = [
            output_path
            for output_path in self._output_paths
            if output_path != path
        ]


class VoiceListWorker(QThread):
    voices_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = None
        self._task = None

    def requestInterruption(self):
        super().requestInterruption()
        if self._loop is not None and self._task is not None:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass

    async def _list(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            if self.isInterruptionRequested():
                return []
            return await asyncio.wait_for(edge_tts.list_voices(), timeout=20)
        finally:
            self._loop = self._task = None

    def run(self) -> None:
        try:
            if getattr(edge_tts, "Communicate", None) is None:
                raise RuntimeError("The edge-tts package is not installed.")
            voices = asyncio.run(self._list())
            if not self.isInterruptionRequested():
                self.voices_ready.emit(voices)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


def _segment_word_spans(
    text: str,
    segments: list[str],
    source_words: list[tuple[int, int]] | None = None,
    source_tokens: list[str] | None = None,
) -> list[list[tuple[int, int]]]:
    """Map normalized speech segments back to word ranges in the displayed text.

    ``source_tokens`` lets a caller supply the *spoken* word for each source
    range when the display text and the synthesis text differ (a foreign name
    is spoken in Latin where the display shows its Arabic transliteration).
    """
    if source_words is None:
        source_words = _speech_source_word_spans(text)
    next_source_word = 0
    partial_word_offset = 0
    result: list[list[tuple[int, int]]] = []
    for segment in segments:
        spans: list[tuple[int, int]] = []
        for segment_start, segment_end in _word_spans(segment):
            token = segment[segment_start:segment_end].casefold()
            if next_source_word >= len(source_words):
                continue
            source_start, source_end = source_words[next_source_word]
            source_token = (
                source_tokens[next_source_word].casefold()
                if source_tokens is not None
                else text[source_start:source_end].casefold()
            )
            if source_tokens is not None:
                planned = _normalize_boundary_token(source_token)
                spoken = _normalize_boundary_token(token)
                if not planned or not spoken or not (
                    planned.startswith(spoken) or spoken.startswith(planned)
                ):
                    continue
                spans.append((source_start, source_end))
                next_source_word += 1
                partial_word_offset = 0
                continue
            token_start = partial_word_offset
            if not source_token[token_start:].startswith(token):
                continue
            start = source_start + max(0, token_start)
            end = min(source_end, start + segment_end - segment_start)
            spans.append((start, end))
            if end >= source_end:
                next_source_word += 1
                partial_word_offset = 0
            else:
                partial_word_offset = end - source_start
        result.append(spans)
    return result


class LatinOnlySpeech(NamedTuple):
    """Speech text built from a display text, plus its highlight map.

    ``words`` are word spans inside ``text``; ``display_spans`` holds the
    matching span in the original display text for each spoken word. A spoken
    foreign name points at the Arabic words it replaces, so the highlight stays
    on the Arabic while the voice says the Latin name.
    """

    text: str
    words: list[tuple[int, int]]
    display_spans: list[tuple[int, int]]
    learned: dict[str, str]


_LATIN_PAIR_RE = re.compile(r"[\(\[]\s*([^\(\)\[\]]{1,120}?)\s*[\)\]]")
_ARABIC_MARK_RE = re.compile(
    r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_ARABIC_LETTER_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
# Bidirectional and zero-width controls carry no sound. Translation models
# often put them around a Latin name inside Arabic text; they must not split
# the pair or reach the voice.
_INVISIBLE_SPEECH_RE = re.compile(
    r"[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PAIR_GAP_RE = re.compile(
    r"[\s،,«»\"'“”‘’„‟‹›「」『』"
    r"\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]*")
_LATIN_QUOTE_CHARACTERS = "\"'«»“”‘’„‟‹›「」『』"
_MAX_PAIR_WORDS = 6

# One Arabic letter or diacritic: the edge of a word that touches Latin.
# Arabic punctuation lives in the same Unicode block, so exclude it.
_ARABIC_SEAM = (
    r"(?![،؛؟٪-٬۔])"
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff"
    r"\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]"
)
_FOREIGN_SEAM_START_RE = re.compile(
    rf"({_ARABIC_SEAM})(\s*)(?=[A-Za-z\u00c0-\u00d6\u00d8-\u00f6"
    rf"\u00f8-\u024f\u1e00-\u1eff])")
_FOREIGN_SEAM_END_RE = re.compile(
    rf"(?<=[A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f"
    rf"\u1e00-\u1eff'’])(\s*)({_ARABIC_SEAM})")
_LATIN_SEAM_LETTER_RE = re.compile(
    r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff]")


def _with_foreign_pauses(text: str) -> str:
    """Add a comma after an inline Latin run before Arabic resumes.

    A multilingual voice slides from Arabic into an inline Latin name and
    clips the name's final word. A comma after the name lengthens it, but it
    also inserts an audible stop after every foreign phrase. That stop is kept
    because it makes the name intelligible and the text reads better. No comma
    is added before the name, at paragraph edges, or beside real punctuation.
    The displayed text and the word/highlight map never change, and
    punctuation does not create words, so word indices stay identical.
    """
    text = _INVISIBLE_SPEECH_RE.sub("", text)
    # Emphasis and quote markers carry no sound; keep them out of the seam so
    # ``*Jess Haskins*`` gets the same space as the plain name.
    text = re.sub(r"[*_\"“”‘’«»]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    if (
        not _ARABIC_LETTER_RE.search(text)
        or not _LATIN_SEAM_LETTER_RE.search(text)
    ):
        return text
    # A plain space before the Latin run: the voice must enter the name
    # without a pause. The comma belongs after the run, where it ends the
    # foreign phrase before Arabic resumes.
    text = _FOREIGN_SEAM_START_RE.sub(r"\1 ", text)
    text = _FOREIGN_SEAM_END_RE.sub(r"، \2", text)
    text = re.sub(r"[ \t]*،[ \t]*", "، ", text)
    return text.strip()

# Arabic words that describe the name instead of transliterating it. They stay
# in Arabic: ``شمال كارولاينا (North Carolina)`` keeps ``شمال`` audible.
_ARABIC_NAME_STOP_WORDS = frozenset({
    "في", "من", "إلى", "الى", "على", "عن", "مع", "ثم", "و", "أو", "او",
    "أن", "ان", "إن", "كان", "كانت", "هذا", "هذه", "ذلك", "تلك", "كل",
    "بعد", "قبل", "بين", "حتى", "قد", "لا", "ما", "هو", "هي", "هم",
    "كما", "لكن", "حيث", "عند", "لدى", "شمال", "جنوب", "شرق", "غرب",
    "وسط", "ضد", "حول", "خلال", "دون", "سوى", "نحو", "منذ", "مدينة",
})


def _normalize_arabic_phrase(text: str) -> str:
    """Stable key for the same name with or without its diacritics."""
    text = _ARABIC_MARK_RE.sub("", text)
    for source, replacement in (
        ("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه"),
    ):
        text = text.replace(source, replacement)
    return " ".join(text.split())


_NORMALIZED_NAME_STOP_WORDS = frozenset(
    _normalize_arabic_phrase(word) for word in _ARABIC_NAME_STOP_WORDS)

# Month, weekday and calendar-unit names are Arabic calendar words, never the
# foreign name behind a parenthetical. Once a book glossary learned
# ``يناير -> January`` it read every later date in English, even where no
# parenthetical remained.
_ARABIC_CALENDAR_WORDS = frozenset({
    "يناير", "فبراير", "مارس", "أبريل", "ابريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "اغسطس", "سبتمبر", "أكتوبر", "اكتوبر",
    "نوفمبر", "ديسمبر",
    "كانون", "كانون الأول", "كانون الثاني", "شباط", "آذار", "نيسان",
    "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين",
    "تشرين الأول", "تشرين الثاني",
    "الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة",
    "السبت",
    "شهر", "الشهر", "شهور", "أشهر", "يوم", "اليوم", "أيام",
    "سنة", "السنة", "سنوات", "عام", "العام", "أعوام",
    "أسبوع", "الأسبوع", "أسبوعين", "أسابيع",
})
_NORMALIZED_CALENDAR_WORDS = frozenset(
    _normalize_arabic_phrase(word) for word in _ARABIC_CALENDAR_WORDS)


def _contains_calendar_word(key: str) -> bool:
    """True when a learned phrase is or includes a month/weekday name."""
    if key in _NORMALIZED_CALENDAR_WORDS:
        return True
    return any(
        word in _NORMALIZED_CALENDAR_WORDS for word in key.split())

_ARABIC_PHONETIC_MAP = {
    "ا": "a", "أ": "a", "إ": "e", "آ": "a", "ء": "", "ؤ": "", "ئ": "",
    "ى": "a", "ة": "h", "ب": "b", "ت": "t", "ث": "th", "ج": "j",
    "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "s",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "dh",
    "ع": "", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l",
    "م": "m", "ن": "n", "ه": "h", "و": "w", "ي": "y",
}
_FOREIGN_MATCH_THRESHOLD = 0.72


def _phonetic_key(text: str) -> str:
    """Consonant skeleton shared by an Arabic transliteration and its original.

    ``غْرَيْهَاوُنْد`` and ``Greyhound`` both reduce to ``GRHND``, while
    ``حَافِلَات`` (``HFLT``) stays far from ``Bus`` (``BS``), so the reader can
    keep genuine Arabic words and pronounce the foreign ones.
    """
    mapped = "".join(
        _ARABIC_PHONETIC_MAP.get(character, character)
        for character in _ARABIC_MARK_RE.sub("", text).casefold()
    )
    mapped = unicodedata.normalize("NFKD", mapped)
    mapped = "".join(
        character for character in mapped if character.isalpha())
    for source, replacement in (
        ("ph", "f"), ("gh", "g"), ("kh", "k"), ("sh", "s"), ("ch", "s"),
        ("th", "t"), ("dh", "d"), ("ck", "k"), ("qu", "k"),
        ("ci", "si"), ("ce", "se"), ("cy", "sy"),
        ("q", "k"), ("c", "k"), ("x", "ks"), ("j", "g"),
        ("v", "f"), ("p", "b"), ("z", "s"),
        ("w", ""), ("y", ""),
        ("a", ""), ("e", ""), ("i", ""), ("o", ""), ("u", ""),
    ):
        mapped = mapped.replace(source, replacement)
    mapped = "".join(
        character for character in mapped if character.isalpha())
    collapsed: list[str] = []
    for character in mapped:
        if not collapsed or collapsed[-1] != character:
            collapsed.append(character)
    return "".join(collapsed).upper()


def _looks_like_arabic_derivation(word: str) -> bool:
    """True for a genuine Arabic word built with Arabic morphology.

    ``الفِدْرَالِيّ`` shares its consonants with ``Federal`` because it is a
    loanword, but the article plus the nisba ending mark it as a translated
    adjective: it must stay Arabic, and its parenthetical must not be read.
    """
    word = _normalize_arabic_phrase(word)
    if not word:
        return False
    if word.endswith("يه"):
        return True
    return word.startswith("ال") and word.endswith("ي")


def _word_match_score(arabic_key: str, latin_key: str) -> float:
    """Similarity of two consonant skeletons, exact for one-letter names."""
    if len(arabic_key) < 2 or len(latin_key) < 2:
        return 1.0 if arabic_key == latin_key else 0.0
    return difflib.SequenceMatcher(None, arabic_key, latin_key).ratio()


def _match_pair_words(
    text: str,
    arabic_words: list[tuple[int, int]],
    latin: str,
) -> list[tuple[tuple[int, int], str]]:
    """Pair each Arabic word with the Latin word it transliterates."""
    latin_spans = _speech_source_word_spans(latin)
    latin_keys = [_phonetic_key(latin[start:end]) for start, end in latin_spans]
    candidates: list[tuple[float, int, int]] = []
    for arabic_index, (start, end) in enumerate(arabic_words):
        if _looks_like_arabic_derivation(text[start:end]):
            continue
        arabic_key = _phonetic_key(text[start:end])
        if not arabic_key:
            continue
        for latin_index, latin_key in enumerate(latin_keys):
            if not latin_key:
                continue
            score = _word_match_score(arabic_key, latin_key)
            if score >= _FOREIGN_MATCH_THRESHOLD:
                candidates.append((score, arabic_index, latin_index))
    candidates.sort(reverse=True)
    used_arabic: set[int] = set()
    used_latin: set[int] = set()
    matched: list[tuple[tuple[int, int], str]] = []
    for _score, arabic_index, latin_index in candidates:
        if arabic_index in used_arabic or latin_index in used_latin:
            continue
        used_arabic.add(arabic_index)
        used_latin.add(latin_index)
        latin_start, latin_end = latin_spans[latin_index]
        matched.append((
            arabic_words[arabic_index], latin[latin_start:latin_end]))
    matched.sort()
    return matched


def _is_attached_waw_conjunction(text: str, latin: str) -> bool:
    """True for an Arabic ``و`` that joins a Latin name, not a W name.

    ``وَجُو دِنِيس (Joe Denis)`` starts with the conjunction ``و``, while
    ``وُودْلُون (Woodlawn)`` starts with the name's own W. The Latin spelling
    disambiguates: a W name must keep its Arabic W. The helper is only used
    when a name is already being replaced, so a false positive cannot turn a
    genuine Arabic word into ``and``.
    """
    words = text.split(None, 1)
    if not words:
        return False
    key = _normalize_arabic_phrase(words[0])
    if len(key) < 2 or key[0] != "و" or key[1] == "و":
        return False
    return not latin.lstrip().casefold().startswith(("w", "and "))


def _without_attached_waw(key: str, latin: str) -> str | None:
    """Return the name key without a joining waw, or None for a W name."""
    if _is_attached_waw_conjunction(key, latin):
        return key[1:]
    return None


def _with_attached_and(text: str, latin: str) -> str:
    """Speak a joining Arabic waw as English ``and`` before a Latin name."""
    if _is_attached_waw_conjunction(text, latin):
        return "and " + latin
    return latin


def _pair_learning(
    text: str,
    pair: tuple[int, int, str, int, int],
    words: list[tuple[int, int]],
) -> tuple[dict[str, str], bool]:
    """Learn the foreign words of one pair; report whether it is whole.

    A whole pair (every Arabic word transliterates a Latin word) is learned
    as one phrase and read entirely in Latin. A mixed pair such as
    ``محطة حافلات غريهاوند`` keeps its Arabic words and learns only
    ``غريهاوند -> Greyhound``. A pair with no phonetic match is a genuine
    translation: every Arabic word stays and the parenthetical is dropped.
    """
    start, end, latin, _open, _close = pair
    arabic_words = [
        (word_start, word_end)
        for word_start, word_end in words
        if word_start >= start and word_end <= end
    ]
    matches = _match_pair_words(text, arabic_words, latin)
    learned: dict[str, str] = {}
    for (word_start, word_end), latin_word in matches:
        key = _normalize_arabic_phrase(text[word_start:word_end])
        latin_word = _normalize_latin_speech_text(latin_word)
        if (
            len(key) < 2
            or key in _NORMALIZED_NAME_STOP_WORDS
            or _contains_calendar_word(key)
        ):
            continue
        learned[key] = latin_word
        without_waw = _without_attached_waw(key, latin_word)
        if without_waw:
            learned[without_waw] = latin_word
    whole = bool(matches) and len(matches) == len(arabic_words)
    if whole:
        key = _normalize_arabic_phrase(text[start:end])
        latin = _normalize_latin_speech_text(latin)
        if _contains_calendar_word(key):
            whole = False
        elif key:
            learned[key] = latin
            without_waw = _without_attached_waw(key, latin)
            if without_waw:
                learned[without_waw] = latin
    return learned, whole


def _span_inside_pairs(
    start: int, end: int, pair_spans: list[tuple[int, int]],
) -> bool:
    """A learned word may sit inside a pair, but never straddle its edge."""
    for pair_start, pair_end in pair_spans:
        if start < pair_end and end > pair_start:
            return start >= pair_start and end <= pair_end
    return True


def _foreign_pairs(
    text: str,
) -> list[tuple[int, int, str, int, int]]:
    """Find ``Arabic transliteration (Latin original)`` pairs.

    Returns ``(arabic_start, arabic_end, latin_text, paren_start, paren_end)``.
    Up to as many Arabic words are claimed as the Latin phrase has words, so
    ``مدينة هنتنغتون بيتش (Huntington Beach)`` keeps ``مدينة`` in Arabic; a
    descriptive word such as ``شمال`` also stays Arabic.
    """
    words = _speech_source_word_spans(text)
    pairs: list[tuple[int, int, str, int, int]] = []
    claimed: set[int] = set()
    for match in _LATIN_PAIR_RE.finditer(text):
        latin = _INVISIBLE_SPEECH_RE.sub("", match.group(1))
        latin = latin.strip().strip(_LATIN_QUOTE_CHARACTERS).strip()
        if not _LATIN_LETTER_RE.search(latin):
            continue
        if _ARABIC_LETTER_RE.search(latin):
            continue
        latin_words = _speech_source_word_spans(latin)
        if not 1 <= len(latin_words) <= _MAX_PAIR_WORDS:
            continue
        if any(
            not _LATIN_LETTER_RE.search(latin[start:end])
            for start, end in latin_words
        ):
            continue
        before = [
            index for index, (_, end) in enumerate(words)
            if end <= match.start()
        ]
        if not before:
            continue
        last = before[-1]
        if last in claimed:
            continue
        first = last
        collected = 0
        while first >= 0 and collected < len(latin_words):
            start, end = words[first]
            word = _normalize_arabic_phrase(text[start:end])
            if word in _NORMALIZED_NAME_STOP_WORDS:
                break
            first -= 1
            collected += 1
        first += 1
        if collected < 1:
            continue
        chosen = words[first:last + 1]
        if any(index in claimed for index in range(first, last + 1)):
            continue
        if not _PAIR_GAP_RE.fullmatch(text[chosen[-1][1]:match.start()]):
            continue
        if any(
            not _ARABIC_LETTER_RE.search(text[start:end])
            for start, end in chosen
        ):
            continue
        if any(
            _LATIN_LETTER_RE.search(text[start:end])
            for start, end in chosen
        ):
            continue
        pairs.append((
            chosen[0][0], chosen[-1][1], latin,
            match.start(), match.end(),
        ))
        claimed.update(range(first, last + 1))
    return pairs


def learn_latin_pairs(text: str) -> dict[str, str]:
    """Learn the foreign name behind each Arabic transliteration in ``text``."""
    words = _speech_source_word_spans(text)
    learned: dict[str, str] = {}
    for pair in _foreign_pairs(text):
        pair_learned, _whole = _pair_learning(text, pair, words)
        learned.update(pair_learned)
    return learned


def build_latin_only_speech(
    text: str,
    glossary: dict[str, str] | None = None,
) -> LatinOnlySpeech:
    """Read each foreign word in Latin and keep genuine Arabic words.

    A word written in Arabic that matches the Latin original phonetically is
    spoken in Latin; a genuine Arabic word next to it (``حافلات``, ``مدينة``)
    stays Arabic. Parentheticals that pair with the preceding Arabic name are
    dropped from the speech text and from the highlight, and names learned
    from the book glossary are replaced on every later occurrence.
    """
    glossary = dict(glossary or {})
    words = _speech_source_word_spans(text)
    pairs = _foreign_pairs(text)
    learned: dict[str, str] = {}
    replacements: dict[int, tuple[int, str]] = {}
    pair_spans: list[tuple[int, int]] = []
    deleted: list[tuple[int, int]] = []
    for pair in pairs:
        start, end, latin, open_paren, close_paren = pair
        pair_learned, whole = _pair_learning(text, pair, words)
        learned.update(pair_learned)
        if whole:
            replacements[start] = (
                end,
                _with_attached_and(
                    text[start:end],
                    _normalize_latin_speech_text(latin),
                ),
            )
        pair_spans.append((start, end))
        deleted.append((open_paren, close_paren))

    known = {
        key: _normalize_latin_speech_text(value)
        for key, value in {**glossary, **learned}.items()
        if not _contains_calendar_word(key)
    }
    max_words = max((len(key.split()) for key in known), default=0)
    if known and words:
        index = 0
        while index < len(words):
            word_start, word_end = words[index]
            if any(
                word_start < close and word_end > open
                for open, close in deleted
            ):
                index += 1
                continue
            match: tuple[int, int, str, int] | None = None
            for count in range(min(max_words, len(words) - index), 0, -1):
                start = words[index][0]
                end = words[index + count - 1][1]
                if any(start < close and end > open for open, close in deleted):
                    continue
                candidate = _normalize_arabic_phrase(text[start:end])
                if candidate in known:
                    match = (start, end, known[candidate], count)
                    break
            if match is None:
                index += 1
                continue
            start, end, latin, count = match
            if not _span_inside_pairs(start, end, pair_spans):
                index += 1
                continue
            replacements.setdefault(
                start,
                (end, _with_attached_and(text[start:end], latin)),
            )
            index += count

    characters: list[str] = []
    display_of: list[int] = []
    source_of: list[tuple[int, int] | None] = []
    deletions = sorted(set(deleted))
    delete_index = 0
    cursor = 0
    while cursor < len(text):
        if (
            delete_index < len(deletions)
            and cursor >= deletions[delete_index][1]
        ):
            delete_index += 1
            continue
        if (
            delete_index < len(deletions)
            and deletions[delete_index][0] <= cursor < deletions[delete_index][1]
        ):
            cursor = deletions[delete_index][1]
            continue
        replacement = replacements.get(cursor)
        if replacement is not None:
            end, latin = replacement
            if characters and (
                characters[-1].isalnum()
                or unicodedata.category(characters[-1]).startswith("M")
            ):
                characters.append(" ")
                display_of.append(cursor)
                source_of.append(None)
            for character in latin:
                characters.append(character)
                display_of.append(cursor)
                source_of.append((cursor, end))
            cursor = end
            # A following word must not be glued to the Latin name. Look past
            # the parenthetical being deleted: ``هاسكينز(Jess Haskins)ملاحظات``
            # must become ``Jess Haskins ملاحظات``, not one giant token.
            probe = cursor
            while True:
                containing = next(
                    (close for open_, close in deletions
                     if open_ <= probe < close),
                    None,
                )
                if containing is None:
                    break
                probe = containing
            if (
                probe < len(text)
                and not text[probe].isspace()
                and not unicodedata.category(text[probe]).startswith(("P", "S"))
            ):
                characters.append(" ")
                display_of.append(cursor)
                source_of.append(None)
            continue
        character = text[cursor]
        if character.isspace() and characters and characters[-1].isspace():
            cursor += 1
            continue
        characters.append(character)
        display_of.append(cursor)
        source_of.append(None)
        cursor += 1

    trim_start = 0
    while trim_start < len(characters) and characters[trim_start].isspace():
        trim_start += 1
    trim_end = len(characters)
    while trim_end > trim_start and characters[trim_end - 1].isspace():
        trim_end -= 1
    characters = characters[trim_start:trim_end]
    display_of = display_of[trim_start:trim_end]
    source_of = source_of[trim_start:trim_end]

    speech = "".join(characters)
    speech_words = _speech_source_word_spans(speech)
    display_spans: list[tuple[int, int]] = []
    for word_start, word_end in speech_words:
        starts: list[int] = []
        ends: list[int] = []
        for position in range(word_start, word_end):
            source = source_of[position]
            if source is None:
                starts.append(display_of[position])
                ends.append(display_of[position] + 1)
            else:
                starts.append(source[0])
                ends.append(source[1])
        display_spans.append((min(starts), max(ends)))
    return LatinOnlySpeech(speech, speech_words, display_spans, learned)


def latin_only_click_offset(text: str, position: int) -> int:
    """Start reading at the Arabic name when its Latin gloss is clicked."""
    for start, _end, _latin, open_paren, close_paren in _foreign_pairs(text):
        if open_paren <= position < close_paren:
            return start
    return position


def _speech_segments_with_voices(
    text: str,
    selected_voice: str,
    *,
    automatic_voice: bool = False,
    target_language: str = "ar",
    first_chunk_length: int = FIRST_CHUNK_LENGTH,
    read_latin_words: bool = False,
    continuous: bool = False,
    latin_only: bool = False,
    glossary: dict[str, str] | None = None,
) -> tuple[list[str], list[str], list[list[tuple[int, int]]]]:
    """Split speech into script runs and retain each segment's source spans.

    ``continuous=True`` keeps the whole read as one synthesis request. Separate
    Edge requests restart prosody and MP3 framing, which is heard as a short
    stop at an otherwise arbitrary word boundary.
    """
    words = _speech_source_word_spans(text)
    script_spans = words
    if not words:
        if continuous:
            segments = _split_continuous_speech_text(text)
        else:
            segments = split_speech_text(
                text, first_chunk_length=first_chunk_length)
        voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        return (
            segments,
            [voice] * len(segments),
            _segment_word_spans(text, segments),
        )

    is_arabic_target = (
        target_language.casefold().split("-", 1)[0] == "ar")
    if is_arabic_target and latin_only:
        plan = build_latin_only_speech(text, glossary)
        synthesis_text = plan.text
        if continuous:
            segments = _split_continuous_speech_text(synthesis_text)
        else:
            segments = split_speech_text(
                synthesis_text, first_chunk_length=first_chunk_length)
        segment_spans = _segment_word_spans(
            synthesis_text,
            segments,
            source_words=plan.display_spans,
            source_tokens=[
                synthesis_text[start:end] for start, end in plan.words
            ],
        )
        voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        return segments, [voice] * len(segments), segment_spans

    skip_latin = (
        is_arabic_target
        and not read_latin_words
    )
    if (
        target_language.casefold().split("-", 1)[0] == "ar"
        and read_latin_words
    ):
        # Keep mixed scripts in one request to avoid hand-offs at transitions.
        synthesis_text = _normalize_latin_speech_text(text)
        if continuous:
            segments = _split_continuous_speech_text(synthesis_text)
        else:
            segments = split_speech_text(
                synthesis_text, first_chunk_length=first_chunk_length)
        segment_spans = _segment_word_spans(text, segments)
        voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        return segments, [voice] * len(segments), segment_spans

    scripts: list[str | None] = []
    for start, end in script_spans:
        word = text[start:end]
        letters = [character for character in word if character.isalpha()]
        if word and all(character.isdigit() for character in word):
            scripts.append("number" if skip_latin else None)
        elif any(
            unicodedata.name(character, "").startswith("ARABIC ")
            for character in letters
        ):
            scripts.append("arabic")
        elif any(
            unicodedata.name(character, "").startswith("LATIN ")
            for character in letters
        ):
            scripts.append("latin")
        elif letters:
            scripts.append("other")
        else:
            scripts.append(None)

    for index, script in enumerate(scripts):
        if script is not None:
            continue
        previous = next(
            (value for value in reversed(scripts[:index]) if value is not None),
            None,
        )
        following = next(
            (value for value in scripts[index + 1:] if value is not None),
            None,
        )
        scripts[index] = previous if previous is not None else following

    runs: list[tuple[int, int, str]] = []
    run_start = script_spans[0][0]
    run_script = scripts[0] or "other"
    for index in range(1, len(script_spans)):
        script = scripts[index] or run_script
        if script != run_script:
            runs.append((run_start, script_spans[index][0], run_script))
            run_start = script_spans[index][0]
            run_script = script
    runs.append((run_start, len(text), run_script))

    segments: list[str] = []
    voices: list[str] = []
    segment_spans: list[list[tuple[int, int]]] = []
    if skip_latin:
        spoken_words = [
            span for span, script in zip(script_spans, scripts)
            if script != "latin"
        ]
        latin_words = [
            span for span, script in zip(script_spans, scripts)
            if script == "latin"
        ]
        parts: list[str] = []
        previous_end: int | None = None
        for start, end in spoken_words:
            if previous_end is not None:
                gap = text[previous_end:start]
                if any(
                    latin_start >= previous_end and latin_end <= start
                    for latin_start, latin_end in latin_words
                ):
                    gap_chars = list(gap)
                    for latin_start, latin_end in latin_words:
                        if latin_start >= previous_end and latin_end <= start:
                            for position in range(
                                latin_start - previous_end,
                                latin_end - previous_end,
                            ):
                                gap_chars[position] = ""
                    gap = "".join(gap_chars)
                    gap = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", gap)
                    if not gap.strip():
                        gap = " "
                parts.append(gap)
            parts.append(text[start:end])
            previous_end = end

        spoken_text = "".join(parts)
        if continuous:
            segments = _split_continuous_speech_text(spoken_text)
        else:
            segments = split_speech_text(
                spoken_text, first_chunk_length=first_chunk_length)
        segment_spans = _segment_word_spans(
            text, segments, source_words=spoken_words
        )
        run_voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        return (
            segments,
            [run_voice] * len(segments),
            segment_spans,
        )

    if continuous:
        # Only non-Arabic targets reach this point: Arabic with skipped or
        # included Latin returned above. Keep the whole read in one request
        # and normalize the full text exactly like the mixed-script path, so
        # run edges can never glue two words into one unmappable token.
        combined = _normalize_latin_speech_text(text)
        segments = _split_continuous_speech_text(combined)
        segment_spans = _segment_word_spans(text, segments)
        run_voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        return segments, [run_voice] * len(segments), segment_spans

    for start, end, script in runs:
        source_run_text = text[start:end]
        run_text = source_run_text
        if script == "latin":
            run_text = _normalize_latin_speech_text(run_text)
        run_segments = split_speech_text(
            run_text,
            first_chunk_length=(
                first_chunk_length if not segments else MAX_CHUNK_LENGTH
            ),
        )
        segments.extend(run_segments)
        segment_spans.extend([
            [(word_start + start, word_end + start)
             for word_start, word_end in spans]
            for spans in _segment_word_spans(
                source_run_text,
                run_segments,
                source_words=_speech_source_word_spans(source_run_text),
            )
        ])
        run_voice = (
            AUTOMATIC_SPEECH_VOICES.get(target_language, selected_voice)
            if automatic_voice else selected_voice
        )
        voices.extend([run_voice] * len(run_segments))
    return segments, voices, segment_spans


def _normalize_latin_speech_text(text: str) -> str:
    """Treat wrappers and separators inside Latin phrases as spacing, not pauses."""
    characters = []
    for index, character in enumerate(text):
        if character in _NON_PAUSING_SPEECH_WRAPPERS:
            characters.append(" ")
        elif character in ("'", "’") and not (
            index > 0
            and index + 1 < len(text)
            and text[index - 1].isalpha()
            and text[index + 1].isalpha()
        ):
            characters.append(" ")
        else:
            characters.append(character)
    normalized = "".join(characters)
    normalized = re.sub(
        r"(?<![A-Za-z])([A-Za-z])\.(?=\s+[A-Za-z])",
        r"\1",
        normalized,
    )
    return " ".join(normalized.split())


def _speech_source_word_spans(text: str) -> list[tuple[int, int]]:
    """Return spoken word ranges, treating notation punctuation as separators."""
    return _word_spans_with(text, underscore=False)


_MAX_BOUNDARY_WORDS = 32
"""How many source words a single provider boundary may be matched against.

Edge reports one boundary per spoken token, but tokens that carry no boundary
of their own -- digits, notation -- are folded into the neighbouring word. Real
synthesis of "l'heure du décès entre 2 h et 3 h du matin." returns
"entre 2 h et 3 h" as one boundary covering seven source words, so a small
window left it unmatched and the highlight froze there for the rest of the
read. The loop still stops as soon as the accumulated text stops being a
prefix of the boundary, so this bound is only a safety valve.
"""


def _match_boundary_span(
    boundary_text: str,
    source_text: str,
    spans: list[tuple[int, int]],
    next_word_index: int,
) -> tuple[int, int, int] | None:
    """Align a provider boundary with the next consecutive source words."""
    target = _normalize_boundary_token(boundary_text)
    if not target or next_word_index >= len(spans):
        return None
    start_index = next_word_index
    combined = ""
    for end_index in range(
            start_index, min(start_index + _MAX_BOUNDARY_WORDS, len(spans))):
        start, end = spans[end_index]
        source_token = _normalize_boundary_token(source_text[start:end])
        combined += source_token
        if combined == target:
            return spans[start_index][0], end, end_index + 1
        if len(combined) > len(target) or not target.startswith(combined):
            break
        if end_index == start_index and combined.startswith(target):
            matched_end = min(end, start + len(boundary_text))
            return start, matched_end, start_index + 1
    return None


def _normalize_boundary_token(token: str) -> str:
    token = token.translate(_ARABIC_DIGITS_TO_WESTERN).casefold()
    token = _normalize_arabic_pronunciation_token(token)
    # Keep combining marks: Devanagari matras distinguish कि from की, and Edge
    # reports Hindi words exactly as written. Arabic tashkeel was already
    # removed above because synthesis routinely drops it.
    token = "".join(
        character for character in token
        if character.isalnum()
        or unicodedata.category(character).startswith("M")
    )
    if token == "فروليك":
        return "فرولك"
    return token


def _match_synthesis_boundary_span(
    boundary_text: str,
    synthesis_text: str,
    source_text: str,
    source_spans: list[tuple[int, int]],
    next_word_index: int,
) -> tuple[int, int, int] | None:
    """Prefer alignment against the actual spoken words after pronunciation conversion."""
    target = _normalize_boundary_token(boundary_text)
    spoken_words = _speech_source_word_spans(synthesis_text)
    if target:
        search_end = min(next_word_index + 4, len(spoken_words))
        for start_index in range(next_word_index, search_end):
            combined = ""
            for end_index in range(
                start_index,
                min(start_index + _MAX_BOUNDARY_WORDS, len(spoken_words)),
            ):
                start, end = spoken_words[end_index]
                combined += _normalize_boundary_token(
                    synthesis_text[start:end])
                if combined == target:
                    if end_index >= len(source_spans):
                        return None
                    return (
                        source_spans[start_index][0],
                        source_spans[end_index][1],
                        end_index + 1,
                    )
                if len(combined) > len(target) or not target.startswith(combined):
                    break
    return _match_boundary_span(
        boundary_text, source_text, source_spans, next_word_index)
