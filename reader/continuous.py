"""Continuous vertical page reader with lazy rendering + drag selection."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import NamedTuple

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal, QThread, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QLabel, QRubberBand, QScrollArea, QVBoxLayout, QWidget,
)

log = logging.getLogger("harmony_translator.reader.continuous")

# A live QThread needs at least one Python reference; dropping the last one
# mid-run aborts the process with "QThread: Destroyed while thread is still
# running". The reader widget can be destroyed without a close event (main.py's
# _close_tab calls deleteLater), so render workers are parked in this
# module-level list, which outlives every widget. Each entry is released from
# the worker's own finished signal.
_PARKED_RENDER_WORKERS: list = []

# Used only for diagnostics: how long a stopped-but-still-running worker has
# been parked. Kept out of the release decision on purpose -- releasing a live
# thread is exactly the abort we are avoiding.
_RENDER_WORKER_PARK_WARN_SECONDS = 30.0


def _unpark_render_worker(worker) -> None:
    try:
        _PARKED_RENDER_WORKERS.remove(worker)
    except ValueError:
        pass


def _sweep_render_workers() -> None:
    """Bounded reaper, so the module-level list cannot grow without limit."""
    now = time.monotonic()
    for worker in list(_PARKED_RENDER_WORKERS):
        if not worker.isRunning():
            _unpark_render_worker(worker)
            continue
        if now - getattr(worker, "_parked_at", now) > _RENDER_WORKER_PARK_WARN_SECONDS:
            log.warning(
                "Render worker still running %.0fs after it was asked to "
                "stop; leaving it parked", now - worker._parked_at)
            worker._parked_at = now      # do not repeat the warning every sweep


def _park_render_worker(worker) -> None:
    _sweep_render_workers()
    if worker not in _PARKED_RENDER_WORKERS:
        _PARKED_RENDER_WORKERS.append(worker)
        worker._parked_at = time.monotonic()
        try:
            worker.finished.connect(lambda w=worker: _unpark_render_worker(w))
        except (RuntimeError, TypeError):
            log.debug("Render worker has no usable finished signal",
                      exc_info=True)


class PageLabel(QLabel):
    selected = Signal(int, QRect)  # page_index, rect in label pixels
    contextRequested = Signal(int, QPoint)

    def __init__(self, page_index: int):
        super().__init__()
        self.page_index = page_index
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Drag with the LEFT mouse button over any sentence to translate it")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("QLabel { background: white; border: 1px solid #d5dee9; }")
        self._origin = QPoint()
        self._band: QRubberBand | None = None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._origin = e.position().toPoint()
            if self._band is None:
                self._band = QRubberBand(QRubberBand.Shape.Rectangle, self)
            self._band.setGeometry(QRect(self._origin, e.position().toPoint()).normalized())
            self._band.show()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._band and self._band.isVisible():
            self._band.setGeometry(QRect(self._origin, e.position().toPoint()).normalized())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._band:
            self._band.hide()
            rect = QRect(self._origin, e.position().toPoint()).normalized()
            if rect.width() > 8 and rect.height() > 8 and self.pixmap() and not self.pixmap().isNull():
                self.selected.emit(self.page_index, rect)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, event) -> None:
        self.contextRequested.emit(self.page_index, event.globalPos())
        event.accept()


class _RenderJob(NamedTuple):
    token: int
    index: int
    zoom: float
    generation: int


class _RenderOutcome(NamedTuple):
    token: int
    index: int
    zoom: float
    generation: int
    image: object            # QImage, or None on failure/cancellation
    error: object            # Exception, or None
    elapsed_ms: float
    cancelled: bool = False


class PageRenderWorker(QThread):
    """Runs `render_fn` off the GUI thread and hands the QImage back.

    The raster is the slow part of scrolling a page view -- 23 ms at 100% on
    this machine, 130 ms at 1000% -- and `_render_page_index` used to run it
    inline from the GUI-thread QTimer pump, so every page froze the UI for the
    whole render. PyMuPDF is not re-entrant across threads, so this is a single
    serialized worker and ContinuousReader dispatches at most one page at a
    time (MAX_INFLIGHT): at most one raster is ever in flight, which preserves
    the memory profile of the old synchronous pump.

    The QImage is built here and only here. QPixmap is a GUI resource and must
    be built on the GUI thread, which the reader does when the result arrives.
    """

    pageRendered = Signal(object)      # _RenderOutcome

    def __init__(self, render_fn):
        super().__init__()
        self._render_fn = render_fn
        self._jobs: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._parked_at = 0.0

    def submit(self, job: _RenderJob) -> None:
        self._jobs.put(job)

    def stop(self) -> None:
        """Ask the worker to finish after the job in flight. Non-blocking."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._jobs.put(None)

    def run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            if self._stopping.is_set():
                self.pageRendered.emit(_RenderOutcome(
                    job.token, job.index, job.zoom, job.generation,
                    None, None, 0.0, cancelled=True))
                continue
            started = time.perf_counter()
            try:
                image = self._render_fn(job.index, job.zoom)
                error = None
            except Exception as exc:            # reported upward, not swallowed
                image, error = None, exc
            self.pageRendered.emit(_RenderOutcome(
                job.token, job.index, job.zoom, job.generation, image, error,
                (time.perf_counter() - started) * 1000))


