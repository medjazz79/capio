"""Floating translator for other apps (Foxit Reader, browser...).

Dead simple: click the floating button -> drag-select any text on screen ->
the Arabic popup appears. Drag the button to move it.
Right-click: translation options, settings, and quit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Shared bootstrap with main.py: a frozen build and a source checkout resolve
# their app folder differently, and chdir-ing into PyInstaller's temporary
# extraction dir would break every relative path.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
    for p in (str(RESOURCE_DIR), str(APP_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR
    os.chdir(APP_DIR)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

# Right-clicking "Settings" must never run this on a system Python that lacks
# PySide6/PyMuPDF — that is why the float launcher used to fail to start.
VENV_PYTHON = APP_DIR / ".venv" / "Scripts" / "python.exe"
VENV_PYTHONW = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
if (
    __name__ == "__main__"
    and not getattr(sys, "frozen", False)
    and os.name == "nt"
    and VENV_PYTHON.is_file()
    and os.path.normcase(str(Path(sys.executable).resolve()))
    not in {
        os.path.normcase(str(VENV_PYTHON.resolve())),
        os.path.normcase(str(VENV_PYTHONW.resolve())),
    }
):
    import subprocess

    raise SystemExit(subprocess.call(
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(APP_DIR),
    ))

from config.app_log import get_logger, setup_logging
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QGuiApplication, QIcon, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QMenu, QMessageBox, QPushButton,
    QSystemTrayIcon, QVBoxLayout,
)

from config.settings import load, save
from popup.translate_popup import TranslatePopup
from ui.file_drop import local_document_paths
from ui.theme import THEMES

log = get_logger("float")


def _floating_app_icon() -> QIcon:
    master = QPixmap(256, 256)
    master.fill(Qt.GlobalColor.transparent)
    painter = QPainter(master)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    background = QPainterPath()
    background.addRoundedRect(12, 12, 232, 232, 58, 58)
    gradient = QLinearGradient(24, 20, 232, 236)
    gradient.setColorAt(0, QColor("#183B70"))
    gradient.setColorAt(1, QColor("#10213F"))
    painter.fillPath(background, gradient)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255, 50), 3))
    painter.drawPath(background)

    left_page = QPainterPath()
    left_page.moveTo(54, 74)
    left_page.cubicTo(79, 72, 108, 78, 128, 94)
    left_page.lineTo(128, 190)
    left_page.cubicTo(105, 173, 79, 168, 54, 174)
    left_page.closeSubpath()
    right_page = QPainterPath()
    right_page.moveTo(202, 74)
    right_page.cubicTo(177, 72, 148, 78, 128, 94)
    right_page.lineTo(128, 190)
    right_page.cubicTo(151, 173, 177, 168, 202, 174)
    right_page.closeSubpath()

    painter.setPen(QPen(QColor("#DCEBFF"), 3))
    painter.setBrush(QColor("#F7FAFF"))
    painter.drawPath(left_page)
    painter.drawPath(right_page)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#70BDEB"), 5, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    for y in (119, 141, 163):
        painter.drawLine(72, y, 108, y + 2)
        painter.drawLine(148, y + 2, 184, y)
    painter.setPen(QPen(QColor("#42D6C5"), 6, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.drawLine(128, 98, 128, 182)
    painter.end()

    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(master.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
    return icon


def _log(stage: str) -> None:
    """Now goes to the rotating log (app_log) instead of an endless temp file."""
    log.debug("%s", stage)


def _estimate_vertical_scroll(previous, current) -> int:
    """Estimate viewport movement by matching image rows across screenshots."""
    from PIL import Image, ImageChops, ImageStat

    if previous.size != current.size or previous.height < 32:
        return 0

    sample_width = min(96, previous.width)
    sample_height = min(360, previous.height)
    sample_size = (sample_width, sample_height)
    old = previous.convert("L").resize(sample_size, Image.Resampling.BILINEAR)
    new = current.convert("L").resize(sample_size, Image.Resampling.BILINEAR)
    max_shift = sample_height - max(12, sample_height // 5)
    best_shift = 0
    best_score = float("inf")
    for shift in range(-max_shift, max_shift + 1):
        if shift == 0:
            continue
        if shift > 0:
            old_part = old.crop((0, shift, sample_width, sample_height))
            new_part = new.crop((0, 0, sample_width, sample_height - shift))
        else:
            old_part = old.crop((0, 0, sample_width, sample_height + shift))
            new_part = new.crop((0, -shift, sample_width, sample_height))
        score = ImageStat.Stat(ImageChops.difference(old_part, new_part)).mean[0]
        if score < best_score:
            best_shift = shift
            best_score = score

    unchanged_score = ImageStat.Stat(ImageChops.difference(old, new)).mean[0]
    if best_shift == 0 or unchanged_score < 1.5 or best_score > unchanged_score * 0.8:
        return 0
    return round(best_shift * previous.height / sample_height)


def _compose_scrolled_selection(frames, scroll_offset: int, endpoint_y: int):
    """Stitch viewport captures and crop the document area traversed by a drag."""
    from PIL import Image

    if not frames:
        return None
    width = max(image.width for image, _offset in frames)
    top = min(0, min(offset for _image, offset in frames), scroll_offset)
    endpoint = scroll_offset + endpoint_y
    selection_top = min(0, endpoint)
    selection_bottom = max(0, endpoint)
    bottom = max(
        max(offset + image.height for image, offset in frames),
        selection_bottom,
    )
    if bottom <= top:
        return None
    stitched = Image.new("RGB", (width, bottom - top), "white")
    for image, offset in frames:
        stitched.paste(image.convert("RGB"), (0, offset - top))
    return stitched.crop((
        0, selection_top - top, width, selection_bottom - top,
    ))


def _forward_wheel_to_window_under_cursor(delta: int, overlay_handle: int) -> bool:
    """Forward wheel input without hiding the overlay or breaking an active drag."""
    if os.name != "nt":
        log.warning("Forwarding mouse-wheel input is only supported on Windows")
        return False

    import ctypes
    from ctypes import wintypes

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(Point)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [Point]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t,
    ]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    cursor = Point()
    if not user32.GetCursorPos(ctypes.byref(cursor)):
        raise ctypes.WinError(ctypes.get_last_error())

    ex_style_index = -20
    transparent_style = 0x20
    original_style = user32.GetWindowLongPtrW(overlay_handle, ex_style_index)
    user32.SetWindowLongPtrW(
        overlay_handle, ex_style_index, original_style | transparent_style)
    try:
        if not user32.SetWindowPos(
            overlay_handle, None, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        target = user32.WindowFromPoint(cursor)
        if not target or target == overlay_handle:
            log.warning("Could not find a window under the cursor for wheel input")
            return False

        root = user32.GetAncestor(target, 2)
        class_name = ctypes.create_unicode_buffer(128)
        if root:
            user32.GetClassNameW(root, class_name, len(class_name))
        # Chromium/Electron routes wheel input through its top-level widget,
        # while many native controls handle it on the child under the pointer.
        if root and class_name.value.startswith("Chrome_WidgetWin"):
            target = root

        wheel_delta = (delta & 0xFFFF) << 16
        screen_position = (
            (cursor.x & 0xFFFF) | ((cursor.y & 0xFFFF) << 16)
        )
        if not user32.PostMessageW(
            target, 0x020A, wheel_delta, screen_position
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        user32.SetWindowLongPtrW(
            overlay_handle, ex_style_index, original_style)
        user32.SetWindowPos(
            overlay_handle, None, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020,
        )


class SnipOverlay(QDialog):
    """Full-screen dim layer for selecting any region on the active display.

    Drag a rectangle to select. Right-click or Esc cancels.
    Auto-closes after 60s safety timeout.
    """

    def __init__(self, area):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._screen_origin = QPoint(area.topLeft())
        self.setGeometry(area)
        self._origin: QPoint | None = None
        self._current_point: QPoint | None = None
        self._selection_rect: QRect | None = None
        self.selected_rect = None
        self.selected_pil = None
        self._scrolling_selection = False
        self._scroll_capture_rect: QRect | None = None
        self._scroll_frames = []
        self._scroll_last_frame = None
        self._scroll_offset = 0
        self._scroll_capture_timer = QTimer(self)
        self._scroll_capture_timer.setInterval(140)
        self._scroll_capture_timer.timeout.connect(self._capture_scroll_frame)
        QTimer.singleShot(60000, self.reject)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(128, 128, 128, 51))
        if self._selection_rect is not None:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(self._selection_rect, QColor(0, 0, 0, 0))
        if self._scrolling_selection and self._scroll_capture_rect is not None:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(self._scroll_capture_rect, QColor(0, 0, 0, 0))
        painter.end()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if os.name != "nt":
            return
        import ctypes

        set_affinity = ctypes.WinDLL("user32", use_last_error=True).SetWindowDisplayAffinity
        set_affinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        set_affinity.restype = ctypes.c_int
        if not set_affinity(int(self.winId()), 0x11):
            error = ctypes.get_last_error()
            log.warning(
                "Could not exclude selection overlay from screen captures "
                "(Windows error %s); scroll stitching may include overlay pixels",
                error,
            )

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.RightButton:
            self._scroll_capture_timer.stop()
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self.reject()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self._origin = e.position().toPoint()
            self._current_point = self._origin
            self._selection_rect = QRect(self._origin, self._origin)
            self.grabMouse(Qt.CursorShape.CrossCursor)
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._origin is not None:
            self._current_point = e.position().toPoint()
            if not self._scrolling_selection:
                self._selection_rect = QRect(
                    self._origin, self._current_point).normalized()
            else:
                self._update_scrolled_selection_rect()
            self._update_scroll_capture_rect()
            if self._scroll_capture_rect is not None:
                self._scroll_capture_timer.start()
            self.update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            origin = self._origin
            self._current_point = e.position().toPoint()
            r = QRect(origin, self._current_point).normalized()
            self._scroll_capture_timer.stop()
            if self._scrolling_selection:
                self._capture_scroll_frame()
                self.selected_pil = _compose_scrolled_selection(
                    self._scroll_frames,
                    self._scroll_offset,
                    self._current_point.y() - origin.y(),
                )
            self._origin = None
            self.releaseMouse()
            if self._scrolling_selection:
                image = self.selected_pil
                if image is not None and image.width > 10 and image.height > 10:
                    left = min(origin.x(), self._current_point.x())
                    top = min(origin.y(), self._current_point.y())
                    self.selected_rect = QRect(
                        self._screen_origin + QPoint(left, top),
                        QSize(image.width, image.height),
                    )
                    self.accept()
                    return
            elif r.width() > 10 and r.height() > 10:
                self.selected_rect = QRect(
                    self._screen_origin + r.topLeft(), r.size()
                )
                self.accept()
                return
            self._selection_rect = None
            self._current_point = None
            self._scroll_capture_rect = None
            self._scrolling_selection = False
            self._scroll_frames.clear()
            self._scroll_last_frame = None
            self._scroll_offset = 0
            self._scroll_capture_rect = None
            self.update()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self._scroll_capture_timer.stop()
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self.reject()
        else:
            super().keyPressEvent(e)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y()
        if not delta:
            event.accept()
            return

        if self._origin is not None:
            self._scrolling_selection = True
            self._update_scroll_capture_rect()
            if self._scroll_capture_rect is not None:
                self._update_scrolled_selection_rect()
                self.update()
                QApplication.processEvents()
            self._capture_scroll_frame()

        try:
            _forward_wheel_to_window_under_cursor(delta, int(self.winId()))
        except OSError:
            log.exception("Could not forward mouse-wheel input to the window")
        event.accept()

    def _update_scroll_capture_rect(self) -> None:
        if self._origin is None or self._current_point is None:
            return
        width = abs(self._current_point.x() - self._origin.x()) + 1
        height = self.height() - self._origin.y()
        if width < 11 or height < 11:
            return
        new_rect = QRect(
            min(self._origin.x(), self._current_point.x()),
            self._origin.y(),
            width,
            height,
        )
        if self._scroll_capture_rect != new_rect:
            self._scroll_capture_rect = new_rect
            self._scroll_last_frame = None
        if not self._scroll_capture_timer.isActive():
            self._scroll_capture_timer.start()

    def _capture_scroll_frame(self) -> None:
        if self._scroll_capture_rect is None:
            return
        area = QRect(self._scroll_capture_rect)
        area.translate(self.geometry().topLeft())
        frame = capture_pil(area)
        if frame is None:
            log.warning("Could not capture viewport while extending a selection")
            return
        if self._scroll_last_frame is None:
            if not self._scroll_frames:
                self._scroll_frames = [(frame, self._scroll_offset)]
            else:
                self._scroll_frames.append((frame, self._scroll_offset))
        else:
            movement = _estimate_vertical_scroll(self._scroll_last_frame, frame)
            if movement:
                self._scroll_offset += movement
                self._scroll_frames.append((frame, self._scroll_offset))
                self._scrolling_selection = True
                self._update_scrolled_selection_rect()
        self._scroll_last_frame = frame
        if self._scrolling_selection:
            self.update()

    def _update_scrolled_selection_rect(self) -> None:
        if self._origin is None or self._current_point is None:
            return
        left = min(self._origin.x(), self._current_point.x())
        width = max(1, abs(self._current_point.x() - self._origin.x()) + 1)
        height = max(
            1,
            self._scroll_offset + self._current_point.y() - self._origin.y(),
        )
        self._selection_rect = QRect(left, self._origin.y(), width, height)


def _mss_grab_pil(rect):
    """Region screenshot via mss (real pixels). Returns PIL image or raises."""
    import mss as mss_lib
    from PIL import Image
    screen = (QGuiApplication.screenAt(rect.center())
              or QGuiApplication.primaryScreen())
    dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
    region = {"top": int(rect.y() * dpr), "left": int(rect.x() * dpr),
              "width": max(1, int(rect.width() * dpr)),
              "height": max(1, int(rect.height() * dpr))}
    with mss_lib.MSS() as sct:
        shot = sct.grab(region)
    pil = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
    if abs(dpr - 1.0) > 0.01:
        pil = pil.resize((rect.width(), rect.height()), Image.LANCZOS)
    return pil


def _qt_grab_pil(rect):
    """Fallback: Qt screen grab, then crop. Returns PIL image or None.

    `grabWindow(0, x, y, w, h)` does NOT crop: in Qt the window id 0 means the
    whole desktop, so the four arguments are ignored and the OCR would receive
    the entire screen. Grab first, then cut the region out of the QImage.
    """
    from reader import pdf_utils
    screen = (QGuiApplication.screenAt(rect.center())
              or QGuiApplication.primaryScreen())
    if screen is None:
        return None
    img = screen.grabWindow(0).toImage()
    if img.isNull():
        return None

    # Same devicePixelRatio correction the mss path applies: on a 200% screen
    # the grabbed pixels are twice the logical size the rect is given in.
    dpr = float(screen.devicePixelRatio() or 1.0)
    origin = screen.geometry().topLeft()
    x = int(round((rect.x() - origin.x()) * dpr))
    y = int(round((rect.y() - origin.y()) * dpr))
    w = max(1, int(round(rect.width() * dpr)))
    h = max(1, int(round(rect.height() * dpr)))
    # A rect dragged partly off-screen must not produce a null image.
    x = max(0, min(x, img.width() - 1))
    y = max(0, min(y, img.height() - 1))
    w = max(1, min(w, img.width() - x))
    h = max(1, min(h, img.height() - y))

    return pdf_utils.qimage_to_pil(img.copy(x, y, w, h))


def capture_pil(rect):
    """Screenshot a global rect -> PIL Image (None when impossible)."""
    rect = QRect(rect).normalized()
    if rect.width() < 10 or rect.height() < 10:
        return None
    try:
        return _mss_grab_pil(rect)
    except Exception as e:
        _log(f"mss grab failed ({e!r}), trying Qt")
    try:
        return _qt_grab_pil(rect)
    except Exception as e:
        _log(f"qt grab failed ({e!r})")
        return None


def capture_rect(rect) -> object | None:
    """Legacy wrapper (tests): screenshot -> QImage or None."""
    from PySide6.QtGui import QImage
    from reader import pdf_utils  # noqa: F401
    pil = capture_pil(rect)
    if pil is None:
        return None
    from io import BytesIO
    buf = BytesIO()
    pil.save(buf, format="PNG")
    img = QImage()
    img.loadFromData(buf.getvalue(), "PNG")
    return img if not img.isNull() else None


class FloatButton(QDialog):
    """One button: click -> select on screen -> translate. Drag to move it."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.settings = load()
        self.popups: list[TranslatePopup] = []
        self._drag_from: QPoint | None = None
        self._moved = False
        self._picked_image = None
        self._pick_error: str | None = None
        self._theme_action_sets: list[dict[str, QAction]] = []
        self._explain_actions: list[QAction] = []

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setWindowTitle("Floating Translator")
        self._app_icon = _floating_app_icon()
        self.setWindowIcon(self._app_icon)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.btn = QPushButton()
        self.btn.setIcon(self._app_icon)
        self.btn.setIconSize(QSize(34, 34))
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setToolTip(
            "Click: select text on screen → translate\n"
            "Drag: move me around; drop a PDF to open it in the reader\n"
            "Right-click: toggle Explain / settings / quit")
        self.btn.setFixedSize(56, 56)
        self.btn.setAcceptDrops(True)
        self._apply_float_theme()
        self.btn.clicked.connect(self._on_click)
        self.btn.installEventFilter(self)
        lay.addWidget(self.btn)
        self.setFixedSize(self.sizeHint())

        self.menu = QMenu(self)
        self.menu.addAction("🌐 Translation languages…", self._open_float_languages)
        self._explain_action = self.menu.addAction("Explain")
        self._explain_actions.append(self._explain_action)
        self._explain_action.setCheckable(True)
        self._explain_action.setToolTip(
            "Add a concise explanation below each translation")
        self._explain_action.triggered.connect(self._set_explain_mode)
        self._sync_explain_action()
        self._selection_mode_actions = self._add_selection_mode_actions(
            self.menu)
        self._add_theme_menu(self.menu)
        self.menu.addSeparator()
        self.menu.addAction("⚙ Settings", self._open_settings)
        self.menu.addAction("✕ Quit", self._quit)
        self.btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn.customContextMenuRequested.connect(
            lambda pos: self.menu.exec(self.btn.mapToGlobal(pos)))

        # A tray icon is the only way back if the button is dragged off-screen
        # or a pinned popup keeps the process alive after "Quit".
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self)
            self._tray.setIcon(_floating_app_icon())
            self._tray.setToolTip("Floating Translator")
            tray_menu = QMenu()
            tray_menu.addAction(
                "🌐 Translation languages…", self._open_float_languages)
            tray_explain_action = tray_menu.addAction("Explain")
            tray_explain_action.setCheckable(True)
            tray_explain_action.setToolTip(
                "Add a concise explanation below each translation")
            tray_explain_action.triggered.connect(self._set_explain_mode)
            self._explain_actions.append(tray_explain_action)
            self._tray_explain_action = tray_explain_action
            self._tray_selection_mode_actions = (
                self._add_selection_mode_actions(tray_menu))
            self._add_theme_menu(tray_menu)
            tray_menu.addSeparator()
            tray_menu.addAction("⚙ Settings", self._open_settings)
            tray_menu.addAction("Select text on screen", self._on_click)
            tray_menu.addAction("✕ Quit", self._quit)
            self._tray.setContextMenu(tray_menu)
            self._tray.activated.connect(
                lambda reason: self._on_click()
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None)
            self._tray.show()
        self._sync_theme_actions()
        self._apply_float_theme()
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        pos = self.settings.get("float_pos") or []
        if len(pos) == 2:
            try:
                self.move(self._bounded_position(
                    QPoint(int(pos[0]), int(pos[1]))))
            except (TypeError, ValueError):
                self._place_default()
        else:
            self._place_default()

        # Same shared settings file as the main reader: pick up font/model
        # changes saved there (and vice versa) without a restart.
        self._settings_poll = QTimer(self)
        self._settings_poll.setInterval(1500)
        self._settings_poll.timeout.connect(self._reload_settings)
        self._settings_poll.start()

    def _reload_settings(self) -> None:
        try:
            cur = load()
        except Exception:
            return
        if cur == self.settings:
            return
        old = self.settings
        self.settings = cur
        self._sync_selection_mode_actions()
        self._sync_explain_action()
        if cur.get("theme") != old.get("theme"):
            self._sync_theme_actions()
            self._apply_float_theme()
        if (cur.get("font_size") != old.get("font_size")
                or cur.get("translation_font") != old.get("translation_font")
                or cur.get("translation_font_arabic")
                != old.get("translation_font_arabic")
                or cur.get("translation_font_latin")
                != old.get("translation_font_latin")
                or cur.get("arabic_tashkeel")
                != old.get("arabic_tashkeel")
                or cur.get("line_spacing_px") != old.get("line_spacing_px")
                or cur.get("translation_side_margin_px")
                != old.get("translation_side_margin_px")
                or cur.get("theme") != old.get("theme")
                or cur.get("explain_mode") != old.get("explain_mode")
                or cur.get("explain_instructions")
                != old.get("explain_instructions")):
            for popup in self.popups:
                if popup.isVisible():
                    popup.refresh_display(self._popup_settings())

    def event(self, event):
        handled = super().event(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and QApplication.activeWindow() is self
        ):
            QTimer.singleShot(0, self._restore_latest_popup)
        return handled

    def _restore_latest_popup(self) -> None:
        self.popups = [popup for popup in self.popups if popup.isVisible()]
        if not self.popups:
            self.raise_()
            return
        popup = self.popups[-1]
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(self._bounded_position(
                QPoint(area.right() - self.width() - 24,
                       area.bottom() - self.height() - 80)))

    def _bounded_position(
        self,
        position: QPoint,
        screen_hint: QPoint | None = None,
    ) -> QPoint:
        """Keep the whole bubble inside the available area of its display."""
        probe = screen_hint or QPoint(
            position.x() + self.width() // 2,
            position.y() + self.height() // 2,
        )
        screen = QGuiApplication.screenAt(probe) or QApplication.primaryScreen()
        if screen is None:
            return position
        area = screen.availableGeometry()
        max_x = max(area.left(), area.right() - self.width() + 1)
        max_y = max(area.top(), area.bottom() - self.height() + 1)
        return QPoint(
            min(max(position.x(), area.left()), max_x),
            min(max(position.y(), area.top()), max_y),
        )

    def _move_bubble(self, position: QPoint, pointer: QPoint) -> None:
        self.move(self._bounded_position(position, pointer))

    def _save_bubble_position(self) -> None:
        self.settings["float_pos"] = [self.x(), self.y()]
        try:
            save(self.settings)
        except OSError:
            log.exception("could not save floating bubble position")

    # ---- drag to move (presses land on the button, so filter it) ----
    def eventFilter(self, obj, e) -> bool:
        from PySide6.QtCore import QEvent
        if obj is self.btn:
            if e.type() == QEvent.Type.DragEnter:
                if local_document_paths(e.mimeData()):
                    e.acceptProposedAction()
                    return True
                e.ignore()
                return True
            if e.type() == QEvent.Type.Drop:
                paths = local_document_paths(e.mimeData())
                if paths:
                    self._open_in_reader(paths)
                    e.acceptProposedAction()
                else:
                    e.ignore()
                return True
            if e.type() == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton:
                self._drag_from = e.globalPosition().toPoint()
                self._moved = False
                return False
            if e.type() == QEvent.Type.MouseMove and self._drag_from is not None:
                if e.buttons() & Qt.MouseButton.LeftButton:
                    delta = e.globalPosition().toPoint() - self._drag_from
                    if delta.manhattanLength() > 4:
                        self._moved = True
                        pointer = e.globalPosition().toPoint()
                        self._move_bubble(self.pos() + delta, pointer)
                        self._drag_from = pointer
                return False
            if e.type() == QEvent.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton:
                was_moved = self._moved
                self._drag_from = None
                if was_moved:
                    self._save_bubble_position()
                    return True  # swallow the click after a drag
                self._moved = False
                return False
        return super().eventFilter(obj, e)

    def dragEnterEvent(self, event) -> None:
        if local_document_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        paths = local_document_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        self._open_in_reader(paths)
        event.acceptProposedAction()

    def _open_in_reader(self, paths: list[str]) -> None:
        import subprocess

        if getattr(sys, "frozen", False):
            candidates = (
                APP_DIR / "CapioReader.exe",
                APP_DIR / "Capio.exe",
                APP_DIR / "HarmonyTranslatorReader.exe",
                APP_DIR / "HarmonyTranslator.exe",
            )
            executable = next(
                (path for path in candidates if path.is_file()), None)
            command = [str(executable), *paths] if executable else None
            if command is None:
                self._show_reader_window(paths)
                return
        else:
            interpreter = VENV_PYTHONW if VENV_PYTHONW.is_file() else Path(sys.executable)
            reader_script = APP_DIR / "main.py"
            command = (
                [str(interpreter), str(reader_script), *paths]
                if reader_script.is_file() else None
            )
        if command is None:
            QMessageBox.warning(
                self,
                "Reader unavailable",
                "Could not find the PDF reader beside the floating translator. "
                "Start Capio's reader and drop the PDF there.",
            )
            return
        try:
            subprocess.Popen(command, cwd=str(APP_DIR), close_fds=True)
        except OSError as error:
            log.exception("could not open dropped PDFs in the reader")
            QMessageBox.warning(
                self, "Could not open PDF", str(error))

    def _show_reader_window(self, paths: list[str]) -> None:
        try:
            window = getattr(self, "_reader_window", None)
            if window is None or getattr(window, "_closing", False):
                from main import MainWindow
                window = MainWindow()
                self._reader_window = window
            window.open_paths(paths)
            window.show()
            window.raise_()
            window.activateWindow()
        except Exception as error:
            log.exception("could not open dropped PDFs in the embedded reader")
            QMessageBox.warning(
                self, "Could not open PDF", str(error))

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_from = e.globalPosition().toPoint()
            self._moved = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag_from is not None:
            delta = e.globalPosition().toPoint() - self._drag_from
            if delta.manhattanLength() > 4:
                self._moved = True
                pointer = e.globalPosition().toPoint()
                self._move_bubble(self.pos() + delta, pointer)
                self._drag_from = pointer
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._moved:
            self._save_bubble_position()
            e.accept()
            self._drag_from = None
            return
        self._drag_from = None
        super().mouseReleaseEvent(e)

    def _on_click(self) -> None:
        if self._moved:
            self._moved = False
            return
        _log("click")
        self.btn.setIcon(QIcon())
        self.btn.setText("…")
        try:
            self.snip_now()
        finally:
            self.btn.setText("")
            self.btn.setIcon(self._app_icon)

    # ---- click -> select -> translate ----
    def snip_now(self) -> bool:
        self._picked_image = None
        rect = self._pick_rect()
        try:
            if rect is None:
                _log("pick: none")
                if self._pick_error:
                    QMessageBox.warning(
                        self, "Could not select text", self._pick_error)
                else:
                    self._blink("?")
                return False
            _log(f"pick: {rect.x()},{rect.y()},{rect.width()}x{rect.height()}")
            # The overlay just closed: let its pixels vanish before grabbing.
            QApplication.processEvents()
            import time
            time.sleep(0.15)
            pil = self._picked_image
            self._picked_image = None
            if pil is None:
                pil = capture_pil(rect)
            if pil is None:
                _log("capture: null")
                QMessageBox.warning(
                    self,
                    "Could not capture selection",
                    "The selected screen area could not be captured. Check "
                    "that screen capture is allowed, then try again.",
                )
                return False
            _log(f"capture: ok {pil.size}")
            return self.translate_pil(pil)
        finally:
            try:
                self.show()
            except Exception:
                pass

    def translate_pil(self, pil_image) -> bool:
        # The reader and floating translator share settings. Don't let a
        # translation start with the previous poll's prompt options.
        self._reload_settings()
        if self.settings.get("selection_mode", "ocr") == "image":
            clean = pil_image
            _log("image: sending selected pixels directly to the selected model")
        else:
            from translators import ocr
            _log(f"ocr: available={ocr.is_available()}")
            if not ocr.is_available():
                QMessageBox.warning(
                    self,
                    "OCR is unavailable",
                    "Scanned text selection requires OneOCR from the "
                    "Windows 11 Snipping Tool. Install or repair Snipping "
                    "Tool from the Microsoft Store, or switch selection mode "
                    "to Image in the floating translator menu.",
                )
                return False
            clean = ocr.prepare_image(pil_image)
            _log("ocr: reading…")
        self.popups = [p for p in self.popups if p.isVisible()]
        for p in self.popups:
            if not getattr(p, "_pinned", False):
                p.close()
        from popup.translate_popup import TranslatePopup
        popup = TranslatePopup(self._popup_settings(), pil_image=clean,
                               ocr_langs=None, parent=None,
                               floating_language_controls=True,
                               float_language_preferences=True,
                               language_change_callback=self._save_float_languages)
        popup.show()
        self.popups.append(popup)
        _log("popup: shown")
        return True

    def _popup_settings(self) -> dict:
        settings = dict(self.settings)
        settings["source_language"] = self.settings.get(
            "float_source_language", "auto")
        settings["target_language"] = self.settings.get(
            "float_target_language", "ar")
        return settings

    def _add_selection_mode_actions(self, menu: QMenu) -> dict[str, object]:
        group = QActionGroup(menu)
        group.setExclusive(True)
        actions = {}
        for mode, label in (("image", "Image"), ("ocr", "OCR")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, selected=mode:
                    self._set_selection_mode(selected) if checked else None
            )
            actions[mode] = action
        self._sync_selection_mode_actions(actions)
        return actions

    def _sync_selection_mode_actions(self, actions=None) -> None:
        active = self.settings.get("selection_mode", "ocr")
        action_sets = []
        bubble_actions = getattr(self, "_selection_mode_actions", None)
        if bubble_actions is not None:
            action_sets.append(bubble_actions)
        tray_actions = getattr(self, "_tray_selection_mode_actions", None)
        if tray_actions is not None:
            action_sets.append(tray_actions)
        if actions is not None:
            action_sets.append(actions)
        for action_set in action_sets:
            for mode, action in action_set.items():
                action.setChecked(mode == active)

    def _set_selection_mode(self, mode: str) -> None:
        if mode not in {"image", "ocr"}:
            raise ValueError(f"Unknown selection mode: {mode!r}")
        self.settings["selection_mode"] = mode
        self._sync_selection_mode_actions()
        try:
            save(self.settings)
        except OSError:
            log.exception("could not save floating selection mode")
            QMessageBox.warning(
                self,
                "Could not save selection mode",
                "The selection mode could not be saved. Check that the "
                "settings file is writable, then try again.",
            )

    def _sync_explain_action(self) -> None:
        enabled = bool(self.settings.get("explain_mode", False))
        for action in self._explain_actions:
            action.setChecked(enabled)

    def _add_theme_menu(self, menu: QMenu) -> dict[str, QAction]:
        theme_menu = menu.addMenu("Theme")
        group = QActionGroup(theme_menu)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for theme in THEMES:
            action = theme_menu.addAction(theme)
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, selected=theme:
                    self._set_theme(selected) if checked else None)
            actions[theme] = action
        self._theme_action_sets.append(actions)

    def _sync_theme_actions(self) -> None:
        active = self.settings.get("theme", "Dark Slate")
        for action_set in self._theme_action_sets:
            for theme, action in action_set.items():
                action.setChecked(theme == active)

    def _apply_float_theme(self) -> None:
        theme = self.settings.get("theme", "Dark Slate")
        colors = THEMES.get(theme, THEMES["Dark Slate"])
        self.btn.setStyleSheet(
            "QPushButton { "
            f"background-color: {colors['accent']}; "
            f"color: {colors['text']}; "
            f"border: 2px solid {colors['accent_light']}; "
            "border-radius: 28px; font-size: 26px; padding: 0; }"
            "QPushButton:hover { "
            f"background-color: {colors['accent_hover']}; }}"
            "QPushButton:pressed { "
            f"background-color: {colors['selection']}; }}"
        )
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(
                "QMenu { "
                f"background-color: {colors['panel']}; "
                f"color: {colors['text']}; "
                f"border: 1px solid {colors['border']}; }}"
                "QMenu::item:selected { "
                f"background-color: {colors['selection']}; }}"
                "QMenu::item:checked { "
                f"color: {colors['accent_light']}; }}"
            )

    def _set_theme(self, theme: str) -> None:
        if theme not in THEMES:
            raise ValueError(f"Unknown theme: {theme!r}")
        previous = self.settings.get("theme", "Dark Slate")
        self.settings["theme"] = theme
        try:
            save(self.settings)
        except OSError:
            self.settings["theme"] = previous
            self._sync_theme_actions()
            log.exception("could not save floating translator theme")
            QMessageBox.warning(
                self,
                "Could not save theme",
                "The theme could not be saved. Check that the settings "
                "file is writable, then try again.",
            )
            return
        self._sync_theme_actions()
        self._apply_float_theme()
        for popup in self.popups:
            if popup.isVisible():
                popup.refresh_display(self._popup_settings())

    def _set_explain_mode(self, enabled: bool) -> None:
        previous = bool(self.settings.get("explain_mode", False))
        self.settings["explain_mode"] = bool(enabled)
        try:
            save(self.settings)
        except OSError:
            self.settings["explain_mode"] = previous
            self._sync_explain_action()
            log.exception("could not save floating Explain mode")
            QMessageBox.warning(
                self,
                "Could not save Explain mode",
                "Explain mode could not be saved. Check that the settings "
                "file is writable, then try again.",
            )
            return
        self._sync_explain_action()
        for popup in self.popups:
            if popup.isVisible():
                popup.refresh_display(self._popup_settings())

    def translate_image(self, qimage) -> bool:
        from reader import pdf_utils
        return self.translate_pil(pdf_utils.qimage_to_pil(qimage))

    def _pick_rect(self):
        """Enter a full-screen crosshair selection mode on the active display."""
        from PySide6.QtGui import QCursor
        self._pick_error = None
        try:
            screen = (QGuiApplication.screenAt(QCursor.pos())
                      or QGuiApplication.primaryScreen())
            if screen is None:
                _log("pick: no screen")
                self._pick_error = (
                    "No display was found. Connect or enable a display, then "
                    "try selecting the text again."
                )
                return None
            self.hide()
            QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
            try:
                ov = SnipOverlay(screen.geometry())
                ov.showFullScreen()
                ov.raise_()
                _log("overlay: shown")
                ok = ov.exec()
                rect = ov.selected_rect
                self._picked_image = ov.selected_pil
                ov.deleteLater()
                _log(f"overlay: closed ok={ok} rect={rect is not None}")
                return rect if ok and rect is not None else None
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:
            _log(f"pick: EXCEPTION {e!r}")
            self._pick_error = (
                f"Screen selection failed: {e}\n\n"
                "Try again. If the problem continues, restart the floating "
                "translator and check its log."
            )
            try:
                self.show()
            except Exception:
                pass
            return None

    def _blink(self, mark: str) -> None:
        old = self.btn.text()
        self.btn.setText(mark)
        QTimer.singleShot(700, lambda: self.btn.setText(old))

    def _open_settings(self) -> None:
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            previous = self.settings
            updated = dlg.values()
            try:
                save(updated)
            except OSError:
                self.settings = previous
                log.exception("could not save settings from the float tool")
                QMessageBox.warning(
                    self,
                    "Could not save settings",
                    "Settings could not be saved. Check that the settings "
                    "file is writable, then try again.",
                )
                return
            self.settings = updated
            self._sync_selection_mode_actions()
            self._sync_explain_action()
            self._sync_theme_actions()
            self._apply_float_theme()
            for popup in self.popups:
                if popup.isVisible():
                    popup.refresh_display(self._popup_settings())

    def _open_float_languages(self) -> None:
        from ui.float_language_dialog import FloatLanguageDialog

        dialog = FloatLanguageDialog(self.settings, self)
        if not dialog.exec():
            return
        self._save_float_languages(*dialog.values())

    def _save_float_languages(
        self, source_language: str, target_language: str, _popup=None,
    ) -> None:
        self.settings["float_source_language"] = source_language
        self.settings["float_target_language"] = target_language
        try:
            save(self.settings)
        except OSError:
            log.exception("could not save floating translator languages")
            QMessageBox.warning(
                self,
                "Could not save languages",
                "The language choices could not be saved. Check that the "
                "settings file is writable, then try again.",
            )

    def _quit(self) -> None:
        """Real quit: pinned popups used to keep an invisible process alive."""
        log.info("floating translator quitting")
        for p in list(self.popups):
            try:
                p.close()
            except RuntimeError:
                pass
        self.popups.clear()
        if self._tray is not None:
            self._tray.hide()
            self._tray = None
        try:
            save(self.settings)
        except Exception:
            log.exception("could not save settings on quit")
        app = QApplication.instance()
        if app is not None:
            app.quit()
        self.deleteLater()


def main():
    setup_logging(stream=True)
    from translators import usage
    usage.start_session()
    app = QApplication(sys.argv)
    app.setApplicationName("Capio")
    app.setWindowIcon(_floating_app_icon())
    w = FloatButton()
    w.show()
    w.raise_()
    w.activateWindow()
    log.info("floating translator started (frozen=%s)", getattr(sys, "frozen", False))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
