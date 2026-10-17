"""Left thumbnails panel. PyMuPDF rendering runs in spawned worker processes."""
from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.spawn
import os
import sys
import threading
import time
from queue import Empty
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QThread,
    QTimer, Signal,
)
from PySide6.QtGui import QIcon, QPixmap, QImage, QPainter, QColor, QPainterPath
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QToolButton, QHBoxLayout, QVBoxLayout,
    QStackedWidget, QWidget, QMenu, QStyledItemDelegate, QStyle,
)
from ui.context_menu import style_context_menu

log = logging.getLogger("harmony_translator.reader.thumbnails")

_VIEW_POSITION_ROLE = Qt.ItemDataRole.UserRole + 1

# A live QThread needs at least one Python reference or it is garbage-collected
# mid-run and the process aborts with "QThread: Destroyed while thread is still
# running". Parking it on the ThumbPanel is not enough: main.py:_close_tab
# calls close_doc() + deleteLater(), which destroys the panel and its list while
# a worker that ignored stop() is still shutting its child processes down. This
# list is module level, so it outlives every widget; entries are dropped as soon
# as the worker really finishes (see _forget_retired_worker).
_RETIRED_WORKERS: list[QThread] = []
_SPAWN_CONFIGURATION_LOCK = threading.Lock()


def _forget_retired_worker(worker) -> None:
    try:
        _RETIRED_WORKERS.remove(worker)
    except ValueError:
        pass


def _reap_retired_workers() -> None:
    """Drop finished workers so the registry cannot grow without bound."""
    for worker in [w for w in _RETIRED_WORKERS if not w.isRunning()]:
        _forget_retired_worker(worker)


def _park_retired_worker(worker) -> None:
    """Keep a still-running thread referenced for as long as it needs one."""
    _reap_retired_workers()
    if worker in _RETIRED_WORKERS:
        return
    _RETIRED_WORKERS.append(worker)
    try:
        worker.finished.connect(lambda w=worker: _forget_retired_worker(w))
    except (AttributeError, RuntimeError, TypeError):
        log.debug(
            "Retired thumbnail worker has no finished signal to re-register",
            exc_info=True,
        )


def _start_thumbnail_processes(processes) -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        for process in processes:
            process.start()
        return

    pythonw = Path(sys._base_executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        log.warning(
            "pythonw.exe was not found beside %s; thumbnail workers may "
            "show console windows", sys._base_executable)
        for process in processes:
            process.start()
        return

    launcher_variable = "__PYVENV_LAUNCHER__"
    is_venv = sys.prefix != sys.base_prefix
    with _SPAWN_CONFIGURATION_LOCK:
        previous_executable = multiprocessing.spawn.get_executable()
        previous_launcher = os.environ.get(launcher_variable)
        try:
            multiprocessing.set_executable(os.fsencode(pythonw))
            if is_venv:
                os.environ[launcher_variable] = sys.executable
            for process in processes:
                process.start()
        finally:
            multiprocessing.set_executable(previous_executable)
            if previous_launcher is None:
                os.environ.pop(launcher_variable, None)
            else:
                os.environ[launcher_variable] = previous_launcher


class _ThumbnailPositionDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        progress = index.data(_VIEW_POSITION_ROLE)
        if progress is None:
            return
        widget = option.widget
        if widget is None:
            return
        icon_rect = widget.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, option, widget)
        if icon_rect.isEmpty():
            return

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(56, 189, 248, 72))
        painter.drawRoundedRect(icon_rect.adjusted(-2, -2, 2, 2), 3, 3)

        marker_y = round(icon_rect.top() + progress * (icon_rect.height() - 1))
        marker = icon_rect.adjusted(2, 0, -2, 0)
        painter.setBrush(QColor("#7f1d1d"))
        painter.drawRect(marker.left(), marker_y - 2, marker.width(), 5)
        painter.setBrush(QColor("#ef4444"))
        painter.drawRect(marker.left(), marker_y - 1, marker.width(), 3)
        painter.restore()


