"""Standalone conversational chat panel for the selected translation model."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QTextCharFormat,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSplitter,
    QSlider,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from ui.context_menu import style_context_menu

from popup.chat_worker import ChatWorker
from popup.speech_worker import (
    ARABIC_VOICE,
    DEFAULT_SPEECH_RATE,
    SpeechWorker,
    normalize_speed,
    python_to_qt_position,
    qt_to_python_position,
    speech_available,
    speech_missing_message,
    speech_start_offset,
    speech_word_span,
)

log = logging.getLogger("harmony_translator.chat")

_EXPLANATION_INSTRUCTIONS = {
    "linguistic": (
        "Explain the selected text linguistically. Describe the relevant "
        "grammar, wording, structure, register, and any idiomatic or figurative "
        "language, without adding unrelated detail."
    ),
    "simple": (
        "Explain the selected text in very simple, everyday language. Avoid "
        "technical terms and use a brief example only if it helps."
    ),
    "etymological": (
        "Explain the selected text etymologically, focusing on the origin and "
        "history of its important words. Distinguish reliable facts from "
        "uncertainty and do not invent word origins."
    ),
    "translate_and_explain": (
        "First provide a natural translation of the selected text into the "
        "source language of the passage when known; otherwise translate it "
        "into the configured reply language. Then explain its meaning briefly."
    ),
    "context": (
        "Explain what the selected text means in the context of the current "
        "translation and original passage. You may supplement the passage with "
        "relevant general knowledge or web research. For factual details not "
        "clearly stated in the passage, use Google Search when available and "
        "cite its sources."
    ),
}


def explanation_instruction(style: str) -> str:
    return _EXPLANATION_INSTRUCTIONS[style]


class ChatTranscriptBrowser(QTextBrowser):
    textPositionClicked = Signal(int)
    backgroundPositionClicked = Signal(int)
    refreshRequested = Signal()
    reopenRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._click_start = QPoint()
        self._click_had_selection = False
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._click_start = event.position().toPoint()
        self._click_had_selection = self.textCursor().hasSelection()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        if (
            self._click_had_selection
            or self.textCursor().hasSelection()
            or (point - self._click_start).manhattanLength()
            > QApplication.startDragDistance()
            or self.anchorAt(point)
        ):
            return
        document_point = point + QPoint(
            self.horizontalScrollBar().value(),
            self.verticalScrollBar().value(),
        )
        hit_position = self.document().documentLayout().hitTest(
            document_point, Qt.HitTestAccuracy.ExactHit)
        position = self.cursorForPosition(point).position()
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        text = cursor.block().text()
        python_position = qt_to_python_position(
            text, cursor.positionInBlock())
        if (
            hit_position < 0
            or python_position >= len(text)
            or text[python_position].isspace()
        ):
            self.backgroundPositionClicked.emit(position)
            return
        self.textPositionClicked.emit(position)


class ChatInputEdit(QPlainTextEdit):
    sendRequested = Signal()

    def keyPressEvent(self, event) -> None:
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """A general chat that can discuss the latest translated passage."""

    def __init__(
        self,
        settings_provider: Callable[[], dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_provider = settings_provider
        self._messages: list[dict[str, str]] = []
        self._entries: list[tuple[str, str]] = []
        self._worker: ChatWorker | None = None
        self._translation_context = ""
        self._explanation_selection = ""
        try:
            self._chat_font_size = float(
                self._settings_provider().get("chat_font_size", 12.0))
        except (TypeError, ValueError):
            self._chat_font_size = 12.0
        self._chat_font_size = min(30.0, max(8.0, self._chat_font_size))
        self._speech_worker: SpeechWorker | None = None
        self._speech_paths: dict[int, str] = {}
        self._speech_boundaries: dict[int, list[tuple[int, int, int, int]]] = {}
        self._speech_index = 0
        self._speech_document_start: int | None = None
        self._speech_message_start: int | None = None
        self._speech_text = ""
        self._speech_active = False
        self._speech_paused = False
        self._speech_rate = self._configured_speech_rate(
            self._settings_provider())
        self._speech_highlight_background = QColor("#26354A")
        self._speech_highlight_foreground = QColor("#FFFFFF")

        self.setObjectName("standaloneChatPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        self.clear_button = QPushButton("New chat")
        self.clear_button.setObjectName("newChatButton")
        self.clear_button.setFixedHeight(44)
        self.clear_button.clicked.connect(self.clear_chat)
        self.explain_button = QPushButton("Explain")
        self.explain_button.setObjectName("explainSelectionButton")
        self.explain_button.setFixedHeight(44)
        self.explain_button.setToolTip(
            "Explain the selected text, or the current translation")
        self.explain_button.setEnabled(False)
        self.explain_button.clicked.connect(
            lambda: self.explain_selection())
        self.read_aloud_button = QPushButton("Read aloud")
        self.read_aloud_button.setObjectName("chatReadAloud")
        self.read_aloud_button.setFixedHeight(44)
        self.read_aloud_button.setToolTip(
            "Read the latest chat response aloud")
        self.read_aloud_button.clicked.connect(self._read_latest_chat_aloud)
        self.stop_speech_button = QPushButton("Stop")
        self.stop_speech_button.setObjectName("chatStopSpeech")
        self.stop_speech_button.setFixedHeight(44)
        self.stop_speech_button.clicked.connect(self._stop_speech)
        self.stop_speech_button.setEnabled(False)
        self.speed_button = QPushButton(self)
        self.speed_button.setObjectName("chatReadingSpeed")
        self.speed_button.setFixedHeight(44)
        self.speed_menu = QMenu(self)
        self.speed_button.clicked.connect(self._show_speed_menu)
        self._build_speed_menu()
        self._chat_action_buttons = (
            self.speed_button,
            self.read_aloud_button,
            self.stop_speech_button,
            self.explain_button,
            self.clear_button,
        )

        self.context_notice = QLabel()
        self.context_notice.setObjectName("chatContextNotice")
        self.context_notice.setWordWrap(True)
        self.context_notice.hide()
        layout.addWidget(self.context_notice)

        self.input_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.input_splitter.setObjectName("standaloneChatSplitter")
        self.input_splitter.setChildrenCollapsible(False)
        self.input_splitter.setHandleWidth(6)

        chat_content = QWidget(self.input_splitter)
        chat_content_layout = QVBoxLayout(chat_content)
        chat_content_layout.setContentsMargins(0, 0, 0, 0)
        chat_content_layout.setSpacing(6)
        self.chat_actions_scroll = QScrollArea(chat_content)
        self.chat_actions_scroll.setObjectName("chatActionsScroll")
        self.chat_actions_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.chat_actions_scroll.setFixedHeight(64)
        self.chat_actions_scroll.setWidgetResizable(True)
        self.chat_actions_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.chat_actions_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_actions_scroll.setStyleSheet("""
            QScrollArea#chatActionsScroll {
                background-color: transparent;
                border: none;
            }
        """)
        self.chat_actions_widget = QWidget()
        self.chat_actions_widget.setObjectName("chatActionsContainer")
        self.chat_actions_layout = QHBoxLayout(self.chat_actions_widget)
        self.chat_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_actions_layout.setSpacing(2)
        for button in self._chat_action_buttons:
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.chat_actions_layout.addWidget(button, 1)
        self.chat_actions_scroll.setWidget(self.chat_actions_widget)
        self.transcript = ChatTranscriptBrowser(self)
        self.transcript.setObjectName("standaloneChatTranscript")
        self.transcript.setOpenExternalLinks(True)
        self.transcript.textPositionClicked.connect(
            self._on_transcript_position_clicked)
        self.transcript.backgroundPositionClicked.connect(
            self._on_transcript_background_clicked)
        chat_content_layout.addWidget(self.transcript, 1)
        chat_content_layout.addWidget(self.chat_actions_scroll)
        self.input_splitter.addWidget(chat_content)

        self.composer_widget = QWidget(self.input_splitter)
        composer = QHBoxLayout(self.composer_widget)
        composer.setContentsMargins(0, 0, 0, 0)
        composer.setSpacing(8)
        self.input = ChatInputEdit(self)
        self.input.setObjectName("standaloneChatInput")
        self.input.setPlaceholderText(
            "Ask a question… (Enter to send; Shift+Enter for a new line)")
        self.input.setMinimumHeight(64)
        self.input.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.input.viewport().setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu)
        self.input.sendRequested.connect(self.send_message)
        composer.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("standaloneChatSend")
        self.send_button.setFixedHeight(40)
        self.send_button.clicked.connect(self.send_message)
        composer.addWidget(self.send_button)
        self.input_splitter.addWidget(self.composer_widget)
        self.input_splitter.setStretchFactor(0, 1)
        self.input_splitter.setStretchFactor(1, 0)
        self.input_splitter.setSizes([300, 96])
        layout.addWidget(self.input_splitter, 1)
        self._context_menu_target: QTextEdit | QPlainTextEdit | None = None
        self._context_menu = self._create_context_menu()
        self._compact_chat_actions: bool | None = None
        for widget in (
            self, self.transcript, self.transcript.viewport(),
            self.input, self.input.viewport(),
        ):
            widget.installEventFilter(self)
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setPlaybackRate(self._speech_rate)
        self._player.positionChanged.connect(self._update_speech_highlight)
        self._player.mediaStatusChanged.connect(
            self._on_speech_media_status_changed)
        self._player.errorOccurred.connect(self._on_speech_player_error)
        self._font_size_save_timer = QTimer(self)
        self._font_size_save_timer.setSingleShot(True)
        self._font_size_save_timer.setInterval(500)
        self._font_size_save_timer.timeout.connect(
            self._save_chat_font_size)
        self._speech_rate_save_timer = QTimer(self)
        self._speech_rate_save_timer.setSingleShot(True)
        self._speech_rate_save_timer.setInterval(400)
        self._speech_rate_save_timer.timeout.connect(
            self._save_speech_rate)
        self._streaming_response = ""
        self._rendered_streaming_response = ""
        self._streaming_active = False
        self._streaming_final_answer: str | None = None
        self._streaming_timer = QTimer(self)
        self._streaming_timer.setInterval(30)
        self._streaming_timer.timeout.connect(
            self._render_next_stream_piece)
        self._update_context_notice()
        self._apply_chat_font_size()
        self._update_chat_action_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_chat_action_layout()

    def hideEvent(self, event) -> None:
        if self._font_size_save_timer.isActive():
            self._font_size_save_timer.stop()
            self._save_chat_font_size()
        super().hideEvent(event)

    def _update_chat_action_layout(self) -> None:
        compact = self.width() < 380
        if compact == self._compact_chat_actions:
            return
        self._compact_chat_actions = compact

        if compact:
            self.speed_button.setText(
                f"{self._speech_rate:.1f}x")
            self.read_aloud_button.setText("Read")
            self.stop_speech_button.setText("Stop")
            self.explain_button.setText("Explain")
            self.clear_button.setText("New chat")
            self.chat_actions_layout.setSpacing(2)
        else:
            self.read_aloud_button.setText("Read aloud")
            self.stop_speech_button.setText("Stop")
            self.explain_button.setText("Explain")
            self.clear_button.setText("New chat")
            self.chat_actions_layout.setSpacing(2)
        self._fit_chat_action_buttons()

    def _fit_chat_action_buttons(self) -> None:
        for button in self._chat_action_buttons:
            button.setMinimumWidth(0)
            button.setMaximumWidth(16777215)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.chat_actions_widget.updateGeometry()

    def set_translation_context(self, translated_text: str) -> None:
        self._translation_context = translated_text.strip()
        self._update_context_notice()
        self._update_explain_button_state()

    def set_explanation_selection(self, selected_text: str) -> None:
        self._explanation_selection = selected_text.strip()
        self._update_explain_button_state()

    def _update_explain_button_state(self) -> None:
        self.explain_button.setEnabled(
            bool(self._explanation_selection or self._translation_context)
            and self._worker is None
            and not self._streaming_active
        )

    def _update_context_notice(self) -> None:
        self.context_notice.clear()
        self.context_notice.hide()

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._speech_highlight_background = QColor(colors["selection"])
        selection_text = (
            "#222831" if QColor(colors["selection"]).lightness() >= 128
            else colors["text"]
        )
        self._speech_highlight_foreground = QColor(selection_text)
        style_colors = dict(
            colors,
            selection_text=selection_text,
            chat_font_size=f"{self._chat_font_size:g}",
        )
        for scrollbar in self.findChildren(QScrollBar):
            if scrollbar.orientation() == Qt.Orientation.Horizontal:
                is_actions_bar = (
                    scrollbar is self.chat_actions_scroll.horizontalScrollBar()
                )
                thickness = 4 if is_actions_bar else 10
                radius = 2 if is_actions_bar else 5
                minimum_handle = 18 if is_actions_bar else 24
                geometry = (
                    f"height: {thickness}px; "
                    f"min-width: {minimum_handle}px;"
                )
                orientation = "horizontal"
            else:
                thickness = 10
                radius = 5
                minimum_handle = 24
                geometry = (
                    f"width: {thickness}px; "
                    f"min-height: {minimum_handle}px;"
                )
                orientation = "vertical"
            scrollbar_style = (
                f"QScrollBar:{orientation} {{ {geometry} "
                "background: transparent; margin: 0; }"
                f"QScrollBar::handle:{orientation} {{ "
                f"background: {colors['scroll']}; "
                f"border-radius: {radius}px; }}"
                f"QScrollBar::handle:{orientation}:hover {{ "
                f"background: {colors['scroll_hover']}; }}"
                f"QScrollBar::add-line:{orientation}, "
                f"QScrollBar::sub-line:{orientation} {{ "
                f"{'width' if orientation == 'horizontal' else 'height'}: 0; }}"
            )
            if scrollbar.styleSheet() != scrollbar_style:
                scrollbar.setStyleSheet(scrollbar_style)
        actions_style = """
            QScrollArea#chatActionsScroll {
                background-color: transparent;
                border: none;
            }
            QWidget#chatActionsContainer {
                background-color: %(surface)s;
                border: none;
            }
            QScrollBar:horizontal {
                height: 4px;
                background: transparent;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: %(scroll)s;
                border-radius: 2px;
                min-width: 18px;
            }
            QScrollBar::handle:horizontal:hover {
                background: %(scroll_hover)s;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0;
            }
        """ % colors
        if self.chat_actions_scroll.styleSheet() != actions_style:
            self.chat_actions_scroll.setStyleSheet(actions_style)
        button_style = """
            QPushButton#chatReadAloud,
            QPushButton#chatStopSpeech,
            QPushButton#explainSelectionButton,
            QPushButton#newChatButton,
            QPushButton#chatReadingSpeed,
            QPushButton#standaloneChatSend {
                background-color: %(surface)s;
                color: %(text)s;
                border: 1px solid %(border)s;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
                border-bottom-left-radius: 7px;
                border-bottom-right-radius: 7px;
                padding: 6px 4px;
                font-size: 15px;
            }
            QPushButton#chatReadAloud:hover,
            QPushButton#chatStopSpeech:hover,
            QPushButton#explainSelectionButton:hover,
            QPushButton#newChatButton:hover,
            QPushButton#chatReadingSpeed:hover,
            QPushButton#standaloneChatSend:hover {
                background-color: %(border)s;
            }
            QPushButton#chatReadAloud:pressed,
            QPushButton#chatStopSpeech:pressed,
            QPushButton#explainSelectionButton:pressed,
            QPushButton#newChatButton:pressed,
            QPushButton#chatReadingSpeed:pressed,
            QPushButton#standaloneChatSend:pressed {
                background-color: %(accent)s;
                color: %(selection_text)s;
            }
            QPushButton#chatReadAloud:disabled,
            QPushButton#chatStopSpeech:disabled,
            QPushButton#explainSelectionButton:disabled,
            QPushButton#newChatButton:disabled,
            QPushButton#chatReadingSpeed:disabled,
            QPushButton#standaloneChatSend:disabled {
                color: %(text)s;
                background-color: %(surface)s;
                border: 1px solid %(border)s;
            }
        """ % style_colors
        panel_style = """
            QWidget#standaloneChatPanel {
                background-color: %(window)s;
                color: %(text)s;
            }
            QLabel#chatContextNotice {
                color: %(muted)s;
                font-size: 12px;
            }
            QTextBrowser#standaloneChatTranscript {
                color: %(text)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
                padding: 10px;
                font-size: %(chat_font_size)spt;
                background-color: %(surface)s;
                selection-background-color: %(selection)s;
                selection-color: %(selection_text)s;
            }
            QSplitter#standaloneChatSplitter::handle:vertical {
                /* Transparent: this is the thick bar that sat between the
                   action buttons and the composer. It stays a grab area -
                   the margin is unchanged - and the hover tint still shows
                   it can be dragged. */
                background-color: transparent;
                margin: 2px 0;
            }
            QSplitter#standaloneChatSplitter::handle:vertical:hover {
                background-color: %(accent)s;
            }
            QPlainTextEdit#standaloneChatInput {
                color: %(text)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
                padding: 8px;
                font-size: %(chat_font_size)spt;
                background-color: %(surface)s;
                selection-background-color: %(selection)s;
                selection-color: %(selection_text)s;
            }
        """ % style_colors
        if self.styleSheet() != panel_style:
            self.setStyleSheet(panel_style)
        for button in self._chat_action_buttons + (self.send_button,):
            if not button.isFlat():
                button.setFlat(True)
            if button.styleSheet() != button_style:
                button.setStyleSheet(button_style)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched in (self.transcript.viewport(), self.input.viewport())
            and event.type() == QEvent.Type.MouseButtonRelease
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.RightButton
        ):
            self._show_context_menu(
                self.transcript if watched is self.transcript.viewport()
                else self.input,
                event.globalPosition().toPoint(),
            )
            event.accept()
            return True
        if event.type() == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta:
                    self._set_chat_font_size(
                        self._chat_font_size + (1 if delta > 0 else -1))
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _create_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._copy_context_action = menu.addAction("Copy")
        self._copy_context_action.triggered.connect(
            self._copy_context_text)
        self._cut_context_action = menu.addAction("Cut")
        self._cut_context_action.triggered.connect(
            self._cut_context_text)
        self._paste_context_action = menu.addAction("Paste")
        self._paste_context_action.triggered.connect(
            self._paste_context_text)
        self._select_all_context_action = menu.addAction("Select All")
        self._select_all_context_action.triggered.connect(
            self._select_all_context_text)
        menu.addSeparator()
        refresh_action = menu.addAction("Refresh")
        refresh_action.triggered.connect(self.transcript.refreshRequested.emit)
        reopen_action = menu.addAction("ReOpen")
        reopen_action.triggered.connect(self.transcript.reopenRequested.emit)
        return style_context_menu(menu)

    def _show_context_menu(
        self,
        target: QTextEdit | QPlainTextEdit,
        global_position,
    ) -> None:
        self._context_menu_target = target
        has_text = bool(target.toPlainText().strip())
        self._copy_context_action.setEnabled(has_text)
        has_selection = target.textCursor().hasSelection()
        is_editable = target is self.input and not target.isReadOnly()
        self._cut_context_action.setEnabled(
            is_editable and has_selection)
        self._paste_context_action.setEnabled(
            is_editable
            and bool(QApplication.clipboard().text()))
        self._select_all_context_action.setEnabled(has_text)
        self._context_menu.popup(global_position)

    def _copy_context_text(self) -> None:
        target = self._context_menu_target
        if target is None:
            return
        cursor = target.textCursor()
        text = cursor.selectedText() if cursor.hasSelection() else target.toPlainText()
        QApplication.clipboard().setText(
            text.replace("\u2029", "\n").replace("\u2028", "\n"))

    def _select_all_context_text(self) -> None:
        if self._context_menu_target is not None:
            self._context_menu_target.selectAll()

    def _cut_context_text(self) -> None:
        target = self._context_menu_target
        if (
            target is self.input
            and target is not None
            and not target.isReadOnly()
            and target.textCursor().hasSelection()
        ):
            target.cut()

    def _paste_context_text(self) -> None:
        target = self._context_menu_target
        if (
            target is self.input
            and target is not None
            and not target.isReadOnly()
            and bool(QApplication.clipboard().text())
        ):
            target.paste()

    def _set_chat_font_size(self, size: float) -> None:
        self._chat_font_size = min(30.0, max(8.0, size))
        self._settings_provider()["chat_font_size"] = self._chat_font_size
        self._apply_chat_font_size()
        self._font_size_save_timer.start()

    def _save_chat_font_size(self) -> None:
        from config.settings import load, save

        try:
            persisted = load()
            persisted["chat_font_size"] = self._chat_font_size
            save(persisted)
        except (OSError, TypeError, ValueError):
            log.exception("Could not save chat font size")

    def _build_speed_menu(self) -> None:
        panel = QWidget(self.speed_menu)
        panel.setObjectName("chatReadingSpeedPanel")
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._speed_display = QLabel(panel)
        self._speed_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._speed_display)

        self._speed_slider = QSlider(Qt.Orientation.Horizontal, panel)
        self._speed_slider.setRange(25, 200)
        self._speed_slider.setSingleStep(1)
        self._speed_slider.setPageStep(25)
        self._speed_slider.setAccessibleName("Reading speed slider")
        self._speed_slider.setValue(round(self._speech_rate * 100))
        self._speed_slider.valueChanged.connect(self._set_speech_rate)
        layout.addWidget(self._speed_slider)

        action = QWidgetAction(self.speed_menu)
        action.setDefaultWidget(panel)
        self.speed_menu.addAction(action)
        self._update_speed_controls(self._speech_rate)

    def _show_speed_menu(self) -> None:
        self.speed_menu.adjustSize()
        self.speed_menu.popup(
            self.speed_button.mapToGlobal(
                self.speed_button.rect().bottomLeft()))

    def _update_speed_controls(self, speed: float) -> None:
        language_name = (
            "Arabic"
            if self._speech_rate_setting_key(self._settings_provider())
            == "speech_rate_arabic"
            else "Other languages"
        )
        self._speed_display.setText(
            f"{language_name} reading speed: {speed:.2f}x")
        self.speed_button.setText(f"{speed:.1f}x")
        self.speed_button.setToolTip(
            f"{language_name} reading speed: {speed:.2f}x")

    @staticmethod
    def _speech_rate_setting_key(settings: dict) -> str:
        target_language = str(settings.get("target_language", "ar"))
        response_language = str(
            settings.get("chat_reply_language", target_language)
            if settings.get("chat_reply_language_only", False)
            else target_language
        )
        language_code = (
            response_language.replace("_", "-").split("-", 1)[0].casefold())
        return (
            "speech_rate_arabic"
            if language_code == "ar"
            else "speech_rate_other"
        )

    @classmethod
    def _configured_speech_rate(cls, settings: dict) -> float:
        key = cls._speech_rate_setting_key(settings)
        return normalize_speed(
            settings.get(
                key,
                settings.get(
                    "speech_rate", settings.get("chat_speech_rate", 1.0))))

    def refresh_speech_settings(self) -> None:
        """Apply the shared Arabic/other-language speed settings."""
        settings = self._settings_provider()
        self._speech_rate = self._configured_speech_rate(settings)
        if hasattr(self, "_player"):
            self._player.setPlaybackRate(self._speech_rate)
        if hasattr(self, "_speed_slider"):
            self._speed_slider.blockSignals(True)
            self._speed_slider.setValue(round(self._speech_rate * 100))
            self._speed_slider.blockSignals(False)
            self._update_speed_controls(self._speech_rate)

    def _set_speech_rate(self, value: int) -> None:
        self._speech_rate = normalize_speed(value / 100)
        self._player.setPlaybackRate(self._speech_rate)
        self._update_speed_controls(self._speech_rate)
        self._fit_chat_action_buttons()
        settings = self._settings_provider()
        setting_key = self._speech_rate_setting_key(settings)
        settings[setting_key] = self._speech_rate
        settings["chat_speech_rate"] = self._speech_rate
        self._speech_rate_save_timer.start()

    def _save_speech_rate(self) -> None:
        from config.settings import load, save

        try:
            persisted = load()
            setting_key = self._speech_rate_setting_key(
                self._settings_provider())
            persisted[setting_key] = self._speech_rate
            persisted["chat_speech_rate"] = self._speech_rate
            save(persisted)
        except (OSError, TypeError, ValueError):
            log.exception("Could not save Chat reading speed")

    def _apply_chat_font_size(self) -> None:
        font = self.transcript.font()
        font.setPointSizeF(self._chat_font_size)
        self.transcript.setFont(font)
        self.transcript.document().setDefaultFont(font)
        self.input.setFont(font)

    def send_message(self) -> None:
        question = self.input.toPlainText().strip()
        if not question:
            return
        self._send_question(question)

    def explain_selection(self, style: str = "context") -> None:
        selected_text = (
            self._explanation_selection or self._translation_context
        ).strip()
        if not selected_text:
            return
        instruction = explanation_instruction(style)
        self._send_question(
            f"{instruction}\n\nSelected text:\n{selected_text}",
            clear_input=False,
            display_question=f"Explain: {selected_text}",
        )

    def _send_question(
        self,
        question: str,
        *,
        clear_input: bool = True,
        display_question: str | None = None,
    ) -> None:
        if (
            not question
            or self._worker is not None
            or self._streaming_active
        ):
            return
        self._stop_speech()
        self._messages.append({"role": "user", "content": question})
        self._entries.append((
            "user",
            question if display_question is None else display_question,
        ))
        if clear_input:
            self.input.clear()
        self.send_button.setEnabled(False)
        self.send_button.setText("…")
        self.explain_button.setEnabled(False)
        self._streaming_response = ""
        self._rendered_streaming_response = ""
        self._streaming_active = True
        self._streaming_final_answer = None
        settings = dict(self._settings_provider())
        worker = ChatWorker(
            self._messages, settings, "", self._translation_context)
        worker.setParent(self)
        self._worker = worker
        worker.done.connect(self._on_reply)
        worker.partial.connect(self._on_partial_reply)
        worker.failed.connect(self._on_error)
        worker.finished.connect(self._on_finished)
        worker.start()
        self._render()

    def _on_reply(self, answer: str) -> None:
        if self.sender() is not self._worker:
            return
        self._stop_speech()
        self._streaming_response = answer
        self._streaming_final_answer = answer
        if not self._streaming_timer.isActive():
            self._streaming_timer.start()

    def _on_error(self, error: str) -> None:
        if self.sender() is not self._worker:
            return
        self._streaming_timer.stop()
        partial_answer = self._streaming_response.strip()
        if partial_answer:
            self._entries.append(("incomplete", partial_answer))
        self._streaming_response = ""
        self._rendered_streaming_response = ""
        self._streaming_active = False
        self._streaming_final_answer = None
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()
        self._entries.append(("error", error))
        self._render()

    def _on_finished(self) -> None:
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        self.send_button.setText("Send")
        self.send_button.setEnabled(not self._streaming_active)
        self._update_explain_button_state()
        if isinstance(worker, ChatWorker):
            worker.deleteLater()

    def clear_chat(self) -> None:
        if self._worker is not None or self._streaming_active:
            return
        self._stop_speech()
        self._messages.clear()
        self._entries.clear()
        self.transcript.clear()
        self.input.clear()
        self._streaming_response = ""
        self._rendered_streaming_response = ""
        self._streaming_final_answer = None

    def _render(self) -> None:
        settings = self._settings_provider()
        target_language = str(settings.get("target_language", "ar"))
        response_language = str(
            settings.get("chat_reply_language", target_language)
            if settings.get("chat_reply_language_only", False)
            else target_language
        )
        transcript_direction = (
            Qt.LayoutDirection.RightToLeft
            if response_language in {"ar", "fa", "he", "ur"}
            else Qt.LayoutDirection.LeftToRight
        )
        input_direction = (
            Qt.LayoutDirection.RightToLeft
            if target_language in {"ar", "fa", "he", "ur"}
            else Qt.LayoutDirection.LeftToRight
        )
        self.transcript.setLayoutDirection(transcript_direction)
        option = self.transcript.document().defaultTextOption()
        option.setTextDirection(transcript_direction)
        option.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.transcript.document().setDefaultTextOption(option)
        self.input.setLayoutDirection(input_direction)
        input_option = self.input.document().defaultTextOption()
        input_option.setTextDirection(input_direction)
        input_option.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.input.document().setDefaultTextOption(input_option)

        blocks = []
        entries = list(self._entries)
        if self._streaming_active:
            entries.append(("assistant", self._streaming_response))
        for role, text in entries:
            if role == "user":
                content = "\n".join(
                    "> " + re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", line)
                    for line in text.splitlines() or [""]
                )
                blocks.append(f"**You**\n\n{content}")
            elif role == "assistant":
                blocks.append(f"**Chat**\n\n{text}")
            elif role == "incomplete":
                blocks.append(f"**Chat (incomplete)**\n\n{text}")
            else:
                blocks.append(f"**Error**\n\n{text}")
        self.transcript.setMarkdown("\n\n---\n\n".join(blocks))
        self._rendered_streaming_response = (
            self._streaming_response if self._worker is not None else "")
        self.transcript.verticalScrollBar().setValue(
            self.transcript.verticalScrollBar().maximum())
        self._sync_speech_controls()

    def _on_partial_reply(self, text: str) -> None:
        if self.sender() is not self._worker:
            return
        previous = self._streaming_response
        if not text.startswith(previous):
            self._streaming_response = text
            self._rendered_streaming_response = ""
            self._render()
        else:
            self._streaming_response = text
        if not self._streaming_timer.isActive():
            self._streaming_timer.start()

    def _render_next_stream_piece(self) -> None:
        remaining = self._streaming_response[
            len(self._rendered_streaming_response):]
        if not remaining:
            if self._streaming_final_answer is not None:
                self._finish_streamed_reply()
            else:
                self._streaming_timer.stop()
            return

        match = re.match(r"\s*\S+\s*", remaining)
        if match is None:
            self._streaming_timer.stop()
            return
        piece = match.group()
        is_final = self._streaming_final_answer is not None
        if not piece[-1:].isspace() and not is_final:
            self._streaming_timer.stop()
            return

        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(piece)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()
        self._rendered_streaming_response += piece

        if self._rendered_streaming_response == self._streaming_response:
            if is_final:
                self._finish_streamed_reply()
            else:
                self._streaming_timer.stop()

    def _finish_streamed_reply(self) -> None:
        answer = self._streaming_final_answer
        if answer is None:
            return
        self._streaming_timer.stop()
        self._streaming_active = False
        self._streaming_final_answer = None
        self._streaming_response = ""
        self._rendered_streaming_response = ""
        self._messages.append({"role": "assistant", "content": answer})
        self._entries.append(("assistant", answer))
        self._render()
        self.send_button.setText("Send")
        self.send_button.setEnabled(self._worker is None)
        self._update_explain_button_state()

    def _on_transcript_position_clicked(self, position: int) -> None:
        # A word click always starts the read from that word, even while
        # something is already playing.
        self._start_speech_from_position(position)

    def _toggle_message_speech(self, position: int) -> None:
        """Pause or resume the read when the empty space of a message is clicked.

        Clicking words is not a toggle: a word click always starts the read
        from that word. Only a click on the blank space around the words pauses
        the voice, and a later blank click resumes it from the last spoken
        word. A blank click while nothing is playing starts the whole message.
        """
        message = self._transcript_message_at_position(position)
        if message is None:
            return
        role, marker_position, content_blocks = (
            message[0], message[1], message[3])
        if role not in {"user", "assistant"}:
            return
        if not any(block.text().strip() for block in content_blocks):
            return
        if (
            self._speech_active
            and self._speech_message_start == marker_position
        ):
            if self._speech_paused:
                self._resume_speech()
            else:
                self._pause_speech()
            return
        self._start_speech_from_position(position, whole_message=True)

    def _transcript_message_at_position(self, position: int):
        document = self.transcript.document()
        cursor = QTextCursor(document)
        cursor.setPosition(max(0, min(position, document.characterCount() - 1)))
        blocks = []
        block = document.begin()
        while block.isValid():
            blocks.append(block)
            block = block.next()

        markers = {
            "You": "user",
            "Chat": "assistant",
            "Chat (incomplete)": "incomplete",
            "Error": "error",
        }
        clicked_index = cursor.block().blockNumber()
        marker_index = next(
            (index for index in range(clicked_index, -1, -1)
             if blocks[index].text().strip() in markers),
            None,
        )
        if marker_index is None:
            return None
        marker = blocks[marker_index]
        role = markers[marker.text().strip()]
        content_blocks = []
        for block in blocks[marker_index + 1:]:
            if block.text().strip() in markers:
                break
            content_blocks.append(block)
        return role, marker.position(), cursor, content_blocks

    def _on_transcript_background_clicked(self, position: int) -> None:
        self._toggle_message_speech(position)

    def _start_speech_from_position(
        self, position: int, *, whole_message: bool = False,
    ) -> None:
        settings = dict(self._settings_provider())
        self.refresh_speech_settings()
        engine = str(settings.get("speech_engine", "edge"))
        api_key = str(settings.get("gemini_key") or "")
        if not speech_available(engine, api_key):
            self.context_notice.setText(speech_missing_message(engine))
            self.context_notice.show()
            return
        self._update_context_notice()

        message = self._transcript_message_at_position(position)
        if message is None:
            return
        role, marker_position, cursor, content_blocks = message
        if role not in {"user", "assistant"}:
            return
        clicked_block = cursor.block()
        if not any(
            block.position() == clicked_block.position()
            for block in content_blocks
        ):
            return

        if whole_message:
            readable_blocks = [
                block for block in content_blocks if block.text().strip()
            ]
            if not readable_blocks:
                return
            first_block = readable_blocks[0]
            first_text = first_block.text()
            leading_whitespace = len(first_text) - len(first_text.lstrip())
            speech_text = "\n".join(
                [first_text[leading_whitespace:]]
                + [block.text() for block in readable_blocks[1:]]
            )
            document_start = (
                first_block.position()
                + python_to_qt_position(first_text, leading_whitespace)
            )
            start = speech_start_offset(speech_text, 0)
        else:
            clicked_text = clicked_block.text()
            text_position = qt_to_python_position(
                clicked_text, cursor.positionInBlock())
            start = speech_start_offset(clicked_text, text_position)
            if start >= len(clicked_text):
                return
            readable_blocks = [
                clicked_text[start:]
            ] + [
                block.text()
                for block in content_blocks
                if block.position() > clicked_block.position()
                and block.text().strip()
            ]
            speech_text = "\n".join(readable_blocks)
            document_start = (
                clicked_block.position()
                + python_to_qt_position(clicked_text, start)
            )
        if not speech_text.strip():
            return
        word_start, word_end = speech_word_span(
            speech_text, speech_start_offset(speech_text, 0))
        self._stop_speech()
        self._speech_text = speech_text
        self._speech_document_start = document_start
        self._speech_message_start = marker_position
        self._speech_index = 0
        self._speech_paths.clear()
        self._speech_boundaries.clear()
        self._speech_active = True
        self._speech_paused = False
        self._highlight_speech_range(word_start, word_end)
        self._sync_speech_controls()
        self._player.setPlaybackRate(self._speech_rate)
        target_language = str(settings.get("target_language", "ar"))
        response_language = str(
            settings.get("chat_reply_language", target_language)
            if settings.get("chat_reply_language_only", False)
            else target_language
        )

        configured_voice = settings.get("speech_voice")
        voice = (
            configured_voice
            if isinstance(configured_voice, str) and configured_voice
            else ARABIC_VOICE
        )
        worker = SpeechWorker(
            self._speech_text,
            self,
            rate=DEFAULT_SPEECH_RATE,
            voice=voice,
            automatic_voice=bool(
                settings.get("automatic_speech_voice", False)),
            target_language=response_language,
            fast=bool(settings.get("fast_mode", True)),
            stream_audio=False,
            read_latin_words=bool(
                settings.get("speech_read_latin_words", False)),
            engine=engine,
            gemini_api_key=api_key,
            gemini_voice=str(settings.get("gemini_tts_voice") or "Kore"),
        )
        self._speech_worker = worker
        worker.segment_ready.connect(self._on_speech_segment_ready)
        worker.word_boundaries.connect(self._on_speech_word_boundaries)
        worker.failed.connect(self._on_speech_failed)
        worker.finished.connect(self._on_speech_worker_finished)
        worker.start()

    def _read_latest_chat_aloud(self) -> None:
        blocks = []
        block = self.transcript.document().begin()
        while block.isValid():
            blocks.append(block)
            block = block.next()
        for index in range(len(blocks) - 1, -1, -1):
            if blocks[index].text().strip() == "Chat":
                for content_block in blocks[index + 1:]:
                    if content_block.text().strip() in {"You", "Chat", "Error"}:
                        break
                    if content_block.text().strip():
                        self._start_speech_from_position(
                            content_block.position())
                        return

    def _on_speech_segment_ready(self, index: int, path: str) -> None:
        worker = self.sender()
        if worker is not self._speech_worker or not self._speech_active:
            Path(path).unlink(missing_ok=True)
            return
        self._speech_paths[index] = path
        if index == self._speech_index and not self._speech_paused:
            self._play_speech_segment(path)

    def _on_speech_word_boundaries(
        self, index: int, boundaries: list[tuple[int, int, int, int]]
    ) -> None:
        if self.sender() is self._speech_worker and self._speech_active:
            self._speech_boundaries[index] = boundaries
            self._update_speech_highlight(self._player.position())

    def _play_speech_segment(self, path: str) -> None:
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _on_speech_media_status_changed(self, status) -> None:
        if (
            not self._speech_active
            or self._speech_paused
            or status != QMediaPlayer.MediaStatus.EndOfMedia
        ):
            return
        self._speech_index += 1
        next_path = self._speech_paths.get(self._speech_index)
        if next_path is not None:
            self._play_speech_segment(next_path)
        elif self._speech_worker is None or not self._speech_worker.isRunning():
            self._finish_speech()

    def _on_speech_failed(self, error: str) -> None:
        if self.sender() is not self._speech_worker:
            return
        self.context_notice.setText(f"Read aloud failed: {error}")
        self.context_notice.show()
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._finish_speech()

    def _on_speech_player_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self.context_notice.setText(f"Read aloud playback failed: {message}")
        self.context_notice.show()
        self._finish_speech()

    def _on_speech_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, SpeechWorker):
            return
        if worker is not self._speech_worker:
            worker.cleanup_output()
            worker.deleteLater()
            return
        if worker.isInterruptionRequested():
            worker.cleanup_output()
            self._speech_worker = None
            worker.deleteLater()
            return
        if self._speech_paused:
            return
        if not self._speech_active:
            self._speech_worker = None
            self._clear_speech_output(worker)
        elif self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            next_path = self._speech_paths.get(self._speech_index)
            if next_path is not None:
                self._play_speech_segment(next_path)
            else:
                self._finish_speech()

    def _update_speech_highlight(self, position_ms: int = 0) -> None:
        if not self._speech_active or self._speech_document_start is None:
            return
        boundaries = self._speech_boundaries.get(self._speech_index, [])
        if not boundaries:
            return
        position_ticks = position_ms * 10_000
        active = None
        previous = None
        for boundary in boundaries:
            if boundary[0] > position_ticks:
                break
            if position_ticks < boundary[0] + boundary[1]:
                active = boundary
                break
            previous = boundary
        selected = active or previous
        if selected is not None:
            self._highlight_speech_range(selected[2], selected[3])

    def _highlight_speech_range(self, start: int, end: int) -> None:
        if self._speech_document_start is None:
            return
        cursor = QTextCursor(self.transcript.document())
        cursor.setPosition(self._speech_document_start + python_to_qt_position(
            self._speech_text, start))
        cursor.setPosition(self._speech_document_start + python_to_qt_position(
            self._speech_text, end), QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        text_format = QTextCharFormat()
        text_format.setBackground(self._speech_highlight_background)
        text_format.setForeground(self._speech_highlight_foreground)
        selection.format = text_format
        self.transcript.setExtraSelections([selection])
        target_rect = self.transcript.cursorRect(cursor)
        if not self.transcript.viewport().rect().contains(
            target_rect.center()
        ):
            self.transcript.setTextCursor(cursor)
            self.transcript.ensureCursorVisible()

    def _sync_speech_controls(self) -> None:
        has_assistant_reply = any(
            role == "assistant" and text.strip()
            for role, text in self._entries
        )
        settings = self._settings_provider()
        engine = str(settings.get("speech_engine", "edge"))
        available = speech_available(
            engine, str(settings.get("gemini_key") or ""))
        self.read_aloud_button.setEnabled(
            has_assistant_reply and available and not self._speech_active)
        self.stop_speech_button.setEnabled(self._speech_active)
        self.read_aloud_button.setToolTip(
            "Read the latest chat response aloud"
            if available else speech_missing_message(engine))

    def _pause_speech(self) -> None:
        if not self._speech_active or self._speech_paused:
            return
        self._speech_paused = True
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        self._sync_speech_controls()

    def _resume_speech(self) -> None:
        if not self._speech_active or not self._speech_paused:
            return
        self._speech_paused = False
        state = self._player.playbackState()
        if state == QMediaPlayer.PlaybackState.PausedState:
            self._player.play()
        elif state != QMediaPlayer.PlaybackState.PlayingState:
            path = self._speech_paths.get(self._speech_index)
            if path is not None:
                self._play_speech_segment(path)
            elif self._speech_worker is None or not self._speech_worker.isRunning():
                self._finish_speech()
        self._sync_speech_controls()

    def _stop_speech(self) -> None:
        worker = self._speech_worker
        self._speech_active = False
        self._speech_paused = False
        self._speech_document_start = None
        self._speech_message_start = None
        self.transcript.setExtraSelections([])
        self._player.stop()
        self._player.setSource(QUrl())
        self._sync_speech_controls()
        if worker is not None:
            self._speech_worker = None
            if worker.isRunning():
                worker.requestInterruption()
            else:
                self._clear_speech_output(worker)

    def _finish_speech(self) -> None:
        self._speech_active = False
        self._speech_paused = False
        self._speech_document_start = None
        self._speech_message_start = None
        self.transcript.setExtraSelections([])
        self._player.stop()
        self._player.setSource(QUrl())
        self._sync_speech_controls()
        worker = self._speech_worker
        if worker is not None and not worker.isRunning():
            self._speech_worker = None
            self._clear_speech_output(worker)

    @staticmethod
    def _clear_speech_output(worker: SpeechWorker) -> None:
        worker.cleanup_output()
        worker.deleteLater()

    @property
    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    @property
    def is_speech_busy(self) -> bool:
        return (
            self._speech_worker is not None
            and self._speech_worker.isRunning()
        )

    def stop_speech(self) -> None:
        self._stop_speech()
