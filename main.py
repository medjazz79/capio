"""Capio — read books in other languages with in-context translation and speech.

Redesigned with a modern, high-end desktop interface and large, comfortable buttons.
"""
from __future__ import annotations

import os
import sys
import logging
import re
import subprocess
from pathlib import Path

# One bootstrap for both entry points: a frozen build and a source checkout
# resolve their "app folder" differently, and chdir-ing into PyInstaller's
# temporary extraction dir would break every relative path.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent      # next to the .exe
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # bundled data
    for p in (str(RESOURCE_DIR), str(APP_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR
    os.chdir(APP_DIR)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

# Double-clicking main.py may use a system Python without the app dependencies.
VENV_PYTHON = APP_DIR / ".venv" / "Scripts" / "python.exe"
VENV_PYTHONW = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
MAX_RECENT_FILES = 15
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

from config.app_log import log_path, setup_logging  # noqa: E402  (needs sys.path)

_fault_log_file = None
from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QPoint, QRect, QTimer, QUrl, Qt, Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QInputDialog, QPushButton, QPlainTextEdit, QTabWidget, QTextEdit, QToolBar,
    QToolButton, QVBoxLayout,
    QWidget, QSpinBox,
)

from config.settings import DEFAULT_MODEL, load, resolve_model, save
from popup.translate_popup import TranslatePopup
from reader import pdf_utils
from reader.bookview import BookView
from reader.document_import import SUPPORTED_EXTENSIONS, SUPPORTED_FILE_FILTER
from reader.continuous import ContinuousReader  # noqa: F401 (used in tests)
from translators import ocr
from translators.prompt import LANGUAGE_CHOICES
from ui.file_drop import local_document_paths
from ui.theme import THEMES, theme_colors

APP_STYLE_TEMPLATE = """
* {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
QMainWindow, QDialog { 
    background-color: @window@; 
    color: @text@; 
}
QWidget { 
    color: @text@; 
}

/* ToolBar Setup */
QToolBar {
    background-color: @toolbar@; 
    spacing: 7px; 
    padding: 8px 12px;
    border: none; 
    border-bottom: 1px solid @border@;
}
QToolBar::separator { 
    background-color: @border@; 
    width: 1px; 
    margin: 6px 6px; 
}
QToolBar QToolButton {
    background-color: @surface@; 
    color: @text@; 
    border: 1px solid @border@;
    border-radius: 8px; 
    min-height: 32px; 
    padding: 5px 8px;
    font-size: 13px; 
    font-weight: 600;
}
QToolBar QToolButton:hover { 
    background-color: @surface_hover@; 
    border-color: @accent_light@; 
}
QToolBar QToolButton:pressed {
    background-color: @selection@; 
    border-color: @accent_light@; 
    color: @text@;
}

/* PushButtons (Large & Comfortable) */
QPushButton {
    background-color: @surface@; 
    color: @text@; 
    border: 1px solid @border@;
    border-radius: 8px; 
    min-height: 32px; 
    padding: 6px 14px;
    font-size: 13px; 
    font-weight: 600;
}
QPushButton:hover { 
    background-color: @surface_hover@; 
    border-color: @accent_light@; 
}
QPushButton:pressed { 
    background-color: @selection@; 
}
QPushButton:disabled, QToolButton:disabled { 
    color: @muted@; 
    opacity: 0.5;
}

/* Inputs & Combos matched to button height */
QPushButton#openPdfButton {
    background-color: @accent@;
    color: #FFFFFF;
    border: none;
    border-radius: 12px;
    min-height: 44px;
    padding: 10px 28px;
    font-size: 15px;
    font-weight: 700;
}
QPushButton#openPdfButton:hover {
    background-color: @accent_hover@;
}

QComboBox, QLineEdit, QSpinBox, QTextEdit {
    background-color: @surface@; 
    color: @text@; 
    border: 1px solid @border@;
    border-radius: 8px; 
    padding: 6px 10px; 
    font-size: 13px;
    selection-background-color: @accent@;
}
QComboBox { 
    min-height: 30px; 
    padding-right: 22px;
}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {
    border-color: @accent_light@;
}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QTextEdit:focus {
    border-color: @accent_light@;
}
QComboBox::drop-down {
    subcontrol-origin: padding; 
    subcontrol-position: top right;
    width: 24px; 
    border: none; 
    background: transparent;
}
QComboBox::down-arrow {
    image: none; 
    width: 0; 
    height: 0;
    border-left: 5px solid transparent; 
    border-right: 5px solid transparent;
    border-top: 6px solid @muted@;
}
QComboBox QAbstractItemView {
    background-color: @panel@; 
    color: @text@; 
    border: 1px solid @border@;
    border-radius: 8px;
    padding: 5px;
    selection-background-color: @selection@; 
    selection-color: @text@;
    outline: none;
}

/* GroupBox & Cards */
QGroupBox {
    background-color: @panel@; 
    border: 1px solid @border@; 
    border-radius: 10px;
    margin-top: 14px; 
    padding: 16px 14px 14px 14px; 
    font-weight: 700;
    font-size: 12px;
}
QGroupBox::title { 
    subcontrol-origin: margin; 
    left: 14px; 
    padding: 0 6px; 
    color: @muted@;
}

/* Tabs */
QTabWidget::pane { 
    border: none; 
    background-color: @canvas@; 
}
QTabBar { 
    background-color: @toolbar@; 
    border-bottom: 1px solid @border@;
}
QTabBar::tab {
    background-color: transparent; 
    color: @muted@; 
    border: none;
    border-bottom: 3px solid transparent;
    padding: 11px 20px;
    min-width: 120px; 
    font-size: 13px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background-color: @surface@; 
    color: @text@; 
    font-weight: 700;
    border-bottom: 3px solid @accent_light@;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:hover:!selected { 
    background-color: @surface_hover@; 
    color: @text@; 
}
QLabel#modelReadout {
    color: @text@;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
    border: none;
}

/* Scroll Area & Canvas */
QScrollArea#pdfCanvas { 
    background-color: @pdf_canvas@;
    border: none; 
}
QWidget#pdfPageContainer {
    background-color: @pdf_canvas@;
}
QSplitter::handle { 
    background-color: @border@; 
    width: 1px; 
}
QSplitter::handle:hover {
    background-color: @accent_light@;
}
QMenu { 
    background-color: @panel@; 
    color: @text@; 
    border: 1px solid @border@; 
    border-radius: 8px;
    padding: 5px;
}
QMenu::item { 
    padding: 7px 22px; 
    border-radius: 5px;
    font-size: 13px;
}
QMenu::item:selected { 
    background-color: @selection@; 
    color: @text@;
}

/* Scrollbars */
QScrollBar:vertical, QScrollBar:horizontal { 
    background-color: transparent; 
    margin: 0; 
}
QScrollBar:vertical { 
    width: 10px; 
}
QScrollBar:horizontal { 
    height: 10px; 
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: @scroll@; 
    min-width: 24px; 
    min-height: 24px;
    border-radius: 5px; 
    margin: 2px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background-color: @scroll_hover@;
}
QScrollBar::add-line, QScrollBar::sub-line { 
    width: 0; 
    height: 0; 
}
QScrollBar::add-page, QScrollBar::sub-page { 
    background: none; 
}
QScrollBar#pdfVerticalScrollBar:vertical {
    background-color: @canvas@;
    margin: 0;
}
QScrollBar#pdfVerticalScrollBar::handle:vertical {
    background-color: @accent_light@;
    min-height: 64px;
    border-radius: 7px;
    margin: 2px 3px;
}
QScrollBar#pdfVerticalScrollBar::handle:vertical:hover {
    background-color: @accent@;
}
QScrollBar#pdfThumbnailScrollBar {
    background-color: @panel@;
    margin: 0;
}
QScrollBar#pdfThumbnailScrollBar::handle:vertical {
    background-color: @accent_light@;
    min-height: 64px;
    border-radius: 7px;
    margin: 2px 3px;
}
QScrollBar#pdfThumbnailScrollBar::handle:vertical:hover {
    background-color: @accent@;
}

/* Labels & Status */
QLabel#brandLabel { 
    color: @text@; 
    font-size: 14px; 
    font-weight: 800; 
    letter-spacing: 1.2px;
}
QLabel#engineStatus { 
    background-color: @surface@;
    border: 1px solid @border@;
    border-radius: 14px;
    padding: 5px 12px;
    color: @muted@; 
    font-size: 12px; 
    font-weight: 600; 
}

/* Welcome Screen Hero */
QFrame#welcomeCard {
    background-color: @panel@; 
    border: 1px solid @border@; 
    border-radius: 18px;
}
QLabel#welcomeEyebrow { 
    color: @accent_light@; 
    font-size: 12px; 
    font-weight: 800; 
    letter-spacing: 1.5px;
}
QLabel#welcomeTitle { 
    color: @text@; 
    font-size: 34px; 
    font-weight: 800; 
}
QLabel#welcomeDescription { 
    color: @muted@; 
    font-size: 15px; 
}
QFrame#stepsCard {
    background-color: @surface@; 
    border: 1px solid @border@; 
    border-radius: 12px;
}
QLabel#stepNumber { 
    color: @accent_light@; 
    font-size: 13px; 
    font-weight: 800; 
}
QLabel#stepText { 
    color: @text@; 
    font-size: 13px; 
    font-weight: 600; 
}

/* Thumbnails panel (needs matching selectors inside reader/bookview.py) */
QListWidget#pdfThumbnails::viewport,
QListWidget#pdfBookmarks::viewport {
    background-color: @panel@;
}
QWidget#thumbsPanel {
    background-color: @panel@;
    border-right: 1px solid @border@;
}
QScrollArea#thumbsScroll {
    background-color: @panel@;
    border: none;
}
QLabel#thumbItem {
    background-color: @surface@;
    border: 1px solid @border@;
    border-radius: 8px;
}
QLabel#thumbItem:hover {
    border-color: @accent_light@;
}
QLabel#thumbItemActive {
    background-color: @surface@;
    border: 2px solid @accent@;
    border-radius: 8px;
}
"""


def app_stylesheet(
    theme: str = "Dark Slate",
    scheme: str = "dark",
    pdf_canvas_theme_background: bool = False,
) -> str:
    colors = theme_colors(theme, scheme)
    style = APP_STYLE_TEMPLATE
    for key, value in colors.items():
        style = style.replace(f"@{key}@", value)
    pdf_canvas = (
        colors["canvas"] if pdf_canvas_theme_background else "#F1F3F5")
    style = style.replace("@pdf_canvas@", pdf_canvas)
    return style


APP_STYLE = app_stylesheet()

ZOOM_PRESETS = ["50%", "75%", "80%", "90%", "100%", "125%", "150%",
                "175%", "200%", "250%", "300%", "400%", "500%",
                "600%", "800%", "1000%"]

MAIN_W, MAIN_H = 1803, 1044


from ui.settings_dialog import SettingsDialog  # noqa: F401  (re-exported)


class _CtrlWheelThumbsFilter(QObject):
    """Global filter: Ctrl+Wheel over a book's thumbnails panel zooms it.

    Requires `BookView.thumbs_widget()` (returns the widget/area that hosts
    the thumbnails, so we know when the cursor hovers it) and
    `BookView.thumbs_zoom(steps:int)` on reader/bookview.py.
    """

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self._window = window

    @staticmethod
    def _is_thumbnail_context_target(target: QWidget) -> bool:
        widget = target
        while widget is not None:
            if widget.objectName() in ("pdfThumbnails", "pdfBookmarks"):
                return True
            widget = widget.parentWidget()
        return False

    @staticmethod
    def _is_page_canvas_context_target(target: QWidget) -> bool:
        widget = target
        while widget is not None:
            if widget.objectName() == "pdfCanvas":
                return True
            widget = widget.parentWidget()
        return False

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ContextMenu:
            target = obj if isinstance(obj, QWidget) else None
            if (
                target is not None
                and target.window() is self._window
                and target is not self._window.tabs.tabBar()
                and not self._is_thumbnail_context_target(target)
                and not self._is_page_canvas_context_target(target)
            ):
                self._window._show_app_context_menu(event.globalPos())
                return True
        if event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                book = self._window.current_book()
                if book is not None and hasattr(book, "thumbs_widget") and hasattr(book, "thumbs_zoom"):
                    thumbs_widget = book.thumbs_widget()
                    target = (
                        obj if isinstance(obj, QWidget)
                        else QApplication.widgetAt(event.globalPosition().toPoint())
                    )
                    if (thumbs_widget is not None and target is not None
                            and (target is thumbs_widget
                                 or thumbs_widget.isAncestorOf(target))):
                        steps = 1 if event.angleDelta().y() > 0 else -1
                        book.thumbs_zoom(steps)
                        return True
        return super().eventFilter(obj, event)


def _abbrev(value: int) -> str:
    """`7400` -> `7k`, `1048576` -> `1M`.

    The breakdown beside the total is there to show the shape of the spend,
    not to be read digit by digit, so thousands are rounded — the exact
    figure is in the tooltip.
    """
    value = max(0, int(value or 0))
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{round(value / 1000):,}k"
    return f"{round(value / 1_000_000, 1):,}M"


class MainWindow(QMainWindow):
    ocrWarmed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Capio")
        self.resize(1050, 720)
        self.setAcceptDrops(True)
        self.settings = load()
        saved_theme = self.settings.get("theme", "Dark Slate")
        self.settings["theme"] = saved_theme if saved_theme in THEMES else "Dark Slate"
        app = QApplication.instance()
        self._previous_app_stylesheet = app.styleSheet()
        app.setStyleSheet(app_stylesheet(
            self.settings["theme"], self.settings.get("color_scheme", "system"),
            bool(self.settings.get("pdf_canvas_theme_background", False))))
        app.styleHints().colorSchemeChanged.connect(
            self._on_system_color_scheme_changed)
        self._closing = False
        self._shutdown_confirmed = False
        self._shutdown_waiting = False
        self._close_snapshot_saved = False
        self._restart_requested = False
        self._restoring_session = False
        self.popups: list[TranslatePopup] = []
        self._automatic_page_reading_book: BookView | None = None
        self._automatic_page_reading_popup: TranslatePopup | None = None
        self._automatic_page_reading_page: int | None = None
        self._automatic_page_waiting_for_speech = False
        self._automatic_page_reading_at_end = False
        self._ocr_ok: bool | None = None
        self.ocrWarmed.connect(self._on_ocr_warmed)

        # Global Ctrl+Wheel => zoom thumbnails (needs BookView.thumbs_zoom/thumbs_widget)
        self._thumbs_wheel_filter = _CtrlWheelThumbsFilter(self)
        QApplication.instance().installEventFilter(self._thumbs_wheel_filter)

        # Toolbar setup with large, comfortable buttons
        bar = QToolBar("Tools")
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setMovable(False)
        bar.setFloatable(False)
        self._toolbar = bar
        self.addToolBar(bar)

        def add_action(label: str, slot, tip: str):
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            bar.addAction(a)

        self.file_button = QToolButton(self)
        self.file_button.setObjectName("fileMenuButton")
        self.file_button.setText("File")
        self.file_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.file_menu = QMenu(self.file_button)
        self.open_file_action = self.file_menu.addAction(
            "Open file…", self.open_pdf)
        self.open_file_action.setShortcut(QKeySequence.StandardKey.Open)
        self.save_file_action = self.file_menu.addAction(
            "Save", self.save_file)
        self.save_file_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_as_action = self.file_menu.addAction(
            "Save as PDF…", self.save_file_as)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.file_menu.addSeparator()
        self.edit_source_action = self.file_menu.addAction(
            "Edit source text")
        self.edit_source_action.setCheckable(True)
        self.edit_source_action.setShortcut("Ctrl+Alt+E")
        self.edit_source_action.toggled.connect(self._set_source_editing)
        self.export_pages_action = self.file_menu.addAction(
            "Export selected pages…", self.export_selected_pages)
        self.print_action = self.file_menu.addAction(
            "Print…", self.print_pdf)
        self.pdf_properties_action = self.file_menu.addAction(
            "Document properties", self.show_pdf_properties)
        self.reload_pdf_action = self.file_menu.addAction(
            "Reload document", self.reload_pdf)
        self.file_menu.addSeparator()
        self.pdf_location_action = self.file_menu.addAction(
            "File location", self.open_pdf_location)
        self.copy_path_action = self.file_menu.addAction(
            "Copy as path", self.copy_pdf_path)
        self.file_menu.addSeparator()
        self.recent_files_menu = self.file_menu.addMenu("Open recent")
        self.recent_files_menu.aboutToShow.connect(
            self._populate_recent_files_menu)
        self.file_button.setMenu(self.file_menu)
        bar.addWidget(self.file_button)
        bar.addSeparator()

        add_action(
            "📂  Open",
            self.open_pdf,
            "Open supported books and documents — each in its own tab",
        )
        bar.addSeparator()

        add_action("◀  Prev page", self.prev_page, "Jump to the previous page")
        add_action("Next page  ▶", self.next_page, "Jump to the next page")
        bar.addSeparator()

        add_action("🔍  Zoom −", self.zoom_out, "Zoom out")
        add_action("🔍  Zoom +", self.zoom_in, "Zoom in")

        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.setToolTip("Zoom % — pick or type a number")
        self.zoom_combo.addItems(ZOOM_PRESETS)
        self.zoom_combo.setCurrentText("100%")
        self.zoom_combo.setFixedWidth(84)
        self.zoom_combo.activated.connect(lambda _i: self._on_zoom_chosen())
        self.zoom_combo.lineEdit().returnPressed.connect(self._on_zoom_chosen)
        bar.addWidget(self.zoom_combo)

        self.fit_button = QToolButton(self)
        self.fit_button.setText("⛶  Fit")
        self.fit_button.setToolTip("Choose a page fitting and zoom mode")
        self.fit_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fit_menu = QMenu(self.fit_button)
        for label, callback in (
            ("Actual Size", self.zoom_actual_size),
            ("Fit Page", self.zoom_fit_page),
            ("Fit Width", self.zoom_fit),
            ("Fit Visible", self.zoom_fit_visible),
            ("Zoom To…", self.zoom_to),
        ):
            self.fit_menu.addAction(label, callback)
        self.fit_button.setMenu(self.fit_menu)
        bar.addWidget(self.fit_button)
        bar.addSeparator()

        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sourceLanguageCombo")
        self.source_combo.setToolTip(
            "Source language — Auto detects it, or choose a language")
        self.source_combo.addItem("Auto", "auto")
        for label, code in LANGUAGE_CHOICES:
            self.source_combo.addItem(label, code)
        self.source_combo.setCurrentIndex(
            self._language_index(
                self.settings.get("source_language", "auto"), default="auto"))
        self.source_combo.setMinimumWidth(90)
        self.source_combo.currentIndexChanged.connect(
            self._on_source_language_changed)
        bar.addWidget(self.source_combo)
        direction = QLabel("→")
        direction.setToolTip("Translate from the selected source language")
        bar.addWidget(direction)
        self.target_combo = QComboBox()
        self.target_combo.setObjectName("targetLanguageCombo")
        self.target_combo.setToolTip("Language to translate into")
        for label, code in LANGUAGE_CHOICES:
            self.target_combo.addItem(label, code)
        self.target_combo.setCurrentIndex(
            self._language_index(self.settings.get("target_language", "ar")) - 1)
        self.target_combo.setMinimumWidth(90)
        self.target_combo.currentIndexChanged.connect(
            self._on_target_language_changed)
        bar.addWidget(self.target_combo)
        bar.addSeparator()

        self.thumbs_action = QAction("🖼  Pages", self)
        self.thumbs_action.setToolTip("Show / hide the pages panel")
        self.thumbs_action.setCheckable(True)
        self.thumbs_action.setChecked(bool(self.settings.get("thumbs_visible", False)))
        self.thumbs_action.toggled.connect(self._toggle_thumbs)
        bar.addAction(self.thumbs_action)
        bar.addSeparator()

        add_action("⚙  Settings", self.open_settings, "API keys and options")
        bar.addSeparator()

        self.engine_label = QLabel()
        self.engine_label.setObjectName("engineStatus")
        self.statusBar().addPermanentWidget(self.engine_label)
        self.statusBar().hide()
        self._refresh_engine_label()

        self.theme_button = QToolButton()
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setText("Themes")
        self.theme_button.setToolTip("Choose the application color theme")
        self.theme_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.theme_menu = QMenu(self.theme_button)
        self._theme_actions = {}
        for theme in THEMES:
            action = self.theme_menu.addAction(theme)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selected=theme: self._on_theme_changed(selected))
            self._theme_actions[theme] = action
        self.theme_button.setMenu(self.theme_menu)
        self._sync_theme_button(self.settings.get("theme", "Dark Slate"))
        bar.addWidget(self.theme_button)
        bar.addSeparator()

        self.selection_mode_button = QToolButton(self)
        self.selection_mode_button.setObjectName("selectionModeButton")
        self.selection_mode_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.selection_mode_button.setFixedWidth(72)
        self.selection_mode_button.clicked.connect(
            self._toggle_selection_mode)
        bar.addWidget(self.selection_mode_button)
        self._sync_selection_mode_button()

        # Right-hand corner: which model answered, and what it has cost in
        # total. It lives here rather than in the translation popup because
        # the popup covers the page, while this stays visible while you read.
        self.model_readout = QLabel(self)
        self.model_readout.setObjectName("modelReadout")
        self.model_readout.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.model_readout.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.model_readout.setMinimumWidth(160)
        self.model_readout.setToolTip("Model that answered, and its running cost.")
        spacer = QWidget(self)
        spacer.setFixedWidth(14)
        bar.addWidget(spacer)
        bar.addWidget(self.model_readout)
        self._model_readout_resize_timer = QTimer(self)
        self._model_readout_resize_timer.setSingleShot(True)
        self._model_readout_resize_timer.timeout.connect(
            self._sync_model_readout_width)
        QTimer.singleShot(0, self._sync_model_readout_width)
        self._show_model_usage("", "")

        self._settings_poll = QTimer(self)
        self._settings_poll.setInterval(1500)
        self._settings_poll.timeout.connect(self._reload_settings)
        self._settings_poll.start()

        # Modern Welcome Hero Screen with large buttons
        self.welcome = QWidget()
        wlay = QVBoxLayout(self.welcome)
        wlay.setContentsMargins(32, 32, 32, 32)
        wlay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        welcome_card = QFrame()
        welcome_card.setObjectName("welcomeCard")
        welcome_card.setMinimumWidth(700)
        welcome_card.setMaximumWidth(820)

        card_layout = QVBoxLayout(welcome_card)
        card_layout.setContentsMargins(48, 44, 48, 40)
        card_layout.setSpacing(16)

        eyebrow = QLabel("YOUR PERSONAL PDF READING SPACE")
        eyebrow.setObjectName("welcomeEyebrow")
        card_layout.addWidget(eyebrow, alignment=Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Read. Select. Understand.")
        title.setObjectName("welcomeTitle")
        card_layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        description = QLabel("Open a book, select a passage, and translate it into your chosen language.")
        description.setObjectName("welcomeDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(description)

        card_layout.addSpacing(10)

        self.welcome_open_btn = QPushButton("📂   Open")
        self.welcome_open_btn.setObjectName("openPdfButton")
        self.welcome_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.welcome_open_btn.setMinimumWidth(320)
        self.welcome_open_btn.clicked.connect(self.open_pdf)
        card_layout.addWidget(
            self.welcome_open_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        steps_card = QFrame()
        steps_card.setObjectName("stepsCard")
        steps_layout = QHBoxLayout(steps_card)
        steps_layout.setContentsMargins(20, 16, 20, 16)
        steps_layout.setSpacing(24)

        for number, text in (
            ("01", "Open a PDF"),
            ("02", "Drag over a passage"),
            ("03", "Read the translation"),
        ):
            step = QVBoxLayout()
            number_label = QLabel(number)
            number_label.setObjectName("stepNumber")
            text_label = QLabel(text)
            text_label.setObjectName("stepText")
            step.addWidget(number_label)
            step.addWidget(text_label)
            steps_layout.addLayout(step)

        card_layout.addSpacing(12)
        card_layout.addWidget(steps_card)
        wlay.addWidget(welcome_card)

        # Tab Widget
        self.tabs = QTabWidget()
        self._sync_file_actions()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabBar().tabMoved.connect(lambda _s, _t: self._save_session_soon())
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)

        central = QWidget()
        central.setAcceptDrops(True)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.welcome, 1)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.tabs.hide()

        self._flash_token = 0
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(500)
        self._geo_timer.timeout.connect(self._save_session_live)

        self._refresh_engine_label()
        self._apply_startup_geometry()

        QTimer.singleShot(400, self._warm_ocr)
        QTimer.singleShot(0, self._restore_session)

        tr = QAction("Translate", self)
        tr.setShortcut(QKeySequence("Ctrl+T"))
        tr.triggered.connect(self.translate_last)
        self.addAction(tr)

    @staticmethod
    def parse_zoom_text(text: str) -> float | None:
        try:
            value = float(text.strip().rstrip("%").strip())
        except (TypeError, ValueError):
            return None
        if 20.0 <= value <= 1000.0:
            return value
        return None

    def current_book(self) -> BookView | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, BookView) else None

    def _iter_books(self):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, BookView):
                yield w

    def _has_doc(self) -> bool:
        book = self.current_book()
        return book.has_doc() if book else False

    def _update_empty(self) -> None:
        empty = self.tabs.count() == 0
        self.welcome.setVisible(empty)
        self.tabs.setVisible(not empty)

    def open_pdf(self):
        start = (
            self.settings.get("last_open_dir")
            or self.settings.get("last_dir")
            or str(Path.home())
        )
        if not Path(start).is_dir():
            start = str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose books and documents",
            start,
            f"{SUPPORTED_FILE_FILTER};;All files (*)",
        )
        if paths:
            self.open_paths(paths)

    def open_paths(self, paths: list[str]) -> None:
        for path in paths:
            norm = str(Path(path).resolve())
            already_open = False
            for book in self._iter_books():
                try:
                    if book.path and str(Path(book.path).resolve()) == norm:
                        self.tabs.setCurrentWidget(book)
                        self._record_recent_file(book.path)
                        already_open = True
                        break
                except OSError:
                    pass
            else:
                already_open = self._add_tab(path, activate=True) is not None
            if already_open:
                self._remember_open_directory(norm)
        self._save_session_soon()

    def _remember_open_directory(self, path: str) -> None:
        try:
            directory = Path(path).expanduser().resolve().parent
            if directory.is_dir():
                self.settings["last_open_dir"] = str(directory)
        except OSError:
            logging.exception("Could not remember opened file directory: %s", path)

    def _recent_file_paths(self) -> list[str]:
        paths = self.settings.get("recent_files", [])
        if not isinstance(paths, list):
            logging.error("Saved recent_files setting is not a list; ignoring it")
            return []
        return [
            path for path in paths
            if isinstance(path, str) and path.strip()
        ][:MAX_RECENT_FILES]

    def _record_recent_file(self, path: str) -> None:
        recent = self._recent_file_paths()
        normalized = str(Path(path).resolve())
        normalized_key = os.path.normcase(normalized)
        self.settings["recent_files"] = [
            normalized,
            *(saved for saved in recent
              if os.path.normcase(str(Path(saved).resolve())) != normalized_key),
        ][:MAX_RECENT_FILES]

    def _populate_recent_files_menu(self) -> None:
        self.recent_files_menu.clear()
        recent = self._recent_file_paths()
        if not recent:
            empty = self.recent_files_menu.addAction("No recent files")
            empty.setEnabled(False)
        else:
            for path in recent:
                file_path = Path(path)
                label = file_path.name or path
                if not file_path.is_file():
                    label = f"{label} (not found)"
                action = self.recent_files_menu.addAction(label)
                action.setToolTip(path)
                action.setEnabled(file_path.is_file())
                action.triggered.connect(
                    lambda _checked=False, saved_path=path:
                    self.open_paths([saved_path]))
        if recent:
            self.recent_files_menu.addSeparator()
            self.recent_files_menu.addAction(
                "Clear recent files", self._clear_recent_files)

    def _clear_recent_files(self) -> None:
        self.settings["recent_files"] = []
        self._save_session_soon()
        self._populate_recent_files_menu()

    def _sync_file_actions(self) -> None:
        book = self.current_book()
        has_book = book is not None and book.has_doc()
        has_selected_pages = (
            has_book and bool(book.thumbs.selected_page_indices()))
        self.save_file_action.setEnabled(
            has_book and book.is_modified)
        self.save_as_action.setEnabled(
            has_book and not book.has_unsaved_source_text)
        self.edit_source_action.blockSignals(True)
        self.edit_source_action.setEnabled(
            has_book and book.can_edit_source)
        self.edit_source_action.setChecked(
            bool(has_book and book.is_editing_source))
        self.edit_source_action.blockSignals(False)
        self.export_pages_action.setEnabled(has_selected_pages)
        self.print_action.setEnabled(has_book)
        self.pdf_properties_action.setEnabled(
            has_book and book.path.lower().endswith(".pdf"))
        self.reload_pdf_action.setEnabled(has_book)
        self.pdf_location_action.setEnabled(has_book)
        self.copy_path_action.setEnabled(has_book)

    def _set_source_editing(self, enabled: bool) -> None:
        book = self.current_book()
        if book is None or not book.can_edit_source:
            return
        if not enabled and book.has_unsaved_source_text:
            if not self._confirm_pending_edits(
                    book, "leaving source editing"):
                self.edit_source_action.blockSignals(True)
                self.edit_source_action.setChecked(True)
                self.edit_source_action.blockSignals(False)
                return
        book.set_source_editing(enabled)
        self._sync_book_ui()

    def _confirm_pending_edits(self, book: BookView, action: str) -> bool:
        if not book.is_modified:
            return True
        detail = f"Save changes to “{book.title}” before {action}?"
        if not book.can_save_in_place:
            detail += (
                "\n\nThe source file will remain unchanged; saving creates "
                "a PDF copy.")
        answer = QMessageBox.warning(
            self,
            "Unsaved document changes",
            detail,
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            if not book.can_save_in_place:
                return self._save_book_as_pdf(book)
            try:
                book.save()
            except (OSError, RuntimeError, ValueError) as error:
                QMessageBox.critical(self, "Could not save document", str(error))
                return False
            self._sync_file_actions()
            self._save_session_soon()
            return True
        if answer == QMessageBox.StandardButton.Discard:
            if book.has_unsaved_source_text:
                try:
                    book.discard_source_text_changes()
                except (OSError, RuntimeError, ValueError) as error:
                    QMessageBox.critical(
                        self, "Could not discard source changes", str(error))
                    return False
            return True
        return False

    def export_selected_pages(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        pages = book.thumbs.selected_page_indices()
        if not pages:
            QMessageBox.information(
                self, "No pages selected",
                "Select one or more pages in the thumbnails panel first.")
            return
        first, last = pages[0] + 1, pages[-1] + 1
        default_path = str(Path(book.path).with_name(
            f"{Path(book.path).stem}_pages_{first}-{last}.pdf"))
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Export selected pages", default_path, "PDF files (*.pdf)")
        if not output_path:
            return
        if Path(output_path).suffix.lower() != ".pdf":
            output_path += ".pdf"
        try:
            book.save_pages_as_pdf(pages, output_path)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(
                self, "Could not export pages",
                f"Failed to save the selected pages:\n{error}")
            return
        self._flash_info(f"Exported {len(pages)} page(s) to {output_path}")

    def print_pdf(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(book.title)
        printer.setResolution(150)
        printer.setFromTo(1, book.page_count())
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle(f"Print {book.title}")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return

        first_page, last_page = 1, book.page_count()
        if printer.printRange() == QPrinter.PrintRange.PageRange:
            first_page = max(1, printer.fromPage())
            last_page = min(book.page_count(), printer.toPage())
        if first_page > last_page:
            return

        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.critical(self, "Could not print PDF",
                                 "The selected printer could not be started.")
            return
        try:
            target = printer.pageRect(QPrinter.Unit.DevicePixel)
            if target.width() <= 0 or target.height() <= 0:
                raise RuntimeError("The printer reported an invalid page size")
            for page_number in range(first_page, last_page + 1):
                if page_number > first_page and not printer.newPage():
                    raise RuntimeError("The printer could not start a new page")
                image = book.render_page_image(
                    page_number - 1, printer.resolution())
                if image.isNull():
                    raise RuntimeError(
                        f"PDF page {page_number} rendered as an empty image")
                scale = min(
                    target.width() / image.width(),
                    target.height() / image.height(),
                )
                width, height = image.width() * scale, image.height() * scale
                destination = target.adjusted(0, 0, 0, 0)
                destination.setX(target.x() + (target.width() - width) / 2)
                destination.setY(target.y() + (target.height() - height) / 2)
                destination.setWidth(width)
                destination.setHeight(height)
                painter.drawImage(destination, image)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "Could not print PDF", str(error))
            return
        finally:
            painter.end()
        self._flash_info(f"Sent {last_page - first_page + 1} page(s) to printer")

    def show_pdf_properties(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        path = Path(book.path).expanduser().resolve()
        try:
            stat = path.stat()
            metadata = book.metadata
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "Could not read PDF properties", str(error))
            return
        size_mb = stat.st_size / (1024 * 1024)
        details = [
            f"File: {path.name}",
            f"Location: {path.parent}",
            f"Size: {size_mb:.2f} MB",
            f"Pages: {book.page_count()}",
        ]
        for key, label in (
            ("title", "Title"), ("author", "Author"),
            ("subject", "Subject"), ("creator", "Creator"),
            ("producer", "Producer"), ("creationDate", "Created"),
            ("modDate", "Modified"),
        ):
            value = metadata.get(key)
            if value:
                details.append(f"{label}: {value}")
        QMessageBox.information(
            self, "PDF properties", "\n".join(details))

    def reload_pdf(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        if not self._confirm_pending_edits(book, "reloading"):
            return
        path = book.path
        zoom = book.zoom_percent
        page = book.current_page()
        splitter_sizes = book.splitter.sizes()
        thumbnail_zoom = book.thumbs.thumbnail_zoom
        bookmarks = sorted(book.bookmarked_pages)
        pinned = book.pinned
        try:
            book.open(path, zoom_percent=zoom, splitter_sizes=splitter_sizes)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "Could not reload PDF", str(error))
            return
        book.pinned = pinned
        book.thumbs.set_thumbnail_zoom(thumbnail_zoom)
        book.set_bookmarks(bookmarks)
        book.scroll_to_page(min(page, book.page_count() - 1))
        self._apply_pin_title(book)
        self._sync_file_actions()
        self._save_session_soon()
        self._flash_info(f"Reloaded {book.path}")

    def open_pdf_location(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        folder = Path(book.path).expanduser().resolve().parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(
                self, "Could not open location",
                f"Could not open the PDF folder:\n{folder}")

    def copy_pdf_path(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        QApplication.clipboard().setText(
            str(Path(book.path).expanduser().resolve()))

    def save_file(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc() or not book.is_modified:
            return
        if not book.can_save_in_place:
            self.save_file_as()
            return
        try:
            book.save()
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "Could not save document", str(error))
            return
        self._flash_info(f"Saved {book.path}")
        self._save_session_soon()

    def save_file_as(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        if book.has_unsaved_source_text:
            self._flash_info("Save the edited source text before exporting it as PDF.")
            return
        self._save_book_as_pdf(book)

    def _save_book_as_pdf(self, book: BookView) -> bool:
        default_path = (
            book.path if Path(book.path).suffix.lower() == ".pdf"
            else str(Path(book.path).with_suffix(".pdf"))
        )
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save as PDF", default_path, "PDF files (*.pdf)")
        if not output_path:
            return False
        if Path(output_path).suffix.lower() != ".pdf":
            output_path += ".pdf"
        try:
            book.save_as_pdf(output_path)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "Could not save PDF", str(error))
            return False
        self._apply_pin_title(book)
        self._record_recent_file(book.path)
        self.settings["last_pdf"] = book.path
        self.settings["last_dir"] = str(Path(book.path).parent)
        self._sync_file_actions()
        self._save_session_soon()
        self._flash_info(f"Saved PDF as {book.path}")
        return True

    def delete_pages(self, book: BookView, pages: list[int]) -> None:
        count = len(pages)
        label = "page" if count == 1 else "pages"
        answer = QMessageBox.question(
            self,
            "Delete PDF pages",
            f"Delete {count} selected {label}? This change can be saved with File > Save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted_count = book.delete_pages(pages)
        except (RuntimeError, ValueError) as error:
            QMessageBox.information(self, "Cannot delete pages", str(error))
            return
        self._flash_info(f"Deleted {deleted_count} page(s)")

    def _saved_book_page(self, path: str) -> int | None:
        positions = self.settings.get("book_positions", {})
        if isinstance(positions, dict):
            try:
                key = os.path.normcase(str(Path(path).resolve()))
                value = positions.get(key)
                if isinstance(value, dict):
                    value = value.get("page")
                if value is not None:
                    return max(0, int(value))
            except (OSError, TypeError, ValueError):
                logging.exception("Could not read remembered page for %s", path)
        try:
            same_last_book = (
                os.path.normcase(str(Path(path).resolve()))
                == os.path.normcase(str(Path(self.settings.get("last_pdf", "")).resolve()))
            )
            if same_last_book:
                return max(0, int(self.settings.get("last_page", 0)))
        except (OSError, TypeError, ValueError):
            pass
        return None

    def _saved_bookmarks(self, path: str) -> list[int] | None:
        positions = self.settings.get("book_positions", {})
        if not isinstance(positions, dict):
            return None
        try:
            key = os.path.normcase(str(Path(path).resolve()))
            saved = positions.get(key)
        except (OSError, TypeError, ValueError):
            logging.exception("Could not read remembered bookmarks for %s", path)
            return None
        if isinstance(saved, dict):
            bookmarks = saved.get("bookmarks")
            if isinstance(bookmarks, list):
                return bookmarks
        return None

    def _remember_book_page(self, book: BookView) -> None:
        if not book.path:
            return
        positions = self.settings.get("book_positions")
        if not isinstance(positions, dict):
            positions = {}
        try:
            key = os.path.normcase(str(Path(book.path).resolve()))
            page = (
                book.pending_page
                if book.pending_page is not None
                else book.current_page()
            )
            previous = positions.get(key)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry["page"] = max(0, int(page))
            entry["bookmarks"] = sorted(book.bookmarked_pages)
            positions[key] = entry
        except (OSError, RuntimeError, TypeError, ValueError):
            logging.exception("Could not remember current page for %s", book.path)
            return
        self.settings["book_positions"] = positions

    def _add_tab(self, path: str, zoom: float | None = None, page: int | None = None,
                 ocr: str | None = None, pinned: bool = False,
                 splitter_sizes: list | None = None,
                 activate: bool = True,
                 thumbnail_zoom: float | None = None,
                 bookmarks: list[int] | None = None) -> BookView | None:
        if zoom is None:
            zoom = self.parse_zoom_text(str(self.settings.get("last_zoom", "")))
        book = BookView(ocr or self.settings.get("ocr_langs", "auto"))
        book.pageSelected.connect(lambda p, r, b=book: self.on_selected(b, p, r))
        book.viewChanged.connect(self._on_book_view_changed)
        book.zoomChanged.connect(lambda value, b=book: self._remember_zoom(b, value))
        book.thumbs.translatePageRequested.connect(
            lambda page, b=book: self.translate_page(b, page))
        book.thumbs.extractPagesRequested.connect(
            lambda pages, b=book: self.extract_pages(b, pages))
        book.thumbs.deletePagesRequested.connect(
            lambda pages, b=book: self.delete_pages(b, pages))
        book.thumbs.selectedPagesChanged.connect(self._sync_file_actions)
        book.thumbs.copyPageImageRequested.connect(
            lambda page, b=book: self.copy_page_image(b, page))
        book.thumbs.refreshRequested.connect(self._refresh_application)
        book.thumbs.reopenRequested.connect(self._reopen_application)
        book.pageContextRequested.connect(
            lambda page, position, b=book:
                self._show_book_page_context_menu(b, page, position))
        book.reader.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_geometry_save())
        try:
            book.open(
                path,
                zoom_percent=zoom,
                splitter_sizes=splitter_sizes,
                defer_render=not activate or zoom is None,
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not open the file:\n{path}\n{e}")
            book.close_doc()
            book.deleteLater()
            return None
        book.pinned = pinned
        if thumbnail_zoom is not None:
            book.thumbs.set_thumbnail_zoom(float(thumbnail_zoom))
        if bookmarks is not None:
            book.set_bookmarks(bookmarks)
        else:
            saved_bookmarks = self._saved_bookmarks(path)
            if saved_bookmarks is not None:
                book.set_bookmarks(saved_bookmarks)
        if page is None:
            page = self._saved_book_page(path)
        if page is not None and page > 0:
            book.pending_page = min(page, max(0, book.page_count() - 1))
        idx = self.tabs.addTab(book, book.title)
        self.tabs.setTabToolTip(idx, path)
        self._apply_pin_title(book)
        book.set_thumbs_visible(self.thumbs_action.isChecked())
        if hasattr(book, "apply_theme"):
            try:
                book.apply_theme(theme_colors(
                    self.settings.get("theme", "Dark Slate"),
                    self.settings.get("color_scheme", "system"),
                ))
            except Exception:
                pass
        self._update_empty()
        if activate:
            self.tabs.setCurrentWidget(book)
        if zoom is None:
            QTimer.singleShot(0, lambda b=book: self._finish_initial_fit(b))
        elif activate:
            QTimer.singleShot(0, lambda b=book: self._center_opened_book(b))
        self.settings["last_dir"] = str(Path(path).parent)
        self.settings["last_pdf"] = path
        self._record_recent_file(book.path)
        return book

    def _book_tab_index(self, book: BookView) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is book:
                return i
        return -1

    def _apply_pin_title(self, book: BookView) -> None:
        idx = self._book_tab_index(book)
        if idx >= 0:
            title = ("📌 " if getattr(book, "pinned", False) else "")
            if book.is_modified:
                title += "* "
            title += book.title
            self.tabs.setTabText(idx, title)
            self.tabs.setTabToolTip(idx, book.path)

    def _close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if not isinstance(w, BookView):
            return
        if getattr(w, "pinned", False):
            self._flash_info("Tab is pinned 📌 — right-click the tab to unpin it first")
            return
        if self._confirm_pending_edits(w, "closing"):
            self._remove_book_tab(w)

    def _remove_book_tab(self, book: BookView) -> None:
        index = self._book_tab_index(book)
        if index < 0:
            return
        self._remember_book_page(book)
        book.close_doc()
        self.tabs.removeTab(index)
        book.deleteLater()
        self._update_empty()
        self._sync_book_ui()
        self._save_session_soon()

    def _close_books(self, books: list[BookView], action: str) -> None:
        targets = [
            book for book in books
            if self._book_tab_index(book) >= 0 and not book.pinned
        ]
        modified = [book for book in targets if book.is_modified]
        if modified:
            answer = QMessageBox.warning(
                self,
                "Unsaved document changes",
                f"Save changes to {len(modified)} modified document(s) before {action}?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Save:
                try:
                    for book in modified:
                        if not book.can_save_in_place:
                            if not self._save_book_as_pdf(book):
                                return
                            continue
                        book.save()
                except (OSError, RuntimeError, ValueError) as error:
                    QMessageBox.critical(
                        self, "Could not save document", str(error))
                    return
        for book in targets:
            self._remove_book_tab(book)

    def _tab_menu(self, pos) -> None:
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        book = self.tabs.widget(idx)
        if not isinstance(book, BookView):
            return
        pinned = getattr(book, "pinned", False)
        menu = QMenu(self)
        close_act = menu.addAction("✕ Close tab")
        others_act = menu.addAction("Close other tabs")
        unpinned_act = menu.addAction("Close unpinned tabs")
        menu.addSeparator()
        pin_act = menu.addAction(
            "📍 Unpin tab" if pinned else "📌 Pin tab")
        chosen = menu.exec(bar.mapToGlobal(pos))
        if chosen == pin_act:
            book.pinned = not pinned
            self._apply_pin_title(book)
            self._move_pinned_tabs_to_bottom()
            self._save_session_soon()
        elif chosen == close_act:
            self._close_tab(idx)
        elif chosen == others_act:
            self._close_books(
                [self.tabs.widget(i) for i in range(self.tabs.count())
                 if i != idx and isinstance(self.tabs.widget(i), BookView)],
                "closing other tabs",
            )
        elif chosen == unpinned_act:
            self._close_books(
                [self.tabs.widget(i) for i in range(self.tabs.count())
                 if isinstance(self.tabs.widget(i), BookView)],
                "closing unpinned tabs",
            )

    def _move_pinned_tabs_to_bottom(self) -> None:
        current = self.tabs.currentWidget()
        self.tabs.blockSignals(True)
        try:
            order = [self.tabs.widget(i) for i in range(self.tabs.count())]
            order.sort(key=lambda book: bool(getattr(book, "pinned", False)))
            for i, book in enumerate(order):
                index = self._book_tab_index(book)
                text = self.tabs.tabText(index)
                tip = self.tabs.tabToolTip(index)
                self.tabs.removeTab(index)
                self.tabs.insertTab(i, book, text)
                self.tabs.setTabToolTip(i, tip)
                self._apply_pin_title(book)
        finally:
            self.tabs.blockSignals(False)
        if current is not None:
            self.tabs.setCurrentWidget(current)
        self._sync_book_ui()

    def _show_book_page_context_menu(
        self, book: BookView, page_index: int, global_position,
    ) -> None:
        menu = self._book_page_context_menu(book, page_index)
        if menu is not None:
            menu.exec(global_position)

    def _book_page_context_menu(
        self, book: BookView, page_index: int,
    ) -> QMenu | None:
        if not book.has_doc() or not 0 <= page_index < book.page_count():
            return None
        menu = QMenu(self)
        translate_action = menu.addAction("Translate full page")
        translate_action.triggered.connect(
            lambda: self.translate_page(book, page_index))
        extract_action = menu.addAction("Extract page…")
        extract_action.triggered.connect(
            lambda: self.extract_pages(book, [page_index]))
        delete_action = menu.addAction("Delete page")
        delete_action.triggered.connect(
            lambda: self.delete_pages(book, [page_index]))
        copy_action = menu.addAction("Copy page image")
        copy_action.triggered.connect(
            lambda: self.copy_page_image(book, page_index))
        menu.addSeparator()
        bookmark_action = menu.addAction(
            "Remove bookmark" if page_index in book.bookmarked_pages
            else "Add bookmark")
        bookmark_action.triggered.connect(
            lambda: book.toggle_bookmark(page_index))
        rotate_left_action = menu.addAction("Rotate page left (view only)")
        rotate_left_action.triggered.connect(
            lambda: self._rotate_book_page(book, page_index, 90))
        rotate_right_action = menu.addAction("Rotate page right (view only)")
        rotate_right_action.triggered.connect(
            lambda: self._rotate_book_page(book, page_index, -90))
        copy_text_action = menu.addAction("Copy page text")
        copy_text_action.triggered.connect(
            lambda: self.copy_page_text(book, page_index))
        menu.addSeparator()
        refresh_action = menu.addAction("⟳ Refresh")
        refresh_action.setToolTip(
            "Reapply saved settings and refresh open pages")
        refresh_action.triggered.connect(self._refresh_application)
        reopen_action = menu.addAction("↻ ReOpen")
        reopen_action.setToolTip(
            "Restart the application to load updated code and settings")
        reopen_action.triggered.connect(self._reopen_application)
        return menu

    def _rotate_book_page(
        self, book: BookView, page_index: int, degrees: int,
    ) -> None:
        try:
            rotation = book.rotate_page(page_index, degrees)
        except (IndexError, RuntimeError, ValueError) as error:
            QMessageBox.critical(
                self, "Could not rotate page",
                f"Failed to rotate page {page_index + 1}:\n{error}")
            return
        direction = "left" if degrees > 0 else "right"
        self._flash_info(
            f"Page {page_index + 1} rotated {direction} for this view "
            f"({rotation}°)")

    def _show_app_context_menu(self, pos) -> None:
        menu = QMenu(self)
        refresh_action = menu.addAction("⟳  Refresh")
        refresh_action.setToolTip(
            "Reapply saved settings and refresh open pages")
        reopen_action = menu.addAction("↻  ReOpen")
        reopen_action.setToolTip(
            "Restart the application to load updated code and settings")
        selected_action = menu.exec(pos)
        if selected_action == refresh_action:
            self._refresh_application()
        elif selected_action == reopen_action:
            self._reopen_application()

    def _refresh_application(self) -> None:
        self._save_session_soon()
        self.settings = load()
        self._sync_language_combos()
        theme = self.settings.get("theme", "Dark Slate")
        if theme not in THEMES:
            theme = "Dark Slate"
            self.settings["theme"] = theme
        scheme = self.settings.get("color_scheme", "system")
        app = QApplication.instance()
        app.setStyleSheet(app_stylesheet(
            theme, scheme,
            bool(self.settings.get("pdf_canvas_theme_background", False))))
        self._sync_theme_button(theme)
        thumbs_visible = bool(self.settings.get("thumbs_visible", False))
        self.thumbs_action.blockSignals(True)
        self.thumbs_action.setChecked(thumbs_visible)
        self.thumbs_action.blockSignals(False)

        for book in self._iter_books():
            book.set_thumbs_visible(thumbs_visible)
            book.apply_theme(theme_colors(theme, scheme))
            book.refresh_content()
        for popup in self.popups:
            if popup.isVisible():
                popup.refresh_display(self.settings)
        self._refresh_engine_label()
        self._sync_book_ui()
        self._flash_info("Refreshed open pages and saved settings")

    def _reopen_application(self) -> None:
        self._restart_requested = True
        self._flash_info("Reopening to load the latest application code")
        QTimer.singleShot(0, self.close)

    def _launch_replacement_process(self) -> None:
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            interpreter = (
                VENV_PYTHONW
                if os.name == "nt" and VENV_PYTHONW.is_file()
                else Path(sys.executable)
            )
            command = [str(interpreter), str(APP_DIR / "main.py")]
        options = {"cwd": str(APP_DIR), "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            options["start_new_session"] = True
        subprocess.Popen(command, **options)

    def _on_tab_changed(self, _index: int) -> None:
        self._sync_file_actions()
        book = self.current_book()
        if book is None:
            self._sync_book_ui()
            self._save_session_soon()
            return
        if book.pending_scroll_position is not None or book.pending_page is not None:
            self._restore_pending_position(book)
        elif book.initial_fit_pending:
            QTimer.singleShot(0, lambda b=book: self._finish_initial_fit(b))
        else:
            self._sync_book_ui()
            self._save_session_soon()

    def _finish_initial_fit(self, book: BookView, tries: int = 0) -> None:
        if self._book_tab_index(book) < 0 or not book.has_doc():
            return
        if self.current_book() is not book:
            return
        if book.reader.viewport().width() <= 0:
            if tries < 30:
                QTimer.singleShot(
                    100, lambda: self._finish_initial_fit(book, tries + 1))
            else:
                book.refresh_visible()
            return
        if abs(book.zoom - 1.0) <= 0.01 and book.initial_fit_pending:
            book.fit_to_width()
        self._remember_zoom(book, book.zoom_percent)
        book.reader.center_page_horizontally()
        book.refresh_visible()
        self._sync_book_ui()
        self._save_session_soon()

    def _center_opened_book(self, book: BookView, tries: int = 0) -> None:
        if self._book_tab_index(book) < 0 or not book.has_doc():
            return
        if book.reader.viewport().width() <= 0:
            if tries < 30:
                QTimer.singleShot(
                    100, lambda b=book: self._center_opened_book(b, tries + 1))
            return
        book.reader.center_page_horizontally()

    def _remember_zoom(self, book: BookView, zoom_percent: float) -> None:
        if self._book_tab_index(book) < 0:
            return
        self.settings["last_zoom"] = zoom_percent
        self._save_session_soon()

    def _restore_pending_position(self, book: BookView, tries: int = 0) -> None:
        if self.current_book() is not book or not book.has_doc():
            return
        scrollbar = book.reader.verticalScrollBar()
        if (book.page_count() > 1 and scrollbar.maximum() == 0 and tries < 20):
            QTimer.singleShot(100, lambda: self._restore_pending_position(book, tries + 1))
            return
        if book.pending_scroll_position is not None:
            position = book.pending_scroll_position
            if (book.pending_page is not None
                    and book.pending_scroll_offset is not None
                    and book.pending_page < book.reader.page_count()):
                page_top = book.reader._labels[book.pending_page].y()
                position = page_top + book.pending_scroll_offset
            scrollbar.setValue(max(0, min(position, scrollbar.maximum())))
            horizontal = book.reader.horizontalScrollBar()
            horizontal.setValue(max(
                0, min(book.pending_horizontal_scroll_position or 0, horizontal.maximum())))
            book.pending_scroll_position = None
            book.pending_scroll_offset = None
            book.pending_horizontal_scroll_position = None
            book.pending_page = None
            self._sync_book_ui()
            self._save_session_soon()
        elif book.pending_page is not None:
            self._jump_to(book, book.pending_page)
        else:
            self._sync_book_ui()
            self._save_session_soon()

    def _jump_to(self, book: BookView, page: int, tries: int = 0) -> None:
        if self.tabs.widget(self.tabs.currentIndex()) is not book or not book.has_doc():
            return
        book.scroll_to_page(page)
        if book.current_page() != page and tries < 10:
            QTimer.singleShot(250, lambda: self._jump_to(book, page, tries + 1))
        else:
            if book.pending_page == page:
                book.pending_page = None
            self._sync_book_ui()
            self._save_session_soon()

    def _toggle_thumbs(self, on: bool) -> None:
        for book in self._iter_books():
            book.set_thumbs_visible(on)
        self._save_session_soon()

    @staticmethod
    def _language_index(code: str, default: str = "ar") -> int:
        codes = ["auto", *(language_code for _, language_code in LANGUAGE_CHOICES)]
        try:
            return codes.index(code)
        except ValueError:
            return codes.index(default)

    def _on_source_language_changed(self, index: int) -> None:
        code = self.source_combo.itemData(index) or "auto"
        self.settings["source_language"] = code
        save(self.settings)
        self._sync_popup_languages()

    def _sync_book_ui(self) -> None:
        self._sync_file_actions()
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        text = f"{int(book.zoom_percent)}%"
        self.zoom_combo.blockSignals(True)
        self.zoom_combo.setCurrentText(text)
        self.zoom_combo.blockSignals(False)

    def _on_target_language_changed(self, index: int) -> None:
        code = self.target_combo.itemData(index)
        if not code:
            return
        self.settings["target_language"] = code
        save(self.settings)
        self._sync_popup_languages()

    def _on_popup_languages_changed(
        self, source_language: str, target_language: str,
        origin: TranslatePopup,
    ) -> None:
        self.settings["source_language"] = source_language
        self.settings["target_language"] = target_language
        self._sync_language_combos()
        for popup in self.popups:
            if popup is not origin and popup.isVisible():
                popup.sync_languages(source_language, target_language)
        save(self.settings)

    def _sync_language_combos(self) -> None:
        self.source_combo.blockSignals(True)
        self.target_combo.blockSignals(True)
        self.source_combo.setCurrentIndex(
            self._language_index(
                self.settings.get("source_language", "auto"), default="auto"))
        self.target_combo.setCurrentIndex(
            self._language_index(self.settings.get("target_language", "ar")) - 1)
        self.source_combo.blockSignals(False)
        self.target_combo.blockSignals(False)

    def _sync_popup_languages(self) -> None:
        source = self.settings.get("source_language", "auto")
        target = self.settings.get("target_language", "ar")
        for popup in self.popups:
            if popup.isVisible():
                popup.sync_languages(source, target)

    def _on_book_view_changed(self) -> None:
        self._sync_book_ui()
        for book in self._iter_books():
            self._apply_pin_title(book)
        self._schedule_geometry_save()

    def _on_zoom_chosen(self) -> None:
        book = self.current_book()
        if book is None:
            return
        value = self.parse_zoom_text(self.zoom_combo.currentText())
        if value is None:
            self._sync_book_ui()
            return
        book.set_zoom_percent(value)
        self._remember_zoom(book, book.zoom_percent)
        self._sync_book_ui()
        self._save_session_soon()

    def prev_page(self):
        book = self.current_book()
        if book and book.has_doc():
            book.scroll_to_page(book.current_page() - 1)

    def next_page(self):
        book = self.current_book()
        if book and book.has_doc():
            book.scroll_to_page(book.current_page() + 1)

    def zoom_in(self):
        book = self.current_book()
        if book:
            book.zoom_step(+1)
            self._remember_zoom(book, book.zoom_percent)
            self._save_session_soon()

    def zoom_out(self):
        book = self.current_book()
        if book:
            book.zoom_step(-1)
            self._remember_zoom(book, book.zoom_percent)
            self._save_session_soon()

    def zoom_fit(self):
        book = self.current_book()
        if book:
            book.fit_to_width()
            self._remember_zoom(book, book.zoom_percent)
            self._save_session_soon()

    def zoom_actual_size(self) -> None:
        self._set_current_zoom(100.0)

    def zoom_fit_page(self) -> None:
        book = self.current_book()
        if book is not None:
            book.fit_to_page()
            self._remember_zoom(book, book.zoom_percent)
            self._save_session_soon()

    def zoom_fit_visible(self) -> None:
        book = self.current_book()
        if book is not None:
            book.fit_visible()
            self._remember_zoom(book, book.zoom_percent)
            self._save_session_soon()

    def zoom_to(self) -> None:
        book = self.current_book()
        if book is None or not book.has_doc():
            return
        value, accepted = QInputDialog.getInt(
            self, "Zoom To", "Zoom percentage:",
            int(round(book.zoom_percent)), 20, 1000, 10)
        if accepted:
            self._set_current_zoom(float(value))

    def _set_current_zoom(self, zoom_percent: float) -> None:
        book = self.current_book()
        if book is not None:
            book.set_zoom_percent(zoom_percent)
            self._remember_zoom(book, book.zoom_percent)
            self._sync_book_ui()
            self._save_session_soon()

    def _toggle_current_bookmark(self) -> None:
        book = self.current_book()
        if book is not None and book.has_doc():
            book.toggle_bookmark()
            self._save_session_soon()

    def on_selected(self, book: BookView, page_no: int, rect: QRect):
        book.last_selection = (page_no, rect)
        self._save_session_soon()
        self.translate_selection(book, page_no, rect)

    def _guide(self, msg: str) -> None:
        QMessageBox.information(self, "How to translate", msg)

    def translate_last(self):
        book = self.current_book()
        if book is None or not book.has_doc():
            self._guide(
                "Please open a book or document first.\n\n"
                "Click  📂  Open  (or the big button "
                "in the middle).")
            return
        if book.last_selection is not None:
            self.translate_selection(book, *book.last_selection)
        else:
            self._guide(
                "No selection yet.\n\n"
                "1. Scroll to any page of the book.\n"
                "2. Press and HOLD the LEFT mouse button on the page.\n"
                "3. Drag over a sentence — a rectangle appears.\n"
                "4. Release: the translation pops up above it.\n\n"
                "Works on scanned pages too (automatic OCR).\n"
                "You do NOT need to press the Translate button first — "
                "just drag on the page. Ctrl+T repeats the last selection."
            )

    def translate_selection(self, book: BookView, page_no: int, rect: QRect):
        self._translate_selection(book, page_no, rect, force_ocr=False)

    def _translate_selection(
        self, book: BookView, page_no: int, rect: QRect, force_ocr: bool,
        popup: TranslatePopup | None = None,
        omit_running_header: bool = False,
    ) -> None:
        if book is None or not book.has_doc():
            return

        def show_translation(
            text: str | None = None,
            pil_image=None,
            ocr_langs=("ru", "en"),
        ) -> None:
            if popup is None:
                self._show_popup(
                    text=text,
                    pil_image=pil_image,
                    ocr_langs=ocr_langs,
                    book=book,
                    page_no=page_no,
                    full_page_translation=omit_running_header,
                )
                return
            popup.set_page_navigation(page_no, book.page_count())
            popup.set_source_content(
                text=text, pil_image=pil_image, ocr_langs=ocr_langs)

        if omit_running_header and not force_ocr:
            body_text, page_number = book.page_body_text(page_no)
            if body_text:
                if page_number:
                    body_text = (
                        f"{body_text}\n\n<small>{page_number}</small>")
                show_translation(text=body_text)
                self._flash_info(
                    f"Translating page text into "
                    f"{self.target_combo.currentText()}…")
                return
        if self.settings.get("selection_mode", "ocr") == "image":
            if force_ocr:
                image = book.render_page_image(page_no, dpi=300)
            else:
                image = book.render_crop(page_no, rect, dpi=300)
            pil_image = pdf_utils.qimage_to_pil(image)
            show_translation(pil_image=pil_image, ocr_langs=())
            self._flash_info(
                f"Sending selected image to the selected model for translation into "
                f"{self.target_combo.currentText()}…")
            return
        kind, payload = book.extract(page_no, rect)
        if kind == "text" and not force_ocr:
            show_translation(text=payload)
            self._flash_info(
                f"Translating {len(payload)} characters into "
                f"{self.target_combo.currentText()}…")
            return
        if not ocr.is_available():
            self._guide(
                "This page is a scanned image (no text layer).\n\n"
                "Scanned-page OCR requires OneOCR from the Windows 11 "
                "Snipping Tool.\n\n"
                "Install or update Snipping Tool from the Microsoft Store, "
                "then select the page again."
            )
            return
        if force_ocr:
            image = book.render_page_image(page_no, dpi=300)
        else:
            image = book.render_crop(page_no, rect, dpi=300)
        pil_image = ocr.prepare_image(pdf_utils.qimage_to_pil(image))
        source_language = self.settings.get("source_language", "auto")
        ocr_langs = self._ocr_languages_for(source_language)
        show_translation(
            pil_image=pil_image,
            ocr_langs=ocr_langs,
        )
        self._flash_info(
            f"Reading scanned text (OCR), then translating into "
            f"{self.target_combo.currentText()}…")

    def translate_page(self, book: BookView, page_no: int) -> None:
        if not book.has_doc() or not 0 <= page_no < book.page_count():
            return
        self._translate_selection(
            book,
            page_no,
            book.page_rect_on_screen(page_no),
            force_ocr=book.page_needs_full_page_ocr(page_no),
            omit_running_header=True,
        )

    def _set_automatic_page_reading(
        self,
        book: BookView,
        popup: TranslatePopup,
        enabled: bool,
    ) -> None:
        if not enabled:
            if popup is self._automatic_page_reading_popup:
                self._stop_automatic_page_reading()
            return
        page_no = popup.navigation_page
        if (
            not book.has_doc()
            or page_no is None
            or not 0 <= page_no < book.page_count()
        ):
            popup.set_automatic_page_reading(False)
            self._flash_info("Automatic page reading needs a PDF page.")
            return
        if self._automatic_page_reading_popup is not None:
            self._stop_automatic_page_reading()
        self._automatic_page_reading_book = book
        self._automatic_page_reading_popup = popup
        self._automatic_page_reading_page = page_no
        self._automatic_page_waiting_for_speech = False
        self._automatic_page_reading_at_end = False
        self._translate_automatic_page(book, popup, page_no)

    def _translate_automatic_page(
        self,
        book: BookView,
        popup: TranslatePopup,
        page_no: int,
    ) -> None:
        if (
            popup is not self._automatic_page_reading_popup
            or book is not self._automatic_page_reading_book
            or not book.has_doc()
            or not 0 <= page_no < book.page_count()
        ):
            self._stop_automatic_page_reading()
            return
        if (
            self.settings.get("selection_mode", "ocr") != "image"
            and book.page_needs_full_page_ocr(page_no)
            and not ocr.is_available()
        ):
            self._guide(
                "Automatic page reading stopped: scanned-page OCR requires "
                "OneOCR from the Windows 11 Snipping Tool.")
            self._stop_automatic_page_reading()
            return

        self._automatic_page_reading_page = page_no
        self._automatic_page_waiting_for_speech = False
        tab_index = self.tabs.indexOf(book)
        if tab_index >= 0:
            self.tabs.setCurrentIndex(tab_index)
        book.scroll_to_page(page_no)
        book.last_selection = None
        self._save_session_soon()
        popup.set_page_navigation(page_no, book.page_count())
        self._translate_selection(
            book,
            page_no,
            book.page_rect_on_screen(page_no),
            force_ocr=book.page_needs_full_page_ocr(page_no),
            popup=popup,
            omit_running_header=True,
        )

    def _on_automatic_page_translation_ready(
        self, popup: TranslatePopup
    ) -> None:
        if (
            popup is not self._automatic_page_reading_popup
            or self._automatic_page_reading_book is None
            or self._automatic_page_reading_at_end
            or popup.navigation_page != self._automatic_page_reading_page
        ):
            return
        if not popup._translation_text.strip():
            self._stop_automatic_page_reading()
            self._flash_info("Automatic page reading stopped: page is empty.")
            return
        if not popup._speech_available:
            self._stop_automatic_page_reading()
            self._flash_info(
                "Automatic page reading stopped: the selected speech engine "
                "is not available.")
            return
        self._automatic_page_waiting_for_speech = True
        popup._toggle_speech()

    def _on_automatic_page_speech_finished(
        self, popup: TranslatePopup, error: str
    ) -> None:
        if (
            popup is not self._automatic_page_reading_popup
            or not self._automatic_page_waiting_for_speech
        ):
            return
        self._automatic_page_waiting_for_speech = False
        if error:
            page_no = self._automatic_page_reading_page
            self._stop_automatic_page_reading()
            self._flash_info(
                f"Automatic page reading stopped on page "
                f"{(page_no or 0) + 1}: {error}")
            return
        book = self._automatic_page_reading_book
        page_no = self._automatic_page_reading_page
        if book is None or page_no is None:
            self._stop_automatic_page_reading()
            return
        next_page = page_no + 1
        if next_page >= book.page_count():
            self._automatic_page_reading_at_end = True
            self._flash_info(
                "Automatic page reading reached the end of the book. "
                "Click the page counter to turn it off.")
            return
        QTimer.singleShot(
            0,
            lambda: self._translate_automatic_page(book, popup, next_page),
        )

    def _on_automatic_page_translation_failed(
        self, popup: TranslatePopup, error: str
    ) -> None:
        if popup is self._automatic_page_reading_popup:
            self._stop_automatic_page_reading()
            self._flash_info(f"Automatic page reading stopped: {error}")

    def _stop_automatic_page_reading(self) -> None:
        popup = self._automatic_page_reading_popup
        self._automatic_page_reading_book = None
        self._automatic_page_reading_popup = None
        self._automatic_page_reading_page = None
        self._automatic_page_waiting_for_speech = False
        self._automatic_page_reading_at_end = False
        if popup is not None:
            popup.set_automatic_page_reading(False)
            if popup._speech_active:
                popup._toggle_speech()

    def _navigate_translation_page(
        self, book: BookView, popup: TranslatePopup, direction: int,
    ) -> None:
        page_no = popup.navigation_page
        if (
            page_no is None
            or direction not in (-1, 1)
            or not book.has_doc()
        ):
            return
        page_no += direction
        if not 0 <= page_no < book.page_count():
            return
        if (
            popup is self._automatic_page_reading_popup
            and book is self._automatic_page_reading_book
        ):
            self._automatic_page_reading_at_end = False
            self._translate_automatic_page(book, popup, page_no)
            return
        tab_index = self.tabs.indexOf(book)
        if tab_index >= 0:
            self.tabs.setCurrentIndex(tab_index)
        book.scroll_to_page(page_no)
        book.last_selection = None
        self._save_session_soon()
        self._translate_selection(
            book,
            page_no,
            book.page_rect_on_screen(page_no),
            force_ocr=book.page_needs_full_page_ocr(page_no),
            popup=popup,
        )

    def extract_pages(self, book: BookView, page_indices: list[int]) -> None:
        pages = sorted(set(page_indices))
        if not book.has_doc() or len(pages) < 2:
            return
        first, last = pages[0] + 1, pages[-1] + 1
        default_path = str(
            Path(book.path).with_name(
                f"{Path(book.path).stem}_pages_{first}-{last}.pdf"))
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save extracted pages",
            default_path,
            "PDF files (*.pdf)",
        )
        if not output_path:
            return
        try:
            book.save_pages_as_pdf(pages, output_path)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.critical(
                self, "Could not extract pages",
                f"Failed to save the selected pages:\n{error}")
            return
        self._flash_info(f"Saved {len(pages)} pages to {output_path}")

    def copy_page_image(self, book: BookView, page_no: int) -> None:
        if not book.has_doc() or not 0 <= page_no < book.page_count():
            return
        try:
            image = book.render_page_image(page_no)
            if image.isNull():
                raise RuntimeError("The PDF page rendered as an empty image")
            QApplication.clipboard().setImage(image)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(
                self, "Could not copy page image",
                f"Failed to copy the page image to the clipboard:\n{error}")
            return
        self._flash_info(
            f"Page {page_no + 1} image copied — paste it with Ctrl+V")

    def copy_page_text(self, book: BookView, page_no: int) -> None:
        if not book.has_doc() or not 0 <= page_no < book.page_count():
            return
        try:
            text = book.page_text(page_no)
            if not text:
                self._flash_info(
                    f"No selectable text on page {page_no + 1}; "
                    "use Translate full page for scanned pages")
                return
            QApplication.clipboard().setText(text)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(
                self, "Could not copy page text",
                f"Failed to copy text from page {page_no + 1}:\n{error}")
            return
        self._flash_info(
            f"Page {page_no + 1} text copied — paste it with Ctrl+V")

    @staticmethod
    def _ocr_languages_for(source_language: str) -> tuple[str, ...] | None:
        if source_language == "auto":
            return None
        language_map = {
            "ar": ("ar",),
            "fr": ("fr", "en"),
            "en": ("en",),
            "ru": ("ru", "en"),
            "it": ("it", "en"),
            "es": ("es", "en"),
            "de": ("de", "en"),
        }
        try:
            return language_map[source_language]
        except KeyError:
            raise ValueError(f"Unsupported OCR source language: {source_language}")

    def _show_popup(self, text: str | None = None, pil_image=None,
                    ocr_langs=("ru", "en"), book: BookView | None = None,
                    page_no: int | None = None,
                    full_page_translation: bool = False) -> TranslatePopup:
        self.popups = [p for p in self.popups if p.isVisible()]
        for p in self.popups:
            if not getattr(p, "_pinned", False):
                p.close()
        popup = TranslatePopup(
            dict(self.settings), source_text=text, pil_image=pil_image,
            ocr_langs=ocr_langs, parent=self,
            floating_language_controls=True,
            language_change_callback=self._on_popup_languages_changed,
            full_page_translation=full_page_translation,
        )
        popup.automaticPageReadingToggled.connect(
            lambda enabled, source_book=book, source_popup=popup:
                self._set_automatic_page_reading(
                    source_book, source_popup, enabled)
                if source_book is not None else None)
        popup.translationReady.connect(
            lambda source_popup=popup:
                self._on_automatic_page_translation_ready(source_popup))
        popup.translationFailed.connect(
            lambda error, source_popup=popup:
                self._on_automatic_page_translation_failed(
                    source_popup, error))
        popup.speechFinished.connect(
            lambda error, source_popup=popup:
                self._on_automatic_page_speech_finished(
                    source_popup, error))
        if book is not None and page_no is not None:
            popup.set_page_navigation(page_no, book.page_count())
            popup.pageNavigationRequested.connect(
                lambda direction, source_book=book, source_popup=popup:
                    self._navigate_translation_page(
                        source_book, source_popup, direction))
        popup.usageReported.connect(self._show_model_usage)
        popup.show()
        self.popups.append(popup)
        return popup

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.values()
            self._sync_language_combos()
            save(self.settings)
            QApplication.instance().setStyleSheet(app_stylesheet(
                self.settings.get("theme", "Dark Slate"),
                self.settings.get("color_scheme", "system"),
                bool(self.settings.get("pdf_canvas_theme_background", False)),
            ))
            self._apply_theme_to_books(self.settings.get("theme", "Dark Slate"))
            for popup in self.popups:
                if popup.isVisible():
                    popup.refresh_display(self.settings)
            self._sync_theme_button(self.settings.get("theme", "Dark Slate"))
            self._sync_selection_mode_button()
            self._refresh_engine_label()
            self._flash_info("Settings saved ✓")

    def _on_theme_changed(self, theme: str) -> None:
        if theme not in THEMES:
            return
        self.settings["theme"] = theme
        if theme == "Pure Light":
            self.settings["color_scheme"] = "light"
        self._sync_theme_button(theme)
        QApplication.instance().setStyleSheet(app_stylesheet(
            theme,
            self.settings.get("color_scheme", "system"),
            bool(self.settings.get("pdf_canvas_theme_background", False)),
        ))
        self._apply_theme_to_books(theme)
        for popup in self.popups:
            if popup.isVisible():
                popup.refresh_display(self.settings)
        save(self.settings)

    def _on_system_color_scheme_changed(self, _scheme=None) -> None:
        if self.settings.get("color_scheme", "system") != "system":
            return
        theme = self.settings.get("theme", "Dark Slate")
        QApplication.instance().setStyleSheet(app_stylesheet(
            theme, "system",
            bool(self.settings.get("pdf_canvas_theme_background", False))))
        self._apply_theme_to_books(theme)
        for popup in self.popups:
            if popup.isVisible():
                popup.refresh_display(self.settings)

    def _show_model_usage(self, model_label: str, token_line: str) -> None:
        """Put the answering model and its running cost in the toolbar corner.

        The popup shows this only while it is open and over the page; the
        corner keeps it in view afterwards. The number is that model's running
        total for the whole session, not the last selection — the popup already
        shows the per-request figure in its tooltip, and a reader working
        through a book wants the sum.
        """
        label = (model_label or "").strip()
        if not label:
            try:
                label = resolve_model(self.settings)
            except Exception:
                label = ""
        if not label:
            self.model_readout.setText("")
            return
        # Same normalisation the popup badge uses: drop the mark and the word
        # before the engine name, and the "(provider: model)" tail, so a long
        # gateway id does not take the whole corner.
        for pattern in (r"\bchosen image:\s*", r"\bchosen:\s*",
                        r"\bcustom:\s*"):
            label = re.sub(pattern, "", label)
        label = re.sub(r"[✦◈⚡☁🌀🔧✠⚑]\s*", "", label).strip(" ·-")
        label = label.split("(")[0].strip() or label
        if len(label) > 30:
            label = label[:29] + "…"

        total, prompt, completion = self._model_running_total()
        second = (
            f"{total:,} tokens  "
            f"({_abbrev(prompt)} in / {_abbrev(completion)} out)"
        )
        self.model_readout.setText(
            f"{label}\n{second}" if second else label)

        tip = f"{label} — answered the last selection."
        tip += f"\nRunning total: {total:,} tokens"
        if token_line:
            tip += f"\nLast selection: {token_line}"
        self.model_readout.setToolTip(tip)

    def _sync_model_readout_width(self) -> None:
        """Use the remaining toolbar width so the readout stays centered."""
        if not hasattr(self, "model_readout"):
            return
        toolbar = self._toolbar
        left = self.model_readout.mapTo(toolbar, QPoint(0, 0)).x()
        available = toolbar.contentsRect().right() - left + 1
        self.model_readout.setFixedWidth(max(160, available))

    def _model_running_total(self) -> tuple[int, int, int]:
        """(total, prompt, completion) this session has spent on that model."""
        try:
            from translators import usage
            from translators.registry import _find_custom_provider

            chosen = resolve_model(self.settings)
            entry = _find_custom_provider(self.settings, chosen) or {}
            # The ledger files counts under the bare model id the gateway was
            # called with, not the `custom:<id>` alias the settings store.
            model = str(entry.get("model") or "").strip() or chosen
            row = usage.accumulated(model)
            total = int(row.get("total") or 0)
            return (total, int(row.get("in") or 0), int(row.get("out") or 0))
        except Exception:
            return (0, 0, 0)

    def _sync_theme_button(self, theme: str) -> None:
        for name, action in self._theme_actions.items():
            action.setChecked(name == theme)

    def _sync_selection_mode_button(self) -> None:
        mode = self.settings.get("selection_mode", "ocr")
        if mode == "image":
            self.selection_mode_button.setText("Image")
            self.selection_mode_button.setToolTip(
                "Selection mode: Image. Click to switch to OCR.")
        else:
            self.selection_mode_button.setText("OCR")
            self.selection_mode_button.setToolTip(
                "Selection mode: OCR. Click to switch to Image.")

    def _toggle_selection_mode(self, _checked: bool = False) -> None:
        self.settings["selection_mode"] = (
            "image" if self.settings.get("selection_mode", "ocr") == "ocr"
            else "ocr"
        )
        save(self.settings)
        self._sync_selection_mode_button()
        mode_name = "Image" if self.settings["selection_mode"] == "image" else "OCR"
        self._flash_info(f"Selection mode: {mode_name}")

    def _apply_theme_to_books(self, theme: str) -> None:
        """Push the active theme colors to every open BookView's thumbnails
        panel. Requires `BookView.apply_theme(colors: dict)` to exist in
        reader/bookview.py (it should restyle the thumbs panel/background,
        thumbnail cards, scrollbars, etc. using the same keys as THEMES)."""
        colors = theme_colors(
            theme, self.settings.get("color_scheme", "system"))
        for book in self._iter_books():
            if hasattr(book, "apply_theme"):
                try:
                    book.apply_theme(colors)
                except Exception:
                    pass

    def _reload_settings(self):
        try:
            cur = load()
            if cur != self.settings:
                old = self.settings
                self.settings = cur
                self._sync_language_combos()
                self._sync_selection_mode_button()
                self._refresh_engine_label()
                theme_changed = (
                    cur.get("theme") != old.get("theme")
                    or cur.get("color_scheme") != old.get("color_scheme")
                    or cur.get("pdf_canvas_theme_background")
                    != old.get("pdf_canvas_theme_background")
                )
                if theme_changed:
                    theme = cur.get("theme", "Dark Slate")
                    if theme not in THEMES:
                        theme = "Dark Slate"
                        self.settings["theme"] = theme
                    scheme = cur.get("color_scheme", "system")
                    QApplication.instance().setStyleSheet(app_stylesheet(
                        theme, scheme,
                        bool(cur.get("pdf_canvas_theme_background", False)),
                    ))
                    self._sync_theme_button(theme)
                    self._apply_theme_to_books(theme)
                if (cur.get("font_size") != old.get("font_size")
                        or cur.get("translation_font")
                        != old.get("translation_font")
                        or cur.get("translation_font_arabic")
                        != old.get("translation_font_arabic")
                        or cur.get("translation_font_latin")
                        != old.get("translation_font_latin")
                        or cur.get("arabic_tashkeel")
                        != old.get("arabic_tashkeel")
                        or cur.get("line_spacing_px") != old.get("line_spacing_px")
                        or cur.get("translation_side_margin_px")
                        != old.get("translation_side_margin_px")
                        or cur.get("translation_top_bottom_margin_px")
                        != old.get("translation_top_bottom_margin_px")
                        or theme_changed
                        or cur.get("explain_mode") != old.get("explain_mode")
                        or cur.get("explain_instructions")
                        != old.get("explain_instructions")):
                    for popup in self.popups:
                        if popup.isVisible():
                            popup.refresh_display(cur)
        except Exception:
            pass

    def _refresh_engine_label(self):
        model = resolve_model(self.settings)
        # Custom providers: show friendly label like "OpenRouter (gpt-4o)"
        # instead of raw "custom:custom-abcd".
        if model.startswith("custom:"):
            try:
                from translators.registry import custom_provider_label
                short = custom_provider_label(model, self.settings)
            except Exception:
                short = model
        else:
            short = model.replace("-free", "")
        if len(short) > 28:
            short = short[:27] + "…"
        if self._ocr_ok is None:
            ocr_state = "OCR …"
        else:
            ocr_state = f"{ocr.backend_name()} ✓" if self._ocr_ok else "OCR ✗"
        self.engine_label.setText(f"  Model: {short}   ·   {ocr_state}  ")

    def _apply_startup_geometry(self) -> None:
        geometry = self.settings.get("window_geometry")
        if geometry:
            try:
                encoded = QByteArray.fromBase64(str(geometry).encode("ascii"))
                if self.restoreGeometry(encoded):
                    return
            except (TypeError, ValueError):
                pass
        self.resize(MAIN_W, MAIN_H)
        try:
            scr = (self.screen().availableGeometry()
                   if self.screen() else QApplication.primaryScreen().availableGeometry())
            self.move(scr.x() + max(0, (scr.width() - MAIN_W) // 2),
                      scr.y() + max(0, (scr.height() - MAIN_H) // 2))
        except (AttributeError, RuntimeError):
            pass

    def _warm_ocr(self) -> None:
        import threading
        def job():
            try:
                self.ocrWarmed.emit(ocr.is_available())
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start()

    def _on_ocr_warmed(self, ok: bool) -> None:
        self._ocr_ok = ok
        self._refresh_engine_label()

    def _session_tabs(self) -> list[dict]:
        tabs = []
        for book in self._iter_books():
            if not book.path:
                continue
            try:
                st = book.session_state()
            except Exception:
                logging.exception("Could not capture full session state for %s", book.path)
                try:
                    st = {
                        "path": book.path,
                        "page": book.pending_page if book.pending_page is not None else book.current_page(),
                        "zoom": book.zoom_percent,
                        "ocr": book.ocr_langs,
                        "scroll_position": book.reader.verticalScrollBar().value(),
                        "scroll_offset": 0,
                        "horizontal_scroll_position": book.reader.horizontalScrollBar().value(),
                    }
                except Exception:
                    logging.exception("Could not capture fallback session state for %s", book.path)
                    st = {"path": book.path, "page": 0, "zoom": book.zoom_percent,
                          "ocr": book.ocr_langs}
            if book.pending_page is not None:
                st["page"] = book.pending_page
            if book.pending_scroll_position is not None:
                st["scroll_position"] = book.pending_scroll_position
            if book.pending_scroll_offset is not None:
                st["scroll_offset"] = book.pending_scroll_offset
            st["pinned"] = bool(getattr(book, "pinned", False))
            st["splitter_sizes"] = book.splitter.sizes()
            st["thumbnail_zoom"] = book.thumbs.thumbnail_zoom
            st["bookmarks"] = sorted(book.bookmarked_pages)
            tabs.append(st)
        return tabs

    def _save_session_soon(self) -> None:
        if self._restoring_session:
            return
        if self._closing and self._close_snapshot_saved:
            return
        try:
            for book in self._iter_books():
                self._remember_book_page(book)
            self.settings["open_tabs"] = self._session_tabs()
            self.settings["active_tab"] = self.tabs.currentIndex()
            self.settings["thumbs_visible"] = self.thumbs_action.isChecked()
            self.settings["window_geometry"] = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            book = self.current_book()
            if book is not None and book.path:
                self.settings["last_pdf"] = book.path
                self.settings["last_dir"] = str(Path(book.path).parent)
            save(self.settings)
            if self._closing:
                self._close_snapshot_saved = True
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save the application session")

    def _restore_session(self) -> None:
        if getattr(self, "_restored", False) or self.tabs.count() > 0:
            return
        self._restored = True
        self._restoring_session = True
        tabs = self.settings.get("open_tabs") or []
        if not isinstance(tabs, list):
            logging.error("Saved open_tabs setting is not a list; ignoring it")
            tabs = []
        if tabs:
            for st in tabs:
                if not isinstance(st, dict):
                    logging.error("Ignoring malformed saved book state: %r", st)
                    continue
                try:
                    path = st.get("path") or ""
                    if path and Path(path).is_file():
                        book = self._add_tab(
                            path,
                            zoom=float(st.get("zoom") or 100.0),
                            ocr=st.get("ocr") or self.settings.get("ocr_langs", "ru,en"),
                            pinned=bool(st.get("pinned", False)),
                            splitter_sizes=st.get("splitter_sizes"),
                            thumbnail_zoom=st.get("thumbnail_zoom"),
                            bookmarks=st.get("bookmarks"),
                            activate=False
                        )
                        if book is not None:
                            if "page" in st:
                                book.pending_page = max(0, int(st.get("page") or 0))
                            scroll_position = st.get("scroll_position")
                            if scroll_position is not None:
                                book.pending_scroll_position = max(0, int(scroll_position))
                            scroll_offset = st.get("scroll_offset")
                            if scroll_offset is not None:
                                book.pending_scroll_offset = max(0, int(scroll_offset))
                            horizontal_position = st.get("horizontal_scroll_position")
                            if horizontal_position is not None:
                                book.pending_horizontal_scroll_position = max(
                                    0, int(horizontal_position))
                            selection = st.get("last_selection")
                            restored = book._selection_from_state(selection)
                            if restored is not None:
                                book.last_selection = restored
                    elif path:
                        logging.error("Saved book is missing and cannot be restored: %s", path)
                except Exception:
                    logging.exception("Could not restore saved book: %r", st.get("path"))
            self._restoring_session = False
            try:
                active = int(self.settings.get("active_tab", 0))
            except (TypeError, ValueError):
                active = 0
            if self.tabs.count():
                self.tabs.setCurrentIndex(max(0, min(active, self.tabs.count() - 1)))
                book = self.current_book()
                if book is not None:
                    if (book.pending_scroll_position is not None
                            or book.pending_page is not None):
                        self._restore_pending_position(book)
                    else:
                        self._save_session_soon()
                    self._flash_info(f"Welcome back — {book.title}")
                self._save_session_soon()
            return

        self._restoring_session = False
        path = self.settings.get("last_pdf") or ""
        if path and Path(path).is_file():
            book = self._add_tab(path, activate=True)
            if book is not None:
                try:
                    page = int(self.settings.get("last_page", 0))
                except (TypeError, ValueError):
                    page = 0
                if page > 0:
                    book.pending_page = page
                    self._restore_pending_position(book)
                else:
                    self._save_session_soon()
                self._flash_info(f"Welcome back — {book.title}, page {page + 1}")

    def _flash_info(self, msg: str, ms: int = 3500) -> None:
        self._flash_token += 1
        tok = self._flash_token
        self.setWindowTitle(f"Capio — {msg}")

        def _restore():
            if tok == self._flash_token:
                self.setWindowTitle("Capio")

        QTimer.singleShot(ms, _restore)

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        self._schedule_geometry_save()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if hasattr(self, "_model_readout_resize_timer"):
            self._model_readout_resize_timer.start()
        self._schedule_geometry_save()

    def _schedule_geometry_save(self) -> None:
        if not self._closing and not self._geo_timer.isActive():
            self._geo_timer.start()

    def _save_session_live(self) -> None:
        if self._closing:
            return
        book = self.current_book()
        if book is not None and book.path:
            self.settings["last_page"] = book.current_page()
        self._save_session_soon()

    def closeEvent(self, e):
        if not self._shutdown_confirmed:
            books = [
                book for book in self._iter_books()
                if book.is_modified
            ]
            if books:
                answer = QMessageBox.warning(
                    self,
                    "Unsaved document changes",
                    f"Save changes to {len(books)} modified document(s) before exiting?",
                    QMessageBox.StandardButton.Save
                    | QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Save,
                )
                if answer == QMessageBox.StandardButton.Cancel:
                    self._restart_requested = False
                    e.ignore()
                    return
                if answer == QMessageBox.StandardButton.Save:
                    try:
                        for book in books:
                            if not book.can_save_in_place:
                                if not self._save_book_as_pdf(book):
                                    e.ignore()
                                    return
                                continue
                            book.save()
                    except (OSError, RuntimeError, ValueError) as error:
                        QMessageBox.critical(
                            self, "Could not save document", str(error))
                        e.ignore()
                        return
            self._shutdown_confirmed = True

        if self._shutdown_waiting:
            if any(
                popup.isVisible() or getattr(popup, "_close_pending", False)
                for popup in self.popups
            ):
                e.ignore()
                QTimer.singleShot(100, self.close)
                return
            self._shutdown_waiting = False
        else:
            for popup in list(self.popups):
                popup.close()
            if any(
                popup.isVisible() or getattr(popup, "_close_pending", False)
                for popup in self.popups
            ):
                self._shutdown_waiting = True
                e.ignore()
                QTimer.singleShot(100, self.close)
                return

        if self._restart_requested:
            self._save_session_soon()
            try:
                self._launch_replacement_process()
            except OSError as error:
                logging.exception("Could not restart Capio")
                self._restart_requested = False
                self._shutdown_confirmed = False
                QMessageBox.critical(
                    self,
                    "Could not restart",
                    f"The application is still open, but could not start a "
                    f"replacement process:\n{error}",
                )
                e.ignore()
                return

        self._closing = True
        self._geo_timer.stop()
        self._settings_poll.stop()
        app = QApplication.instance()
        app.removeEventFilter(self._thumbs_wheel_filter)
        if app.styleSheet() == app_stylesheet(
                self.settings.get("theme", "Dark Slate"),
                self.settings.get("color_scheme", "system"),
                bool(self.settings.get("pdf_canvas_theme_background", False))):
            app.setStyleSheet(self._previous_app_stylesheet)
        self._save_session_soon()
        try:
            for book in list(self._iter_books()):
                try:
                    book.close_doc()
                except RuntimeError:
                    pass
        except RuntimeError:
            pass
        super().closeEvent(e)

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
        self.open_paths(paths)
        event.acceptProposedAction()


def _speech_code_fingerprint() -> str:
    """Prove which speech/highlight code the running app loaded.

    Code fixes only take effect after a restart; without this, a test run on
    a stale process looks exactly like "the fix failed".
    """
    from datetime import datetime, timezone

    parts = []
    root = Path(__file__).resolve().parent
    for name in ("popup/translate_popup.py", "popup/speech_worker.py"):
        path = root / name
        try:
            stamp = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).strftime("%H:%M:%S")
            parts.append(f"{path.name}@{stamp}")
        except OSError:
            parts.append(f"{path.name}@missing")
    return " ".join(parts)


def main():
    log = setup_logging()
    try:
        # A native crash (Qt/FFmpeg) leaves no traceback; faulthandler dumps
        # the Python stack to the log instead of silence.
        import faulthandler
        global _fault_log_file
        _fault_log_file = open(log_path(), "ab")
        faulthandler.enable(file=_fault_log_file)
    except Exception:
        log.exception("Could not enable faulthandler")
    from popup.speech_worker import _prune_cache
    _prune_cache()
    from translators import usage
    usage.start_session()
    app = QApplication(sys.argv)
    app.setApplicationName("Capio")
    app.setStyleSheet(APP_STYLE)
    try:
        win = MainWindow()
    except Exception:
        # Without this a packaged build exits with no visible reason at all.
        log.exception("MainWindow failed to start")
        raise
    documents = [
        argument for argument in sys.argv[1:]
        if Path(argument).suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if documents:
        win.open_paths([p for p in documents if Path(p).exists()])
    win.show()
    win.raise_()
    win.activateWindow()
    log.info("Capio started (frozen=%s)", getattr(sys, "frozen", False))
    log.info("Speech code fingerprint: %s", _speech_code_fingerprint())
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
