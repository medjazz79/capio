"""Tabbed book and document view with page thumbnails and selection support."""
from __future__ import annotations

import math
import os
import tempfile
import time
from html import escape
from pathlib import Path

import pymupdf
from PySide6.QtCore import QEvent, QPoint, QRect, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor, QTransform
from PySide6.QtWidgets import (
    QHBoxLayout, QSplitter, QStackedWidget, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from config.app_log import get_logger
from reader import pdf_utils
from reader.continuous import ContinuousReader
from reader.document_import import prepare_document, text_direction
from reader.thumbnails import ThumbPanel

log = get_logger("reader.bookview")


class BookView(QWidget):
    pageSelected = Signal(int, QRect)
    pageContextRequested = Signal(int, QPoint)
    viewChanged = Signal()
    zoomChanged = Signal(float)

    MIN_ZOOM = 20.0
    MAX_ZOOM = 1000.0

    # Rendered-pixel budget for one page raster.
    #
    # MAX_ZOOM alone is not a memory bound: at 1000% a Letter page is
    # 6120 x 7920 = 48.5 MPix, i.e. 145 MB for the Format_RGB888 QImage,
    # which ContinuousReader then duplicates into a 32-bit QPixmap
    # (reader/continuous.py), so ~291 MB for a single page before any other
    # page is counted. Measured resident on this machine: 290-580 MB.
    #
    # 12 MPix brings that to 36 MB of QImage + 48 MB of pixmap (~84 MB), a
    # 3.5x cut, and is still far past useful reading zoom: it allows a native
    # raster up to ~497% on Letter, so only the far end of the zoom range is
    # affected. The zoom *percentage* range is unchanged (1000% still selects
    # and reports 1000%); the cap applies to the raster, and the label keeps
    # its full logical size, so the QLabel scales the coarser raster down for
    # display and scroll position and selection coordinates are unaffected.
    MAX_RASTER_PIXELS = 12_000_000

    def __init__(self, ocr_langs: str = "auto", parent=None):
        super().__init__(parent)
        self.path = ""
        self.title = "Untitled"
        self.ocr_langs = ocr_langs
        self.pinned = False
        self.last_selection: tuple[int, QRect] | None = None
        self.bookmarked_pages: set[int] = set()
        self.pending_page: int | None = None
        self.pending_scroll_position: int | None = None
        self.pending_scroll_offset: int | None = None
        self.pending_horizontal_scroll_position: int | None = None
        self.initial_fit_pending = False
        self._document: pymupdf.Document | None = None
        self._document_path = ""
        self._temporary_view_path: str | None = None
        self._page_sizes: list[tuple[float, float]] = []
        self._view_rotations: list[int] = []
        self._zoom_percent = 100.0
        self._modified = False
        self._source_text_modified = False
        self._pdf_view_modified = False
        self._source_encoding = "utf-8"
        self._source_newline = "\n"
        self._source_editor_font_size = 18
        self._loading_source_text = False
        self._updating_source_directions = False
        self._editing_source = False

        import threading
        self._doc_lock = threading.RLock()
        self.thumbs = ThumbPanel()
        self.reader = ContinuousReader(self._render_page)
        self.reader.pageSelected.connect(self.pageSelected)
        self.reader.pageContextRequested.connect(self.pageContextRequested)
        self.reader.currentPageChanged.connect(self._on_current_page_changed)
        self.reader.viewPositionChanged.connect(
            self.thumbs.mark_current_position)
        self.reader.zoomStep.connect(self.zoom_step)
        self.reader.initialRenderComplete.connect(self.thumbs.start_rendering)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.thumbs)
        self.splitter.addWidget(self.reader)
        self.splitter.setStretchFactor(1, 1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.splitter)
        self.source_editor = QTextEdit()
        self.source_editor.setObjectName("sourceTextEditor")
        self.source_editor.setAcceptRichText(False)
        self.source_editor.textChanged.connect(self._on_source_text_changed)
        self.source_editor.viewport().installEventFilter(self)
        self.source_editor.setToolTip(
            "Edit source text. Hold Ctrl and scroll to change font size.")
        self.view_stack.addWidget(self.source_editor)
        layout.addWidget(self.view_stack)
        self.thumbs.pageChosen.connect(self.scroll_to_page)
        self.thumbs.zoomRequested.connect(self.thumbs_zoom)
        self.thumbs.bookmarkRequested.connect(self.toggle_bookmark)
        self.thumbs.bookmarkJumpRequested.connect(self.jump_to_bookmark)
        self.reader.verticalScrollBar().valueChanged.connect(
            lambda _value: self.viewChanged.emit())
        self.reader.horizontalScrollBar().valueChanged.connect(
            lambda _value: self.viewChanged.emit())

    @property
    def zoom_percent(self) -> float:
        return self._zoom_percent

    @property
    def zoom(self) -> float:
        return self._zoom_percent / 100.0

    def open(
        self,
        path: str,
        zoom_percent: float | None = None,
        splitter_sizes: list[int] | None = None,
        defer_render: bool = False,
    ) -> None:
        total_started = time.perf_counter()
        phase_started = total_started
        self.close_doc()
        close_ms = (time.perf_counter() - phase_started) * 1000

        phase_started = time.perf_counter()
        prepared = prepare_document(path)
        try:
            document = pdf_utils.open_doc(prepared.view_path)
        except Exception:
            if prepared.temporary_path:
                Path(prepared.temporary_path).unlink(missing_ok=True)
            raise
        open_ms = (time.perf_counter() - phase_started) * 1000
        self._document = document
        self._document_path = prepared.view_path
        self._temporary_view_path = prepared.temporary_path
        self.path = str(Path(path))
        self.title = Path(path).stem
        self._editing_source = False
        if self.is_text_source:
            text, self._source_encoding, self._source_newline = (
                self._read_editable_source(Path(path)))
            self._loading_source_text = True
            try:
                self._set_source_editor_text(text)
            finally:
                self._loading_source_text = False
        self.view_stack.setCurrentWidget(self.splitter)
        phase_started = time.perf_counter()
        self._page_sizes = [
            (float(page.rect.width), float(page.rect.height))
            for page in document
        ]
        self._view_rotations = [0] * len(self._page_sizes)
        geometry_ms = (time.perf_counter() - phase_started) * 1000
        self._zoom_percent = zoom_percent if zoom_percent is not None else 100.0
        self._zoom_percent = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom_percent))

        phase_started = time.perf_counter()
        self.reader.set_zoom(self.zoom)
        self.reader.set_document(
            self._display_page_sizes(), render=not defer_render)
        reader_setup_ms = (time.perf_counter() - phase_started) * 1000

        phase_started = time.perf_counter()
        self.thumbs.set_document(
            self._document_path, len(self._page_sizes),
            render_thumbnails=False)
        thumbnails_setup_ms = (time.perf_counter() - phase_started) * 1000
        self._update_bookmark_ui()
        self.initial_fit_pending = zoom_percent is None
        if splitter_sizes:
            self.splitter.setSizes(splitter_sizes)
        else:
            self.splitter.setSizes([180, 720])
        self.viewChanged.emit()
        log.info(
            "Opened document (%d pages): close_previous=%.1fms open_document=%.1fms "
            "page_geometry=%.1fms reader_widgets=%.1fms thumbnails_setup=%.1fms "
            "total=%.1fms deferred_render=%s",
            len(self._page_sizes),
            close_ms,
            open_ms,
            geometry_ms,
            reader_setup_ms,
            thumbnails_setup_ms,
            (time.perf_counter() - total_started) * 1000,
            defer_render,
        )

    def close_doc(self) -> None:
        # Reset first: every field below is per-document state. `open()` is
        # written as reusable, and leaving these set meant session_state()
        # serialised the previous book's rectangle and Ctrl+T would translate
        # page N of the new book with the old book's selection.
        self.last_selection = None
        self.pending_page = None
        self.pending_scroll_position = None
        self.pending_scroll_offset = None
        self.pending_horizontal_scroll_position = None
        self.initial_fit_pending = False
        temporary_path = self._temporary_view_path
        thumbnail_worker = getattr(self.thumbs, "_worker", None)
        self.thumbs.clear_document()
        self.reader.clear_document()
        document = self._document
        self._document = None
        if document is not None:
            document.close()
        if temporary_path:
            self._remove_temporary_view_file(temporary_path, thumbnail_worker)
        self._document_path = ""
        self._temporary_view_path = None
        self._page_sizes = []
        self._view_rotations = []
        self.bookmarked_pages.clear()
        self._modified = False
        self._source_text_modified = False
        self._pdf_view_modified = False
        self._editing_source = False
        self.path = ""
        self.title = "Untitled"

    def has_doc(self) -> bool:
        return self._document is not None

    @property
    def can_save_in_place(self) -> bool:
        return bool(self.path) and (
            Path(self.path).suffix.lower() == ".pdf"
            or (self.is_text_source
                and self._source_text_modified
                and not self._pdf_view_modified))

    @property
    def is_text_source(self) -> bool:
        return Path(self.path).suffix.lower() in {
            ".txt", ".md", ".markdown",
        }

    @property
    def is_editing_source(self) -> bool:
        return self._editing_source

    @property
    def can_edit_source(self) -> bool:
        return (
            self.has_doc()
            and self.is_text_source
            and not self._pdf_view_modified
        )

    @property
    def has_unsaved_source_text(self) -> bool:
        return self._source_text_modified

    @property
    def is_modified(self) -> bool:
        return self._modified

    def save(self) -> None:
        if self._document is None:
            raise RuntimeError("Cannot save without an open document")
        if self.is_text_source:
            if not self._source_text_modified or self._pdf_view_modified:
                raise RuntimeError(
                    "Save the edited text source separately from PDF page changes.")
            self._save_text_source()
            was_editing = self._editing_source
            zoom = self._zoom_percent
            splitter_sizes = self.splitter.sizes()
            path = self.path
            self.open(path, zoom_percent=zoom, splitter_sizes=splitter_sizes)
            if was_editing:
                self.set_source_editing(True)
            return
        if not self.can_save_in_place:
            raise RuntimeError(
                "This source format is read-only. Use Save As to create a PDF.")
        if not self._modified:
            return
        self._save_document_to(self.path)
        self._modified = False
        self._source_text_modified = False
        self._pdf_view_modified = False
        self.thumbs.set_document(
            self._document_path,
            len(self._page_sizes),
            render_thumbnails=False,
            view_rotations=self._view_rotations,
        )
        self._update_bookmark_ui()
        self.viewChanged.emit()

    @staticmethod
    def _read_editable_source(
        source: Path,
    ) -> tuple[str, str, str]:
        data = source.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif data.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"
        text = data.decode(encoding)
        newline = "\r\n" if "\r\n" in text else (
            "\r" if "\r" in text else "\n")
        return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, newline

    def _on_source_text_changed(self) -> None:
        if (
            self._loading_source_text
            or self._updating_source_directions
            or not self.is_text_source
        ):
            return
        self._modified = True
        self._source_text_modified = True
        self._apply_source_editor_directions()
        self.viewChanged.emit()

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.source_editor.viewport()
            and event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and self.is_editing_source
        ):
            delta = event.angleDelta().y()
            if delta:
                self._zoom_source_editor_font(1 if delta > 0 else -1)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _zoom_source_editor_font(self, steps: int) -> None:
        self._source_editor_font_size = max(
            8, min(48, self._source_editor_font_size + steps))
        self._apply_source_editor_style()

    def _set_source_editor_text(self, text: str) -> None:
        paragraphs = []
        for line in text.split("\n"):
            direction = text_direction(line)
            alignment = "right" if direction == "rtl" else "left"
            content = line or "\u200b"
            paragraphs.append(
                f'<p dir="{direction}" align="{alignment}">'
                f'{escape(content)}</p>'
            )
        self.source_editor.setHtml(
            "<html><body>" + "".join(paragraphs) + "</body></html>")

    def _source_editor_text(self) -> str:
        return self.source_editor.toPlainText().replace("\u200b", "")

    def _apply_source_editor_directions(self) -> None:
        if self._updating_source_directions:
            return
        editor = self.source_editor
        active_cursor = editor.textCursor()
        position = active_cursor.position()
        anchor = active_cursor.anchor()
        self._updating_source_directions = True
        try:
            block_positions = []
            block = editor.document().begin()
            while block.isValid():
                block_positions.append(block.position())
                block = block.next()
            for block_position in block_positions:
                block = editor.document().findBlock(block_position)
                text = block.text().replace("\u200b", "")
                direction = text_direction(text)
                desired_direction = (
                    Qt.LayoutDirection.RightToLeft
                    if direction == "rtl"
                    else Qt.LayoutDirection.LeftToRight
                )
                if block.blockFormat().layoutDirection() == desired_direction:
                    continue
                alignment = "right" if direction == "rtl" else "left"
                content = escape(text or "\u200b")
                cursor = QTextCursor(block)
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.insertHtml(
                    f'<p dir="{direction}" align="{alignment}">{content}</p>')
        finally:
            self._updating_source_directions = False
        active_cursor = QTextCursor(editor.document())
        max_position = editor.document().characterCount() - 1
        active_cursor.setPosition(min(anchor, max_position))
        active_cursor.setPosition(
            min(position, max_position), QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(active_cursor)

    def discard_source_text_changes(self) -> None:
        if not self._source_text_modified:
            return
        was_editing = self._editing_source
        self.open(
            self.path,
            zoom_percent=self._zoom_percent,
            splitter_sizes=self.splitter.sizes(),
        )
        if was_editing:
            self.set_source_editing(True)

    def _save_text_source(self) -> None:
        destination = Path(self.path)
        text = self._source_editor_text().replace(
            "\n", self._source_newline)
        payload = text.encode(self._source_encoding)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=destination.suffix,
            dir=str(destination.parent))
        try:
            with os.fdopen(descriptor, "wb") as source_file:
                source_file.write(payload)
                source_file.flush()
                os.fsync(source_file.fileno())
            os.replace(temporary_path, destination)
        except Exception:
            Path(temporary_path).unlink(missing_ok=True)
            raise
        self._modified = False

    def set_source_editing(self, enabled: bool) -> None:
        if enabled and not self.can_edit_source:
            raise RuntimeError("Only TXT and Markdown sources can be edited")
        if enabled:
            self._apply_source_editor_directions()
        self._editing_source = enabled
        self.view_stack.setCurrentWidget(
            self.source_editor if enabled else self.splitter)
        if enabled:
            self.source_editor.setFocus()
        self.viewChanged.emit()

    def save_as_pdf(self, output_path: str) -> None:
        if self._document is None:
            raise RuntimeError("Cannot save without an open document")
        destination = Path(output_path).expanduser().resolve()
        if destination == Path(self.path).resolve():
            self.save()
            return
        self._save_document_to(str(destination))
        saved_document = pdf_utils.open_doc(str(destination))
        previous_document = self._document
        thumbnail_worker = getattr(self.thumbs, "_worker", None)
        temporary_path = self._temporary_view_path
        self._document = saved_document
        self._document_path = str(destination)
        self.path = str(destination)
        self.title = destination.stem
        self._modified = False
        self._source_text_modified = False
        self._pdf_view_modified = False
        self._editing_source = False
        self.view_stack.setCurrentWidget(self.splitter)
        if previous_document is not None:
            previous_document.close()
        self._temporary_view_path = None
        self.thumbs.set_document(
            self._document_path,
            len(self._page_sizes),
            render_thumbnails=False,
            view_rotations=self._view_rotations,
        )
        if temporary_path:
            self._remove_temporary_view_file(
                temporary_path, thumbnail_worker)
        self._update_bookmark_ui()
        self.viewChanged.emit()

    @staticmethod
    def _remove_temporary_view_file(
        path: str,
        thumbnail_worker: QThread | None,
    ) -> None:
        if (
            thumbnail_worker is not None
            and thumbnail_worker.isRunning()
            and getattr(thumbnail_worker, "_path", None) == path
        ):
            thumbnail_worker.finished.connect(
                lambda temporary_path=path:
                    Path(temporary_path).unlink(missing_ok=True))
            if thumbnail_worker.isRunning():
                return
        Path(path).unlink(missing_ok=True)

    def _save_document_to(self, output_path: str) -> None:
        if self._document is None:
            raise RuntimeError("Cannot save without an open PDF")
        destination = Path(output_path)
        if destination.suffix.lower() != ".pdf":
            raise ValueError("The output file must use the .pdf extension")
        if not destination.parent.is_dir():
            raise FileNotFoundError(
                f"The destination folder does not exist: {destination.parent}")

        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".pdf",
            dir=str(destination.parent))
        os.close(descriptor)
        os.unlink(temporary_path)
        try:
            self._document.save(temporary_path, garbage=4, deflate=True)
            if destination.resolve() == Path(self.path).resolve():
                document = self._document
                self._document = None
                document.close()
                try:
                    os.replace(temporary_path, destination)
                except Exception:
                    self._document = pdf_utils.open_doc(self.path)
                    raise
                self._document = pdf_utils.open_doc(self.path)
            else:
                os.replace(temporary_path, destination)
        finally:
            Path(temporary_path).unlink(missing_ok=True)

    def page_count(self) -> int:
        return len(self._page_sizes)

    @property
    def metadata(self) -> dict[str, str]:
        if self._document is None:
            raise RuntimeError("Cannot read PDF metadata without an open PDF")
        return dict(self._document.metadata)

    def delete_pages(self, page_indices: list[int]) -> int:
        if self._document is None:
            raise RuntimeError("Cannot delete pages without an open PDF")
        pages = sorted(set(page_indices))
        if not pages:
            return 0
        if any(page < 0 or page >= self.page_count() for page in pages):
            raise ValueError("One or more selected pages are out of range")
        if len(pages) >= self.page_count():
            raise ValueError("A PDF must contain at least one page")

        current_page = self.current_page()
        deleted = set(pages)
        bookmarks = {
            page - sum(deleted_page < page for deleted_page in pages)
            for page in self.bookmarked_pages
            if page not in deleted
        }
        new_current_page = current_page - sum(
            page <= current_page for page in pages)

        for page in reversed(pages):
            self._document.delete_page(page)
        self._page_sizes = [
            size for index, size in enumerate(self._page_sizes)
            if index not in deleted
        ]
        self._view_rotations = [
            rotation for index, rotation in enumerate(self._view_rotations)
            if index not in deleted
        ]
        self.bookmarked_pages = bookmarks
        self.last_selection = None
        self.reader.set_document(self._display_page_sizes())
        self.thumbs.set_document(
            self._document_path,
            len(self._page_sizes),
            render_thumbnails=False,
            view_rotations=self._view_rotations,
        )
        self._update_bookmark_ui()
        self.reader.scroll_to_page(
            min(new_current_page, len(self._page_sizes) - 1))
        self._modified = True
        self._pdf_view_modified = True
        self.viewChanged.emit()
        return len(pages)

    def current_page(self) -> int:
        return self.reader.current_page()

    def _display_page_sizes(self) -> list[tuple[float, float]]:
        return [
            (height, width) if rotation % 180 else (width, height)
            for (width, height), rotation in zip(
                self._page_sizes, self._view_rotations)
        ]

    def rotate_page(self, page_index: int, degrees: int) -> int:
        """Rotate a page for this view only; no PDF document metadata changes."""
        if self._document is None:
            raise RuntimeError("Cannot rotate a page without an open PDF")
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        if degrees not in (-90, 90):
            raise ValueError("Page rotation must be -90 or 90 degrees")

        anchor = self.reader._anchor_state()
        self._view_rotations[page_index] = (
            self._view_rotations[page_index] + degrees) % 360
        self.reader.set_document(self._display_page_sizes())
        if anchor is not None:
            QTimer.singleShot(0, lambda: self.reader._restore_anchor(anchor))
        self.thumbs.set_document(
            self._document_path,
            self.page_count(),
            render_thumbnails=True,
            view_rotations=self._view_rotations,
        )
        self._update_bookmark_ui()
        self.viewChanged.emit()
        return self._view_rotations[page_index]

    def page_text(self, page_index: int) -> str:
        if self._document is None:
            raise RuntimeError("Cannot read text without an open PDF")
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        return pdf_utils.page_text(self._document[page_index])

    def page_body_text(self, page_index: int) -> tuple[str, str | None]:
        if self._document is None:
            raise RuntimeError("Cannot read text without an open PDF")
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        return pdf_utils.page_body_text(self._document[page_index])

    def scroll_to_page(self, page: int) -> None:
        self.reader.scroll_to_page(page)

    def _render_page(self, page_index: int, zoom: float):
        if self._document is None:
            raise RuntimeError("Cannot render a page without an open PDF")
        with self._doc_lock:
            if self._document is None:
                raise RuntimeError("Document closed during render")
            page = self._document[page_index]
            raster_zoom = self._raster_zoom(page_index, zoom)
        # render_page does I/O-bound PyMuPDF work; lock released to avoid blocking GUI
        # but page object already captured. Re-acquire for safety if needed:
        image = pdf_utils.render_page(page, raster_zoom)
        rotation = self._view_rotations[page_index]
        if rotation:
            image = image.transformed(
                QTransform().rotate(rotation),
                Qt.TransformationMode.SmoothTransformation,
            )
        return image

    def _raster_zoom(self, page_index: int, zoom: float) -> float:
        """Largest zoom whose raster for this page stays inside the budget.

        Returns `zoom` unchanged unless the page would exceed
        MAX_RASTER_PIXELS, in which case it is scaled down uniformly so the
        aspect ratio survives. The label is still sized at the full zoom, so
        what the user sees is the same page at the same scroll position, just
        with a coarser raster instead of an out-of-memory allocation.
        """
        if zoom <= 0 or not 0 <= page_index < len(self._page_sizes):
            return zoom
        width, height = self._page_sizes[page_index]
        if width <= 0 or height <= 0:
            return zoom
        budget = self.MAX_RASTER_PIXELS
        if math.ceil(width * zoom) * math.ceil(height * zoom) <= budget:
            return zoom
        # PyMuPDF rounds the pixel box up, so the predicted size has to round
        # the same way: predicting with floor/round let the real raster land a
        # few thousand pixels over the budget.
        scale = zoom
        pixels = math.ceil(width * zoom) * math.ceil(height * zoom)
        for _ in range(16):
            scale *= math.sqrt(budget / pixels) * 0.999
            pixels = math.ceil(width * scale) * math.ceil(height * scale)
            if pixels <= budget:
                break
        log.info(
            "Raster for page %d capped at %.1f%% of the requested %.1f%% to "
            "stay inside the %d pixel budget",
            page_index + 1, scale * 100.0, zoom * 100.0, budget,
        )
        return scale

    def _display_rect(
        self, page_index: int, rect: QRect
    ) -> tuple[float, float, float, float]:
        if not 0 <= page_index < len(self._page_sizes):
            raise IndexError(f"PDF page index out of range: {page_index}")
        scale = self.zoom
        return (
            rect.left() / scale,
            rect.top() / scale,
            rect.right() / scale,
            rect.bottom() / scale,
        )

    def _base_display_rect(
        self, page_index: int, rect: QRect,
    ) -> pymupdf.Rect:
        display_rect = pymupdf.Rect(*self._display_rect(page_index, rect))
        rotation = self._view_rotations[page_index]
        page_width, page_height = self._page_sizes[page_index]
        if rotation:
            points = (
                (display_rect.x0, display_rect.y0),
                (display_rect.x1, display_rect.y0),
                (display_rect.x0, display_rect.y1),
                (display_rect.x1, display_rect.y1),
            )
            if rotation == 90:
                points = tuple(
                    (page_width - y, x) for x, y in points)
            elif rotation == 180:
                points = tuple(
                    (page_width - x, page_height - y)
                    for x, y in points
                )
            else:
                points = tuple(
                    (y, page_height - x) for x, y in points)
            xs, ys = zip(*points)
            display_rect = pymupdf.Rect(
                min(xs), min(ys), max(xs), max(ys))
        return display_rect

    def _page_rect(
        self, page_index: int, rect: QRect
    ) -> tuple[float, float, float, float]:
        if self._document is None:
            raise RuntimeError("Cannot map a selection without an open PDF")
        display_rect = self._base_display_rect(page_index, rect)
        page = self._document[page_index]
        unrotated_rect = display_rect * page.derotation_matrix
        return (
            unrotated_rect.x0,
            unrotated_rect.y0,
            unrotated_rect.x1,
            unrotated_rect.y1,
        )

    def extract(self, page_index: int, rect: QRect) -> tuple[str, str]:
        if self._document is None:
            raise RuntimeError("Cannot extract text without an open PDF")
        extraction = pdf_utils.extract_region(
            self._document[page_index], self._page_rect(page_index, rect))
        if extraction.text:
            return "text", extraction.text
        return "image", ""

    def page_needs_full_page_ocr(self, page_index: int) -> bool:
        if self._document is None:
            raise RuntimeError("Cannot inspect a page without an open PDF")
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        return pdf_utils.has_sparse_text_over_scan(self._document[page_index])

    def render_crop(self, page_index: int, rect: QRect, dpi: float = 300):
        if self._document is None:
            raise RuntimeError("Cannot render a crop without an open PDF")
        display_rect = self._base_display_rect(page_index, rect)
        # Pixmap clips use page.rect coordinates; _page_rect is for the
        # unrotated text coordinates consumed by extract_region.
        image = pdf_utils.render_crop(
            self._document[page_index],
            (
                display_rect.x0,
                display_rect.y0,
                display_rect.x1,
                display_rect.y1,
            ),
            dpi=dpi,
        )
        rotation = self._view_rotations[page_index]
        if rotation:
            image = image.transformed(
                QTransform().rotate(rotation),
                Qt.TransformationMode.SmoothTransformation,
            )
        return image

    def page_rect_on_screen(self, page_index: int) -> QRect:
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        width, height = self._display_page_sizes()[page_index]
        return QRect(
            0, 0,
            round(width * self.zoom) + 1,
            round(height * self.zoom) + 1,
        )

    def render_page_image(self, page_index: int, dpi: float = 150):
        if self._document is None:
            raise RuntimeError("Cannot render a page without an open PDF")
        if not 0 <= page_index < self.page_count():
            raise IndexError(f"PDF page index out of range: {page_index}")
        image = pdf_utils.render_page_at_dpi(self._document[page_index], dpi)
        rotation = self._view_rotations[page_index]
        if rotation:
            image = image.transformed(
                QTransform().rotate(rotation),
                Qt.TransformationMode.SmoothTransformation,
            )
        return image

    def save_pages_as_pdf(self, page_indices: list[int], output_path: str) -> None:
        if self._document is None:
            raise RuntimeError("Cannot export pages without an open PDF")
        pages = sorted(set(page_indices))
        if not pages or any(not 0 <= page < self.page_count() for page in pages):
            raise ValueError("Select one or more valid PDF pages to export")
        if Path(output_path).resolve() == Path(self.path).resolve():
            raise ValueError("The output PDF cannot overwrite the open source book")
        output = pymupdf.open()
        try:
            for page_index in pages:
                output.insert_pdf(
                    self._document,
                    from_page=page_index,
                    to_page=page_index,
                )
            output.save(output_path)
        finally:
            output.close()

    def set_zoom_percent(self, value: float) -> None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        value = max(self.MIN_ZOOM, min(self.MAX_ZOOM, value))
        if abs(value - self._zoom_percent) < 0.01:
            return
        self._zoom_percent = value
        self.reader.set_zoom(self.zoom)
        self.initial_fit_pending = False
        self.zoomChanged.emit(value)
        self.viewChanged.emit()

    def zoom_step(self, steps: int) -> None:
        if steps:
            self.set_zoom_percent(self._zoom_percent * (1.1 ** steps))

    def fit_to_width(self) -> None:
        if not self._page_sizes or self.reader.viewport().width() <= 0:
            return
        page = self.current_page()
        page_width = self._display_page_sizes()[page][0]
        margin = round(self.reader.logicalDpiX() / 2.54)
        available = self.reader.viewport().width() - 2 * margin
        candidate_zoom = available / page_width
        scrollbar = self.reader.verticalScrollBar()
        if (not scrollbar.isVisible()
                and self.reader.verticalScrollBarPolicy()
                != Qt.ScrollBarPolicy.ScrollBarAlwaysOff):
            layout = self.reader._layout
            layout_margins = layout.contentsMargins()
            document_height = (
                sum(height for _width, height in self._display_page_sizes())
                * candidate_zoom
                + max(0, len(self._page_sizes) - 1) * layout.spacing()
                + layout_margins.top()
                + layout_margins.bottom()
            )
            if document_height > self.reader.viewport().height():
                available -= scrollbar.sizeHint().width()
        self.set_zoom_percent(max(1, available) / page_width * 100.0)
        self.initial_fit_pending = False

    def fit_to_page(self) -> None:
        if not self._page_sizes:
            return
        page = self.current_page()
        page_width, page_height = self._display_page_sizes()[page]
        margin = round(self.reader.logicalDpiX() / 2.54)
        available_width = self.reader.viewport().width() - 2 * margin
        available_height = self.reader.viewport().height() - 2 * margin
        if available_width <= 0 or available_height <= 0:
            return
        scale = min(available_width / page_width, available_height / page_height)
        self.set_zoom_percent(scale * 100.0)
        self.initial_fit_pending = False

    def fit_visible(self) -> None:
        if self._document is None or not self._page_sizes:
            return
        page_index = self.current_page()
        page = self._document[page_index]
        bounds = pymupdf.Rect(page.rect)
        content = pymupdf.Rect()
        for _kind, bbox in page.get_bboxlog():
            rect = pymupdf.Rect(bbox) & page.rect
            if not rect.is_empty and not rect.is_infinite:
                content |= rect
        if content.is_empty:
            self.fit_to_page()
            return
        padding = 4
        content.x0 = max(bounds.x0, content.x0 - padding)
        content.y0 = max(bounds.y0, content.y0 - padding)
        content.x1 = min(bounds.x1, content.x1 + padding)
        content.y1 = min(bounds.y1, content.y1 + padding)
        margin = round(self.reader.logicalDpiX() / 2.54)
        available_width = self.reader.viewport().width() - 2 * margin
        available_height = self.reader.viewport().height() - 2 * margin
        if available_width <= 0 or available_height <= 0:
            return
        scale = min(
            available_width / content.width,
            available_height / content.height,
        )
        self.set_zoom_percent(scale * 100.0)
        self.initial_fit_pending = False

        def scroll_to_content() -> None:
            if page_index >= len(self.reader._labels):
                return
            label = self.reader._labels[page_index]
            self.reader.horizontalScrollBar().setValue(
                round(label.x() + content.x0 * self.zoom))
            self.reader.verticalScrollBar().setValue(
                round(label.y() + content.y0 * self.zoom))

        QTimer.singleShot(0, scroll_to_content)

    def toggle_bookmark(self, page_index: int | None = None) -> None:
        page = self.current_page() if page_index is None else page_index
        if not 0 <= page < self.page_count():
            return
        if page in self.bookmarked_pages:
            self.bookmarked_pages.remove(page)
        elif 0 <= page < self.page_count():
            self.bookmarked_pages.add(page)
        self._update_bookmark_ui()
        self.viewChanged.emit()

    def jump_to_bookmark(self, page: int) -> None:
        self.thumbs.bookmarks_button.setChecked(False)
        self.scroll_to_page(page)

    def set_bookmarks(self, pages: list[int] | tuple[int, ...] | set[int] | None) -> None:
        if not isinstance(pages, (list, tuple, set)):
            pages = ()
        self.bookmarked_pages = {
            int(page) for page in pages
            if type(page) is int and 0 <= page < self.page_count()
        }
        self._update_bookmark_ui()

    def _update_bookmark_ui(self) -> None:
        self.thumbs.set_bookmarks(self.bookmarked_pages)
        self.thumbs.set_current_bookmarked(
            self.current_page() in self.bookmarked_pages)

    def refresh_visible(self) -> None:
        self.reader._ensure_visible()

    def refresh_content(self) -> None:
        """Refresh page and thumbnail rasters without resetting the open book."""
        if not self.has_doc():
            return
        thumbnail_page = self.current_page()
        thumbnail_progress = 0.0
        if 0 <= thumbnail_page < len(self.reader._labels):
            label = self.reader._labels[thumbnail_page]
            thumbnail_progress = min(
                1.0,
                max(
                    0.0,
                    (self.reader.verticalScrollBar().value() - label.y())
                    / max(1, label.height()),
                ),
            )
        thumbnail_scroll = self.thumbs.list.verticalScrollBar().value()
        bookmark_scroll = (
            self.thumbs.bookmarks_list.verticalScrollBar().value())
        bookmarks_visible = self.thumbs.bookmarks_button.isChecked()

        self.reader.refresh_pages()
        self.thumbs.set_document(
            self._document_path,
            self.page_count(),
            render_thumbnails=True,
            view_rotations=self._view_rotations,
        )
        self._update_bookmark_ui()
        self.thumbs.bookmarks_button.setChecked(bookmarks_visible)
        self.thumbs.mark_current_position(
            thumbnail_page, thumbnail_progress)
        QTimer.singleShot(
            0,
            lambda: self.thumbs.list.verticalScrollBar().setValue(
                thumbnail_scroll),
        )
        QTimer.singleShot(
            0,
            lambda: self.thumbs.bookmarks_list.verticalScrollBar().setValue(
                bookmark_scroll),
        )
        self.viewChanged.emit()

    def set_thumbs_visible(self, visible: bool) -> None:
        self.thumbs.setVisible(visible)

    def thumbs_widget(self) -> QWidget:
        return self.thumbs

    def thumbs_zoom(self, steps: int) -> None:
        if steps:
            self.thumbs.set_thumbnail_zoom(
                self.thumbs.thumbnail_zoom * (1.1 ** steps))
            self.viewChanged.emit()

    def apply_theme(self, colors: dict[str, str]) -> None:
        background = colors.get("panel", colors.get("window", "#161B22"))
        text = colors.get("text", "#F0F6FC")
        border = colors.get("border", "#30363D")
        surface = colors.get("surface", "#21262D")
        accent = colors.get("accent", "#4F46E5")
        selection = colors.get("selection", "#26354A")
        self._apply_source_editor_style()
        self.thumbs.setStyleSheet(
            f"QWidget#bookmarkRail {{ background-color: {background}; "
            f"border-right: 1px solid {border}; }}"
            f"QToolButton#bookmarkPageButton, "
            f"QToolButton#myBookmarksButton {{ color: {text}; "
            f"background: transparent; border: 1px solid transparent; "
            f"border-radius: 4px; font-size: 15px; padding: 0; }}"
            f"QToolButton#bookmarkPageButton:hover, "
            f"QToolButton#myBookmarksButton:hover {{ "
            f"background-color: {surface}; border-color: {border}; }}"
            f"QToolButton#bookmarkPageButton:checked {{ color: {accent}; "
            f"background-color: {selection}; border-color: {accent}; }}"
            f"QWidget#pdfThumbnailsPanel {{ background-color: {background}; "
            f"border-right: 1px solid {border}; }}"
            f"QListWidget#pdfThumbnails, QListWidget#pdfBookmarks "
            f"{{ background-color: {background}; "
            f"color: {text}; border: none; }}"
            f"QListWidget#pdfThumbnails::viewport, "
            f"QListWidget#pdfBookmarks::viewport "
            f"{{ background-color: {background}; }}"
            f"QListWidget#pdfThumbnails::item:selected, "
            f"QListWidget#pdfBookmarks::item:selected {{ "
            f"background-color: #8BE9FD; color: #0D1117; }}"
        )

    def _apply_source_editor_style(self) -> None:
        self.source_editor.setStyleSheet(
            "QTextEdit#sourceTextEditor {"
            " background-color: #FFFFFF; color: #202124;"
            " border: 1px solid #C8CDD3;"
            " selection-background-color: #BBD7FF;"
            " selection-color: #111827; padding: 12px;"
            f" font-family: Consolas, monospace; "
            f"font-size: {self._source_editor_font_size}px;"
            " }"
        )

    def session_state(self) -> dict:
        scroll = self.reader.verticalScrollBar()
        horizontal = self.reader.horizontalScrollBar()
        current = self.current_page()
        page_top = 0
        if 0 <= current < len(self.reader._labels):
            page_top = self.reader._labels[current].y()
        selection = None
        if self.last_selection is not None:
            page, rect = self.last_selection
            # Keep the selection in displayed page points so saved selections
            # remain compatible and can be reconstructed at the current zoom.
            try:
                x0, y0, x1, y1 = self._display_rect(page, rect)
                selection = ["pdf", page,
                             round(x0, 2), round(y0, 2),
                             round(x1, 2), round(y1, 2)]
            except (IndexError, ValueError, TypeError):
                selection = None
        return {
            "path": self.path,
            "page": current,
            "zoom": self.zoom_percent,
            "thumbnail_zoom": self.thumbs.thumbnail_zoom,
            "bookmarks": sorted(self.bookmarked_pages),
            "ocr": self.ocr_langs,
            "scroll_position": scroll.value(),
            "scroll_offset": scroll.value() - page_top,
            "horizontal_scroll_position": horizontal.value(),
            "last_selection": selection,
        }

    def _selection_from_state(self, selection) -> tuple[int, QRect] | None:
        """Rebuild a label-space QRect from a saved selection.

        The saved form is PDF points, so it is re-scaled by whatever the zoom
        is when the tab comes back — that is what keeps "Translate" pointed at
        the same words after the app fit the page to the window on startup.
        """
        if not selection or len(selection) != 6:
            return None
        kind, page, x0, y0, x1, y1 = selection
        try:
            page = int(page)
            x0, y0, x1, y1 = (float(x0), float(y0), float(x1), float(y1))
        except (TypeError, ValueError):
            return None
        if page < 0 or not self._page_sizes:
            return None
        scale = self.zoom
        # Two-point form: the 4-argument QRect is (x, y, width, height), which
        # is not what we have — these are the rect's corners.
        return page, QRect(
            QPoint(int(round(x0 * scale)), int(round(y0 * scale))),
            QPoint(int(round(x1 * scale)), int(round(y1 * scale))),
        )

    def closeEvent(self, event) -> None:
        self.close_doc()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.initial_fit_pending:
            QTimer.singleShot(0, self._fit_initial_view)

    def _on_current_page_changed(self, page: int, _total: int) -> None:
        self.thumbs.mark_current(page)
        self.thumbs.set_current_bookmarked(page in self.bookmarked_pages)
        self.viewChanged.emit()

    def _fit_initial_view(self) -> None:
        if self.initial_fit_pending and self.isVisible():
            self.fit_to_width()