def _render_thumbnails(
    path: str,
    indices: list[int],
    width: int,
    view_rotations: list[int],
    output_queue,
    stop_event,
) -> None:
    """Render thumbnails outside the GUI process; PyMuPDF is not thread-safe."""
    started = time.perf_counter()
    rendered = 0
    failed = 0
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            for i in indices:
                if stop_event.is_set():
                    break
                if i >= len(doc):
                    continue
                try:
                    page = doc[i]
                    rotation = view_rotations[i] if i < len(view_rotations) else 0
                    page_width = page.rect.width
                    if rotation % 180:
                        page_width = page.rect.height
                    scale = width / max(1.0, page_width)
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale).prerotate(rotation),
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    output_queue.put((
                        "thumb", i, pix.width, pix.height, pix.stride,
                        bytes(pix.samples_mv),
                    ))
                    rendered += 1
                except Exception as error:
                    failed += 1
                    output_queue.put(("error", i, str(error)))
    except Exception as error:
        failed += 1
        output_queue.put(("fatal", str(error)))
    finally:
        output_queue.put((
            "done", rendered, failed,
            (time.perf_counter() - started) * 1000,
            stop_event.is_set(),
        ))


class ThumbWorker(QThread):
    thumbReady = Signal(int, QImage)
    thumbFailed = Signal(int, str)

    def __init__(
        self,
        path: str,
        count: int,
        width: int = 132,
        indices: list[int] | None = None,
        view_rotations: list[int] | None = None,
    ):
        super().__init__()
        self._path = path
        self._count = count
        self._width = width
        self._view_rotations = list(view_rotations or ())
        self._indices = (
            list(range(count)) if indices is None
            else sorted(set(i for i in indices if 0 <= i < count))
        )
        self._stop_requested = False
        self._stop_event = None

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    def _drain_output_queue(self, output_queue) -> int:
        """Consume whatever the children left buffered. True if any was found.

        multiprocessing.Queue hands writes to a feeder thread, so a child can be
        reaped while its payloads are still in flight. Returning True tells the
        caller that the queue really is empty now and the receive loop can stop.
        """
        handled = 0
        while True:
            try:
                result = output_queue.get_nowait()
            except Empty:
                return handled > 0
            self._handle_result(result)
            handled += 1

    def run(self) -> None:
        started = time.perf_counter()
        context = multiprocessing.get_context("spawn")
        self._stop_event = context.Event()
        if self._stop_requested:
            self._stop_event.set()
        worker_count = min(
            4,
            max(1, (multiprocessing.cpu_count() + 1) // 2),
            len(self._indices),
        )
        if worker_count == 0:
            self._stop_event = None
            return
        output_queue = context.Queue(maxsize=worker_count * 2)
        processes = [
            context.Process(
                target=_render_thumbnails,
                args=(
                    self._path,
                    self._indices[worker_index::worker_count],
                    self._width,
                    self._view_rotations,
                    output_queue,
                    self._stop_event,
                ),
                daemon=True,
            )
            for worker_index in range(worker_count)
        ]
        rendered = 0
        failed = 0
        stopped = False
        completed_workers = 0
        completed = False
        totals = {"rendered": 0, "failed": 0, "workers": 0, "stopped": False}

        def handle(result) -> None:
            nonlocal completed_workers
            kind = result[0]
            if kind == "thumb":
                _, index, width, height, stride, data = result
                image = QImage(
                    data, width, height, stride,
                    QImage.Format.Format_RGB888,
                ).copy()
                self.thumbReady.emit(index, image)
                totals["rendered"] += 1
            elif kind == "error":
                _, index, error = result
                log.warning(
                    "Could not render thumbnail for page %d: %s",
                    index + 1,
                    error,
                )
                self.thumbFailed.emit(index, error)
                totals["failed"] += 1
            elif kind == "fatal":
                log.error(
                    "Could not create PDF thumbnail worker: %s", result[1])
                for index in self._indices:
                    self.thumbFailed.emit(index, result[1])
            elif kind == "done":
                _, worker_rendered, worker_failed, elapsed, worker_stopped = result
                totals["rendered"] += worker_rendered
                totals["failed"] += worker_failed
                completed_workers += 1
                totals["stopped"] = totals["stopped"] or worker_stopped
                log.debug(
                    "Thumbnail process finished: rendered=%d failed=%d "
                    "elapsed=%.1fms stopped=%s",
                    worker_rendered, worker_failed, elapsed, worker_stopped,
                )

        self._handle_result = handle
        try:
            _start_thumbnail_processes(processes)
            while completed_workers < worker_count:
                try:
                    result = output_queue.get(timeout=0.1)
                except Empty:
                    if not any(process.is_alive() for process in processes):
                        # Every child has exited, but multiprocessing.Queue
                        # buffers payloads in a feeder thread, so the parent can
                        # observe the exit before it has consumed the last
                        # writes. Draining before breaking is what stops
                        # already-rendered thumbnails from being discarded.
                        self._drain_output_queue(output_queue)
                        break
                    continue
                handle(result)
            rendered = totals["rendered"]
            failed = totals["failed"]
            stopped = totals["stopped"]
            completed = completed_workers == worker_count
            if completed:
                log.info(
                    "Thumbnail rendering finished: rendered=%d failed=%d "
                    "elapsed=%.1fms stopped=%s workers=%d",
                    rendered,
                    failed,
                    (time.perf_counter() - started) * 1000,
                    stopped,
                    worker_count,
                )
        except Exception as error:
            log.exception("Could not run PDF thumbnail process")
            for index in self._indices:
                self.thumbFailed.emit(index, str(error))
        finally:
            for process in processes:
                if process.pid is not None:
                    process.join(timeout=1.0)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=1.0)
                    if process.exitcode not in (None, 0) and not completed:
                        log.error(
                            "PDF thumbnail process exited unexpectedly: "
                            "exitcode=%s",
                            process.exitcode,
                        )
            output_queue.close()
            output_queue.join_thread()
            self._stop_event = None
        if not completed:
            log.warning(
                "Thumbnail rendering ended without completion: rendered=%d "
                "failed=%d elapsed=%.1fms stopped=%s",
                rendered,
                failed,
                (time.perf_counter() - started) * 1000,
                self._stop_requested,
            )


