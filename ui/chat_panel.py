"""Standalone conversational chat panel for the selected translation model."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QKeySequence,
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
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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
        "clearly stated in the passage, use the available Google Search tool "
        "and cite its sources; do not limit your answer to the passage."
    ),
}


class ChatTranscriptBrowser(QTextBrowser):
    textPositionClicked = Signal(int)
    refreshRequested = Signal()
    reopenRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().installEventFilter(self)
        self._context_menu = self._create_context_menu()
        self._copy_action = self._context_menu.actions()[0]
        self._select_all_action = self._context_menu.actions()[1]

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.RightButton
        ):
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _show_context_menu(self, global_position) -> None:
        has_text = bool(self.toPlainText().strip())
        self._copy_action.setEnabled(has_text)
        self._select_all_action.setEnabled(has_text)
        self._context_menu.popup(global_position)

    def _create_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        copy_action = menu.addAction("Copy")
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.setEnabled(bool(self.toPlainText().strip()))
        copy_action.triggered.connect(self._copy_chat_text)
        select_all_action = menu.addAction("Select All")
        select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all_action.setEnabled(bool(self.toPlainText().strip()))
        select_all_action.triggered.connect(self.selectAll)
        menu.addSeparator()
        refresh_action = menu.addAction("⟳ Refresh")
        refresh_action.triggered.connect(self.refreshRequested.emit)
        reopen_action = menu.addAction("↻ ReOpen")
        reopen_action.triggered.connect(self.reopenRequested.emit)
        return menu

    def _copy_chat_text(self) -> None:
        cursor = self.textCursor()
        text = (
            cursor.selectedText()
            if cursor.hasSelection()
            else self.toPlainText()
        )
        QApplication.clipboard().setText(
            text.replace("\u2029", "\n").replace("\u2028", "\n"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            cursor = self.cursorForPosition(event.position().toPoint())
            self.textPositionClicked.emit(cursor.position())
        super().mousePressEvent(event)


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
        self._speech_text = ""
        self._speech_active = False
        self._speech_highlight_background = QColor("#26354A")
        self._speech_highlight_foreground = QColor("#FFFFFF")

        self.setObjectName("standaloneChatPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        chat_actions = QHBoxLayout()
        chat_actions.addStretch(1)
        self.clear_button = QPushButton("New chat")
        self.clear_button.setObjectName("newChatButton")
        self.clear_button.clicked.connect(self.clear_chat)
        self.explain_button = QPushButton("Explain")
        self.explain_button.setObjectName("explainSelectionButton")
        self.explain_button.setToolTip(
            "Explain the selected text from the translation")
        self.explain_button.setEnabled(False)
        self.explain_button.clicked.connect(
            lambda: self.explain_selection())
        self.read_aloud_button = QPushButton("Read aloud")
        self.read_aloud_button.setObjectName("chatReadAloud")
        self.read_aloud_button.clicked.connect(self._read_latest_chat_aloud)
        self.stop_speech_button = QPushButton("Stop")
        self.stop_speech_button.setObjectName("chatStopSpeech")
        self.stop_speech_button.clicked.connect(self._stop_speech)
        self.stop_speech_button.setEnabled(False)
        chat_actions.addWidget(self.read_aloud_button)
        chat_actions.addWidget(self.stop_speech_button)
        chat_actions.addWidget(self.explain_button)
        chat_actions.addWidget(self.clear_button)

        self.context_notice = QLabel()
        self.context_notice.setObjectName("chatContextNotice")
        self.context_notice.setWordWrap(True)
        self.context_notice.hide()
        layout.addWidget(self.context_notice)

        self.transcript = ChatTranscriptBrowser(self)
        self.transcript.setObjectName("standaloneChatTranscript")
        self.transcript.setOpenExternalLinks(True)
        self.transcript.textPositionClicked.connect(
            self._on_transcript_position_clicked)
        layout.addWidget(self.transcript, 1)
        layout.addLayout(chat_actions)

        composer = QHBoxLayout()
        composer.setSpacing(8)
        self.input = ChatInputEdit(self)
        self.input.setObjectName("standaloneChatInput")
        self.input.setPlaceholderText(
            "Ask a question… (Enter to send; Shift+Enter for a new line)")
        self.input.setFixedHeight(84)
        self.input.sendRequested.connect(self.send_message)
        composer.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("standaloneChatSend")
        self.send_button.setFixedHeight(44)
        self.send_button.clicked.connect(self.send_message)
        composer.addWidget(self.send_button)
        layout.addLayout(composer)
        for widget in (
            self, self.transcript, self.transcript.viewport(),
            self.input, self.input.viewport(),
        ):
            widget.installEventFilter(self)
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setPlaybackRate(
            normalize_speed(self._settings_provider().get("speech_rate")))
        self._player.positionChanged.connect(self._update_speech_highlight)
        self._player.mediaStatusChanged.connect(
            self._on_speech_media_status_changed)
        self._player.errorOccurred.connect(self._on_speech_player_error)
        self._font_size_save_timer = QTimer(self)
        self._font_size_save_timer.setSingleShot(True)
        self._font_size_save_timer.setInterval(500)
        self._font_size_save_timer.timeout.connect(
            self._save_chat_font_size)
        self._streaming_response = ""
        self._update_context_notice()
        self._apply_chat_font_size()

    def set_translation_context(self, translated_text: str) -> None:
        self._translation_context = translated_text.strip()
        self._update_context_notice()

    def set_explanation_selection(self, selected_text: str) -> None:
        self._explanation_selection = selected_text.strip()
        self.explain_button.setEnabled(
            bool(self._explanation_selection) and self._worker is None)

    def _update_context_notice(self) -> None:
        self.context_notice.clear()
        self.context_notice.hide()

    def apply_theme(self, colors: dict[str, str]) -> None:
        self._speech_highlight_background = QColor(colors["selection"])
        self._speech_highlight_foreground = QColor(colors["text"])
        self.setStyleSheet("""
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
                background-color: %(surface)s;
                selection-background-color: %(accent)s;
                selection-color: %(text)s;
            }
            QPlainTextEdit#standaloneChatInput {
                color: %(text)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
                padding: 8px;
                background-color: %(surface)s;
                selection-background-color: %(accent)s;
                selection-color: %(text)s;
            }
        """ % colors)
        self._apply_chat_font_size()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta:
                    self._set_chat_font_size(
                        self._chat_font_size + (1 if delta > 0 else -1))
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _set_chat_font_size(self, size: float) -> None:
        self._chat_font_size = min(30.0, max(8.0, size))
        self._settings_provider()["chat_font_size"] = self._chat_font_size
        self._apply_chat_font_size()
        self._font_size_save_timer.start()

    def _save_chat_font_size(self) -> None:
        from config.settings import save

        try:
            save(self._settings_provider())
        except (OSError, TypeError, ValueError):
            log.exception("Could not save chat font size")

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
        selected_text = self._explanation_selection.strip()
        if not selected_text:
            return
        instruction = _EXPLANATION_INSTRUCTIONS[style]
        self._send_question(
            f"{instruction}\n\nSelected text:\n{selected_text}",
            clear_input=False,
        )

    def _send_question(self, question: str, *, clear_input: bool = True) -> None:
        if not question or self._worker is not None:
            return
        self._stop_speech()
        self._messages.append({"role": "user", "content": question})
        self._entries.append(("user", question))
        if clear_input:
            self.input.clear()
        self.send_button.setEnabled(False)
        self.send_button.setText("…")
        self.explain_button.setEnabled(False)
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
        self._streaming_response = ""
        self._messages.append({"role": "assistant", "content": answer})
        self._entries.append(("assistant", answer))
        self._render()

    def _on_error(self, error: str) -> None:
        if self.sender() is not self._worker:
            return
        self._streaming_response = ""
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()
        self._entries.append(("error", error))
        self._render()

    def _on_finished(self) -> None:
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        self.send_button.setText("Send")
        self.send_button.setEnabled(True)
        self.explain_button.setEnabled(bool(self._explanation_selection))
        if isinstance(worker, ChatWorker):
            worker.deleteLater()

    def clear_chat(self) -> None:
        if self._worker is not None:
            return
        self._stop_speech()
        self._messages.clear()
        self._entries.clear()
        self.transcript.clear()
        self.input.clear()

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
        if self._streaming_response:
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
            else:
                blocks.append(f"**Error**\n\n{text}")
        self.transcript.setMarkdown("\n\n---\n\n".join(blocks))
        self.transcript.verticalScrollBar().setValue(
            self.transcript.verticalScrollBar().maximum())
        self._sync_speech_controls()

    def _on_partial_reply(self, text: str) -> None:
        if self.sender() is not self._worker:
            return
        self._streaming_response = text
        self._render()

    def _on_transcript_position_clicked(self, position: int) -> None:
        self._start_speech_from_position(position)

    def _start_speech_from_position(self, position: int) -> None:
        settings = dict(self._settings_provider())
        engine = str(settings.get("speech_engine", "edge"))
        api_key = str(settings.get("gemini_key") or "")
        if not speech_available(engine, api_key):
            self.context_notice.setText(speech_missing_message(engine))
            self.context_notice.show()
            return
        self._update_context_notice()

        document = self.transcript.document()
        cursor = QTextCursor(document)
        cursor.setPosition(max(0, min(
            position, document.characterCount() - 1)))
        blocks = []
        block = document.begin()
        while block.isValid():
            blocks.append(block)
            block = block.next()
        clicked_block_index = cursor.block().blockNumber()
        assistant_marker = next(
            (index for index in range(clicked_block_index, -1, -1)
             if blocks[index].text().strip() == "Chat"),
            None,
        )
        if assistant_marker is None or assistant_marker == clicked_block_index:
            return
        if any(
            blocks[index].text().strip() in {"You", "Error"}
            for index in range(assistant_marker + 1, clicked_block_index + 1)
        ):
            return

        message_blocks = []
        for block in blocks[clicked_block_index:]:
            if block.text().strip() in {"You", "Chat", "Error"}:
                break
            message_blocks.append(block)
        if not message_blocks:
            return
        clicked_text = message_blocks[0].text()

        text_position = qt_to_python_position(
            clicked_text, cursor.positionInBlock())
        start = speech_start_offset(clicked_text, text_position)
        if start >= len(clicked_text):
            return
        word_start, word_end = speech_word_span(clicked_text, start)
        self._stop_speech()
        self._speech_text = "\n".join(
            [clicked_text[start:]]
            + [block.text() for block in message_blocks[1:]])
        self._speech_document_start = (
            message_blocks[0].position()
            + python_to_qt_position(clicked_text, start))
        self._speech_index = 0
        self._speech_paths.clear()
        self._speech_boundaries.clear()
        self._speech_active = True
        self._highlight_speech_range(word_start - start, word_end - start)
        self._sync_speech_controls()
        self._player.setPlaybackRate(
            normalize_speed(settings.get("speech_rate")))

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
            target_language=str(settings.get("target_language", "ar")),
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
        if index == self._speech_index:
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

    def _stop_speech(self) -> None:
        worker = self._speech_worker
        self._speech_active = False
        self._speech_document_start = None
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
        self._speech_document_start = None
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
