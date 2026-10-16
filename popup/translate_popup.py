"""Frameless popup shown above selected text with its translation."""
from __future__ import annotations

import html
import math
import re
import logging
import sys
import time
import unicodedata
from collections.abc import Callable

# The separator the badge uses before its token tail, mirrored from
# translators.chain so the popup can split on it without pulling in
# the whole routing layer.
TOKEN_TAIL = "  ·  "
# Background speech prefetch: briefly debounce page-flips before synthesising
# the latest translation into the disk cache, and the longest text worth
# pre-synthesising (about ten minutes of audio; beyond that the read press
# streams at normal speed instead of holding a connection open in vain).
# The worker never streams, so no queue can block it.
SPEECH_PREFETCH_DELAY_MS = 500
SPEECH_PREFETCH_MAX_CHARS = 8000
SPEECH_AUTO_SCROLL_RESUME_MS = 1400
from PySide6.QtCore import QEvent, QPoint, QRect, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QTextBlockFormat, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat, QTextOption,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
    QMessageBox, QPushButton, QStyle, QStyleOptionSlider, QSizePolicy,
    QSlider, QTextEdit, QToolButton, QTextBrowser, QSplitter, QVBoxLayout,
    QWidget, QWidgetAction,
)

from .audio_stream import AudioStreamServer
from .speech_trace import SpeechTrace
from .worker import TranslateWorker
from .chat_worker import ChatWorker
from translators.prompt import LANGUAGE_CHOICES
from .speech_worker import (
    ARABIC_VOICE, DEFAULT_SPEECH_RATE, GEMINI_TTS_ENGINE,
    MAX_SPEECH_RATE, MIN_SPEECH_RATE, SpeechWorker, VoiceListWorker,
    _cache_key, _speech_text_for_synthesis, normalize_speed,
    python_to_qt_position, qt_to_python_position,
    snap_speed_to_quarter, speech_available, speech_missing_message,
    speech_start_offset,
    speech_word_span,
)
from config.settings import (
    DEFAULT_LINE_SPACING, DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
    DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
    MAX_LINE_SPACING, MAX_TRANSLATION_SIDE_MARGIN_PX,
    MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX, MIN_LINE_SPACING,
    MAX_TRANSLATION_FONT_SIZE, MIN_TRANSLATION_FONT_SIZE,
    MIN_TRANSLATION_SIDE_MARGIN_PX,
    MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX, load, save,
)

_POPUP_COLORS = {
    "window": "#f7f8fa",
    "panel": "#f7f8fa",
    "surface": "#f1f3f6",
    "surface_hover": "#e9edf2",
    "text": "#222831",
    "muted": "#52677f",
    "border": "#d8dde4",
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "accent_light": "#60a5fa",
    "selection": "#fff2a8",
    "warning": "#b45309",
}
_MARKDOWN_HTML_TAGS = frozenset({
    "a", "address", "article", "aside", "b", "blockquote", "br", "caption",
    "center", "cite", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins",
    "kbd", "li", "ol", "p", "pre", "s", "samp", "section", "small", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "u", "ul", "var",
})
_TABLE_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
_MUSICAL_ACCIDENTAL_AFTER_NOTE = re.compile(
    r"(?<![A-Za-z])([A-Ga-g])(?=[♭♯#♮])")


class TranslationTextEdit(QTextEdit):
    positionClicked = Signal(int)
    zoomRequested = Signal(float)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.textCursor().hasSelection()
        ):
            self.positionClicked.emit(
                self.cursorForPosition(event.position().toPoint()).position()
            )

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            pixel_delta = event.pixelDelta().y()
            steps = (
                pixel_delta / 40.0
                if pixel_delta
                else event.angleDelta().y() / 120.0
            )
            if steps:
                self.zoomRequested.emit(
                    math.pow(1.1, max(-20.0, min(20.0, steps))))
                event.accept()
                return
        super().wheelEvent(event)


class ChatInputEdit(QPlainTextEdit):
    sendRequested = Signal()

    def keyPressEvent(self, event) -> None:
        if (
            event.key() == Qt.Key.Key_Return
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)


class ChatTranscriptBrowser(QTextBrowser):
    paragraphClicked = Signal(int)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        cursor = self.cursorForPosition(event.position().toPoint())
        self.paragraphClicked.emit(cursor.position())