class ContinuousReader(QScrollArea):
    pageSelected = Signal(int, QRect)
    pageContextRequested = Signal(int, QPoint)
    currentPageChanged = Signal(int, int)  # current 0-based, total
    viewPositionChanged = Signal(int, float)  # page index, vertical progress
    zoomStep = Signal(int)  # +1 zoom in / -1 zoom out (Ctrl+wheel)
    initialRenderComplete = Signal()

    BUFFER_PX = 600
    # Rasters in flight at once. One keeps the peak memory of the old
    # synchronous pump and keeps the PyMuPDF calls strictly serial.
    MAX_INFLIGHT = 1
    # Upper bound on how long close/clear waits for a raster already running.
    WORKER_STOP_WAIT_MS = 2000

    def __init__(self, render_fn, parent=None):
        """render_fn(page_index, zoom) -> QImage."""
        super().__init__(parent)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._render_fn = render_fn
        self._sizes: list[tuple[float, float]] = []
        self._zoom = 1.5
        self._labels: list[PageLabel] = []
        self._rendered: set[int] = set()
        self._failed: set[int] = set()   # pages whose render raised
        self._current = 0
        # Off-thread raster bookkeeping. `_inflight` maps a dispatch token to
        # its page so a zoom or document change can drop results that no longer
        # describe what is on screen, and so a page is never queued twice while
        # its raster is still running.
        self._inflight: dict[int, int] = {}
        self._next_token = 0
        self._generation = 0
        self._render_worker = None

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(8)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setWidgetResizable(True)
        self.setWidget(self._container)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setObjectName("pdfCanvas")
        scrollbar = self.verticalScrollBar()
        scrollbar.setObjectName("pdfVerticalScrollBar")
        scrollbar.setStyleSheet("QScrollBar:vertical { width: 18px; }")
        scrollbar.setAccessibleName("PDF page scrollbar")
        scrollbar.setToolTip("Scroll through PDF pages")

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._ensure_visible)
        # Render pump: one page per tick keeps the UI responsive on heavy books.
        self._queue: list[int] = []
        self._pump = QTimer(self)
        self._pump.setInterval(25)
        self._pump.timeout.connect(self._pump_one)
        self._document_set_at = 0.0
        self._first_render_logged = False
        self._initial_batch_logged = False
        self._initial_render_complete_emitted = False
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._schedule())
        self.verticalScrollBar().valueChanged.connect(self._update_current)
        # Wheel events go to the viewport, not the scroll area: filter them there.
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.viewport() and e.type() == QEvent.Type.Wheel:
            if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.zoomStep.emit(1 if e.angleDelta().y() > 0 else -1)
                return True
        return super().eventFilter(obj, e)

    def wheelEvent(self, e) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomStep.emit(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
        else:
            super().wheelEvent(e)

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_document(self, sizes: list[tuple[float, float]], render: bool = True) -> None:
        self._clear()
        self._document_set_at = time.perf_counter()
        self._first_render_logged = False
        self._initial_batch_logged = False
        self._initial_render_complete_emitted = False
        self._sizes = list(sizes)
        for i, (w, h) in enumerate(self._sizes):
            lbl = PageLabel(i)
            lbl.setFixedSize(int(w * self._zoom), int(h * self._zoom))
            lbl.setText(f"Page {i + 1} …")
            lbl.selected.connect(self.pageSelected)
            lbl.contextRequested.connect(self.pageContextRequested)
            self._layout.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
            self._labels.append(lbl)
        self._layout.activate()
        self._current = 0
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        if render:
            QTimer.singleShot(0, self._ensure_visible)
        self.currentPageChanged.emit(0, len(self._labels))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._update_current)
        QTimer.singleShot(0, self._ensure_visible)

    def hideEvent(self, event) -> None:
        self._pump.stop()
        self._queue = []
        self._stop_render_worker()
        super().hideEvent(event)

    def clear_document(self) -> None:
        self._clear()
        self.currentPageChanged.emit(0, 0)

    def _clear(self) -> None:
        # Bump the generation first: any raster already in flight describes the
        # document we are throwing away, so `_on_page_rendered` will discard it
        # instead of painting into the new one.
        self._generation += 1
        self._sizes = []
        self._labels = []
        self._rendered = set()
        self._failed = set()
        self._queue = []
        self._pump.stop()
        self._stop_render_worker()
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def closeEvent(self, event) -> None:
        self._generation += 1
        self._pump.stop()
        self._queue = []
        self._stop_render_worker()
        super().closeEvent(event)

    def set_zoom(self, zoom: float) -> None:
        if zoom == self._zoom:
            return
        # Remember which page is under the viewport top and how far into it we
        # were. Without this the numeric scrollbar value survives a zoom while
        # the content height changes, so the page under the cursor drifts.
        anchor = self._anchor_state()
        self._zoom = zoom
        # A raster still running at the old scale must not be painted over the
        # new one, and the token table has to go with it.
        self._generation += 1
        self._inflight.clear()
        self._rendered = set()
        self._failed = set()     # a new scale is worth one fresh attempt
        self._queue = []
        self._pump.stop()
        self.horizontalScrollBar().setValue(0)
        for i, lbl in enumerate(self._labels):
            w, h = self._sizes[i]
            lbl.setFixedSize(int(w * zoom), int(h * zoom))
            lbl.setPixmap(QPixmap())
            lbl.setText(f"Page {i + 1} …")
        self._layout.invalidate()
        self._layout.activate()
        if anchor is not None:
            QTimer.singleShot(0, lambda: self._restore_anchor(anchor))
        self._ensure_visible()

    def _anchor_state(self) -> tuple[int, float] | None:
        """(page index, fraction into that page at the viewport top)."""
        if not self._labels:
            return None
        top = self.verticalScrollBar().value()
        for i, lbl in enumerate(self._labels):
            if lbl.y() + lbl.height() > top:
                offset = max(0, top - lbl.y())
                return i, min(1.0, offset / max(1, lbl.height()))
        return None

    def _restore_anchor(self, anchor: tuple[int, float] | None) -> None:
        if anchor is None:
            return
        index, progress = anchor
        if not (0 <= index < len(self._labels)):
            return
        label = self._labels[index]
        target = label.y() + round(progress * label.height())
        bar = self.verticalScrollBar()
        bar.setValue(max(bar.minimum(), min(target, bar.maximum())))

    def scroll_to_page(self, index: int) -> None:
        if not self._labels:
            return
        index = max(0, min(index, len(self._labels) - 1))
        lbl = self._labels[index]
        self.ensureWidgetVisible(lbl, 0, 0)
        bar = self.verticalScrollBar()
        centered_top = lbl.y() - max(
            0, (self.viewport().height() - lbl.height()) // 2)
        bar.setValue(max(bar.minimum(), min(centered_top, bar.maximum())))
        if self._current != index:
            self._current = index
            self.currentPageChanged.emit(index, len(self._labels))

    def center_page_horizontally(self, index: int | None = None) -> None:
        if not self._labels or self.viewport().width() <= 0:
            return
        page_index = self._current if index is None else index
        if not 0 <= page_index < len(self._labels):
            return
        self._layout.activate()
        label = self._labels[page_index]
        target = label.x() + (label.width() - self.viewport().width()) // 2
        self.horizontalScrollBar().setValue(target)

    def current_page(self) -> int:
        return self._current

    def page_count(self) -> int:
        return len(self._labels)

    def page_anchor_global(self, page_index: int, point) -> QPoint:
        """Map a point in page-label pixels to global screen coordinates."""
        if 0 <= page_index < len(self._labels):
            return self._labels[page_index].mapToGlobal(point)
        return self.mapToGlobal(point)

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _update_current(self) -> None:
        if not self._labels:
            return
        top = self.verticalScrollBar().value()
        cur = 0
        for i, lbl in enumerate(self._labels):
            if lbl.y() + lbl.height() > top + 4:
                cur = i
                break
            cur = i
        label = self._labels[cur]
        progress = min(
            1.0,
            max(0.0, (top - label.y()) / max(1, label.height())),
        )
        self.viewPositionChanged.emit(cur, progress)
        if cur != self._current:
            self._current = cur
            self.currentPageChanged.emit(cur, len(self._labels))

    def _ensure_visible(self) -> None:
        if not self._labels or not self.isVisible():
            return
        if self.viewport().height() <= 0:
            return
        self._layout.activate()
        vh = self.viewport().height()
        top = self.verticalScrollBar().value()
        lo, hi = top - self.BUFFER_PX, top + vh + self.BUFFER_PX
        self._evict_outside(lo, hi)
        needed = [i for i, lbl in enumerate(self._labels)
                  if i not in self._rendered
                  and lbl.y() + lbl.height() >= lo and lbl.y() <= hi]
        if not needed:
            self._log_initial_batch_complete()
            return
        # Nearest to the viewport first, so the page under the cursor paints
        # before its neighbours.
        mid = top + vh / 2
        needed.sort(key=lambda i: abs(self._labels[i].y() - mid))
        rest = needed[1:]
        if not self._render_page_index(needed[0]):
            # A raster is already in flight, so this page could not be handed
            # over yet; keep it at the head of the queue rather than dropping it.
            rest = needed
        self._queue = [i for i in rest if i not in self._rendered]
        if self._queue and not self._pump.isActive():
            self._pump.start()
        # The first page is only *dispatched* now; the batch is not complete
        # until it comes back, so no completion is logged here.

    def refresh_pages(self) -> None:
        """Discard page rasters and redraw the visible range in place."""
        if not self._labels:
            return
        self._generation += 1
        self._rendered.clear()
        self._failed.clear()
        self._queue.clear()
        self._pump.stop()
        for index, label in enumerate(self._labels):
            label.setPixmap(QPixmap())
            label.setText(f"Page {index + 1} …")
        self._ensure_visible()

    def _log_initial_batch_complete(self) -> None:
        """Log/announce the end of the first visible batch. Idempotent.

        With the raster off-thread there is no point at which "everything the
        pump wanted to draw" has actually been drawn synchronously, so this is
        driven from `_on_page_rendered` once the queue and the in-flight table
        are both empty. `initialRenderComplete` is what tells BookView to start
        rendering thumbnails, so it must still fire exactly once.
        """
        if not self._labels or self._queue or self._inflight:
            return
        if not self._initial_batch_logged:
            self._log_initial_batch_complete_now()

    def _evict_outside(self, top: int, bottom: int) -> None:
        for i in tuple(self._rendered):
            label = self._labels[i]
            if label.y() + label.height() < top or label.y() > bottom:
                label.setPixmap(QPixmap())
                label.setText(f"Page {i + 1} …")
                self._rendered.remove(i)

    def _pump_one(self) -> None:
        while self._queue and self._queue[0] in self._rendered:
            self._queue.pop(0)
        if not self._queue:
            self._pump.stop()
            self._log_initial_batch_complete()
            return
        if len(self._inflight) >= self.MAX_INFLIGHT:
            # Still waiting on a raster. Keep the page queued and keep ticking:
            # `_on_page_rendered` re-runs `_ensure_visible` when it lands.
            return
        # Dispatch only; the raster runs on the worker and the next page is
        # queued from `_on_page_rendered`, so the pump stays one page deep.
        self._render_page_index(self._queue.pop(0))
        if not self._queue:
            self._pump.stop()
        self._log_initial_batch_complete()

    def _log_initial_batch_complete_now(self) -> None:
        self._initial_batch_logged = True
        log.info(
            "Initial visible-page batch complete: %d pages rendered in %.1fms",
            len(self._rendered),
            (time.perf_counter() - self._document_set_at) * 1000,
        )
        if not self._initial_render_complete_emitted:
            self._initial_render_complete_emitted = True
            self.initialRenderComplete.emit()

    def _render_page_index(self, i: int) -> bool:
        """Hand page `i` to the render worker. Returns whether it was queued.

        The raster used to run inline here, on the GUI thread, so scrolling a
        page view froze the UI for the length of every page (measured 23 ms at
        100% zoom and 130 ms at 1000%). Dispatching keeps `_ensure_visible` and
        the QTimer pump responsive; the QImage comes back by signal and is
        turned into a QPixmap in `_on_page_rendered`, on the GUI thread.
        """
        if (i in self._rendered or i in self._failed
                or not (0 <= i < len(self._labels))):
            return False
        if i in self._inflight.values():
            return False                # already being rendered
        if len(self._inflight) >= self.MAX_INFLIGHT:
            return False
        worker = self._ensure_render_worker()
        if worker is None:
            return False
        self._next_token += 1
        token = self._next_token
        self._inflight[token] = i
        worker.submit(_RenderJob(token, i, self._zoom, self._generation))
        return True

    def _ensure_render_worker(self):
        worker = self._render_worker
        if worker is not None and worker.isRunning():
            return worker
        worker = PageRenderWorker(self._render_fn)
        worker.pageRendered.connect(self._on_page_rendered)
        self._render_worker = worker
        # Parked in module-level storage: a live QThread whose last Python
        # reference disappears aborts the process, and the reader can be
        # destroyed by deleteLater() without ever getting a closeEvent.
        _park_render_worker(worker)
        worker.start(QThread.Priority.LowestPriority)
        return worker

    def _stop_render_worker(self) -> None:
        """Stop the worker and wait briefly; never leave the GUI thread stuck.

        Waiting is bounded because a raster can be running. If the bound is
        exceeded the worker stays in the module-level park, which is what keeps
        the process alive; the park is released by its finished signal.
        """
        worker, self._render_worker = self._render_worker, None
        self._inflight.clear()
        if worker is None:
            return
        try:
            worker.stop()
            if not worker.wait(self.WORKER_STOP_WAIT_MS):
                log.warning(
                    "Render worker did not stop within %dms; leaving it parked",
                    self.WORKER_STOP_WAIT_MS)
        except RuntimeError:
            log.debug("Render worker already gone", exc_info=True)
        _sweep_render_workers()

    def _on_page_rendered(self, outcome) -> None:
        """GUI thread: publish the finished raster, or record the failure."""
        self._inflight.pop(outcome.token, None)
        stale = (outcome.cancelled
                 or outcome.generation != self._generation
                 or outcome.zoom != self._zoom
                 or not (0 <= outcome.index < len(self._labels)))
        if outcome.error is not None:
            # Remember the failure. Otherwise _ensure_visible() re-selects this
            # page on every scroll tick, re-rendering it forever while the
            # exception is swallowed each time.
            if not stale:
                self._failed.add(outcome.index)
                self._labels[outcome.index].setText(
                    f"Page {outcome.index + 1} — cannot render "
                    f"({outcome.error})")
            log.error("Could not render PDF page %d", outcome.index + 1,
                      exc_info=outcome.error)
        elif not stale and isinstance(outcome.image, QImage):
            if outcome.image.isNull():
                log.warning(
                    "Page %d raster was null; skipping",
                    outcome.index + 1)
                return
            # QPixmap is a GUI resource: it must be built here, never in the
            # worker thread.
            self._labels[outcome.index].setPixmap(
                QPixmap.fromImage(outcome.image))
            self._rendered.add(outcome.index)
            if not self._first_render_logged:
                self._first_render_logged = True
                log.info(
                    "First page rendered: page=%d elapsed=%.1fms render=%.1fms",
                    outcome.index + 1,
                    (time.perf_counter() - self._document_set_at) * 1000,
                    outcome.elapsed_ms,
                )
        self._schedule()
        self._ensure_visible()