class ThumbPanel(QWidget):
    refreshRequested = Signal()
    reopenRequested = Signal()
    pageChosen = Signal(int)
    selectedPagesChanged = Signal()
    zoomRequested = Signal(int)
    bookmarkRequested = Signal()
    bookmarkJumpRequested = Signal(int)
    translatePageRequested = Signal(int)
    extractPagesRequested = Signal(object)
    deletePagesRequested = Signal(object)
    copyPageImageRequested = Signal(int)

    BASE_ICON_SIZE = QSize(132, 180)
    ITEM_GAP = 0
    MIN_ZOOM = 0.5
    MAX_ZOOM = 3.0
    PREFETCH_ROWS = 1
    VIEW_POSITION_ROLE = _VIEW_POSITION_ROLE
    # Total budget closeEvent gives live thumbnail workers to wind down.
    CLOSE_WORKER_WAIT_MS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pdfThumbnailsPanel")
        self._worker: ThumbWorker | None = None
        self._path = ""
        self._count = 0
        self._populated = False
        self._thumbnail_rendering_enabled = True
        self._thumbnail_worker_started = False
        self._zoom = 1.0
        self._thumbnail_images: dict[int, QImage] = {}
        self._thumbnail_failed: set[int] = set()
        self._thumbnail_keep: set[int] = set()
        self._document_generation = 0
        self._placeholder_icon = self._placeholder()
        self._bookmarks: set[int] = set()
        self._bookmark_items: dict[int, QListWidgetItem] = {}
        self._position_page: int | None = None
        self._scroll_animations: dict[QListWidget, QPropertyAnimation] = {}
        self._scroll_targets: dict[QListWidget, int] = {}
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setSingleShot(True)
        self._thumbnail_timer.setInterval(100)
        self._thumbnail_timer.timeout.connect(
            self._schedule_visible_thumbnails)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        rail = QWidget(self)
        rail.setObjectName("bookmarkRail")
        rail.setFixedWidth(20)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 2, 0, 0)
        rail_layout.setSpacing(6)

        self.bookmark_button = QToolButton(rail)
        self.bookmark_button.setObjectName("bookmarkPageButton")
        self.bookmark_button.setText("☆")
        self.bookmark_button.setToolTip("Save bookmark for the current page")
        self.bookmark_button.setAccessibleName("Save bookmark")
        self.bookmark_button.setCheckable(True)
        self.bookmark_button.setAutoRaise(True)
        self.bookmark_button.setFixedSize(20, 24)
        self.bookmark_button.setEnabled(False)
        self.bookmark_button.clicked.connect(
            lambda _checked=False: self.bookmarkRequested.emit())
        rail_layout.addWidget(self.bookmark_button)

        self.bookmarks_button = QToolButton(rail)
        self.bookmarks_button.setObjectName("myBookmarksButton")
        self.bookmarks_button.setText("☷")
        self.bookmarks_button.setToolTip("My bookmarks")
        self.bookmarks_button.setAccessibleName("My bookmarks")
        self.bookmarks_button.setAutoRaise(True)
        self.bookmarks_button.setFixedSize(20, 24)
        self.bookmarks_button.setCheckable(True)
        self.bookmarks_button.setToolTip("Show my bookmarked pages")
        self.bookmarks_button.toggled.connect(self._show_bookmarks)
        rail_layout.addWidget(self.bookmarks_button)
        rail_layout.addStretch(1)
        lay.addWidget(rail)

        self.pages = QStackedWidget(self)
        self.list = self._make_page_list("pdfThumbnails")
        self.bookmarks_list = self._make_page_list("pdfBookmarks")
        self.pages.addWidget(self.list)
        self.pages.addWidget(self.bookmarks_list)
        self.set_thumbnail_zoom(self._zoom)
        lay.addWidget(self.pages, 1)

    def _make_page_list(self, object_name: str) -> QListWidget:
        page_list = QListWidget(self)
        page_list.setObjectName(object_name)
        scrollbar = page_list.verticalScrollBar()
        scrollbar.setObjectName(
            "pdfThumbnailScrollBar"
            if object_name == "pdfThumbnails"
            else "pdfBookmarkScrollBar"
        )
        scrollbar.setSingleStep(32)
        scrollbar.setStyleSheet("QScrollBar:vertical { width: 18px; }")
        page_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        page_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel)
        page_list.setViewMode(QListWidget.ViewMode.IconMode)
        page_list.setItemAlignment(Qt.AlignmentFlag.AlignHCenter)
        page_list.setFlow(QListWidget.Flow.LeftToRight)
        page_list.setWrapping(True)
        page_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        page_list.setMovement(QListWidget.Movement.Static)
        page_list.setSpacing(0)
        page_list.setUniformItemSizes(True)
        page_list.setItemDelegate(_ThumbnailPositionDelegate(page_list))
        page_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        if object_name == "pdfThumbnails":
            page_list.itemSelectionChanged.connect(
                self.selectedPagesChanged)
        page_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        if object_name == "pdfThumbnails":
            page_list.verticalScrollBar().valueChanged.connect(
                self._queue_visible_thumbnails)
        page_list.customContextMenuRequested.connect(
            lambda pos, widget=page_list:
            self._show_page_context_menu(widget, pos))
        page_list.installEventFilter(self)
        page_list.viewport().installEventFilter(self)
        if object_name == "pdfThumbnails":
            page_list.itemClicked.connect(
                lambda item: self.pageChosen.emit(page_list.row(item)))
        else:
            page_list.itemClicked.connect(
                lambda item: self.bookmarkJumpRequested.emit(
                    int(item.data(Qt.ItemDataRole.UserRole))))
        return page_list

    @staticmethod
    def _page_index(page_list: QListWidget, item: QListWidgetItem) -> int:
        if page_list.objectName() == "pdfThumbnails":
            return page_list.row(item)
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else -1

    def _show_page_context_menu(
        self, page_list: QListWidget, position: QPoint
    ) -> None:
        item = page_list.itemAt(position)
        if item is None:
            return
        page_index = self._page_index(page_list, item)
        if page_index < 0:
            return
        if not item.isSelected():
            page_list.clearSelection()
            item.setSelected(True)
        selected_pages = self._selected_page_indices(page_list)

        menu = self._page_context_menu(page_index, selected_pages)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.exec(page_list.viewport().mapToGlobal(position))

    def _selected_page_indices(self, page_list: QListWidget) -> list[int]:
        return sorted({
            self._page_index(page_list, selected)
            for selected in page_list.selectedItems()
            if self._page_index(page_list, selected) >= 0
        })

    def selected_page_indices(self) -> list[int]:
        return self._selected_page_indices(self.list)

    def _page_context_menu(
        self, page_index: int, selected_pages: list[int]
    ) -> QMenu:
        menu = QMenu(self)
        translate_action = menu.addAction("Translate full page")
        translate_action.triggered.connect(
            lambda: self.translatePageRequested.emit(page_index))
        extract_label = (
            "Extract pages…" if len(selected_pages) > 1 else "Extract page…")
        extract_action = menu.addAction(extract_label)
        extract_action.triggered.connect(
            lambda: self.extractPagesRequested.emit(selected_pages))
        delete_label = (
            "Delete pages" if len(selected_pages) > 1 else "Delete page")
        delete_action = menu.addAction(delete_label)
        delete_action.triggered.connect(
            lambda: self.deletePagesRequested.emit(selected_pages))
        copy_action = menu.addAction("Copy page image")
        copy_action.triggered.connect(
            lambda: self.copyPageImageRequested.emit(page_index))
        menu.addSeparator()
        refresh_action = menu.addAction("Refresh")
        refresh_action.triggered.connect(self.refreshRequested)
        reopen_action = menu.addAction("ReOpen")
        reopen_action.triggered.connect(self.reopenRequested)
        return style_context_menu(menu)

    def _show_bookmarks(self, shown: bool) -> None:
        self.pages.setCurrentWidget(
            self.bookmarks_list if shown else self.list)
        self.bookmarks_button.setToolTip(
            "Show page thumbnails" if shown else "Show my bookmarked pages")
        self._queue_visible_thumbnails()

    def eventFilter(self, obj, event) -> bool:
        if obj in (
                self.list, self.list.viewport(),
                self.bookmarks_list, self.bookmarks_list.viewport()
        ) and event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Sign, not int(delta/120): a touchpad emits deltas well under
                # 120, so the old truncation made Ctrl+wheel do nothing there.
                if delta:
                    self.zoomRequested.emit(1 if delta > 0 else -1)
                    event.accept()
                    return True
            elif delta:
                page_list = (
                    self.bookmarks_list
                    if obj in (self.bookmarks_list, self.bookmarks_list.viewport())
                    else self.list
                )
                scrollbar = page_list.verticalScrollBar()
                # Small deltas must still move: at least one pixel, scaled to
                # the row height so a flick scrolls several rows.
                pixels = -int(round(delta / 120.0 * scrollbar.singleStep())) or (
                    1 if delta > 0 else -1)
                animation = self._scroll_animations.get(page_list)
                target = (
                    self._scroll_targets.get(page_list, scrollbar.value())
                    if animation is not None and animation.state()
                    == QPropertyAnimation.State.Running
                    else scrollbar.value()
                )
                target = max(
                    scrollbar.minimum(),
                    min(scrollbar.maximum(), target + pixels),
                )
                if animation is None:
                    animation = QPropertyAnimation(
                        scrollbar, b"value", self)
                    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
                    animation.finished.connect(
                        lambda widget=page_list:
                        self._scroll_targets.pop(widget, None))
                    self._scroll_animations[page_list] = animation
                animation.stop()
                animation.setDuration(140)
                animation.setStartValue(scrollbar.value())
                animation.setEndValue(target)
                self._scroll_targets[page_list] = target
                animation.start()
                event.accept()
                return True
        return super().eventFilter(obj, event)

    @property
    def thumbnail_zoom(self) -> float:
        return self._zoom

    def set_thumbnail_zoom(self, zoom: float) -> None:
        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        gap = self.ITEM_GAP
        available_width = max(40, self.list.viewport().width() - 2 * gap)
        base_width = min(self.BASE_ICON_SIZE.width(), available_width)
        icon_width = max(20, round(base_width * self._zoom))
        columns = max(1, round(available_width / icon_width))
        if columns > 1:
            icon_width = max(
                20,
                min(icon_width, available_width // columns - 1),
            )
        icon_size = QSize(
            icon_width,
            round(icon_width * self.BASE_ICON_SIZE.height() / self.BASE_ICON_SIZE.width()),
        )
        label_height = self.list.fontMetrics().height()
        grid_size = QSize(
            icon_size.width() + 2 * gap,
            icon_size.height() + label_height + gap,
        )
        for page_list in (self.list, self.bookmarks_list):
            page_list.setIconSize(icon_size)
            page_list.setGridSize(grid_size)
            for index in range(page_list.count()):
                item = page_list.item(index)
                item.setSizeHint(grid_size)
                page_index = self._page_index(page_list, item)
                image = self._thumbnail_images.get(page_index)
                if image is not None:
                    source_item = self.list.item(page_index)
                    if page_list is self.list or source_item is None:
                        item.setIcon(
                            self._scaled_thumbnail_icon(image, icon_size))
                    else:
                        item.setIcon(source_item.icon())
            page_list.verticalScrollBar().setSingleStep(grid_size.height())
        self._queue_visible_thumbnails()

    def set_bookmarks(self, pages: set[int]) -> None:
        self._bookmarks = set(pages)
        self._bookmark_items.clear()
        self.bookmarks_list.clear()
        if not self._bookmarks:
            empty = QListWidgetItem("No bookmarks yet")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmarks_list.addItem(empty)
            self.set_thumbnail_zoom(self._zoom)
            return
        for page in sorted(self._bookmarks):
            source_item = self.list.item(page)
            icon = (
                source_item.icon()
                if source_item is not None and not source_item.icon().isNull()
                else self._placeholder_icon
            )
            item = QListWidgetItem(icon, str(page + 1))
            item.setData(Qt.ItemDataRole.UserRole, page)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.bookmarks_list.addItem(item)
            self._bookmark_items[page] = item
        self.set_thumbnail_zoom(self._zoom)

    def set_page_count(self, count: int) -> None:
        self.bookmarks_button.setEnabled(count > 0)
        if not count:
            self.bookmarks_button.setChecked(False)

    def set_current_bookmarked(self, bookmarked: bool) -> None:
        self.bookmark_button.setChecked(bookmarked)
        self.bookmark_button.setText("★" if bookmarked else "☆")
        self.bookmark_button.setToolTip(
            "Remove bookmark from the current page"
            if bookmarked else "Save bookmark for the current page")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, lambda: self.set_thumbnail_zoom(self._zoom))

    def hideEvent(self, event) -> None:
        self._thumbnail_timer.stop()
        self._thumbnail_keep.clear()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.stop()
        self._prune_thumbnail_cache(set())
        super().hideEvent(event)

    @staticmethod
    def _placeholder() -> QIcon:
        img = QImage(396, 540, QImage.Format.Format_RGB888)
        img.fill(QColor("#e2e8f0"))
        p = QPainter(img)
        p.setPen(QColor("#94a3b8"))
        p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "…")
        p.end()
        return QIcon(QPixmap.fromImage(img))

    def set_document(
        self,
        path: str,
        count: int,
        render_thumbnails: bool = True,
        view_rotations: list[int] | None = None,
    ) -> None:
        self.clear_document()
        self._path = path
        self._count = count
        self._thumbnail_rendering_enabled = render_thumbnails
        self._view_rotations = list(view_rotations or [0] * count)
        self.bookmark_button.setEnabled(count > 0)
        self.set_page_count(count)
        if self.isVisible():
            self._populate()
        self.set_thumbnail_zoom(self._zoom)
        QTimer.singleShot(0, lambda: self.set_thumbnail_zoom(self._zoom))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._populate()
        self._queue_visible_thumbnails()

    def _populate(self) -> None:
        if self._populated or not self._path or self._count <= 0:
            return
        for i in range(self._count):
            item = QListWidgetItem(self._placeholder_icon, str(i + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.list.addItem(item)
        self.set_thumbnail_zoom(self._zoom)
        self._populated = True
        self._schedule_visible_thumbnails()

    def start_rendering(self) -> None:
        self._thumbnail_rendering_enabled = True
        self._schedule_visible_thumbnails()

    def _queue_visible_thumbnails(self) -> None:
        if self._thumbnail_timer.isActive():
            self._thumbnail_timer.stop()
        self._thumbnail_timer.start()

    def _visible_pages(self, page_list: QListWidget) -> set[int]:
        count = page_list.count()
        viewport_height = page_list.viewport().height()
        grid = page_list.gridSize()
        if count <= 0 or viewport_height <= 0 or grid.height() <= 0:
            return set()

        columns = max(1, page_list.viewport().width() // max(1, grid.width()))
        scrollbar = page_list.verticalScrollBar()
        first_row = max(
            0, scrollbar.value() // grid.height() - self.PREFETCH_ROWS)
        last_row = min(
            (count - 1) // columns,
            (scrollbar.value() + viewport_height) // grid.height()
            + self.PREFETCH_ROWS,
        )
        visible: set[int] = set()
        for row in range(first_row, last_row + 1):
            start = row * columns
            end = min(count, start + columns)
            for item_index in range(start, end):
                page_index = self._page_index(
                    page_list, page_list.item(item_index))
                if 0 <= page_index < self._count:
                    visible.add(page_index)
        return visible

    def _schedule_visible_thumbnails(self) -> None:
        if (
            not self._thumbnail_rendering_enabled
            or not self._populated
            or not self._path
            or not self.isVisible()
        ):
            return
        page_list = (
            self.bookmarks_list
            if self.pages.currentWidget() is self.bookmarks_list
            else self.list
        )
        keep = self._visible_pages(page_list)
        if self._position_page is not None:
            keep.add(self._position_page)
        self._thumbnail_keep = keep
        self._prune_thumbnail_cache(keep)
        self._start_worker()

    def _start_worker(self) -> None:
        if (
            not self._thumbnail_rendering_enabled
            or not self._populated
            or not self._path
            or self._count <= 0
        ):
            return
        current_worker = self._worker
        if current_worker is not None and current_worker.isRunning():
            return
        indices = sorted(
            self._thumbnail_keep
            - self._thumbnail_images.keys()
            - self._thumbnail_failed
        )
        if not indices:
            return
        self._thumbnail_worker_started = True
        generation = self._document_generation
        worker = ThumbWorker(
            self._path,
            self._count,
            width=max(1, self.list.iconSize().width()),
            indices=indices,
            view_rotations=self._view_rotations,
        )
        self._worker = worker
        worker.thumbReady.connect(
            lambda index, image, gen=generation:
            self._on_thumb(index, image, gen))
        worker.thumbFailed.connect(
            lambda index, error, gen=generation:
            self._on_thumb_failed(index, error, gen))
        worker.finished.connect(
            lambda w=worker: self._on_thumbnail_worker_finished(w))
        worker.start(QThread.Priority.LowestPriority)

    def _on_thumbnail_worker_finished(self, worker: ThumbWorker) -> None:
        if worker is self._worker:
            self._worker = None
        if self._path:
            self._queue_visible_thumbnails()

    def _on_thumb_failed(
        self, index: int, error: str, generation: int
    ) -> None:
        if generation != self._document_generation:
            return
        self._thumbnail_failed.add(index)
        log.warning(
            "Could not render thumbnail for page %d: %s", index + 1, error)

    def _retained_workers(self) -> list:
        """Threads that were asked to stop but were still busy.

        Kept for compatibility with callers/tests that inspect the panel's own
        view of what is still running. The authoritative list is the module
        level `_RETIRED_WORKERS`, which outlives the panel.
        """
        return [w for w in getattr(self, "_retired", ()) if w.isRunning()]

    def _reap_retired_workers(self) -> None:
        """Module level reaper, exposed so the registry can be checked."""
        _reap_retired_workers()

    def _retire_worker(self, worker) -> None:
        """Stop a worker without blocking; reap it asynchronously.

        QThread::wait blocks the caller, which here is clear_document() on the
        GUI thread, and ThumbWorker.run's teardown (process.join(timeout=1.0)
        then terminate + join(timeout=1.0) per process) routinely needs several
        seconds. So the worker is only asked to stop; `_park_retired_worker`
        holds the reference that keeps it alive and releases it from the
        `finished` signal.
        """
        if worker is None:
            return
        if not worker.isRunning():
            return
        worker.stop()
        _park_retired_worker(worker)
        retired = getattr(self, "_retired", None)
        if retired is None:
            retired = self._retired = []
        retired.append(worker)

    def closeEvent(self, event) -> None:
        """Give every live thumbnail worker a bounded chance to wind down.

        The panel may be destroyed without a close event (main.py calls
        deleteLater()), which is why the registry is module level; this is the
        orderly path when the widget really is closed.
        """
        for worker in list(_RETIRED_WORKERS):
            try:
                worker.stop()
            except (AttributeError, RuntimeError):
                pass
        # Bounded wait: enough for a worker that is already tearing down, but
        # it must never hang the UI on a worker that ignores stop().
        deadline = time.monotonic() + self.CLOSE_WORKER_WAIT_MS / 1000.0
        for worker in list(_RETIRED_WORKERS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                worker.wait(int(remaining * 1000))
            except (AttributeError, RuntimeError):
                pass
        _reap_retired_workers()
        self._retired = []
        super().closeEvent(event)

    def clear_document(self) -> None:
        self._document_generation += 1
        self._thumbnail_timer.stop()
        self._thumbnail_keep.clear()
        worker = self._worker
        self._retire_worker(worker)
        # Keep pointing at a worker that ignored the stop request, so the panel
        # still holds a reference and the thread cannot be destroyed underfoot.
        self._worker = worker if self._retained_workers() else None
        self.list.clear()
        self.bookmarks_list.clear()
        self._thumbnail_images.clear()
        self._thumbnail_failed.clear()
        self._bookmarks.clear()
        self._bookmark_items.clear()
        self._scroll_targets.clear()
        self._position_page = None
        self._path = ""
        self._count = 0
        self._populated = False
        self._thumbnail_rendering_enabled = False
        self._thumbnail_worker_started = False
        self.bookmark_button.setEnabled(False)
        self.bookmark_button.setChecked(False)
        self.bookmarks_button.setChecked(False)
        self.set_current_bookmarked(False)

    def _on_thumb(
        self, index: int, img: QImage, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self._document_generation:
            return
        if generation is not None and index not in self._thumbnail_keep:
            return
        if img.isNull() or not 0 <= index < self._count:
            return
        self._thumbnail_images[index] = img
        item = self.list.item(index)
        if item is not None:
            icon = self._scaled_thumbnail_icon(img, self.list.iconSize())
            item.setIcon(icon)
            bookmark_item = self._bookmark_items.get(index)
            if bookmark_item is not None:
                bookmark_item.setIcon(icon)

    def _prune_thumbnail_cache(self, keep: set[int]) -> None:
        for index in self._thumbnail_images.keys() - keep:
            del self._thumbnail_images[index]
            item = self.list.item(index)
            if item is not None:
                item.setIcon(self._placeholder_icon)
            bookmark_item = self._bookmark_items.get(index)
            if bookmark_item is not None:
                bookmark_item.setIcon(self._placeholder_icon)
        # A page whose thumbnail failed once is forgotten when it scrolls out
        # of the kept range: the failure was often transient (a torn-down
        # worker, a busy disk), and keeping it blacklisted meant the page
        # stayed blank for the rest of the session. It is retried the next
        # time the page becomes visible again. Pages still in view keep their
        # mark so a permanently broken page is not retried on every tick.
        if self._thumbnail_failed:
            self._thumbnail_failed -= self._thumbnail_failed - keep

    @staticmethod
    def _scaled_thumbnail_icon(image: QImage, size: QSize) -> QIcon:
        scaled = image.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        rounded = QPixmap(scaled.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0, 0, scaled.width(), scaled.height()), 6, 6)
        painter.setClipPath(path)
        painter.drawImage(0, 0, scaled)
        painter.end()
        return QIcon(rounded)

    def mark_current(self, index: int) -> None:
        if 0 <= index < self.list.count() and self.list.currentRow() != index:
            self.list.blockSignals(True)
            self.list.setCurrentRow(index)
            self.list.blockSignals(False)
        bookmark_row = next(
            (row for row in range(self.bookmarks_list.count())
             if self.bookmarks_list.item(row).data(
                 Qt.ItemDataRole.UserRole) == index),
            -1,
        )
        if bookmark_row >= 0 and self.bookmarks_list.currentRow() != bookmark_row:
            self.bookmarks_list.blockSignals(True)
            self.bookmarks_list.setCurrentRow(bookmark_row)
            self.bookmarks_list.blockSignals(False)

    def mark_current_position(self, index: int, progress: float) -> None:
        if not 0 <= index < self.list.count():
            return
        progress = min(1.0, max(0.0, float(progress)))
        previous = self._position_page
        if previous is not None and previous != index:
            previous_item = self.list.item(previous)
            if previous_item is not None:
                previous_item.setData(self.VIEW_POSITION_ROLE, None)
                self.list.update(self.list.visualItemRect(previous_item))
            previous_bookmark = next(
                (row for row in range(self.bookmarks_list.count())
                 if self.bookmarks_list.item(row).data(
                     Qt.ItemDataRole.UserRole) == previous),
                -1,
            )
            if previous_bookmark >= 0:
                old_item = self.bookmarks_list.item(previous_bookmark)
                old_item.setData(self.VIEW_POSITION_ROLE, None)
                self.bookmarks_list.update(
                    self.bookmarks_list.visualItemRect(old_item))
        item = self.list.item(index)
        if item is not None:
            item.setData(self.VIEW_POSITION_ROLE, progress)
            self.list.update(self.list.visualItemRect(item))
        self.mark_current(index)
        page_changed = self._position_page != index
        self._position_page = index
        if page_changed:
            self.list.scrollToItem(
                item, QListWidget.ScrollHint.PositionAtCenter)

        bookmark_row = next(
            (row for row in range(self.bookmarks_list.count())
             if self.bookmarks_list.item(row).data(
                 Qt.ItemDataRole.UserRole) == index),
            -1,
        )
        if bookmark_row >= 0:
            bookmark_item = self.bookmarks_list.item(bookmark_row)
            bookmark_item.setData(self.VIEW_POSITION_ROLE, progress)
            self.bookmarks_list.update(
                self.bookmarks_list.visualItemRect(bookmark_item))
            if page_changed:
                self.bookmarks_list.scrollToItem(
                    bookmark_item, QListWidget.ScrollHint.PositionAtCenter)