class TranslatePopup(QDialog):
    """Sober floating popup: native frame = movable + resizable + close button.

    Fixed opening geometry (user spec, always): (514, 605, 1126, 326).
    """

    FIXED_OUTER = (514, 605, 1126, 326)  # screen (frame-inclusive) geometry

    # Fires once a translation lands, so the main window can keep the model
    # and its token cost in view while the popup is dismissed or moved.
    usageReported = Signal(str, str)
    pageNavigationRequested = Signal(int)
    automaticPageReadingToggled = Signal(bool)
    translationReady = Signal()
    translationFailed = Signal(str)
    speechFinished = Signal(str)

    def __init__(self, settings: dict, source_text: str | None = None,
                 pil_image=None, ocr_langs=("ru", "en"), parent=None,
                 floating_language_controls: bool = False,
                 float_language_preferences: bool = False,
                 language_change_callback: (
                     Callable[[str, str, TranslatePopup], None] | None
                 ) = None,
                 full_page_translation: bool = False):
        super().__init__(parent, Qt.WindowType.Tool)
        self._settings = settings
        self.settings = settings
        self._worker: TranslateWorker | None = None
        self._translation_workers: list[TranslateWorker] = []
        self._chat_workers: list[ChatWorker] = []
        self._chat_worker: ChatWorker | None = None
        self._chat_messages: list[dict[str, str]] = []
        self._chat_entries: list[dict[str, str]] = []
        self._translation_generation = 0
        self._streamed_partial = ""
        self._translation_text = ""
        self._translation_needs_render = False
        self._rendered_translation_text: str | None = None
        self._full_page_translation = full_page_translation
        self._source_text = source_text
        self._source_image = pil_image
        self._speech_worker: SpeechWorker | None = None
        self._speech_stream_server: AudioStreamServer | None = None
        self._voice_list_worker: VoiceListWorker | None = None
        self._speech_paths: dict[int, str] = {}
        self._stale_speech_paths: list[str] = []
        self._speech_stream_urls: dict[int, str] = {}
        self._speech_playing_stream_index: int | None = None
        self._speech_boundaries: dict[int, list[tuple[int, int, int, int]]] = {}
        self._speech_index = 0
        self._pending_player_position_ms: int | None = None
        self._pending_player_source: QUrl | None = None
        self._speech_prefetch_worker: SpeechWorker | None = None
        self._speech_prefetch_workers: list[SpeechWorker] = []
        self._speech_prefetch_key: tuple | None = None
        self._speech_active = False
        self._close_pending = False
        self._speech_waiting = False
        self._speech_error: str | None = None
        self._speech_trace: SpeechTrace | None = None
        self._speech_trace_sample_ns = 0
        self._speech_text_override: str | None = None
        self._speech_start_offset = 0
        self._pending_speech_offset: int | None = None
        self._pending_highlight: tuple[int, int] | None = None
        self._speech_seek_path: str | None = None
        self._chat_speech_document_start: int | None = None
        self._pinned = False
        self._floating_language_controls = floating_language_controls
        self._float_language_preferences = float_language_preferences
        self._language_change_callback = language_change_callback
        self._language_actions: dict[str, dict[str, object]] = {
            "source": {},
            "target": {},
        }
        self._vertical_restore_geometry: QRect | None = None
        self._saved_size = self._read_saved_size(settings.get("popup_size"))
        self._saved_position = self._read_saved_position(settings.get("popup_pos"))
        font_size = self._translation_font_size(settings)

        self.setWindowTitle("Translation")
        self.setMinimumSize(260, 150)
        x, y, width, height = TranslatePopup.FIXED_OUTER
        self.setGeometry(x, y, *(self._saved_size or (width, height)))
        self._fixed_applied = False
        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(400)
        self._size_save_timer.timeout.connect(self._save_geometry)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(4)

        header = QWidget(self)
        header.setFixedHeight(28)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(5)
        header_layout.setDirection(QHBoxLayout.Direction.LeftToRight)
        self.badge = QLabel("")
        self.badge.setObjectName("badge")
        self.badge.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.badge.hide()
        header_layout.addWidget(self.badge, 1)
        self.spin = QLabel("Starting…")
        self.spin.setObjectName("spin")
        header_layout.addWidget(self.spin, 1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedHeight(26)
        self.copy_btn.setToolTip("Copy the translation")
        self.copy_btn.clicked.connect(self._copy)
        self.speak_btn = QPushButton("Read aloud")
        self.speak_btn.setFixedHeight(26)
        self._speech_engine = self.settings.get("speech_engine", "edge")
        gemini_api_key = self.settings.get("gemini_key", "")
        self._speech_available = speech_available(
            self._speech_engine, gemini_api_key)
        speech_engine_name = (
            "Gemini TTS"
            if self._speech_engine == GEMINI_TTS_ENGINE else "Edge TTS"
        )
        self.speak_btn.setToolTip(
            f"Read the translation aloud with {speech_engine_name}"
            if self._speech_available
            else speech_missing_message(self._speech_engine))
        self.speak_btn.setEnabled(False)
        self.speak_btn.clicked.connect(self._toggle_speech)

        # Speed picker: a narrow icon button so the popup keeps its exact size.
        self._speech_rate = normalize_speed(self.settings.get("speech_rate"))
        configured_voice = self.settings.get("speech_voice")
        self._speech_voice = (
            configured_voice if isinstance(configured_voice, str) and configured_voice
            else ARABIC_VOICE
        )
        self._voice_actions = []
        self.speed_menu = QMenu(self)
        self._build_speed_menu()
        self.speed_btn = QToolButton(self)
        self.speed_btn.setText("»")
        self.speed_btn.setToolTip(f"Reading speed: {self._speech_rate:.2f}x")
        self.speed_btn.clicked.connect(self._show_speed_menu)
        self.speed_btn.setFixedWidth(24)
        self.speed_btn.setFixedHeight(26)
        self.speed_btn.setEnabled(False)
        self._style_speed_button()
        self.voice_menu = QMenu(self)
        self.voice_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._voice_placeholder = self.voice_menu.addAction("Open to load voices…")
        self._voice_placeholder.setEnabled(False)
        self.voice_menu.aboutToShow.connect(self._load_voices)
        self.voice_btn = QToolButton(self)
        self.voice_btn.setText("♫")
        self.voice_btn.setToolTip(f"Voice: {self._speech_voice}")
        self.voice_btn.clicked.connect(self._show_voice_menu)
        self.voice_btn.setFixedWidth(24)
        self.voice_btn.setFixedHeight(26)
        self.voice_btn.setEnabled(
            self._speech_available and self._speech_engine == "edge")
        if self._speech_engine == GEMINI_TTS_ENGINE:
            self.voice_btn.setToolTip(
                f"Gemini voice: {self.settings.get('gemini_tts_voice', 'Kore')}")
        self.pin_btn = QPushButton("Pin")
        self.pin_btn.setFixedHeight(26)
        self.pin_btn.setToolTip(
            "Keep this translation open and above other windows")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(False)
        self.pin_btn.clicked.connect(self._toggle_pin)
        self.explain_btn = QPushButton("Explain")
        self.explain_btn.setObjectName("explainModeButton")
        self.explain_btn.setFixedSize(84, 26)
        self.explain_btn.setToolTip(
            "Add a concise explanation below the translation")
        self.explain_btn.setCheckable(True)
        self.explain_btn.toggled.connect(self._set_explain_mode)
        self._sync_explain_button()
        self.explain_btn.hide()
        self.previous_page_btn = QPushButton("◀")
        self.previous_page_btn.setFixedSize(30, 26)
        self.previous_page_btn.setStyleSheet("font-size: 16px; padding: 0;")
        self.previous_page_btn.setToolTip("Translate the previous page")
        self.previous_page_btn.clicked.connect(
            lambda: self.pageNavigationRequested.emit(-1))
        self.next_page_btn = QPushButton("▶")
        self.next_page_btn.setFixedSize(30, 26)
        self.next_page_btn.setStyleSheet("font-size: 16px; padding: 0;")
        self.next_page_btn.setToolTip("Translate the next page")
        self.next_page_btn.clicked.connect(
            lambda: self.pageNavigationRequested.emit(1))
        self.previous_page_btn.hide()
        self.next_page_btn.hide()
        self.page_number_label = QPushButton(self)
        self.page_number_label.setObjectName("translationPageNumber")
        self.page_number_label.setCheckable(True)
        self.page_number_label.setFixedHeight(26)
        self.page_number_label.setStyleSheet(
            "QPushButton#translationPageNumber { color: #52677f; "
            "font-size: 12px; font-weight: 600; padding: 0 5px; }"
            "QPushButton#translationPageNumber:checked { "
            "background: #fff2a8; border-color: #d2a900; color: #5b4700; }")
        self.page_number_label.setToolTip(
            "Click to translate the current page and read pages in order")
        self.page_number_label.toggled.connect(
            self._on_automatic_page_reading_toggled)
        self.page_number_label.hide()
        self.chat_btn = QPushButton("Chat")
        self.chat_btn.setFixedHeight(26)
        self.chat_btn.setToolTip(
            "Ask follow-up questions about this passage")
        self.chat_btn.setEnabled(False)
        self.chat_btn.setMinimumWidth(92)
        self.chat_btn.clicked.connect(self._on_chat_button_clicked)
        self.chat_btn.hide()
        self.source_language_btn = self._create_language_button(
            "source", "Translate from", settings.get("source_language", "auto"))
        self.target_language_btn = self._create_language_button(
            "target", "Translate to", settings.get("target_language", "ar"))
        self.language_action_group = QWidget(header)
        self.language_action_layout = QHBoxLayout(self.language_action_group)
        self.language_action_layout.setContentsMargins(0, 0, 0, 0)
        self.language_action_layout.setSpacing(5)
        self.language_action_group.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft)
        if self._floating_language_controls:
            self.language_action_layout.addWidget(self.target_language_btn)
            self.language_action_layout.addWidget(self.source_language_btn)
        header_layout.addWidget(self.language_action_group)
        header_layout.addWidget(self.copy_btn)
        header_layout.addWidget(self.speak_btn)
        header_layout.addWidget(self.speed_btn)
        header_layout.addWidget(self.voice_btn)
        header_layout.addWidget(self.pin_btn)
        header_layout.addWidget(self.previous_page_btn)
        header_layout.addWidget(self.page_number_label)
        header_layout.addWidget(self.next_page_btn)
        layout.addWidget(header)

        self.navigation_page: int | None = None
        self._navigation_page_total = 0

        self.translation_area = QWidget(self)
        translation_layout = QVBoxLayout(self.translation_area)
        translation_layout.setContentsMargins(0, 0, 0, 0)
        translation_layout.setSpacing(4)

        self.result = TranslationTextEdit()
        self.result.setObjectName("translationResult")
        self.result.setReadOnly(True)
        self._apply_translation_direction()
        result_font = self._translation_font(settings, font_size)
        self.result.setFont(result_font)
        self.result.document().setDefaultFont(result_font)
        self.result.setFrameStyle(0)
        self.result.viewport().setAutoFillBackground(False)
        self.result.positionClicked.connect(self._on_translation_position_clicked)
        self.result.zoomRequested.connect(self._apply_translation_zoom)
        translation_layout.addWidget(self.result, 1)

        self.chat_panel = QWidget(self)
        chat_layout = QVBoxLayout(self.chat_panel)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(4)
        self.chat_transcript = ChatTranscriptBrowser(self.chat_panel)
        self.chat_transcript.setObjectName("chatTranscript")
        self.chat_transcript.setOpenExternalLinks(False)
        self.chat_transcript.installEventFilter(self)
        self.chat_transcript.viewport().installEventFilter(self)
        self.chat_transcript.verticalScrollBar().installEventFilter(self)
        self.chat_transcript.paragraphClicked.connect(
            self._on_chat_transcript_clicked)
        self.chat_transcript.setMinimumHeight(90)
        self.chat_transcript.setMaximumHeight(190)
        for widget in (
            self.result,
            self.result.viewport(),
            self.result.verticalScrollBar(),
            self.chat_transcript,
            self.chat_transcript.viewport(),
            self.chat_transcript.verticalScrollBar(),
        ):
            widget.installEventFilter(self)
        self.chat_input = ChatInputEdit(self.chat_panel)
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setPlaceholderText(
            "Ask a question, then press Send or Ctrl+Enter…")
        self.chat_input.setFixedHeight(58)
        self.chat_input.sendRequested.connect(self._send_chat)
        chat_layout.addWidget(self.chat_transcript)
        self.chat_composer = QHBoxLayout()
        self.chat_composer.setContentsMargins(0, 0, 0, 0)
        self.chat_composer.setSpacing(5)
        self.chat_send_btn = QPushButton("Send", self.chat_panel)
        self.chat_send_btn.setObjectName("chatSendButton")
        self.chat_send_btn.setFixedSize(62, 58)
        self.chat_send_btn.setToolTip("Send message (Ctrl+Enter)")
        self.chat_send_btn.clicked.connect(self._send_chat)
        self.chat_composer.addWidget(self.chat_input, 1)
        self.chat_composer.addWidget(self.chat_send_btn)
        chat_layout.addLayout(self.chat_composer)
        self.chat_panel.hide()

        self.content_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.content_splitter.setObjectName("translationChatSplitter")
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(6)
        self.content_splitter.addWidget(self.translation_area)
        self.content_splitter.addWidget(self.chat_panel)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.content_splitter.setSizes([220, 150])
        layout.addWidget(self.content_splitter, 1)

        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setPlaybackRate(self._speech_rate)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.positionChanged.connect(self._update_speech_highlight)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.errorOccurred.connect(self._on_playback_error)
        self._speech_highlight_timer = QTimer(self)
        self._speech_highlight_timer.setInterval(30)
        self._speech_highlight_timer.timeout.connect(self._update_speech_highlight)
        self._speech_auto_scroll_suspended = False
        self._speech_follow_scroll_active = False
        self._speech_auto_scroll_timer = QTimer(self)
        self._speech_auto_scroll_timer.setSingleShot(True)
        self._speech_auto_scroll_timer.setInterval(
            SPEECH_AUTO_SCROLL_RESUME_MS)
        self._speech_auto_scroll_timer.timeout.connect(
            self._resume_speech_auto_scroll)
        # Debounce for background speech prefetch: translation-ready starts
        # the countdown; a newer translation or an explicit Read restarts or
        # cancels it, so at most one prefetch synthesis is ever in flight.
        self._speech_prefetch_timer = QTimer(self)
        self._speech_prefetch_timer.setSingleShot(True)
        self._speech_prefetch_timer.setInterval(SPEECH_PREFETCH_DELAY_MS)
        self._speech_prefetch_timer.timeout.connect(self._start_speech_prefetch)

        self._apply_popup_style()
        self._start_translation_worker(
            text=source_text if pil_image is None else None,
            pil_image=pil_image,
            ocr_langs=ocr_langs,
        )

    def set_page_navigation(self, page: int, total: int) -> None:
        if total <= 0 or not 0 <= page < total:
            self.navigation_page = None
            self._navigation_page_total = 0
            self.previous_page_btn.hide()
            self.next_page_btn.hide()
            self.page_number_label.hide()
            if self.page_number_label.isChecked():
                self.page_number_label.setChecked(False)
            return
        self.navigation_page = page
        self._navigation_page_total = total
        self.page_number_label.setText(f"Page {page + 1} / {total}")
        self.page_number_label.setToolTip(
            "Automatic translation and read-aloud is on; click to stop"
            if self.page_number_label.isChecked()
            else f"Page {page + 1} of {total} — click to translate and read "
                 "successive pages")
        self.page_number_label.show()
        self.previous_page_btn.setEnabled(page > 0)
        self.next_page_btn.setEnabled(page < total - 1)
        self.previous_page_btn.show()
        self.next_page_btn.show()

    def _on_automatic_page_reading_toggled(self, enabled: bool) -> None:
        if self.navigation_page is not None:
            self.previous_page_btn.setEnabled(self.navigation_page > 0)
            self.next_page_btn.setEnabled(
                self.navigation_page < self._navigation_page_total - 1
            )
        self.page_number_label.setToolTip(
            "Automatic translation and read-aloud is on; click to stop"
            if enabled else
            f"Page {self.navigation_page + 1} of "
            f"{self._navigation_page_total} — click to translate and read "
            "successive pages"
            if self.navigation_page is not None else
            "Click to translate the current page and read pages in order"
        )
        self.automaticPageReadingToggled.emit(enabled)

    def set_automatic_page_reading(self, enabled: bool) -> None:
        previous = self.page_number_label.blockSignals(True)
        self.page_number_label.setChecked(enabled)
        self.page_number_label.blockSignals(previous)

    def set_source_content(
        self,
        text: str | None = None,
        pil_image=None,
        ocr_langs=("ru", "en"),
    ) -> None:
        self._source_text = text
        self._source_image = pil_image
        self._start_translation_worker(
            text=text if pil_image is None else None,
            pil_image=pil_image,
            ocr_langs=ocr_langs,
        )

    def _create_language_button(
        self, kind: str, title: str, selected_code: str,
    ) -> QPushButton:
        button = QPushButton(self)
        button.setToolTip(title)
        button.setMinimumWidth(120)
        button.setFixedHeight(26)
        menu = QMenu(button)
        choices = (("Auto", "auto"), *LANGUAGE_CHOICES) if kind == "source" else LANGUAGE_CHOICES
        for label, code in choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(code == selected_code)
            self._language_actions[kind][code] = action
            action.triggered.connect(
                lambda _checked=False, language=code, language_kind=kind:
                    self._set_translation_language(language_kind, language)
            )
        button.setText(self._language_button_text(kind, selected_code))
        button.setAccessibleName(button.text())
        button.setMenu(menu)
        button.setVisible(self._floating_language_controls)
        return button

    @staticmethod
    def _language_button_text(kind: str, code: str) -> str:
        labels = {language_code: label for label, language_code in LANGUAGE_CHOICES}
        labels["auto"] = "Auto"
        language = labels.get(code, code.upper())
        direction = "From" if kind == "source" else "To"
        return f"{direction}: {language}"

    def _set_translation_language(self, kind: str, code: str) -> None:
        key = "source_language" if kind == "source" else "target_language"
        self.settings[key] = code
        if self._float_language_preferences:
            float_key = (
                "float_source_language" if kind == "source"
                else "float_target_language"
            )
            self.settings[float_key] = code
        self._update_language_control(kind, code)
        if self._language_change_callback is not None:
            self._language_change_callback(
                self.settings.get("source_language", "auto"),
                self.settings.get("target_language", "ar"),
                self,
            )
        self._retranslate_for_current_languages()

    def _update_language_control(self, kind: str, code: str) -> None:
        for language, action in self._language_actions[kind].items():
            action.setChecked(language == code)
        button = (
            self.source_language_btn if kind == "source"
            else self.target_language_btn
        )
        button.setText(self._language_button_text(kind, code))
        button.setAccessibleName(button.text())

    def sync_languages(self, source_language: str, target_language: str) -> None:
        """Apply toolbar languages without calling back to the application."""
        self.settings["source_language"] = source_language
        self.settings["target_language"] = target_language
        self._update_language_control("source", source_language)
        self._update_language_control("target", target_language)
        self._retranslate_for_current_languages()

    def _retranslate_for_current_languages(self) -> None:
        if self._source_text:
            self._start_translation_worker(text=self._source_text)
        elif (self._source_image is not None
              and self.settings.get("selection_mode") == "image"):
            self._start_translation_worker(pil_image=self._source_image)

    def _sync_explain_button(self) -> None:
        enabled = bool(self.settings.get("explain_mode", False))
        self.explain_btn.blockSignals(True)
        self.explain_btn.setChecked(enabled)
        self.explain_btn.blockSignals(False)
        colors = _POPUP_COLORS
        self.explain_btn.setStyleSheet(
            "QPushButton#explainModeButton { "
            f"background: {colors['surface']}; "
            f"border: 1px solid {colors['border']}; "
            "border-radius: 4px; padding: 0 7px; font-size: 12px; "
            f"color: {colors['text']}; }}"
            "QPushButton#explainModeButton:hover, "
            "QPushButton#explainModeButton:checked { "
            f"background-color: {colors['accent']}; "
            f"color: {colors['text']}; }}"
            "QPushButton#explainModeButton:checked:hover { "
            f"background-color: {colors['accent_hover']}; "
            f"color: {colors['text']}; }}"
        )
        self.explain_btn.setToolTip(
            "Explain mode is on: add an explanation below translations"
            if enabled else
            "Explain mode is off: show translations without an explanation"
        )

    def _save_shared_settings(
        self, *, update_local: bool = True, **updates: object,
    ) -> bool:
        try:
            persisted = load()
            persisted.update(updates)
            save(persisted)
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save translation popup settings")
            QMessageBox.warning(
                self,
                "Could not save settings",
                "The setting could not be saved. Check that the settings "
                "file is writable, then try again.",
            )
            return False
        if update_local:
            self.settings.update(updates)
        return True

    def _set_explain_mode(self, enabled: bool) -> None:
        if not self._save_shared_settings(explain_mode=bool(enabled)):
            self._sync_explain_button()
            return
        self._sync_explain_button()
        self._retranslate_for_current_languages()

    def _start_translation_worker(
        self,
        text: str | None = None,
        pil_image=None,
        ocr_langs=("ru", "en"),
    ) -> None:
        self._reset_speech_for_new_translation()
        self._translation_generation += 1
        self._chat_messages.clear()
        self._chat_entries.clear()
        self.chat_transcript.clear()
        self.chat_btn.setEnabled(False)
        self.chat_send_btn.setEnabled(False)
        self._toggle_chat_panel(False)
        generation = self._translation_generation
        self._streamed_partial = ""
        worker_settings = dict(self.settings)
        if self._full_page_translation:
            worker_settings["_full_page_translation"] = True
        worker = TranslateWorker(
            worker_settings,
            text=text,
            pil_image=pil_image,
            ocr_langs=ocr_langs,
        )
        self._worker = worker
        worker._popup_generation = generation
        self._translation_workers.append(worker)
        worker.stage.connect(self._on_worker_stage)
        worker.source_ready.connect(self._on_worker_source_ready)
        worker.partial.connect(self._on_worker_partial)
        worker.done.connect(self._on_worker_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_translation_worker_finished)
        self.spin.show()
        self.spin.setText("Starting…")
        self.result.clear()
        self._apply_streamed_typography()
        self.speak_btn.setEnabled(False)
        worker.start()

    def _reset_speech_for_new_translation(self) -> None:
        self._speech_active = False
        self._speech_waiting = False
        self._speech_start_offset = 0
        self._pending_speech_offset = None
        self._pending_highlight = None
        self._speech_prefetch_timer.stop()
        self._cancel_speech_prefetch()
        self._speech_index = 0
        self._speech_error = None
        self._speech_text_override = None
        self._chat_speech_document_start = None
        self._speech_boundaries.clear()
        self._speech_stream_urls.clear()
        self._speech_playing_stream_index = None
        self._pending_player_position_ms = None
        self._pending_player_source = None
        self._speech_highlight_timer.stop()
        self._clear_speech_highlight()
        self._player.stop()
        self._player.setSource(QUrl())
        # The loopback audio server is intentionally kept alive across
        # translations: tearing it down (shutdown + thread join) and binding
        # a fresh port on every translation blocked the UI thread right when
        # the user expects audio. Stale tokens die with their player
        # connection; each read mints a fresh token on the shared server, so
        # gapless single-stream playback is unchanged.

        worker = self._speech_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
        elif worker is not None:
            worker.cleanup_output()
        self._clear_speech_files()
        self.speak_btn.setText(self._speech_idle_label())
        self.speak_btn.setEnabled(False)

    def _on_translation_stage(self, generation: int, stage: str) -> None:
        if generation == self._translation_generation:
            self.spin.setText(stage)

    def _on_worker_stage(self, stage: str) -> None:
        worker = self.sender()
        if worker is not None:
            self._on_translation_stage(
                getattr(worker, "_popup_generation", -1), stage)

    def _on_worker_source_ready(self, source: str) -> None:
        worker = self.sender()
        if worker is not None:
            self._on_source_ready(worker, source)

    def _on_worker_partial(self, partial_text: str) -> None:
        """Append streamed text cheaply; apply Markdown once the reply is done."""
        worker = self.sender()
        if worker is None or getattr(
                worker, "_popup_generation", -1) != self._translation_generation:
            return
        self.spin.setText("Translating…")
        self._append_streamed_partial(partial_text)
        self.result.show()

    def _append_streamed_partial(self, text: str) -> None:
        previous = self._streamed_partial
        if text == previous:
            return
        if text.startswith(previous):
            cursor = self.result.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            start_position = cursor.position()
            previous_block_count = self.result.document().blockCount()
            cursor.insertText(text[len(previous):])
            self.result.setTextCursor(cursor)
            if not previous:
                self._apply_streamed_typography()
            elif self.result.document().blockCount() > previous_block_count:
                self._apply_streamed_typography(start_position)
            else:
                self._apply_streamed_font_size(start_position)
                self._apply_script_font_families(
                    self.result.document(), start_position)
        else:
            self.result.setPlainText(text)
            cursor = self.result.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.result.setTextCursor(cursor)
            self._apply_streamed_typography()
        self._streamed_partial = text

    def _apply_streamed_font_size(
        self, start_position: int | None = None,
    ) -> None:
        document = self.result.document()
        point_size = self._translation_font_size(self.settings)
        font = self._translation_font(self.settings, point_size)
        document.setDefaultFont(font)
        end_position = document.characterCount() - 1
        start = 0 if start_position is None else start_position
        if point_size <= 0 or end_position <= start:
            return
        cursor = QTextCursor(document)
        cursor.setPosition(start)
        cursor.setPosition(
            end_position, QTextCursor.MoveMode.KeepAnchor)
        char_format = QTextCharFormat()
        char_format.setFontPointSize(point_size)
        cursor.mergeCharFormat(char_format)

    def _apply_streamed_typography(
        self, start_position: int | None = None,
    ) -> None:
        target_language = str(
            self.settings.get("target_language", "ar")).split("-", 1)[0]
        direction = (
            Qt.LayoutDirection.RightToLeft
            if target_language == "ar"
            else Qt.LayoutDirection.LeftToRight
        )
        start_alignment = (
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute
            if target_language == "ar"
            else Qt.AlignmentFlag.AlignLeft
        )
        self.result.setLayoutDirection(direction)
        document = self.result.document()
        self._apply_streamed_font_size(start_position)
        option = document.defaultTextOption()
        option.setTextDirection(direction)
        option.setAlignment(start_alignment)
        document.setDefaultTextOption(option)
        spacing = self._line_spacing(self.settings)
        root_format = document.rootFrame().frameFormat()
        vertical_margin = self._translation_top_bottom_margin(self.settings)
        root_format.setTopMargin(vertical_margin)
        root_format.setBottomMargin(vertical_margin)
        document.rootFrame().setFrameFormat(root_format)
        block = (
            document.begin()
            if start_position is None
            else document.findBlock(start_position)
        )
        first_block_position = block.position()
        while block.isValid():
            if start_position is None or block.position() != first_block_position:
                block_format = block.blockFormat()
                block_format.setLayoutDirection(direction)
                block_format.setAlignment(start_alignment)
                block_format.setTopMargin(0)
                block_format.setBottomMargin(0)
                if spacing and block.text().strip():
                    block_format.setLineHeight(
                        float(spacing),
                        int(QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value),
                    )
                else:
                    block_format.clearProperty(QTextFormat.Property.LineHeight)
                    block_format.clearProperty(
                        QTextFormat.Property.LineHeightType)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(block_format)
            block = block.next()
        self._apply_script_font_families(
            document, start_position if start_position is not None else 0)

    def _on_worker_done(self, translated_text: str, badge: str) -> None:
        worker = self.sender()
        if worker is not None:
            self._on_translation_done(
                getattr(worker, "_popup_generation", -1),
                worker,
                translated_text,
                badge,
            )

    def _on_worker_failed(self, error: str) -> None:
        worker = self.sender()
        if worker is not None:
            self._on_translation_failed(
                getattr(worker, "_popup_generation", -1), error)

    def _on_translation_worker_finished(self) -> None:
        worker = self.sender()
        if worker in self._translation_workers:
            self._translation_workers.remove(worker)
        self._continue_pending_close()

    def _on_source_ready(self, worker: TranslateWorker, source: str) -> None:
        self._source_text = source
        if (
            self.settings.get("source_language", "auto")
            != worker._settings.get("source_language", "auto")
            or self.settings.get("target_language", "ar")
            != worker._settings.get("target_language", "ar")
        ):
            self._start_translation_worker(text=source)

    def _on_translation_done(
        self,
        generation: int,
        worker: TranslateWorker,
        translated_text: str,
        badge: str,
    ) -> None:
        if generation == self._translation_generation:
            self._streamed_partial = ""
            self._worker = worker
            self._on_done(translated_text, badge)
            self.translationReady.emit()

    def _on_translation_failed(self, generation: int, error: str) -> None:
        if generation == self._translation_generation:
            self._streamed_partial = ""
            self._on_failed(error)
            self.translationFailed.emit(error)

    def refresh_display(self, settings: dict | None = None) -> None:
        """Reapply saved typography after refreshing settings in-place."""
        if (
            settings is not None
            and self._rendered_translation_text == self._translation_text
            and self._rendered_translation_text is not None
            and not self._translation_needs_render
            and not any(
                settings.get(key) != self.settings.get(key)
                for key in (
                    "arabic_tashkeel",
                    "explain_instructions",
                    "explain_mode",
                    "font_size",
                    "gemini_key",
                    "gemini_tts_voice",
                    "line_spacing_px",
                    "source_language",
                    "speech_engine",
                    "speech_rate",
                    "speech_read_latin_words",
                    "speech_voice",
                    "target_language",
                    "translation_font",
                    "translation_font_arabic",
                    "translation_font_latin",
                    "translation_side_margin_px",
                    "translation_top_bottom_margin_px",
                    "automatic_speech_voice",
                )
            )
        ):
            self.settings = settings
            return

        old_explain_mode = bool(self.settings.get("explain_mode", False))
        old_explain_instructions = self.settings.get("explain_instructions", "")
        old_arabic_tashkeel = bool(
            self.settings.get("arabic_tashkeel", False))
        old_engine = self._speech_engine
        old_edge_voice = self._speech_voice
        old_gemini_voice = self.settings.get("gemini_tts_voice", "Kore")
        if settings is not None:
            new_engine = settings.get("speech_engine", "edge")
            new_edge_voice = settings.get("speech_voice", ARABIC_VOICE)
            new_gemini_voice = settings.get("gemini_tts_voice", "Kore")
            speech_configuration_changed = (
                new_engine != old_engine
                or (
                    new_engine == "edge"
                    and (
                        new_edge_voice != old_edge_voice
                        or settings.get("automatic_speech_voice", False)
                        != self.settings.get(
                            "automatic_speech_voice", False)
                        or settings.get("speech_read_latin_words", False)
                        != self.settings.get(
                            "speech_read_latin_words", False)
                    )
                )
                or (
                    new_engine == GEMINI_TTS_ENGINE
                    and (
                        new_gemini_voice != old_gemini_voice
                        or settings.get("gemini_key", "")
                        != self.settings.get("gemini_key", "")
                    )
                )
            )
            if self._speech_active and speech_configuration_changed:
                self._toggle_speech()
            self.settings = settings
        self._refresh_speech_controls()
        explain_mode = bool(self.settings.get("explain_mode", False))
        explain_instructions = self.settings.get("explain_instructions", "")
        self._sync_explain_button()
        self.chat_btn.setEnabled(
            bool(getattr(self, "_translation_text", "").strip())
            and self._chat_worker is None
        )
        self.chat_send_btn.setEnabled(
            bool(getattr(self, "_translation_text", "").strip())
            and self._chat_worker is None
        )
        self._apply_chat_direction()
        if (
            settings is not None
            and (old_explain_mode != explain_mode
                 or old_explain_instructions != explain_instructions
                 or old_arabic_tashkeel != bool(
                     self.settings.get("arabic_tashkeel", False)))
        ):
            self._retranslate_for_current_languages()
        font_size = self._translation_font_size(self.settings)
        font = self._translation_font(self.settings, font_size)
        translation_font = QFont(font)
        self.result.setFont(translation_font)
        self.result.document().setDefaultFont(translation_font)
        font_family_css = translation_font.family().replace(
            "\\", "\\\\").replace('"', '\\"')
        side_margin = self._translation_side_margin(self.settings)
        chat_font = QFont(font)
        chat_font_size = max(16, round(font_size))
        chat_font.setPointSize(chat_font_size)
        self.chat_transcript.setFont(chat_font)
        self.chat_input.setFont(font)
        self.chat_transcript.setStyleSheet(
            "QTextBrowser#chatTranscript { "
            "background: #f1f3f6; color: #222831; "
            "border: 1px solid #d8dde4; border-radius: 4px; padding: 5px; "
            f'font-family: "{font_family_css}"; '
            f"font-size: {chat_font_size}pt; }}"
        )
        self.chat_input.setStyleSheet(
            "QPlainTextEdit#chatInput { "
            "background: #f1f3f6; color: #222831; "
            "border: 1px solid #d8dde4; border-radius: 4px; padding: 5px; "
            f'font-family: "{font_family_css}"; font-size: {font_size}pt; }}'
        )
        self.result.setStyleSheet(
            "QTextEdit#translationResult { background: #f1f3f6; color: #222831;"
            " border: none; border-radius: 4px; padding: 0;"
            f" padding-left: {side_margin}px; padding-right: {side_margin}px;"
            f' font-family: "{font_family_css}"; '
            f"font-size: {translation_font.pointSizeF():g}pt; }}"
        )
        if getattr(self, "_translation_text", "") and not self.result.isHidden():
            self._render_translation()
        elif self._streamed_partial and not self.result.isHidden():
            self._apply_streamed_typography()
        if self._chat_entries:
            self._render_chat_transcript()
        self._apply_popup_style()

    def _apply_popup_style(self) -> None:
        colors = _POPUP_COLORS
        border = colors["border"]
        text = colors["text"]
        muted = colors["muted"]
        translation_font_size = self._translation_font_size(self.settings)
        translation_font = self._translation_font(
            self.settings, translation_font_size)
        transcript_font = self.chat_transcript.font()
        input_font = self.chat_input.font()
        result_family = translation_font.family().replace(
            "\\", "\\\\").replace('"', '\\"')
        transcript_family = transcript_font.family().replace(
            "\\", "\\\\").replace('"', '\\"')
        input_family = input_font.family().replace("\\", "\\\\").replace('"', '\\"')
        self.setStyleSheet(
            "QDialog { "
            f"background-color: {colors['window']}; color: {text}; }}"
            "QLabel#badge { "
            f"color: {muted}; font-size: 12px; font-weight: 600; }}"
            "QLabel#badge[warning='true'] { "
            f"color: {colors['warning']}; }}"
            "QLabel#spin { "
            f"color: {muted}; font-size: 12px; }}"
            "QPushButton, QToolButton { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 5px; "
            "padding: 0 7px; font-size: 12px; }"
            "QPushButton:hover, QToolButton:hover { "
            f"background-color: {colors['surface_hover']}; }}"
            "QPushButton:disabled, QToolButton:disabled { "
            f"color: {muted}; }}"
            "QTextEdit#translationResult, "
            "QTextBrowser#chatTranscript, QPlainTextEdit#chatInput, "
            "QPlainTextEdit#explainInstructions { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 5px; }}"
            "QMenu { "
            f"background-color: {colors['panel']}; color: {text}; "
            f"border: 1px solid {border}; }}"
            "QMenu::item:selected { "
            f"background-color: {colors['selection']}; color: {text}; }}"
            "QSplitter#translationChatSplitter::handle:vertical { "
            f"background-color: {colors['accent_light']}; }}"
            "QSplitter#translationChatSplitter::handle:vertical:hover { "
            f"background-color: {colors['accent']}; }}"
        )
        self.result.setStyleSheet(
            "QTextEdit#translationResult { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: none; border-radius: 4px; padding: 0; "
            f"padding-left: {self._translation_side_margin(self.settings)}px; "
            f"padding-right: {self._translation_side_margin(self.settings)}px; "
            f'font-family: "{result_family}"; '
            f"font-size: {translation_font_size:g}pt; }}"
        )
        self.result.document().setDefaultFont(translation_font)
        self.chat_transcript.setStyleSheet(
            "QTextBrowser#chatTranscript { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 4px; padding: 5px; "
            f'font-family: "{transcript_family}"; '
            f"font-size: {transcript_font.pointSizeF():g}pt; }}"
        )
        self.chat_input.setStyleSheet(
            "QPlainTextEdit#chatInput { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 4px; padding: 5px; "
            f'font-family: "{input_family}"; '
            f"font-size: {input_font.pointSizeF():g}pt; }}"
        )
        self.voice_btn.setStyleSheet(
            "QToolButton { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 5px; padding: 0; }}"
            "QToolButton:hover { "
            f"background-color: {colors['surface_hover']}; }}"
        )
        self.page_number_label.setStyleSheet(
            "QPushButton#translationPageNumber { "
            f"color: {muted}; font-size: 12px; font-weight: 600; padding: 0 5px; }}"
            "QPushButton#translationPageNumber:checked { "
            f"background-color: {colors['selection']}; "
            f"border-color: {colors['accent']}; color: {text}; }}"
        )
        self._sync_explain_button()
        self._style_speed_button()
        self.speed_menu.setStyleSheet(
            "QMenu { "
            f"background-color: {colors['panel']}; color: {text}; "
            f"border: 1px solid {border}; padding: 2px; }}"
        )
        self._speed_panel.setStyleSheet(
            "QWidget#speechSpeedPanel { "
            f"background-color: {colors['panel']}; color: {text}; }}"
            "QLabel#speechSpeedTitle, QLabel#speechSpeedValue { "
            f"color: {text}; }}"
            "QLabel#speechSpeedDescription { "
            f"color: {muted}; }}"
            "QSlider::groove:horizontal { "
            f"background-color: {border}; height: 5px; border-radius: 2px; }}"
            "QSlider::sub-page:horizontal { "
            f"background-color: {colors['accent']}; border-radius: 2px; }}"
            "QSlider::handle:horizontal { "
            f"background-color: {colors['surface_hover']}; "
            f"border: 1px solid {colors['accent_light']}; "
            "width: 19px; height: 19px; margin: -8px 0; border-radius: 10px; }"
            "QToolButton { "
            f"background-color: {colors['surface']}; color: {text}; "
            f"border: none; border-radius: 16px; padding: 7px 3px; }}"
            "QToolButton:hover, QToolButton:checked { "
            f"background-color: {colors['selection']}; color: {text}; }}"
        )

    def _refresh_speech_controls(self) -> None:
        old_voice = self._speech_voice
        self._speech_engine = self.settings.get("speech_engine", "edge")
        configured_voice = self.settings.get("speech_voice")
        self._speech_voice = (
            configured_voice
            if isinstance(configured_voice, str) and configured_voice
            else ARABIC_VOICE
        )
        self._speech_rate = normalize_speed(self.settings.get("speech_rate"))
        self._speech_available = speech_available(
            self._speech_engine,
            str(self.settings.get("gemini_key") or ""),
        )
        self._player.setPlaybackRate(self._speech_rate)
        self._speed_slider.blockSignals(True)
        self._speed_slider.setValue(
            self._speed_to_slider_value(self._speech_rate))
        self._speed_slider.blockSignals(False)
        self._update_speed_display(self._speech_rate)
        self.speed_btn.setToolTip(
            f"Reading speed: {self._speech_rate:.2f}x")
        self.speed_btn.setEnabled(self._speech_available)

        engine_name = (
            "Gemini TTS"
            if self._speech_engine == GEMINI_TTS_ENGINE else "Edge TTS"
        )
        self.speak_btn.setToolTip(
            f"Read the translation aloud with {engine_name}"
            if self._speech_available
            else speech_missing_message(self._speech_engine))
        has_text = bool(self._current_speech_text().strip())
        self.speak_btn.setText(self._speech_idle_label())
        self.speak_btn.setEnabled(has_text and self._speech_available)
        self.voice_btn.setEnabled(
            self._speech_available
            and self._speech_engine == "edge"
        )
        if self._speech_engine == GEMINI_TTS_ENGINE:
            self.voice_btn.setToolTip(
                f"Gemini voice: {self.settings.get('gemini_tts_voice', 'Kore')}")
        else:
            self.voice_btn.setToolTip(f"Voice: {self._speech_voice}")

        if old_voice != self._speech_voice:
            self.voice_menu.clear()
            self._voice_actions = []
            self._voice_placeholder = self.voice_menu.addAction(
                "Open to load voices…")
            self._voice_placeholder.setEnabled(False)

    @staticmethod
    def _translation_font(settings: dict, font_size: float) -> QFont:
        font_size = max(
            MIN_TRANSLATION_FONT_SIZE,
            min(MAX_TRANSLATION_FONT_SIZE, float(font_size)),
        )
        font = QFont(QApplication.font())
        font.setPointSizeF(font_size)
        arabic_font = settings.get("translation_font_arabic", "")
        latin_font = settings.get("translation_font_latin", "")
        translation_font = (
            settings.get("translation_font", "")
            if not arabic_font and not latin_font
            else latin_font
        )
        if not isinstance(translation_font, str) or not translation_font:
            return font

        if TranslatePopup._safe_font_family(font, translation_font) is None:
            return font
        selected_font = QFont(font)
        selected_font.setFamily(translation_font)
        return selected_font

    @staticmethod
    def _safe_font_family(base_font: QFont, family: str) -> str | None:
        if not isinstance(family, str) or not family:
            return None
        selected_font = QFont(base_font)
        selected_font.setFamily(family)
        natural_spacing = QFontMetricsF(base_font).lineSpacing()
        selected_spacing = QFontMetricsF(selected_font).lineSpacing()
        if natural_spacing > 0 and selected_spacing > natural_spacing * 2:
            logging.warning(
                "Using the application font instead of translation font %r "
                "because its line spacing is unusually large (%.1f px vs "
                "%.1f px).",
                family,
                selected_spacing,
                natural_spacing,
            )
            return None
        return family

    def _apply_script_font_families(
        self, document: QTextDocument, start_position: int = 0,
    ) -> None:
        arabic_font = self.settings.get("translation_font_arabic", "")
        latin_font = self.settings.get("translation_font_latin", "")
        legacy_font = self.settings.get("translation_font", "")
        if not arabic_font and not latin_font and legacy_font:
            arabic_font = latin_font = legacy_font

        base_font = document.defaultFont()
        arabic_font = self._safe_font_family(base_font, arabic_font)
        latin_font = self._safe_font_family(base_font, latin_font)
        families = {
            "arabic": arabic_font,
            "latin": latin_font,
        }
        block = document.findBlock(start_position)
        while block.isValid():
            text = block.text()
            run_kind = None
            block_start = block.position()
            local_start = max(0, start_position - block_start)
            python_start = qt_to_python_position(text, local_start)
            qt_position = block_start + local_start
            run_qt_start = qt_position
            for character in text[python_start:]:
                codepoint = ord(character)
                if (
                    0x0600 <= codepoint <= 0x06FF
                    or 0x0750 <= codepoint <= 0x077F
                    or 0x0870 <= codepoint <= 0x089F
                    or 0x08A0 <= codepoint <= 0x08FF
                    or 0xFB50 <= codepoint <= 0xFDFF
                    or 0xFE70 <= codepoint <= 0xFEFF
                    or 0x1EE00 <= codepoint <= 0x1EEFF
                ):
                    kind = "arabic"
                elif (
                    character.isalpha()
                    and unicodedata.name(character, "").startswith("LATIN ")
                ):
                    kind = "latin"
                else:
                    kind = None

                width = 2 if codepoint > 0xFFFF else 1
                if kind != run_kind:
                    family = families.get(run_kind)
                    if family and run_qt_start < qt_position:
                        cursor = QTextCursor(document)
                        cursor.setPosition(run_qt_start)
                        cursor.setPosition(
                            qt_position, QTextCursor.MoveMode.KeepAnchor)
                        char_format = QTextCharFormat()
                        char_format.setFontFamilies([family])
                        cursor.mergeCharFormat(char_format)
                    run_kind = kind
                    run_qt_start = qt_position
                qt_position += width

            family = families.get(run_kind)
            if family and run_qt_start < qt_position:
                cursor = QTextCursor(document)
                cursor.setPosition(run_qt_start)
                cursor.setPosition(
                    qt_position, QTextCursor.MoveMode.KeepAnchor)
                char_format = QTextCharFormat()
                char_format.setFontFamilies([family])
                cursor.mergeCharFormat(char_format)
            block = block.next()

    @staticmethod
    def _read_saved_size(value) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            width, height = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            logging.warning("Ignoring invalid saved translation-window size: %r", value)
            return None
        if width < 260 or height < 150:
            logging.warning("Ignoring undersized saved translation-window size: %r", value)
            return None
        return width, height

    @staticmethod
    def _read_saved_position(value) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            logging.warning("Ignoring invalid saved translation-window position: %r", value)
            return None

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._fixed_applied:
            self._fixed_applied = True
            self._apply_fixed_outer()

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            from ctypes import wintypes

            native_message = wintypes.MSG.from_address(int(message))
            if (
                native_message.message == 0x00A3
                and native_message.wParam == 2
            ):
                self._toggle_vertical_maximize()
                return True, 0
        return super().nativeEvent(event_type, message)

    def _toggle_vertical_maximize(self) -> None:
        if self._vertical_restore_geometry is not None:
            target = QRect(self._vertical_restore_geometry)
            self._vertical_restore_geometry = None
            self._set_outer_geometry(target)
            return

        current = QRect(self.frameGeometry())
        bounds = self._vertical_maximize_bounds(current)
        if bounds is None:
            return
        target = QRect(
            current.x(),
            bounds.top(),
            current.width(),
            bounds.height(),
        )
        if target == current:
            return
        self._vertical_restore_geometry = current
        self._set_outer_geometry(target)
        self._size_save_timer.start()

    def _vertical_maximize_bounds(self, current: QRect) -> QRect | None:
        parent = self.parentWidget()
        if parent is not None:
            area = getattr(parent, "centralWidget", None)
            area = area() if callable(area) else None
            if area is None:
                area = parent
            if area.isVisible():
                origin = area.mapToGlobal(QPoint(0, 0))
                return QRect(origin, area.size())

        screen = QApplication.screenAt(current.center()) or self.screen()
        screen = screen or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _set_outer_geometry(self, target: QRect) -> None:
        if sys.platform == "win32":
            try:
                import ctypes

                hwnd = int(self.winId())
                if hwnd and ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    0,
                    target.x(),
                    target.y(),
                    target.width(),
                    target.height(),
                    0x0004 | 0x0010,
                ):
                    QApplication.processEvents()
                    if self.frameGeometry() == target:
                        return
            except (AttributeError, OSError, TypeError, ValueError):
                pass

        for _ in range(3):
            frame = self.frameGeometry()
            if frame == target:
                return
            width_delta = target.width() - frame.width()
            height_delta = target.height() - frame.height()
            if width_delta or height_delta:
                self.resize(
                    max(self.minimumWidth(), self.width() + width_delta),
                    max(self.minimumHeight(), self.height() + height_delta),
                )
            frame = self.frameGeometry()
            self.move(
                self.x() + target.x() - frame.x(),
                self.y() + target.y() - frame.y(),
            )

    def _apply_fixed_outer(self) -> None:
        """Place the saved outer frame within the current screen bounds.

        Primary: Win32 SetWindowPos (outer coordinates by OS guarantee).
        Fallback: Qt frame compensation (for non-Windows / headless).
        """
        fx, fy, fw, fh = self._target_outer_geometry()
        if self._place_via_win32():
            return
        try:
            for _ in range(3):
                fg = self.frameGeometry()
                if (fg.x(), fg.y(), fg.width(), fg.height()) == (fx, fy, fw, fh):
                    break
                width_delta = fw - fg.width()
                height_delta = fh - fg.height()
                if width_delta or height_delta:
                    self.resize(
                        max(260, self.width() + width_delta),
                        max(150, self.height() + height_delta),
                    )
                fg = self.frameGeometry()
                self.move(self.x() + fx - fg.x(), self.y() + fy - fg.y())
        except Exception:
            pass

    def _target_outer_geometry(self) -> tuple[int, int, int, int]:
        default_x, default_y, default_w, default_h = TranslatePopup.FIXED_OUTER
        x, y = self._saved_position or (default_x, default_y)
        width, height = self._saved_size or (default_w, default_h)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return x, y, width, height

        available = screen.availableGeometry()
        width = min(width, available.width())
        height = min(height, available.height())
        x = min(max(x, available.left()), available.right() - width + 1)
        y = min(max(y, available.top()), available.bottom() - height + 1)
        return x, y, width, height

    def _place_via_win32(self) -> bool:
        try:
            import ctypes
            from PySide6.QtWidgets import QApplication
            fx, fy, fw, fh = self._target_outer_geometry()
            hwnd = int(self.winId())
            if not hwnd:
                return False
            SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
            if not ctypes.windll.user32.SetWindowPos(hwnd, 0, fx, fy, fw, fh,
                                                     SWP_NOZORDER | SWP_NOACTIVATE):
                return False
            QApplication.processEvents()
            return self.frameGeometry().getRect() == (fx, fy, fw, fh)
        except Exception:
            return False

    def _on_done(self, translated_text: str, badge: str) -> None:
        self.spin.hide()
        self._translation_text = translated_text
        self._translation_needs_render = bool(translated_text)
        self.chat_btn.setEnabled(bool(translated_text.strip()))
        self.chat_send_btn.setEnabled(
            bool(translated_text.strip()) and self._chat_worker is None)
        self.result.show()
        self.refresh_display(self.settings)
        self.speak_btn.setEnabled(
            bool(translated_text.strip()) and self._speech_available)
        self.speed_btn.setEnabled(self._speech_available)
        self.voice_btn.setEnabled(
            self._speech_available and self._speech_engine == "edge")
        try:
            secs = self._worker.translate_secs if self._worker else 0.0
        except RuntimeError:
            secs = 0.0
        # Keep model status compact; the full fallback reason stays on hover.
        # The token count is peeled off first: it is long, it does not
        # change between selections, and the main window shows it in its
        # corner where it stays visible while you keep reading.
        self.token_line = ""
        head, sep, tail = badge.partition(TOKEN_TAIL)
        if sep:
            self.token_line = tail.strip()
            badge = head
        chunk_summary = re.fullmatch(
            r"⚠\s*(.*?)\s{2}"
            r"(\(\d+/\d+ chunks by your model\)|\(not your model\))",
            badge.strip(),
        )
        if chunk_summary:
            engine = chunk_summary.group(1).strip() or "Translation fallback"
            counts = re.fullmatch(
                r"\((\d+)/(\d+) chunks by your model\)",
                chunk_summary.group(2),
            )
            if counts:
                used, total = counts.groups()
                self.badge.setText(
                    f"⚠ Partial fallback · {used}/{total} · {secs:.1f}s")
                self.badge.setToolTip(
                    f"The selected model translated {used} of {total} chunks."
                    f"\nMost common engine: {engine}.\n{badge.strip()}"
                )
            else:
                self.badge.setText(
                    f"⚠ Fallback · not your model · {secs:.1f}s")
                self.badge.setToolTip(
                    f"The selected model did not translate any chunks."
                    f"\nMost common engine: {engine}.\n{badge.strip()}"
                )
            self.badge.setProperty("warning", True)
        else:
            engine, marker, why = badge.partition("⚠")
            # "\u2726 chosen: gemini-x" -> "\u2726 gemini-x": keep the symbol, drop the word.
            engine = re.sub(r"\bchosen:\s*", "", engine.strip())
            engine = re.sub(r"\bchosen image:\s*", "", engine.strip())
            engine = re.sub(r"\bcustom:\s*", "", engine.strip())
            if marker:
                selected_model = re.search(
                    r"\bchosen model\s+(.+?)\s+failed:", why, re.IGNORECASE)
                selected_model = (
                    selected_model.group(1).strip()
                    if selected_model else ""
                )
                actual_engine = {
                    "Free service": "Google Translate",
                    "Free service (alt)": "MyMemory",
                }.get(engine, engine) or "Translation fallback"
                if selected_model:
                    model_label = (
                        selected_model if len(selected_model) <= 34
                        else f"{selected_model[:31]}..."
                    )
                    status = f"⚠ {actual_engine} · {model_label} failed"
                    tooltip = (
                        f"{actual_engine} answered because the selected model "
                        f"{selected_model} failed.\n{why.strip()}"
                    )
                else:
                    status = f"⚠ {actual_engine}"
                    tooltip = (
                        f"{actual_engine} is NOT the model you chose.\n"
                        f"{why.strip()}"
                    )
                self.badge.setText(f"{status} · {secs:.1f}s")
                self.badge.setToolTip(tooltip)
                self.badge.setProperty("warning", True)
                engine = actual_engine
            else:
                self.badge.setText(f"{engine} · {secs:.1f}s")
                self.badge.setToolTip("Your chosen model translated this.")
                self.badge.setProperty("warning", False)
        self.badge.style().unpolish(self.badge)
        self.usageReported.emit(engine.strip(), self.token_line)
        self.badge.style().polish(self.badge)
        self.badge.show()
        self._schedule_speech_prefetch()

    @staticmethod
    def _line_spacing(settings: dict) -> int:
        try:
            spacing = int(settings.get(
                "line_spacing_px", DEFAULT_LINE_SPACING))
        except (TypeError, ValueError):
            return DEFAULT_LINE_SPACING
        return max(MIN_LINE_SPACING, min(MAX_LINE_SPACING, spacing))

    @staticmethod
    def _translation_side_margin(settings: dict) -> int:
        try:
            margin = int(settings.get(
                "translation_side_margin_px",
                DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
            ))
        except (TypeError, ValueError):
            margin = DEFAULT_TRANSLATION_SIDE_MARGIN_PX
        return max(
            MIN_TRANSLATION_SIDE_MARGIN_PX,
            min(MAX_TRANSLATION_SIDE_MARGIN_PX, margin),
        )

    @staticmethod
    def _translation_top_bottom_margin(settings: dict) -> int:
        try:
            margin = int(settings.get(
                "translation_top_bottom_margin_px",
                DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            ))
        except (TypeError, ValueError):
            margin = DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX
        return max(
            MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            min(MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX, margin),
        )

    @staticmethod
    def _translation_font_size(settings: dict) -> float:
        try:
            size = float(settings.get("font_size", 17))
        except (TypeError, ValueError):
            size = 17.0
        if not 0.0 < size < float("inf"):
            size = 17.0
        return max(
            MIN_TRANSLATION_FONT_SIZE,
            min(MAX_TRANSLATION_FONT_SIZE, size),
        )

    def _apply_translation_zoom(self, factor: float) -> None:
        new_size = round(
            max(
                MIN_TRANSLATION_FONT_SIZE,
                min(
                    MAX_TRANSLATION_FONT_SIZE,
                    self._translation_font_size(self.settings) * factor,
                ),
            ),
            1,
        )
        if new_size == self._translation_font_size(self.settings):
            return
        if not self._save_shared_settings(
            update_local=False, font_size=new_size,
        ):
            return
        owner = self.parentWidget()
        owner_settings = getattr(owner, "settings", None)
        scrollbar = self.result.verticalScrollBar()
        previous_max = scrollbar.maximum()
        scroll_fraction = (
            scrollbar.value() / previous_max if previous_max else 0.0
        )
        for popup in getattr(owner, "popups", []):
            if popup is self or not popup.isVisible():
                continue
            popup_settings = getattr(popup, "settings", None)
            if isinstance(popup_settings, dict):
                popup.refresh_display(
                    dict(popup_settings, font_size=new_size))
        self.refresh_display(dict(self.settings, font_size=new_size))
        if isinstance(owner_settings, dict):
            owner_settings["font_size"] = new_size
        scrollbar.setValue(round(scroll_fraction * scrollbar.maximum()))

    def _apply_line_spacing(self) -> None:
        spacing = self._line_spacing(self.settings)
        vertical_margin = self._translation_top_bottom_margin(self.settings)
        document = self.result.document()
        root_format = document.rootFrame().frameFormat()
        root_format.setTopMargin(vertical_margin)
        root_format.setBottomMargin(vertical_margin)
        document.rootFrame().setFrameFormat(root_format)
        paragraph_gap = max(
            1.0,
            QFontMetricsF(self.result.font()).lineSpacing() + spacing,
        )
        previous_block_is_code = False
        block = document.begin()
        while block.isValid():
            block_format = block.blockFormat()
            next_block = block.next()
            is_code = block_format.hasProperty(
                QTextFormat.Property.BlockCodeFence)
            next_is_code = (
                next_block.isValid()
                and next_block.blockFormat().hasProperty(
                    QTextFormat.Property.BlockCodeFence)
            )
            current_list = block.textList()
            next_list = next_block.textList() if next_block.isValid() else None
            block_format.setTopMargin(
                paragraph_gap if is_code and not previous_block_is_code else 0)
            if is_code:
                bottom_margin = paragraph_gap if not next_is_code else 0
            else:
                bottom_margin = (
                    paragraph_gap
                    if next_block.isValid()
                    and not next_is_code
                    and not (current_list is not None and next_list is not None)
                    else 0
                )
            block_format.setBottomMargin(bottom_margin)
            if spacing == 0 or is_code:
                block_format.clearProperty(QTextFormat.Property.LineHeight)
                block_format.clearProperty(QTextFormat.Property.LineHeightType)
            else:
                block_format.setLineHeight(
                    float(spacing),
                    int(QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value),
                )
            cursor = QTextCursor(block)
            cursor.setBlockFormat(block_format)
            previous_block_is_code = is_code
            block = block.next()

    def _render_markdown(self, text: str) -> None:
        """Render Markdown with consistent headings, direction, and spacing."""
        from translators.chain import _remove_nonheading_bold

        text = _remove_nonheading_bold(text)
        self.result.setMarkdown(self._markdown_with_inline_notation(text))
        self._apply_code_block_style(
            self.result.document(), self.result.font().pointSizeF())
        self._apply_translation_direction()
        self._apply_heading_typography()
        self._apply_line_spacing()
        self._apply_script_font_families(self.result.document())
        self._tighten_musical_accidentals(self.result.document())

    @staticmethod
    def _tighten_musical_accidentals(document: QTextDocument) -> None:
        """Reduce the gap before flat, sharp, and natural note accidentals."""
        block = document.begin()
        while block.isValid():
            text = block.text()
            for match in _MUSICAL_ACCIDENTAL_AFTER_NOTE.finditer(text):
                start = block.position() + python_to_qt_position(
                    text, match.start(1))
                cursor = QTextCursor(document)
                cursor.setPosition(start)
                cursor.setPosition(
                    start + 1, QTextCursor.MoveMode.KeepAnchor)
                char_format = QTextCharFormat()
                char_format.setFontLetterSpacingType(
                    QFont.SpacingType.AbsoluteSpacing)
                char_format.setFontLetterSpacing(-1.5)
                cursor.mergeCharFormat(char_format)
            block = block.next()

    @staticmethod
    def _apply_code_block_style(
        document: QTextDocument, base_size: float,
    ) -> None:
        """Style every fenced Markdown block as compact, distinct code."""
        if base_size <= 0:
            return
        code_size = max(9.0, base_size * 0.85)
        code_background = QColor("#e7edf3")
        code_foreground = QColor("#36566f")
        code_gap = max(
            1.0, QFontMetricsF(document.defaultFont()).lineSpacing())
        previous_block_is_code = False
        block = document.begin()
        while block.isValid():
            block_format = block.blockFormat()
            is_code = block_format.hasProperty(
                QTextFormat.Property.BlockCodeFence)
            next_block = block.next()
            next_is_code = (
                next_block.isValid()
                and next_block.blockFormat().hasProperty(
                    QTextFormat.Property.BlockCodeFence)
            )
            if is_code:
                block_format.setBackground(code_background)
                block_format.setLeftMargin(10)
                block_format.setRightMargin(10)
                block_format.setNonBreakableLines(False)
                block_format.setTopMargin(
                    code_gap if not previous_block_is_code else 0)
                block_format.setBottomMargin(
                    code_gap if not next_is_code else 0)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(block_format)
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                char_format = QTextCharFormat()
                char_format.setFontPointSize(code_size)
                char_format.setFontFixedPitch(True)
                char_format.setForeground(code_foreground)
                cursor.mergeCharFormat(char_format)
            previous_block_is_code = is_code
            block = block.next()

    def _apply_heading_typography(self) -> None:
        """Render headings noticeably larger than the translation body."""
        base_size = self.result.font().pointSizeF()
        if base_size <= 0:
            base_size = self._translation_font_size(self.settings)
        heading_size = max(12.0, base_size + 4.0)
        block = self.result.document().begin()
        while block.isValid():
            block_format = block.blockFormat()
            level = block_format.headingLevel()
            if level:
                size = heading_size
            else:
                block = block.next()
                continue

            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            char_format = QTextCharFormat()
            char_format.setFontPointSize(size)
            cursor.mergeCharFormat(char_format)
            block = block.next()

    def _apply_translation_direction(self) -> None:
        target_language = str(
            self.settings.get("target_language", "ar")).split("-", 1)[0]
        document = self.result.document()
        is_arabic = target_language == "ar"
        direction = (
            Qt.LayoutDirection.RightToLeft
            if is_arabic else Qt.LayoutDirection.LeftToRight
        )
        start_alignment = (
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute
            if is_arabic else Qt.AlignmentFlag.AlignLeft
        )
        self.result.setLayoutDirection(direction)
        option = document.defaultTextOption()
        option.setTextDirection(direction)
        option.setAlignment(start_alignment)
        document.setDefaultTextOption(option)
        block = document.begin()
        while block.isValid():
            block_format = block.blockFormat()
            is_code_block = block_format.hasProperty(
                QTextFormat.Property.BlockCodeFence)
            is_heading = bool(block_format.headingLevel())
            cursor = QTextCursor(block)
            is_table_cell = cursor.currentTable() is not None
            block_format.setLayoutDirection(
                Qt.LayoutDirection.LeftToRight if is_code_block else direction)
            if is_code_block:
                block_format.setAlignment(Qt.AlignmentFlag.AlignLeft)
            elif not is_table_cell:
                block_format.setAlignment(start_alignment)
            cursor.setBlockFormat(block_format)
            block = block.next()

    def _render_translation(self) -> None:
        text = getattr(self, "_translation_text", "")
        self._render_markdown(text)
        self._rendered_translation_text = text
        self._translation_needs_render = False

    def _on_failed(self, err: str) -> None:
        self.spin.hide()
        self.result.setPlainText(err)
        self.result.show()
        self.chat_btn.setEnabled(False)
        self.chat_send_btn.setEnabled(False)

    def _toggle_chat_panel(self, visible: bool) -> None:
        self.chat_panel.setVisible(visible)
        if visible:
            sizes = self.content_splitter.sizes()
            if len(sizes) == 2 and sizes[1] < self.chat_panel.minimumSizeHint().height():
                available = max(
                    self.content_splitter.height(),
                    self.chat_panel.minimumSizeHint().height() + 100,
                )
                chat_height = max(
                    self.chat_panel.minimumSizeHint().height(),
                    available // 3,
                )
                self.content_splitter.setSizes(
                    [max(80, available - chat_height), chat_height])
        self.chat_btn.setText("اشرح بعمق" if visible else "Chat")
        self.speak_btn.setText(self._speech_idle_label())
        self.speak_btn.setToolTip(
            "Read the latest model answer aloud with Edge TTS"
            if visible and self._latest_chat_answer() else
            "Read the translation aloud with Edge TTS")
        self.speak_btn.setEnabled(
            self._speech_available and bool(self._current_speech_text().strip()))
        if visible:
            self.chat_input.setFocus()

    def _on_chat_button_clicked(self) -> None:
        if self.chat_panel.isHidden():
            self._toggle_chat_panel(True)
        else:
            self._toggle_chat_panel(False)

    def _apply_chat_direction(self) -> None:
        direction = (
            Qt.LayoutDirection.RightToLeft
            if self.settings.get("target_language", "ar") == "ar"
            else Qt.LayoutDirection.LeftToRight
        )
        for editor in (self.chat_input,):
            editor.setLayoutDirection(direction)
            option = editor.document().defaultTextOption()
            option.setTextDirection(direction)
            option.setAlignment(
                Qt.AlignmentFlag.AlignLeft
                if direction == Qt.LayoutDirection.RightToLeft
                else Qt.AlignmentFlag.AlignLeft
            )
            editor.document().setDefaultTextOption(option)
        self._render_chat_transcript()

    def _append_chat_line(self, speaker: str, text: str) -> None:
        self._chat_entries.append({
            "role": "user" if speaker == "You" else "assistant",
            "content": text,
            "speaker": speaker,
        })
        self._render_chat_transcript()

    def _on_chat_transcript_clicked(self, position: int) -> None:
        if not self._speech_available:
            return
        cursor = self.chat_transcript.textCursor()
        cursor.setPosition(max(0, min(position, self.chat_transcript.document().characterCount() - 1)))
        paragraph = cursor.block().text()
        position_in_block = cursor.positionInBlock()
        if not paragraph.strip():
            return
        if paragraph.startswith(("You:", "Tutor:", "Error:")):
            return
        start = speech_start_offset(paragraph, position_in_block)
        text = paragraph[start:]
        leading_whitespace = len(text) - len(text.lstrip())
        text = text.lstrip()
        if not text:
            return
        self._speech_text_override = text
        self._chat_speech_document_start = (
            cursor.block().position()
            + python_to_qt_position(paragraph, start + leading_whitespace)
        )
        self._request_speech(0)

    def _latest_chat_answer(self) -> str:
        for message in reversed(self._chat_messages):
            if message["role"] == "assistant":
                return message["content"]
        return ""

    def _current_speech_text(self) -> str:
        if not self.chat_panel.isHidden():
            return self._markdown_speech_text(self._latest_chat_answer())
        return self._speech_text_from_document(self.result.document())

    @staticmethod
    def _markdown_speech_text(markdown: str) -> str:
        document = QTextDocument()
        document.setMarkdown(
            TranslatePopup._markdown_with_inline_notation(markdown))
        return TranslatePopup._speech_text_from_document(document)

    @staticmethod
    def _markdown_with_inline_notation(markdown: str) -> str:
        markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
        markdown_parts = re.split(r"(```.*?```)", markdown, flags=re.DOTALL)

        def split_table_row(line: str) -> list[str]:
            line = line.strip()
            if line.startswith("|"):
                line = line[1:]
            if line.endswith("|") and not line.endswith(r"\|"):
                line = line[:-1]
            return [
                cell.replace(r"\|", "|").strip()
                for cell in re.split(r"(?<!\\)\|", line)
            ]

        def render_table(match: re.Match[str]) -> str:
            lines = match.group(0).strip("\n").splitlines()
            headers = split_table_row(lines[0])
            separators = split_table_row(lines[1])
            if not headers or len(headers) != len(separators):
                return match.group(0)
            if not all(_TABLE_SEPARATOR_CELL.fullmatch(cell)
                       for cell in separators):
                return match.group(0)

            alignments = []
            for cell in separators:
                left, right = cell.startswith(":"), cell.endswith(":")
                alignments.append(
                    "center" if left and right else
                    "left" if left else
                    "right" if right else
                    "left"
                )

            rows = [split_table_row(line) for line in lines[2:]]
            escaped = html.escape

            def cells_markup(values: list[str], tag: str) -> str:
                values = (values + [""] * len(headers))[:len(headers)]
                header_style = ' bgcolor="#e7edf3"' if tag == "th" else ""
                return "".join(
                    f'<{tag} align="{alignment}"{header_style}>'
                    f'{escaped(value)}</{tag}>'
                    for value, alignment in zip(values, alignments)
                )

            body = "".join(
                f"<tr>{cells_markup(row, 'td')}</tr>" for row in rows
            )
            return (
                '\n\n<table border="1" bordercolor="#c5d0da" '
                'cellspacing="0" cellpadding="5" '
                'style="border-collapse:collapse; width:100%;">'
                f"<thead><tr>{cells_markup(headers, 'th')}</tr></thead>"
                f"<tbody>{body}</tbody></table>\n\n"
            )

        def escape_non_html_tag(match: re.Match[str]) -> str:
            if match.group(1).lower() in _MARKDOWN_HTML_TAGS:
                return match.group(0)
            return match.group(0).replace("<", "&lt;").replace(">", "&gt;")

        for index in range(0, len(markdown_parts), 2):
            markdown_parts[index] = re.sub(
                r"(?m)^[ \t]*\|?.+\|.+\|?[ \t]*\n"
                r"[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*\n"
                r"(?:[ \t]*\|?.+\|.+\|?[ \t]*(?:\n|$))*",
                render_table,
                markdown_parts[index],
            )
            markdown_parts[index] = re.sub(
                r"(?<!\\)</?([A-Za-z][A-Za-z0-9-]*)[^>]*>",
                escape_non_html_tag,
                markdown_parts[index],
            )
            markdown_parts[index] = re.sub(
                r"(?<!\\)\\\*\\\*(.+?)(?<!\\)\\\*\\\*",
                r"**\1**",
                markdown_parts[index],
            )
            segments = re.split(
                r"(\n[ \t]*\n+)", markdown_parts[index])
            for part_index in range(0, len(segments), 2):
                paragraph = segments[part_index]
                if re.search(
                    r"(?m)^\s{0,3}(?:#{1,6}[ \t]+|>|"
                    r"(?:[-+*]|\d+[.)])[ \t]+)",
                    paragraph,
                ):
                    continue
                segments[part_index] = re.sub(
                    r"(?<!\n)\n(?!\n)",
                    "\u2028",
                    paragraph,
                )
            markdown_parts[index] = "".join(segments)
        for index in range(1, len(markdown_parts), 2):
            before = re.sub(
                r"[\n\u2028]+$", "", markdown_parts[index - 1])
            after = re.sub(
                r"^[\n\u2028]+", "", markdown_parts[index + 1])
            markdown_parts[index - 1] = before + "\n\n"
            markdown_parts[index + 1] = "\n\n" + after
        markdown = "".join(markdown_parts)
        markdown = re.sub(
            r"(?<!\\)\$\$(.+?)(?<!\\)\$\$",
            lambda match: match.group(1),
            markdown,
        )
        markdown = re.sub(
            r"(?<!\\)(?<!\$)\$(?!\$)(?=\S)([^$]*?\S)"
            r"(?<!\\)(?<!\$)\$(?!\$)",
            lambda match: match.group(1),
            markdown,
        )
        return re.sub(
            r"\[\[grs\]\](.+?)\[\[/grs\]\]",
            lambda match: f"`{match.group(1)}`",
            markdown,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _speech_text_from_document(document: QTextDocument) -> str:
        blocks = []
        speakable_blocks = []
        block = document.begin()
        while block.isValid():
            text = block.text()
            block_format = block.blockFormat()
            is_code = block_format.hasProperty(
                QTextFormat.Property.BlockCodeFence
            )
            if is_code:
                text = " " * len(text)
            else:
                characters = list(text)
                offset = 0
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    fragment_text = fragment.text()
                    char_format = fragment.charFormat()
                    is_small_peripheral_text = (
                        char_format.hasProperty(
                            QTextFormat.Property.FontSizeAdjustment
                        )
                        and char_format.property(
                            QTextFormat.Property.FontSizeAdjustment
                        ) < 0
                    )
                    if (
                        char_format.fontFixedPitch()
                        or is_small_peripheral_text
                    ):
                        characters[offset:offset + len(fragment_text)] = (
                            " " * len(fragment_text))
                    offset += len(fragment_text)
                    iterator += 1
                text = "".join(characters)
            blocks.append(text)
            speakable_blocks.append(bool(text.strip()))
            block = block.next()

        speech_parts = []
        for index, text in enumerate(blocks):
            speech_parts.append(text)
            if index + 1 < len(blocks):
                has_terminal_punctuation = bool(re.search(
                    r"[.!?…،,;:؛]\s*$", text))
                separator = (
                    ","
                    if (
                        speakable_blocks[index]
                        and speakable_blocks[index + 1]
                        and not has_terminal_punctuation
                    )
                    else "\n"
                )
                speech_parts.append(separator)
        return "".join(speech_parts)

    def _speech_idle_label(self) -> str:
        return (
            "Read answer"
            if not self.chat_panel.isHidden() and self._latest_chat_answer()
            else "Read aloud"
        )

    def _render_chat_transcript(self) -> None:
        default_direction = (
            Qt.LayoutDirection.RightToLeft
            if self.settings.get("target_language", "ar") == "ar"
            else Qt.LayoutDirection.LeftToRight
        )
        self.chat_transcript.setLayoutDirection(default_direction)
        entries = []
        for message in self._chat_entries:
            speaker = message["speaker"]
            content = f"**{speaker}:**\n\n{message['content']}"
            markdown_document = QTextDocument()
            markdown_document.setMarkdown(
                self._markdown_with_inline_notation(content))
            self._apply_code_block_style(
                markdown_document, self.result.font().pointSizeF())
            html = markdown_document.toHtml()
            body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.S)
            body = body_match.group(1) if body_match else html
            is_arabic = bool(re.search(
                r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]",
                message["content"],
            ))
            if is_arabic:
                body = re.sub(
                    r"<(p|h[1-6]|li)([^>]*)>",
                    lambda match: self._orient_chat_html_block(
                        match, "rtl", "right"),
                    body,
                )
                body = re.sub(
                    r"</(p|h[1-6]|li)>",
                    lambda match: "\u202c" + match.group(0),
                    body,
                )
                entries.append(body)
            else:
                body = re.sub(
                    r"<(p|h[1-6]|li)([^>]*)>",
                    lambda match: self._orient_chat_html_block(
                        match, "ltr", "left"),
                    body,
                )
                entries.append(body)
        separator = '<hr><p dir="rtl" style="text-align:right;"></p>'
        self.chat_transcript.setHtml(separator.join(entries))
        option = self.chat_transcript.document().defaultTextOption()
        option.setTextDirection(default_direction)
        option.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            if default_direction == Qt.LayoutDirection.RightToLeft
            else Qt.AlignmentFlag.AlignLeft
        )
        self.chat_transcript.document().setDefaultTextOption(option)
        block = self.chat_transcript.document().begin()
        while block.isValid():
            block_format = block.blockFormat()
            is_code_block = block_format.hasProperty(
                QTextFormat.Property.BlockCodeFence)
            is_arabic = bool(re.search(
                r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]",
                block.text(),
            ))
            block_direction = (
                Qt.LayoutDirection.LeftToRight
                if is_code_block else
                Qt.LayoutDirection.RightToLeft
                if is_arabic
                or default_direction == Qt.LayoutDirection.RightToLeft
                else default_direction
            )
            block_alignment = (
                Qt.AlignmentFlag.AlignLeft
                if block_direction == Qt.LayoutDirection.LeftToRight else
                Qt.AlignmentFlag.AlignLeft
            )
            block_format.setLayoutDirection(block_direction)
            block_format.setAlignment(block_alignment)
            cursor = QTextCursor(block)
            cursor.setBlockFormat(block_format)
            block = block.next()
        self._apply_script_font_families(self.chat_transcript.document())
        scrollbar = self.chat_transcript.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _orient_chat_html_block(match, direction: str, alignment: str) -> str:
        tag, attributes = match.groups()
        attributes = re.sub(r"\s(?:dir|align)=(['\"])[^'\"]*\1", "", attributes)
        style = f"direction:{direction}; text-align:{alignment};"
        style_match = re.search(r"\sstyle=(['\"])(.*?)\1", attributes)
        if style_match:
            style += f" {style_match.group(2)}"
            attributes = (
                attributes[:style_match.start()]
                + attributes[style_match.end():]
            )
        return (
            f"<{tag}{attributes} dir='{direction}' align='{alignment}' "
            f"style='{style}'>"
            f"{chr(0x202B) if direction == 'rtl' else ''}"
        )

    def _send_chat(self) -> None:
        question = self.chat_input.toPlainText().strip()
        if not question or self._chat_worker is not None:
            return
        if not getattr(self, "_translation_text", "").strip():
            return

        self._chat_messages.append({"role": "user", "content": question})
        self._append_chat_line("You", question)
        self.chat_input.clear()
        self.chat_send_btn.setEnabled(False)
        self.chat_send_btn.setText("…")
        source = self._source_text or "(Original text unavailable; discuss the translation.)"
        worker = ChatWorker(
            self._chat_messages,
            self.settings,
            source,
            self._translation_text,
        )
        self._chat_worker = worker
        self._chat_workers.append(worker)
        worker.done.connect(self._on_chat_done)
        worker.failed.connect(self._on_chat_failed)
        worker.finished.connect(self._on_chat_worker_finished)
        worker.start()

    def _on_chat_done(self, answer: str) -> None:
        worker = self.sender()
        if worker is not self._chat_worker:
            return
        self._chat_messages.append({"role": "assistant", "content": answer})
        self._append_chat_line("Tutor", answer)
        if self._speech_active:
            self.speak_btn.setText("Stop")
            self.speak_btn.setToolTip("Stop speech playback")
        else:
            self.speak_btn.setText(self._speech_idle_label())
            self.speak_btn.setToolTip(
                "Read the latest model answer aloud with Edge TTS")
        self.speak_btn.setEnabled(self._speech_available)

    def _on_chat_failed(self, error: str) -> None:
        worker = self.sender()
        if worker is not self._chat_worker:
            return
        if self._chat_messages and self._chat_messages[-1]["role"] == "user":
            self._chat_messages.pop()
        self._append_chat_line("Error", error)

    def _on_chat_worker_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, ChatWorker):
            if worker in self._chat_workers:
                self._chat_workers.remove(worker)
            if worker is self._chat_worker:
                self._chat_worker = None
                self.chat_send_btn.setEnabled(
                    bool(getattr(self, "_translation_text", "").strip()))
                self.chat_send_btn.setText("Send")
            worker.deleteLater()
        self._continue_pending_close()

    def _build_speed_menu(self) -> None:
        panel = QWidget(self.speed_menu)
        panel.setObjectName("speechSpeedPanel")
        self._speed_panel = panel
        panel.setFixedWidth(420)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(13)

        title = QLabel("Reading speed", panel)
        title.setObjectName("speechSpeedTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self._speed_display = QLabel(panel)
        self._speed_display.setObjectName("speechSpeedValue")
        self._speed_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._speed_display)

        self._speed_slider = QSlider(Qt.Orientation.Horizontal, panel)
        self._speed_slider.setRange(0, 1200)
        self._speed_slider.setSingleStep(1)
        self._speed_slider.setPageStep(200)
        self._speed_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._speed_slider.setTickInterval(40)
        self._speed_slider.setValue(
            self._speed_to_slider_value(self._speech_rate))
        self._speed_slider.setAccessibleName("Reading speed slider")
        self._speed_slider.setMinimumWidth(320)
        layout.addWidget(self._speed_slider)

        self._speed_preset_row = QWidget(panel)
        self._speed_preset_row.setFixedWidth(410)
        self._speed_preset_row.setFixedHeight(34)
        layout.addWidget(
            self._speed_preset_row, 0, Qt.AlignmentFlag.AlignHCenter)
        self._speed_presets: dict[float, QToolButton] = {}
        for value in (0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            button = QToolButton(self._speed_preset_row)
            button.setText(f"{value:g}")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            if value == 0:
                button.setAccessibleName(
                    "Minimum reading speed, 0.25 times, shown as zero")
                button.setToolTip(
                    "Closest available speed to zero: 0.25x; reading continues")
            else:
                button.setAccessibleName(f"{value:.2f} times speed")
            button.setFixedSize(42, 28)
            button.adjustSize()
            button.clicked.connect(
                lambda _checked=False, speed=value: self._select_speed_preset(
                    speed
                )
            )
            self._speed_presets[value] = button

        self._speed_description = QLabel(panel)
        self._speed_description.setObjectName("speechSpeedDescription")
        self._speed_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._speed_description)

        panel.setStyleSheet(
            "QWidget#speechSpeedPanel { background: #f7f8fa; color: #222831; }"
            "QLabel#speechSpeedTitle { color: #222831; font-size: 14px;"
            " font-weight: 600; }"
            "QLabel#speechSpeedValue { color: #222831; font-size: 23px;"
            " font-weight: 700; padding: 2px; }"
            "QLabel#speechSpeedDescription { color: #687789;"
            " font-size: 12px; min-height: 16px; }"
            "QSlider::groove:horizontal { background: #cbd3dd;"
            " height: 5px; border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: #52677f;"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #ffffff;"
            " border: 1px solid #8b98a8; width: 19px; height: 19px;"
            " margin: -8px 0; border-radius: 10px; }"
            "QToolButton { background: #e9edf2; color: #3f4a57;"
            " border: none; border-radius: 16px; padding: 7px 3px;"
            " font-size: 12px; font-weight: 600; }"
            "QToolButton:hover { background: #dfe5ec; }"
            "QToolButton:checked { background: #d5dee8; color: #222831; }"
        )
        self.speed_menu.setStyleSheet(
            "QMenu { background: #f7f8fa; border: 1px solid #d8dde4;"
            " padding: 2px; }"
        )
        action = QWidgetAction(self.speed_menu)
        action.setDefaultWidget(panel)
        self.speed_menu.addAction(action)
        self._update_speed_display(self._speech_rate)
        self._speed_slider_dragging = False
        self._speed_slider.sliderPressed.connect(self._on_speed_slider_pressed)
        self._speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        self._speed_slider.sliderReleased.connect(self._on_speed_slider_released)
        self._position_speed_preset_buttons()
        self._speed_update_timer = QTimer(self)
        self._speed_update_timer.setSingleShot(True)
        self._speed_update_timer.setInterval(250)
        self._speed_update_timer.timeout.connect(self._commit_speed_controls)

    def _update_speed_display(self, speed: float) -> None:
        self._speed_display.setText(f"{speed:.2f}x")
        self._speed_description.setText(
            "Normal" if abs(speed - 1.0) < 0.005 else "Custom speed"
        )
        for value, button in self._speed_presets.items():
            preset_speed = (
                MIN_SPEECH_RATE if value == 0 else value
            )
            button.setChecked(abs(speed - preset_speed) < 0.005)

    def _select_speed_preset(self, speed: float) -> None:
        if speed == 0:
            speed = MIN_SPEECH_RATE
        self._speed_slider.setValue(self._speed_to_slider_value(speed))
        self._commit_speed_controls()

    def _show_speed_menu(self) -> None:
        self.speed_menu.adjustSize()
        self._position_speed_preset_buttons()
        button_top_left = self.speed_btn.mapToGlobal(QPoint(0, 0))
        button_bottom_right = self.speed_btn.mapToGlobal(
            QPoint(self.speed_btn.width(), self.speed_btn.height())
        )
        menu_size = self.speed_menu.sizeHint()
        button_center_x = button_top_left.x() + self.speed_btn.width() // 2
        x = button_center_x - menu_size.width() // 2
        y = button_bottom_right.y()
        screen = self.speed_btn.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(
                available.left(),
                min(x, available.right() - menu_size.width() + 1),
            )
            if y + menu_size.height() > available.bottom() + 1:
                y = button_top_left.y() - menu_size.height()
            y = max(
                available.top(),
                min(y, available.bottom() - menu_size.height() + 1),
            )
        self.speed_menu.popup(QPoint(x, y))

    def _style_speed_button(self) -> None:
        colors = _POPUP_COLORS
        self.speed_btn.setStyleSheet(
            "QToolButton { "
            f"background-color: {colors['surface']}; "
            f"border: 1px solid {colors['border']}; "
            f"border-radius: 4px; padding: 0; font-size: 16px; "
            f"color: {colors['text']}; }}"
            "QToolButton:hover, QToolButton:pressed { "
            f"background-color: {colors['surface_hover']}; "
            f"color: {colors['text']}; }}"
        )

    def _position_speed_preset_buttons(self) -> None:
        slider = self._speed_slider
        row = self._speed_preset_row
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        for speed, button in self._speed_presets.items():
            preset_speed = MIN_SPEECH_RATE if speed == 0 else speed
            value = self._speed_to_slider_value(preset_speed)
            option.sliderPosition = value
            option.sliderValue = value
            handle = slider.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                slider,
            )
            center_x = slider.x() - row.x() + handle.center().x()
            button.move(
                center_x - button.width() // 2,
                (row.height() - button.height()) // 2,
            )

    @staticmethod
    def _speed_to_slider_value(speed: float) -> int:
        anchors = (
            (MIN_SPEECH_RATE, 0),
            (0.5, 200),
            (0.75, 400),
            (1.0, 600),
            (1.25, 800),
            (1.5, 1000),
            (MAX_SPEECH_RATE, 1200),
        )
        speed = normalize_speed(speed)
        for (low_speed, low_position), (high_speed, high_position) in zip(
            anchors, anchors[1:]
        ):
            if speed <= high_speed:
                fraction = (speed - low_speed) / (high_speed - low_speed)
                return round(
                    low_position + fraction * (high_position - low_position)
                )
        return 1200

    @staticmethod
    def _speed_from_slider_value(position: int) -> float:
        anchors = (
            (0, MIN_SPEECH_RATE),
            (200, 0.5),
            (400, 0.75),
            (600, 1.0),
            (800, 1.25),
            (1000, 1.5),
            (1200, MAX_SPEECH_RATE),
        )
        position = min(1200, max(0, position))
        for (low_position, low_speed), (high_position, high_speed) in zip(
            anchors, anchors[1:]
        ):
            if position <= high_position:
                fraction = (
                    (position - low_position)
                    / (high_position - low_position)
                )
                return normalize_speed(
                    low_speed + fraction * (high_speed - low_speed)
                )
        return MAX_SPEECH_RATE

    def _on_speed_slider_pressed(self) -> None:
        self._speed_slider_dragging = True

    def _on_speed_slider_released(self) -> None:
        self._speed_slider_dragging = False
        self._commit_speed_controls()

    def _on_speed_slider_changed(self, value: int) -> None:
        speed = self._speed_from_slider_value(value)
        snapped = (
            snap_speed_to_quarter(speed)
            if self._speed_slider_dragging else normalize_speed(speed)
        )
        slider_value = self._speed_to_slider_value(snapped)
        if slider_value != value:
            self._speed_slider.blockSignals(True)
            self._speed_slider.setValue(slider_value)
            self._speed_slider.blockSignals(False)
        self._update_speed_display(snapped)
        self._speed_update_timer.start()

    def _commit_speed_controls(self) -> None:
        self._speed_update_timer.stop()
        self._set_speech_rate(
            self._speed_from_slider_value(self._speed_slider.value()))

    def _set_speech_rate(self, multiplier: float) -> None:
        """Remember the speed and adjust active playback without restarting."""
        self._speech_rate = normalize_speed(multiplier)
        if hasattr(self, "_speed_slider"):
            self._speed_slider.blockSignals(True)
            self._speed_slider.setValue(
                self._speed_to_slider_value(self._speech_rate))
            self._speed_slider.blockSignals(False)
        if hasattr(self, "_speed_presets"):
            self._update_speed_display(self._speech_rate)
        self.settings["speech_rate"] = self._speech_rate
        self._player.setPlaybackRate(self._speech_rate)
        try:
            from config.settings import load, save
            persisted = load()
            persisted["speech_rate"] = self._speech_rate
            save(persisted)
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save the reading speed")
        self.speed_btn.setToolTip(f"Reading speed: {self._speech_rate:.2f}x")

    def _load_voices(self) -> None:
        if self._voice_list_worker is not None or self._voice_actions:
            return
        self.voice_menu.clear()
        loading = self.voice_menu.addAction("Loading voices…")
        loading.setEnabled(False)
        self._voice_list_worker = VoiceListWorker(self)
        self._voice_list_worker.voices_ready.connect(self._on_voice_list_ready)
        self._voice_list_worker.failed.connect(self._on_voice_list_failed)
        self._voice_list_worker.finished.connect(self._on_voice_list_finished)
        self._voice_list_worker.start()

    def _show_voice_menu(self) -> None:
        self.voice_menu.adjustSize()
        button_top_left = self.voice_btn.mapToGlobal(QPoint(0, 0))
        button_bottom_right = self.voice_btn.mapToGlobal(
            QPoint(self.voice_btn.width(), self.voice_btn.height())
        )
        menu_width = self.voice_menu.sizeHint().width()
        menu_height = self.voice_menu.sizeHint().height()
        screen = self.voice_btn.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left(), button_top_left.x() - menu_width)
            y = min(
                max(available.top(), button_bottom_right.y() - menu_height),
                available.bottom() - menu_height + 1,
            )
        else:
            x = button_top_left.x() - menu_width
            y = button_bottom_right.y() - menu_height
        self.voice_menu.popup(QPoint(x, y))

    def _on_voice_list_ready(self, voices: list[dict]) -> None:
        from .speech_worker import EDGE_TTS_PREFERRED_LANGUAGE_CODES

        self.voice_menu.clear()
        grouped: dict[str, dict[str, list[dict]]] = {}
        multilingual: dict[str, list[dict]] = {}
        for voice in voices:
            short_name = voice.get("ShortName")
            locale = voice.get("Locale")
            if not isinstance(short_name, str) or not isinstance(locale, str):
                logging.warning("Ignoring malformed Edge TTS voice entry: %r", voice)
                continue
            language, separator, _region = locale.partition("-")
            if not separator:
                logging.warning("Ignoring Edge TTS voice with invalid locale: %r", voice)
                continue
            country = self._voice_country_name(voice, locale)
            if "multilingual" in short_name.casefold():
                multilingual.setdefault(country, []).append(voice)
            if language.casefold() in EDGE_TTS_PREFERRED_LANGUAGE_CODES:
                grouped.setdefault(language.title(), {}).setdefault(country, []).append(voice)
        if not grouped and not multilingual:
            self._on_voice_list_failed("The voice list did not contain usable voices.")
            return

        self._voice_actions = []
        preferred_language_order = ("Fr", "Ar", "It", "En", "De", "Es", "Ru")
        language_order = [
            language
            for language in preferred_language_order
            if language in grouped
        ]
        for language in language_order:
            if language not in grouped:
                continue
            language_menu = self.voice_menu.addMenu(language)
            language_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            self._populate_voice_country_menus(language_menu, grouped[language])
        if multilingual:
            self.voice_menu.addSeparator()
            multilingual_menu = self.voice_menu.addMenu("Multilingual")
            multilingual_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            self._populate_voice_country_menus(multilingual_menu, multilingual)

    @staticmethod
    def _voice_country_name(voice: dict, locale: str) -> str:
        friendly_name = voice.get("FriendlyName")
        if isinstance(friendly_name, str):
            country = re.search(r"\(([^()]*)\)\s*$", friendly_name)
            if country:
                return country.group(1)
        return locale.partition("-")[2].upper() or locale

    def _populate_voice_country_menus(
        self, parent_menu: QMenu, countries: dict[str, list[dict]]
    ) -> None:
        for country in sorted(countries):
            country_menu = parent_menu.addMenu(country)
            country_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            for voice in sorted(
                countries[country],
                key=lambda item: str(item.get("FriendlyName") or item["ShortName"]),
            ):
                short_name = voice["ShortName"]
                label = str(voice.get("FriendlyName") or short_name)
                gender = voice.get("Gender")
                if gender:
                    label = f"{label} ({gender})"
                if short_name == self._speech_voice:
                    self.voice_btn.setToolTip(f"Voice: {label}")
                action = country_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(short_name == self._speech_voice)
                action.setData(short_name)
                action.triggered.connect(
                    lambda _checked=False, name=short_name, display=label:
                        self._set_speech_voice(name, display)
                )
                self._voice_actions.append(action)

    def _on_voice_list_failed(self, error: str) -> None:
        logging.warning("Could not load Edge TTS voices: %s", error)
        self.voice_menu.clear()
        failure = self.voice_menu.addAction(f"Could not load voices: {error}")
        failure.setEnabled(False)
        retry = self.voice_menu.addAction("Retry")
        retry.triggered.connect(self._load_voices)
        self.voice_btn.setToolTip(f"Could not load voices: {error}")

    def _on_voice_list_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, VoiceListWorker):
            if worker is self._voice_list_worker:
                self._voice_list_worker = None
            worker.deleteLater()
        self._continue_pending_close()

    def _set_speech_voice(self, voice: str, display_name: str) -> None:
        self._speech_voice = voice
        self.settings["speech_voice"] = voice
        self._speech_prefetch_timer.stop()
        self._cancel_speech_prefetch()
        for action in self._voice_actions:
            action.setChecked(action.data() == voice)
        self.voice_btn.setToolTip(f"Voice: {display_name}")
        try:
            from config.settings import load, save
            persisted = load()
            persisted["speech_voice"] = voice
            save(persisted)
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save the reading voice")
        if self._speech_active:
            self._request_speech(self._current_speech_offset())
        else:
            # Pre-warm the newly chosen voice so the next Read is instant.
            self._schedule_speech_prefetch()

    def _current_speech_offset(self) -> int:
        position_ticks = self._player.position() * 10_000
        boundaries = self._speech_boundaries.get(self._speech_index, [])
        next_start: int | None = None
        for start, duration, source_start, _ in boundaries:
            if start <= position_ticks < start + duration:
                return source_start
            if start > position_ticks:
                next_start = source_start
                break
        return next_start if next_start is not None else self._speech_start_offset

    def _toggle_speech(self) -> None:
        if not self._speech_available:
            # Nothing should be able to start a thread that cannot work.
            self.speak_btn.setText("No speech engine")
            self.spin.setText(
                speech_missing_message(self._speech_engine).replace("\n", " "))
            return
        if self._speech_active:
            self._speech_start_offset = self._current_speech_offset()
            self._speech_active = False
            self._pending_speech_offset = None
            self._pending_highlight = None
            self._close_speech_stream_server()
            self._speech_stream_urls.clear()
            self._speech_playing_stream_index = None
            self._clear_speech_highlight()
            self._player.stop()
            self._pending_player_position_ms = None
            self._pending_player_source = None
            self._player.setSource(QUrl())
            if self._speech_worker is not None and self._speech_worker.isRunning():
                self._speech_worker.requestInterruption()
                self.speak_btn.setEnabled(
                    bool(self._current_speech_text().strip())
                    and self._speech_available)
                self.speak_btn.setText(self._speech_idle_label())
            else:
                self._clear_speech_files()
                self._speech_text_override = None
                self.speak_btn.setText(self._speech_idle_label())
                self.speak_btn.setEnabled(
                    bool(self._current_speech_text().strip())
                    and self._speech_available)
            self.spin.hide()
            return

        self._speech_text_override = (
            self._markdown_speech_text(self._latest_chat_answer())
            if not self.chat_panel.isHidden() else None
        )
        self._request_speech(
            0 if self._speech_text_override is not None
            else self._speech_start_offset
        )

    def _on_translation_position_clicked(self, position: int) -> None:
        self._speech_available = speech_available(
            self._speech_engine,
            self.settings.get("gemini_key", ""),
        )
        if not self._speech_available:
            self.speak_btn.setEnabled(False)
            self.spin.setText(speech_missing_message(self._speech_engine))
            self.spin.show()
            return
        self._speech_text_override = None
        text = self.result.toPlainText()
        position = qt_to_python_position(text, position)
        self._speech_start_offset = speech_start_offset(text, position)
        if self._speech_start_offset < len(text):
            if not self._speech_active or self._speech_trace is None:
                self._speech_trace = SpeechTrace("translation-word-click")
            self._speech_trace.mark(
                "translation_word_clicked",
                once=False,
                position=self._speech_start_offset,
            )
            word_start, word_end = speech_word_span(
                text, self._speech_start_offset)
            self._pending_highlight = (word_start, word_end)
            if self._speech_active:
                self._pending_speech_offset = self._speech_start_offset
                self._speech_waiting = True
                self._player.pause()
                if self._play_pending_speech_offset():
                    self._highlight_translation_range(word_start, word_end)
                    self._speech_trace.mark(
                        "click_highlight_applied", once=False)
                    return
                if (
                    self._speech_worker is None
                    or not self._speech_worker.isRunning()
                ):
                    self._pending_speech_offset = None
                    self._speech_waiting = False
            self._highlight_translation_range(word_start, word_end)
            self._speech_trace.mark(
                "click_highlight_applied", once=False)
            if not self._seek_in_cached_read(self._speech_start_offset):
                self._request_speech(
                    self._speech_start_offset,
                    trigger="translation-word-click")

    def _seek_in_cached_read(self, offset: int) -> bool:
        """Resume from a clicked word using the cached full read, if present.

        Re-synthesising from the click costs a full network round-trip
        (~2s of silence). But the complete read is usually already on disk:
        either the user has read it, or the background prefetch made it.
        Playing that audio at the clicked word's timestamp answers the click
        in milliseconds, with the highlight driven by the same boundaries.
        Anything unresolved falls back to the normal restart path.
        """
        if (
            self._speech_engine != "edge"
            or not self._speech_available
            or not self.settings.get("fast_mode", True)
            or self._speech_text_override is not None
            or any(
                path != self._speech_seek_path
                for path in self._speech_paths.values()
            )
            or self._close_pending
        ):
            return False
        raw = self._speech_text_from_document(self.result.document())
        text = raw.lstrip()
        if not text:
            return False
        source_offset = len(raw) - len(text)
        kwargs = self._speech_worker_kwargs(text, source_offset)
        probe = SpeechWorker(
            text,
            None,
            **kwargs,
            continuous_stream=True,
            stream_audio=False,
        )
        if len(probe._segments) != 1:
            return False
        segment = probe._segments[0]
        spans = probe._segment_word_spans[0]
        synthesis_text = _speech_text_for_synthesis(
            segment, kwargs["target_language"])
        voice = probe._segment_voices[0]
        key = _cache_key(
            synthesis_text,
            voice,
            probe._rate,
            ssml_language=probe._ssml_language_for_segment(
                synthesis_text, voice),
            engine=self._speech_engine,
        )
        hit = SpeechWorker._read_cache(key)
        if hit is None:
            return False
        audio, cached_boundaries = hit
        if not audio or not all(
            0 <= first < last <= len(spans)
            for _, _, first, last in cached_boundaries
        ):
            return False
        boundaries = [
            (
                start,
                duration,
                spans[first_word][0] + source_offset,
                spans[last_word - 1][1] + source_offset,
            )
            for start, duration, first_word, last_word in cached_boundaries
        ]
        tick = None
        for start, _duration, word_start, word_end in boundaries:
            if word_start <= offset < word_end:
                tick = start
                break
        if tick is None:
            return False
        worker = self._speech_worker
        if worker is not None:
            # Detach first: the in-flight synthesis is obsolete, and its
            # queued finish must land as a harmless no-op instead of
            # resetting buttons or clearing the state set up below.
            self._speech_worker = None
            if worker.isRunning():
                worker.requestInterruption()
        self._player.stop()
        self._pending_player_position_ms = None
        self._pending_player_source = None
        # Stage the cached audio as a plain temp file and play THAT: files
        # seek natively, join the existing temp-file lifecycle, and never
        # block the UI the way pushing megabytes through a live stream can.
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                prefix="harmony-speech-seek",
                suffix=".mp3",
                delete=False,
            ) as tmp:
                tmp.write(audio)
                seek_path = tmp.name
        except OSError:
            return False
        previous_seek = self._speech_seek_path
        if previous_seek is not None and previous_seek != seek_path:
            self._stale_speech_paths.append(previous_seek)
        self._speech_seek_path = seek_path
        self._speech_active = True
        self._speech_waiting = False
        self._speech_error = None
        self._speech_index = 0
        self._speech_boundaries = {0: boundaries}
        self._speech_paths[0] = seek_path
        self._speech_start_offset = offset
        self._pending_speech_offset = None
        self._pending_highlight = None
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "click_seek_cached", once=False, offset=offset)
        self._play_speech_segment(seek_path, tick // 10_000)
        return True

    def _play_pending_speech_offset(self) -> bool:
        offset = self._pending_speech_offset
        if offset is None:
            return False
        for index, boundaries in self._speech_boundaries.items():
            path = self._speech_paths.get(index)
            if path is None:
                continue
            for boundary in boundaries:
                start, _, source_start, source_end = boundary
                if source_start <= offset < source_end:
                    self._pending_speech_offset = None
                    # Seeking inside measured audio: its own boundaries drive
                    # the highlight from here.
                    self._pending_highlight = None
                    self._speech_index = index
                    self._play_speech_segment(
                        path, start // 10_000)
                    return True
        return False

    def _request_speech(
        self, start_offset: int, *, trigger: str = "speech-request"
    ) -> None:
        self._speech_prefetch_timer.stop()
        self._cancel_speech_prefetch()
        if trigger != "translation-word-click":
            self._speech_trace = SpeechTrace(trigger)
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "speech_request_received",
                once=False,
                offset=start_offset,
            )
        worker = self._speech_worker
        if worker is not None and worker.isRunning():
            self._pending_speech_offset = start_offset
            self._speech_active = False
            if trigger != "translation-word-click":
                self._clear_speech_highlight()
            self._player.stop()
            self._pending_player_position_ms = None
            self._pending_player_source = None
            self._player.setSource(QUrl())
            worker.requestInterruption()
            self._close_speech_stream_server()
            self.speak_btn.setEnabled(False)
            self.speak_btn.setText("Restarting from here…")
            self.spin.setText("Restarting speech from selected text…")
            self.spin.show()
            return

        self._begin_speech(start_offset)

    def _speech_worker_kwargs(self, text: str, source_offset: int) -> dict:
        """Keyword arguments both speech workers are built from.

        The background prefetch and the Read press must build the *identical*
        synthesis request, or the prefetch fills a cache entry the read never
        looks up. One builder keeps them in lockstep.
        """
        return {
            "rate": DEFAULT_SPEECH_RATE,
            "source_offset": source_offset,
            "voice": self._speech_voice,
            "automatic_voice": bool(
                self.settings.get("automatic_speech_voice", False)
            ),
            "target_language": self.settings.get("target_language", "ar"),
            "read_latin_words": bool(
                self.settings.get("speech_read_latin_words", False)
            ),
            "fast": bool(self.settings.get("fast_mode", True)),
            "engine": self._speech_engine,
            "gemini_api_key": str(self.settings.get("gemini_key") or ""),
            "gemini_voice": str(
                self.settings.get("gemini_tts_voice") or "Kore"),
        }

    def _try_play_cached_full_text(self, text: str) -> bool:
        """Start playback directly from the prefetched disk cache if present.

        The background prefetch already synthesises the fresh translation into
        the cache as soon as it lands. When the user presses Read aloud for the
        whole text, the identical request is usually cached; re-running
        synthesis would cost a full network round-trip (~2s of silence).
        Instead we replay the cached segment in ~0.01s with the same word
        boundaries, so the first word is heard instantly.
        """
        if (
            self._speech_engine != "edge"
            or not self._speech_available
            or not self.settings.get("fast_mode", True)
            or self._speech_text_override is not None
            or self._close_pending
        ):
            return False
        kwargs = self._speech_worker_kwargs(text, 0)
        probe = SpeechWorker(
            text,
            None,
            **kwargs,
            continuous_stream=True,
            stream_audio=False,
        )
        if len(probe._segments) != 1:
            return False
        segment = probe._segments[0]
        spans = probe._segment_word_spans[0]
        synthesis_text = _speech_text_for_synthesis(
            segment, kwargs["target_language"])
        voice = probe._segment_voices[0]
        key = _cache_key(
            synthesis_text,
            voice,
            probe._rate,
            ssml_language=probe._ssml_language_for_segment(
                synthesis_text, voice),
            engine=self._speech_engine,
        )
        hit = SpeechWorker._read_cache(key)
        if hit is None:
            return False
        audio, cached_boundaries = hit
        if not audio or not all(
            0 <= first < last <= len(spans)
            for _, _, first, last in cached_boundaries
        ):
            return False
        boundaries = [
            (
                start,
                duration,
                spans[first_word][0],
                spans[last_word - 1][1],
            )
            for start, duration, first_word, last_word in cached_boundaries
        ]
        # Detach first: the in-flight synthesis is obsolete, and its queued
        # finish must land as a harmless no-op instead of resetting the state
        # set up below.
        worker = self._speech_worker
        if worker is not None:
            self._speech_worker = None
            if worker.isRunning():
                worker.requestInterruption()
        self._player.stop()
        self._pending_player_position_ms = None
        self._pending_player_source = None
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                prefix="harmony-speech-read",
                suffix=".mp3",
                delete=False,
            ) as tmp:
                tmp.write(audio)
                read_path = tmp.name
        except OSError:
            return False
        previous_read = getattr(self, "_speech_read_path", None)
        if previous_read is not None and previous_read != read_path:
            self._stale_speech_paths.append(previous_read)
        self._speech_read_path = read_path
        self._speech_active = True
        self._speech_waiting = False
        self._speech_error = None
        self._speech_index = 0
        self._speech_boundaries = {0: boundaries}
        self._speech_paths[0] = read_path
        self._speech_start_offset = 0
        self._pending_speech_offset = None
        self._pending_highlight = None
        if self._speech_trace is not None:
            self._speech_trace.mark("read_cached", once=False)
        self._play_speech_segment(read_path, 0)
        return True

    def _begin_speech(self, start_offset: int) -> None:
        self._speech_prefetch_timer.stop()
        if self._speech_active:
            self._player.stop()
            self._pending_player_position_ms = None
            self._pending_player_source = None
            self._player.setSource(QUrl())
        self._close_speech_stream_server()
        self._clear_speech_files()
        self._speech_active = True
        self._speech_waiting = False
        self._speech_error = None
        self._speech_index = 0
        self._speech_stream_urls.clear()
        self._speech_playing_stream_index = None
        self._speech_boundaries.clear()
        self._clear_speech_highlight()
        text = (
            self._speech_text_override
            if self._speech_text_override is not None
            else self._speech_text_from_document(self.result.document())
        )
        if not text.strip():
            if self._speech_trace is not None:
                self._speech_trace.mark("speech_empty_input")
            self._speech_active = False
            return
        text = text[start_offset:]
        leading_whitespace = len(text) - len(text.lstrip())
        text = text.lstrip()
        if not text:
            if self._speech_trace is not None:
                self._speech_trace.mark("speech_empty_input")
            self._speech_active = False
            return
        source_offset = start_offset + leading_whitespace
        self._speech_start_offset = source_offset
        # Start playback from the prefetched cache immediately if this is the
        # first read of the current translation. The background prefetch fills
        # the cache as soon as the translation lands, so the identical request
        # is almost always cached; re-running synthesis would cost a full
        # network round-trip (~2s of silence).
        if (
            start_offset == 0
            and self._speech_text_override is None
            and self._try_play_cached_full_text(text)
        ):
            return
        # A fresh press always starts its own synthesis. Attaching the player
        # to a live background synthesis mid-flight (adopt) proved unstable
        # on real audio backends, so it was removed; the disk cache still
        # makes repeat reads instant.
        self._cancel_speech_prefetch()
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "speech_preparation_started",
                once=False,
                offset=source_offset,
                characters=len(text),
                target_language=self.settings.get("target_language", "ar"),
                speech_engine=self._speech_engine,
                voice=(
                    self.settings.get("gemini_tts_voice", "Kore")
                    if self._speech_engine == GEMINI_TTS_ENGINE
                    else self._speech_voice
                ),
                read_latin_words=bool(
                    self.settings.get("speech_read_latin_words", False)),
                automatic_voice=bool(
                    self.settings.get("automatic_speech_voice", False)),
            )
        self.spin.setText("Preparing speech…")
        self.spin.show()
        self.speak_btn.setEnabled(False)
        self.speak_btn.setText("Preparing speech…")
        try:
            stream_server = (
                self._create_speech_stream_server()
                if self._speech_engine == "edge"
                else None
            )
        except (OSError, RuntimeError) as error:
            if self._speech_trace is not None:
                self._speech_trace.mark(
                    "audio_stream_server_creation_failed",
                    error_type=type(error).__name__,
                )
            self._finish_speech(f"Audio playback failed: {error}")
            return
        kwargs = self._speech_worker_kwargs(text, source_offset)
        self._speech_worker = SpeechWorker(
            text,
            self,
            **kwargs,
            stream_server=stream_server,
            continuous_stream=True,
            stream_audio=False,
            trace=self._speech_trace,
        )
        self._speech_worker.segment_stream_ready.connect(
            self._on_speech_stream_ready)
        self._speech_worker.word_boundary_ready.connect(
            self._on_speech_word_boundary_ready)
        self._speech_worker.word_boundaries.connect(self._on_speech_word_boundaries)
        self._speech_worker.segment_ready.connect(self._on_speech_segment_ready)
        self._speech_worker.failed.connect(self._on_speech_failed)
        self._speech_worker.finished.connect(self._on_speech_worker_finished)
        self._speech_worker.start()
        if self._pending_highlight is not None:
            # Restarting from a clicked word wipes the measured boundaries;
            # keep that word lit until the new synthesis reports its own.
            self._highlight_translation_range(*self._pending_highlight)

    def _create_speech_stream_server(self) -> AudioStreamServer:
        server = AudioStreamServer()
        self._speech_stream_server = server
        return server

    def _schedule_speech_prefetch(self) -> None:
        """Queue a background synthesis of the fresh translation.

        Debounced: every new translation restarts the countdown, so fast
        page-flips never pile up syntheses. Firing only re-derives the
        *current* text, so a stale countdown harmlessly prefetches whatever
        is on screen.
        """
        self._speech_prefetch_timer.start()

    def _start_speech_prefetch(self) -> None:
        """Synthesise the fresh translation into the disk cache, silently.

        A cache hit replays in ~0.01s, which is what makes Read Aloud start
        fast. Deliberately `prefetch_only` with no stream server: the worker
        never touches the player path, so background synthesis cannot
        disturb (or crash) playback. Anything this skips (unavailable
        engine, huge text, chat answers, an in-progress read) simply falls
        back to streaming speed.
        """
        self._speech_prefetch_timer.stop()
        if (
            not self._speech_available
            or self._speech_engine != "edge"
            or not self.settings.get("fast_mode", True)
            or not self.chat_panel.isHidden()
            or not self.isVisible()
            or self._speech_text_override is not None
            or self._speech_active
        ):
            return
        raw = self._speech_text_from_document(self.result.document())
        text = raw.lstrip()
        if not text or len(text) > SPEECH_PREFETCH_MAX_CHARS:
            return
        source_offset = len(raw) - len(text)
        kwargs = self._speech_worker_kwargs(text, source_offset)
        key = (
            text,
            kwargs["voice"],
            kwargs["automatic_voice"],
            kwargs["target_language"],
            kwargs["read_latin_words"],
        )
        if key == self._speech_prefetch_key:
            return
        self._cancel_speech_prefetch()
        trace = SpeechTrace("speech-prefetch")
        kwargs["fast"] = True
        worker = SpeechWorker(
            text,
            self,
            **kwargs,
            continuous_stream=True,
            stream_audio=False,
            prefetch_only=True,
            trace=trace,
        )
        self._speech_prefetch_worker = worker
        self._speech_prefetch_workers.append(worker)
        self._speech_prefetch_key = key
        worker.failed.connect(self._on_speech_prefetch_failed)
        worker.finished.connect(self._on_speech_prefetch_finished)
        worker.start()

    def _cancel_speech_prefetch(self) -> None:
        worker = self._speech_prefetch_worker
        self._speech_prefetch_worker = None
        self._speech_prefetch_key = None
        if worker is not None:
            if worker.isRunning():
                worker.requestInterruption()
                return
            worker.cleanup_output()
            worker.deleteLater()
            if worker in self._speech_prefetch_workers:
                self._speech_prefetch_workers.remove(worker)

    def _on_speech_prefetch_failed(self, error: str) -> None:
        logging.warning("Speech prefetch failed: %s", error)

    def _on_speech_prefetch_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, SpeechWorker):
            return
        if worker is not self._speech_prefetch_worker:
            worker.cleanup_output()
            worker.deleteLater()
            if worker in self._speech_prefetch_workers:
                self._speech_prefetch_workers.remove(worker)
            self._continue_pending_close()
            return
        # A finished prefetch already wrote the disk cache; drop the worker,
        # keep the cache. A later prefetch for the same text is a cheap
        # cache hit, not a new synthesis.
        self._cancel_speech_prefetch()
        self._continue_pending_close()

    def _on_speech_segment_ready(self, index: int, path: str) -> None:
        worker = self.sender()
        if worker is not None and worker is not self._speech_worker:
            from pathlib import Path
            Path(path).unlink(missing_ok=True)
            return
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "speech_segment_ready", once=False, segment=index)
        if not self._speech_active:
            from pathlib import Path
            Path(path).unlink(missing_ok=True)
            return
        self._speech_paths[index] = path
        if self._play_pending_speech_offset():
            return
        if (
            self._speech_active
            and index == self._speech_index
            and self._speech_playing_stream_index != index
        ):
            self._play_ready_speech_segment(index)

    def _on_speech_stream_ready(self, index: int, url: str) -> None:
        worker = self.sender()
        if worker is not None and worker is not self._speech_worker:
            return
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "speech_stream_ready", once=False, segment=index)
        if not self._speech_active:
            return
        self._speech_stream_urls[index] = url
        if (
            index == self._speech_index
            and self._pending_speech_offset is None
            and (
                self._speech_waiting
                or self._player.playbackState()
                != QMediaPlayer.PlaybackState.PlayingState
            )
        ):
            self._play_ready_speech_segment(index)

    def _on_speech_word_boundary_ready(
        self, index: int, boundary: tuple[int, int, int, int]
    ) -> None:
        worker = self.sender()
        if self._speech_active and (
            worker is None or worker is self._speech_worker
        ):
            self._speech_boundaries.setdefault(index, []).append(boundary)

    def _on_speech_word_boundaries(
        self, index: int, boundaries: list[tuple[int, int, int, int]]
    ) -> None:
        worker = self.sender()
        if self._speech_active and (
            worker is None or worker is self._speech_worker
        ):
            self._speech_boundaries[index] = boundaries

    def _play_ready_speech_segment(self, index: int) -> None:
        stream_url = self._speech_stream_urls.get(index)
        if stream_url is not None:
            self._speech_playing_stream_index = index
            self._play_speech_source(QUrl(stream_url))
        else:
            path = self._speech_paths.get(index)
            if path is not None:
                self._play_speech_segment(path)

    def _play_speech_segment(self, path: str, position_ms: int = 0) -> None:
        self._speech_playing_stream_index = None
        self._play_speech_source(QUrl.fromLocalFile(path), position_ms)

    def _play_speech_source(self, source: QUrl, position_ms: int = 0) -> None:
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "player_source_requested",
                once=False,
                source_kind="stream" if source.isLocalFile() is False else "file",
                position_ms=position_ms,
            )
        self._speech_waiting = False
        self.spin.hide()
        self.speak_btn.setEnabled(True)
        self.speak_btn.setText("Stop")
        self._player.setPlaybackRate(self._speech_rate)
        if self._player.source() == source and self._player.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._pending_player_position_ms = None
            self._pending_player_source = None
            self._player.setPosition(position_ms)
            self._player.play()
        else:
            self._pending_player_position_ms = position_ms or None
            self._pending_player_source = source if position_ms else None
            if position_ms:
                self._player.stop()
                self._player.setSource(source)
                self._player.play()
            else:
                self._player.setSource(source)
                self._player.play()
        self._speech_highlight_timer.start()
        self._speech_trace_sample_ns = 0
        self._update_speech_highlight()

    def _update_speech_highlight(self) -> None:
        if not self._speech_active:
            self._clear_speech_highlight()
            return
        self._trace_speech_playback_sample()
        boundaries = self._speech_boundaries.get(self._speech_index, [])
        if not boundaries:
            # No measured words yet (e.g. restarting from a clicked word):
            # hold the requested word instead of flashing an empty highlight.
            # (Chat-panel reads highlight the transcript once audio reports
            # in, so the placeholder only applies to the translation view.)
            if (
                self._pending_highlight is not None
                and self._speech_text_override is None
            ):
                self._highlight_translation_range(*self._pending_highlight)
            else:
                self.result.setExtraSelections([])
                self.chat_transcript.setExtraSelections([])
            return
        # Fresh measurements take over from any clicked-word placeholder.
        self._pending_highlight = None

        if self._pending_player_position_ms is not None:
            # A seek was requested but the player has not reported it yet:
            # highlight where the audio is going, not the stale position.
            position_ticks = self._pending_player_position_ms * 10_000
        else:
            position_ticks = self._player.position() * 10_000
        active_boundary = None
        previous_boundary = None
        for boundary in boundaries:
            if boundary[0] > position_ticks:
                break
            if position_ticks < boundary[0] + boundary[1]:
                active_boundary = boundary
                break
            previous_boundary = boundary
        if active_boundary is None:
            if previous_boundary is None:
                # Before the first spoken word: nothing to hold yet.
                self.result.setExtraSelections([])
                self.chat_transcript.setExtraSelections([])
                return
            # Between words (a natural pause): keep the word just spoken so
            # the highlight walks with the voice instead of blinking out.
            active_boundary = previous_boundary

        _, _, start, end = active_boundary
        if (
            self._speech_text_override is not None
            and self._chat_speech_document_start is not None
        ):
            cursor = QTextCursor(self.chat_transcript.document())
            cursor.setPosition(
                self._chat_speech_document_start
                + python_to_qt_position(self._speech_text_override, start)
            )
            cursor.setPosition(
                self._chat_speech_document_start
                + python_to_qt_position(self._speech_text_override, end),
                QTextCursor.MoveMode.KeepAnchor,
            )
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            text_format = QTextCharFormat()
            text_format.setBackground(QColor("#B3E5FC"))
            selection.format = text_format
            self.chat_transcript.setExtraSelections([selection])
            self.result.setExtraSelections([])
            self._keep_chat_speech_highlight_visible(cursor)
            return

        self._highlight_translation_range(start, end)

    def _trace_speech_playback_sample(self) -> None:
        trace = self._speech_trace
        if trace is None:
            return
        now_ns = time.perf_counter_ns()
        if now_ns - self._speech_trace_sample_ns < 250_000_000:
            return
        self._speech_trace_sample_ns = now_ns
        playback_state = self._player.playbackState()
        media_status = self._player.mediaStatus()
        trace.mark(
            "playback_sample",
            once=False,
            segment=self._speech_index,
            position_ms=self._player.position(),
            state=getattr(playback_state, "name", str(playback_state)),
            media_status=getattr(media_status, "name", str(media_status)),
            buffer_progress=round(self._player.bufferProgress(), 3),
            waiting=self._speech_waiting,
        )

    def _highlight_translation_range(self, start: int, end: int) -> None:
        source_text = self.result.toPlainText()
        cursor = self.result.textCursor()
        cursor.setPosition(python_to_qt_position(source_text, start))
        cursor.setPosition(
            python_to_qt_position(source_text, end),
            QTextCursor.MoveMode.KeepAnchor,
        )
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        text_format = QTextCharFormat()
        text_format.setBackground(QColor("#fff176"))
        selection.format = text_format
        self.result.setExtraSelections([selection])
        self._keep_speech_highlight_visible(cursor)

    def _keep_speech_highlight_visible(self, cursor) -> None:
        if self._speech_auto_scroll_suspended:
            return
        viewport = self.result.viewport()
        word_rect = self.result.cursorRect(cursor)
        scrollbar = self.result.verticalScrollBar()
        midpoint = viewport.height() // 2
        if not self._speech_follow_scroll_active:
            if word_rect.center().y() < midpoint:
                return
            self._speech_follow_scroll_active = True
        distance = word_rect.center().y() - midpoint
        if distance:
            scrollbar.setValue(
                scrollbar.value() + max(-18, min(18, distance)))

    def _keep_chat_speech_highlight_visible(self, cursor) -> None:
        if self._speech_auto_scroll_suspended:
            return
        viewport = self.chat_transcript.viewport()
        word_rect = self.chat_transcript.cursorRect(cursor)
        margin = min(28, max(8, viewport.height() // 8))
        scrollbar = self.chat_transcript.verticalScrollBar()
        if word_rect.top() < margin:
            scrollbar.setValue(scrollbar.value() + word_rect.top() - margin)
        elif word_rect.bottom() > viewport.height() - margin:
            scrollbar.setValue(
                scrollbar.value() + word_rect.bottom() - viewport.height() + margin
            )

    def eventFilter(self, watched, event) -> bool:
        if hasattr(self, "chat_transcript") and watched in (
                self.result, self.result.viewport(),
                self.result.verticalScrollBar(), self.chat_transcript,
                self.chat_transcript.viewport(),
                self.chat_transcript.verticalScrollBar()):
            event_type = event.type()
            if event_type in (
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
                QEvent.Type.TouchBegin,
                QEvent.Type.TouchUpdate,
            ):
                self._suspend_speech_auto_scroll()
            elif (
                event_type == QEvent.Type.MouseMove
                and event.buttons() != Qt.MouseButton.NoButton
            ):
                self._suspend_speech_auto_scroll()
        return super().eventFilter(watched, event)

    def _suspend_speech_auto_scroll(self) -> None:
        self._speech_auto_scroll_suspended = True
        self._speech_follow_scroll_active = False
        self._speech_auto_scroll_timer.start()

    def _resume_speech_auto_scroll(self) -> None:
        self._speech_auto_scroll_suspended = False

    def _clear_speech_highlight(self) -> None:
        self._speech_highlight_timer.stop()
        self._speech_auto_scroll_timer.stop()
        self._speech_auto_scroll_suspended = False
        self._speech_follow_scroll_active = False
        self.result.setExtraSelections([])
        self.chat_transcript.setExtraSelections([])

    def _on_speech_failed(self, error: str) -> None:
        self._speech_error = error
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "speech_error")
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._finish_speech(error)

    def _on_speech_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, SpeechWorker):
            return
        if worker is not self._speech_worker:
            worker.cleanup_output()
            worker.deleteLater()
            return
        interrupted = worker.isInterruptionRequested()
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "popup_worker_finished",
                once=False,
                interrupted=interrupted,
            )
        if interrupted:
            worker.cleanup_output()
        self._speech_worker = None
        worker.deleteLater()
        if self._close_pending:
            self._clear_speech_files()
            self._continue_pending_close()
            return
        if interrupted:
            self._clear_speech_files()
            pending_offset = self._pending_speech_offset
            self._pending_speech_offset = None
            if pending_offset is not None:
                self._begin_speech(pending_offset)
            else:
                self.speak_btn.setText(self._speech_idle_label())
                self.speak_btn.setEnabled(
                    bool(self._current_speech_text().strip())
                    and self._speech_available
                )
        elif self._pending_speech_offset is not None:
            pending_offset = self._pending_speech_offset
            if not self._play_pending_speech_offset():
                self._pending_speech_offset = None
                self._speech_waiting = False
                self._begin_speech(pending_offset)
        elif self._speech_active and self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            if self._speech_playing_stream_index == self._speech_index:
                return
            if (
                self._speech_index in self._speech_paths
                or self._speech_index in self._speech_stream_urls
            ):
                self._play_ready_speech_segment(self._speech_index)
            else:
                self._finish_speech(self._speech_error)

    def _on_playback_state_changed(self, state) -> None:
        if self._speech_trace is not None:
            state_name = getattr(state, "name", str(state))
            self._speech_trace.mark(
                "playback_state",
                once=False,
                state=state_name,
                media_status=getattr(
                    self._player.mediaStatus(), "name",
                    str(self._player.mediaStatus()),
                ),
            )
        if (
            state == QMediaPlayer.PlaybackState.StoppedState
            and self._speech_active
            and not self._speech_waiting
            and self._player.mediaStatus() != QMediaPlayer.MediaStatus.EndOfMedia
            and self._speech_error is not None
        ):
            self._finish_speech(self._speech_error)

    def _on_media_status_changed(self, status) -> None:
        if self._speech_trace is not None:
            self._speech_trace.mark(
                "media_status",
                once=False,
                status=getattr(status, "name", str(status)),
                position_ms=self._player.position(),
            )
        if (
            self._pending_player_position_ms is not None
            and self._player.source() == self._pending_player_source
            and status in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            )
        ):
            self._player.setPosition(self._pending_player_position_ms)
            self._pending_player_position_ms = None
            self._pending_player_source = None
            self._player.play()
        if not self._speech_active or status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self._speech_index += 1
        if (
            self._speech_index in self._speech_paths
            or self._speech_index in self._speech_stream_urls
        ):
            self._play_ready_speech_segment(self._speech_index)
        elif self._speech_worker is not None and self._speech_worker.isRunning():
            self._speech_waiting = True
            self.spin.setText("Preparing next speech part…")
            self.spin.show()
        else:
            self._finish_speech(self._speech_error)

    def _finish_speech(self, error: str | None = None) -> None:
        self._speech_active = False
        self._speech_waiting = False
        self._speech_stream_urls.clear()
        self._speech_playing_stream_index = None
        self._speech_boundaries.clear()
        self._pending_highlight = None
        self._clear_speech_highlight()
        if error and self._speech_worker is not None and self._speech_worker.isRunning():
            self._speech_worker.requestInterruption()
        self._pending_player_position_ms = None
        self._pending_player_source = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._close_speech_stream_server()
        self._clear_speech_files()
        if error:
            self.spin.setText(f"Speech failed: {error}")
            self.spin.show()
        else:
            self.spin.hide()
        self._speech_text_override = None
        self._chat_speech_document_start = None
        self.speak_btn.setText(self._speech_idle_label())
        worker_stopping = self._speech_worker is not None and self._speech_worker.isRunning()
        self.speak_btn.setEnabled(
            bool(self._current_speech_text().strip())
            and self._speech_available and not worker_stopping
        )
        if worker_stopping:
            self.speak_btn.setText("Stopping speech…")
        self.speechFinished.emit(error or "")

    def _on_playback_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError or message:
            error_text = message or error.name
            logging.warning(
                "Audio playback failed: %s (source=%s position=%sms)",
                error_text,
                self._player.source().toString(),
                self._player.position(),
            )
            self._finish_speech(f"Audio playback failed: {error_text}")

    def _close_speech_stream_server(self) -> None:
        server = self._speech_stream_server
        self._speech_stream_server = None
        if server is not None:
            server.close()

    def _clear_speech_files(self) -> None:
        from pathlib import Path
        remaining = []
        paths = self._stale_speech_paths + list(self._speech_paths.values())
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                remaining.append(path)
        self._stale_speech_paths = remaining
        self._speech_paths.clear()
        self._speech_seek_path = None

    def _continue_pending_close(self) -> None:
        if not self._close_pending:
            return
        if (
            self._speech_worker is not None and self._speech_worker.isRunning()
        ) or (
            self._voice_list_worker is not None
            and self._voice_list_worker.isRunning()
        ) or self._speech_prefetch_workers or any(
            worker.isRunning() for worker in self._translation_workers
        ) or any(worker.isRunning() for worker in self._chat_workers):
            return
        self._close_pending = False
        QTimer.singleShot(0, self.close)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.result.toPlainText())

    def _toggle_pin(self) -> None:
        self._pinned = self.pin_btn.isChecked()
        self.pin_btn.setText("Unpin" if self._pinned else "Pin")
        was_visible = self.isVisible()
        self.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint, self._pinned)
        if was_visible:
            self.show()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self._fixed_applied:
            self._size_save_timer.start()

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        if self._fixed_applied:
            self._size_save_timer.start()

    def _save_geometry(self) -> None:
        frame = (
            self._vertical_restore_geometry
            if self._vertical_restore_geometry is not None
            else self.frameGeometry()
        )
        frame_size = frame.size()
        size = [frame_size.width(), frame_size.height()]
        frame_position = frame.topLeft()
        position = [frame_position.x(), frame_position.y()]
        self._settings["popup_size"] = size
        self._settings["popup_pos"] = position
        try:
            from config.settings import load, save
            persisted = load()
            persisted["popup_size"] = size
            persisted["popup_pos"] = position
            save(persisted)
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save translation-window size")

    def closeEvent(self, e) -> None:
        self._size_save_timer.stop()
        self._save_geometry()
        self._speech_prefetch_timer.stop()
        self._cancel_speech_prefetch()
        if self.page_number_label.isChecked():
            self.page_number_label.setChecked(False)
        speech_worker = self._speech_worker
        self._pending_speech_offset = None
        self._pending_player_position_ms = None
        self._pending_player_source = None
        # Release player file locks BEFORE deleting temp mp3s.
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except Exception:
            pass
        self._close_speech_stream_server()
        voice_worker = self._voice_list_worker
        speech_running = speech_worker is not None and speech_worker.isRunning()
        prefetch_pending = bool(self._speech_prefetch_workers)
        voices_running = voice_worker is not None and voice_worker.isRunning()
        translation_running = any(
            worker.isRunning() for worker in self._translation_workers)
        chat_running = any(worker.isRunning() for worker in self._chat_workers)
        if (
            speech_running or voices_running or translation_running
            or chat_running or prefetch_pending
        ):
            self._close_pending = True
            self._speech_active = False
            self._clear_speech_highlight()
            self._player.stop()
            self._player.setSource(QUrl())
            if speech_running:
                speech_worker.requestInterruption()
            for worker in self._speech_prefetch_workers:
                if worker.isRunning():
                    worker.requestInterruption()
            if voices_running:
                voice_worker.requestInterruption()
            for worker in self._translation_workers:
                if worker.isRunning():
                    worker.requestInterruption()
            for worker in self._chat_workers:
                if worker.isRunning():
                    worker.requestInterruption()
            e.ignore()
            self.hide()
            return
        if speech_worker:
            speech_worker.cleanup_output()
        self._speech_active = False
        self._clear_speech_files()
        self._translation_workers.clear()
        self._chat_workers.clear()
        super().closeEvent(e)
