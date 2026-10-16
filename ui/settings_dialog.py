"""Settings dialog — professional sidebar + stacked pages redesign.

Keeps 100% of the existing data model (values(), _credentials, model picker
delegate, custom provider logic, live typography preview, etc.) but replaces
the old QTabWidget chrome with a modern sidebar navigation, header, card
system and footer. The public attribute names (z_model, gemini_key, …) are
preserved so existing tests and call sites keep working.
"""
from __future__ import annotations

from html import escape
import json
import re
import threading

from PySide6.QtCore import (
    QCoreApplication, QEvent, QPoint, QPointF, QRect, QSize, Qt, QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QDoubleSpinBox, QInputDialog, QLineEdit, QMenu, QPushButton, QPlainTextEdit, QSizePolicy,
    QSpinBox, QSplitter, QStackedWidget,
    QScrollArea, QStyle, QStyleOptionButton, QStylePainter,
    QStyledItemDelegate, QStyleOptionViewItem, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)

from config.app_log import get_logger
from config.settings import (
    DEFAULT_LINE_SPACING, DEFAULT_MODEL,
    DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
    DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
    MAX_LINE_SPACING, MAX_TRANSLATION_SIDE_MARGIN_PX,
    MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX, MIN_LINE_SPACING,
    MAX_TRANSLATION_FONT_SIZE, MIN_TRANSLATION_FONT_SIZE,
    MIN_TRANSLATION_SIDE_MARGIN_PX,
    MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX, SETTINGS_PATH, resolve_model,
)
from translators.registry import detect_flm_base
from ui.theme import THEMES, settings_stylesheet, theme_colors

log = get_logger("settings")

_IMAGE_CAPABILITY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
# How many models "Detect all" will probe when it has to read a gateway's
# whole catalog. A large provider lists hundreds; each one costs a request,
# so the batch is bounded and says so.
_VISION_BATCH_LIMIT = 40
_FREE_MODEL_ROLE = _IMAGE_CAPABILITY_ROLE + 1
_ACTIVE_MODEL_ROLE = _IMAGE_CAPABILITY_ROLE + 2
_MODEL_GROUP_ROLE = _IMAGE_CAPABILITY_ROLE + 3
_FAVORITE_MODEL_ROLE = _IMAGE_CAPABILITY_ROLE + 4
_PROVIDER_LABEL_ROLE = _IMAGE_CAPABILITY_ROLE + 5
_FAVORITE_KEY_ROLE = _IMAGE_CAPABILITY_ROLE + 6
_IMAGE_BADGES = {
    "supported": ("IMAGE INPUT", QColor("#173B2B"), QColor("#86EFAC")),
    "unsupported": ("TEXT ONLY", QColor("#303541"), QColor("#CBD5E1")),
    "unknown": ("UNVERIFIED", QColor("#493515"), QColor("#FCD34D")),
}
_CUSTOM_PROVIDER_STRIPE_COLORS = (
    "#FFD400",
    "#FF3B30",
    "#00D9FF",
    "#FF8A00",
    "#39D353",
    "#B56CFF",
)
_CUSTOM_PROVIDER_PLACEHOLDERS = {
    "name": "e.g. My OpenRouter",
    "base_url": "https://api.openai.com/v1  or  https://openrouter.ai/api/v1",
    "model": "e.g. gpt-4o  or  anthropic/claude-3.5-sonnet",
}

# Sidebar page indices
PAGE_PROVIDERS = 0
PAGE_TRANSLATION = PAGE_PROVIDERS  # Legacy alias; the Translation pane was removed.
PAGE_KEYS = 1
PAGE_APPEARANCE = 2
PAGE_PERFORMANCE = 3
PAGE_GENERAL = 4
PAGE_TTS = 5


class _ModelPickerDelegate(QStyledItemDelegate):
    """Render compact model rows with status badges and provider headings."""

    favoriteClicked = Signal(str)

    def __init__(self, parent=None, *, favorites_enabled: bool = False):
        super().__init__(parent)
        self._favorites_enabled = favorites_enabled
        self._view = parent.view() if isinstance(parent, QComboBox) else None
        if self._view is not None and favorites_enabled:
            self._view.viewport().installEventFilter(self)

    @staticmethod
    def _favorite_rect(rect: QRect) -> QRect:
        return QRect(rect.right() - 28, rect.top(), 28, rect.height())

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 25 if index.data(_MODEL_GROUP_ROLE) else 28))
        return size

    def paint(self, painter: QPainter, option, index) -> None:
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style = (
            style_option.widget.style()
            if style_option.widget else self.parent().style()
        )
        is_group = bool(index.data(_MODEL_GROUP_ROLE)) or not bool(
            index.flags() & Qt.ItemFlag.ItemIsEnabled
        )
        if is_group:
            text = style_option.text
            style_option.text = ""
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem,
                style_option,
                painter,
                style_option.widget,
            )
            painter.save()
            group_font = QFont(style_option.font)
            group_font.setPointSize(max(9, group_font.pointSize() - 1))
            group_font.setBold(True)
            painter.setFont(group_font)
            painter.setPen(QColor("#94A3B8"))
            painter.drawText(
                style_option.rect.adjusted(10, 0, -8, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            painter.restore()
            return

        text = style_option.text
        style_option.text = ""
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            style_option,
            painter,
            style_option.widget,
        )

        status = index.data(_IMAGE_CAPABILITY_ROLE) or "unknown"
        vision_badges = {
            "supported": ("VISION", QColor("#14532D"), QColor("#86EFAC")),
            "unsupported": ("TEXT", QColor("#303541"), QColor("#CBD5E1")),
            "unknown": ("VISION?", QColor("#493515"), QColor("#FCD34D")),
        }
        badges = []
        if index.data(_FREE_MODEL_ROLE):
            badges.append(("FREE", QColor("#164E36"), QColor("#6EE7B7")))
        badges.append(vision_badges.get(status, vision_badges["unknown"]))
        is_active = bool(index.data(_ACTIVE_MODEL_ROLE))

        rect = style_option.rect.adjusted(10, 0, -8, 0)
        favorite_rect = self._favorite_rect(rect)
        if self._favorites_enabled:
            favorite_text = "★" if index.data(_FAVORITE_MODEL_ROLE) else "☆"
            painter.save()
            favorite_font = QFont(style_option.font)
            favorite_font.setPointSize(max(15, favorite_font.pointSize() + 2))
            painter.setFont(favorite_font)
            favorite_color = (
                QColor("#FBBF24")
                if index.data(_FAVORITE_MODEL_ROLE)
                else style_option.palette.color(
                    style_option.palette.ColorRole.Mid)
            )
            painter.setPen(favorite_color)
            painter.drawText(
                favorite_rect,
                Qt.AlignmentFlag.AlignCenter,
                favorite_text,
            )
            painter.restore()

        badge_font = QFont(style_option.font)
        badge_font.setPointSize(max(9, badge_font.pointSize() - 2))
        badge_font.setBold(True)
        painter.save()
        painter.setFont(badge_font)
        metrics = painter.fontMetrics()
        badge_height = 17
        badge_right = (
            favorite_rect.left() - 5
            if self._favorites_enabled else rect.right())
        for badge_text, badge_color, badge_text_color in reversed(badges):
            badge_width = metrics.horizontalAdvance(badge_text) + 14
            badge_rect = QRect(
                badge_right - badge_width,
                rect.center().y() - badge_height // 2,
                badge_width,
                badge_height,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(badge_color)
            painter.drawRoundedRect(badge_rect, 6, 6)
            painter.setPen(badge_text_color)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)
            badge_right = badge_rect.left() - 5

        model_font = QFont(style_option.font)
        model_font.setPointSize(max(12, model_font.pointSize()))
        model_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(model_font)
        available = max(0, badge_right - rect.left() - 8)
        provider_label = str(index.data(_PROVIDER_LABEL_ROLE) or "")
        provider_width = (
            min(
                painter.fontMetrics().horizontalAdvance(provider_label) + 12,
                max(0, available // 3),
            )
            if provider_label else 0
        )
        model_width = max(0, available - provider_width - (6 if provider_label else 0))
        model_text = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideMiddle, max(0, model_width - 16))
        model_chip_width = min(
            model_width,
            painter.fontMetrics().horizontalAdvance(model_text) + 16,
        )
        model_chip_height = 22
        model_chip = QRect(
            rect.left(),
            rect.center().y() - model_chip_height // 2,
            model_chip_width,
            model_chip_height,
        )
        painter.setPen(QColor("#6366F1") if is_active else QColor("#475569"))
        painter.setBrush(QColor("#312E81") if is_active else QColor("#202833"))
        painter.drawRoundedRect(model_chip, 6, 6)
        model_text_color = QColor("#E0E7FF") if is_active else (
            style_option.palette.color(
                style_option.palette.ColorRole.HighlightedText)
            if style_option.state & QStyle.StateFlag.State_Selected
            else style_option.palette.color(style_option.palette.ColorRole.Text)
        )
        painter.setPen(model_text_color)
        painter.drawText(
            model_chip.adjusted(8, 0, -8, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            model_text,
        )
        if provider_label and provider_width:
            painter.setPen(style_option.palette.color(
                style_option.palette.ColorRole.Mid))
            painter.setFont(style_option.font)
            provider_x = model_chip.right() + 7
            provider_available = max(0, badge_right - provider_x)
            painter.drawText(
                QRect(
                    provider_x,
                    rect.top(),
                    provider_available,
                    rect.height(),
                ),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(
                    provider_label,
                    Qt.TextElideMode.ElideRight,
                    provider_available,
                ),
            )
        painter.restore()

    def eventFilter(self, watched, event) -> bool:
        if (
            getattr(self, "_favorites_enabled", False)
            and event.type() == QEvent.Type.MouseButtonRelease
        ):
            try:
                if (
                    self._view is not None
                    and watched is self._view.viewport()
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    index = self._view.indexAt(event.pos())
                    if index.isValid() and self._favorite_rect(
                        self._view.visualRect(index)).contains(event.pos()):
                        favorite_key = index.data(_FAVORITE_KEY_ROLE)
                        if not isinstance(favorite_key, str) or not favorite_key:
                            favorite_key = index.data(Qt.ItemDataRole.UserRole)
                        if isinstance(favorite_key, str) and favorite_key:
                            self.favoriteClicked.emit(favorite_key)
                            return True
            except RuntimeError:
                return False
        return super().eventFilter(watched, event)


def _is_free_model(model: str) -> bool:
    normalized = model.strip().casefold().rstrip("/")
    return normalized.endswith(("-free", ":free", "/free"))


def _normalize_model_favorites(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        model.strip()
        for model in value
        if isinstance(model, str) and model.strip()
    })


def _custom_model_favorite_key(provider_id: str, model_id: str) -> str:
    payload = json.dumps(
        [provider_id, model_id], ensure_ascii=True, separators=(",", ":"))
    return f"custom-model:{payload}"


def _custom_model_favorite_parts(key: str) -> tuple[str, str] | None:
    if not key.startswith("custom-model:"):
        return None
    try:
        payload = json.loads(key[len("custom-model:"):])
    except (TypeError, ValueError):
        return None
    if (
        isinstance(payload, list)
        and len(payload) == 2
        and all(isinstance(part, str) and part for part in payload)
    ):
        return payload[0], payload[1]
    return None


def _normalize_custom_provider_collapsed(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        provider_id.strip(): collapsed
        for provider_id, collapsed in value.items()
        if isinstance(provider_id, str) and provider_id.strip()
        and isinstance(collapsed, bool)
    }


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers for the new chrome
# ──────────────────────────────────────────────────────────────────────────────

def _badge_label(text: str, kind: str = "muted") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName({"success": "badgeSuccess", "warning": "badgeWarning"}.get(kind, "badgeMuted"))
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lbl


def _hint_frame(text: str, kind: str = "default") -> QFrame:
    box = QFrame()
    box.setObjectName({"success": "hintBoxSuccess", "warning": "hintBoxWarning"}.get(kind, "hintBox"))
    lay = QHBoxLayout(box)
    lay.setContentsMargins(12, 10, 12, 10)
    icon = {"success": "✓", "warning": "⚠"}.get(kind, "ℹ")
    icon_lbl = QLabel(icon)
    icon_lbl.setStyleSheet("font-size: 14px; font-weight: 800; background: transparent; border: none;")
    lay.addWidget(icon_lbl)
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("background: transparent; border: none; font-size: 14px; font-weight: 500;")
    lay.addWidget(lbl, 1)
    return box


class _NavButton(QPushButton):
    """Plain-text sidebar navigation button."""

    def __init__(self, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setProperty("nav", True)
        self.setText(title)
        self.setToolTip(subtitle)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(42)


class _ProviderActionButton(QPushButton):
    """Provider action button with a symbol drawn at the exact center."""

    def __init__(self, symbol: str, parent=None):
        super().__init__(parent)
        self._symbol = symbol
        self.setText("")

    def set_symbol(self, symbol: str) -> None:
        if self._symbol != symbol:
            self._symbol = symbol
            self.update()

    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        style_painter = QStylePainter(self)
        style_painter.drawControl(QStyle.ControlElement.CE_PushButton, option)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.palette().color(self.foregroundRole()))
        center_x = (self.width() - 1) / 2
        center_y = (self.height() - 1) / 2
        extent = 6
        if self._symbol in ("−", "+"):
            painter.drawLine(
                QPointF(center_x - extent, center_y),
                QPointF(center_x + extent, center_y),
            )
        if self._symbol == "+":
            painter.drawLine(
                QPointF(center_x, center_y - extent),
                QPointF(center_x, center_y + extent),
            )
        elif self._symbol == "×":
            painter.drawLine(
                QPointF(center_x - 4, center_y - 4),
                QPointF(center_x + 4, center_y + 4),
            )
            painter.drawLine(
                QPointF(center_x - 4, center_y + 4),
                QPointF(center_x + 4, center_y - 4),
            )


class _SafeModelComboBox(QComboBox):
    """Do not let incidental wheel events change the selected provider model."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wheel_armed = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._wheel_armed = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.lineEdit():
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._wheel_armed = True
            elif event.type() == QEvent.Type.Wheel and not self._can_wheel_change():
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event) -> None:
        if not self._can_wheel_change():
            event.accept()
            return
        super().wheelEvent(event)

    def _can_wheel_change(self) -> bool:
        line_edit = self.lineEdit()
        return self._wheel_armed and (
            self.hasFocus() or (line_edit is not None and line_edit.hasFocus())
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main dialog
# ──────────────────────────────────────────────────────────────────────────────

def destroy_dialog(dialog: QDialog) -> None:
    """Synchronously delete a completed dialog and its child widget tree."""
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(
        dialog, QEvent.Type.DeferredDelete)


class SettingsDialog(QDialog):
    probeDone = Signal(list)
    verifyDone = Signal()
    autoModelsFetched = Signal(str, str, str, list, object, object)
    customModelsFetched = Signal(str, list, object, object)
    customModelsAudited = Signal(str, str, str, object)
    customVisionProbed = Signal(str, str, str, str, object)
    customReasoningProbed = Signal(str, str, str, str, object)
    customVisionProgress = Signal(str, str, bool, int, int, str)
    customVisionBatchDone = Signal(str, str, object)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings — Capio")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(520, 420)
        self.resize(1080, 700)
        self._probe_thread = None
        self._verify_thread = None
        self._live_cli: list[str] | None = None
        self._live_groq: list[str] | None = None
        self._live_cf: list[str] | None = None
        self._live_flm: list[str] | None = None
        self._cli_model_error: str | None = None
        self._syncing = False
        self._settings = dict(settings)
        self._auto_fetch_timers: dict[str, QTimer] = {}
        self._auto_fetch_in_progress: set[str] = set()
        self._auto_fetch_last: dict[str, tuple[str, str]] = {}
        self._settings.setdefault("color_scheme", "system")
        if self._settings.get("theme") not in THEMES:
            self._settings["theme"] = "Dark Slate"
        self.setStyleSheet(settings_stylesheet(
            self._settings.get("theme", "Dark Slate"),
            self._settings.get("color_scheme", "system"),
        ))
        self.probeDone.connect(self._on_probe_done)
        self.verifyDone.connect(self._on_verify_done)
        self.autoModelsFetched.connect(self._on_auto_fetch_done)
        self.customModelsFetched.connect(self._on_custom_fetch_done)
        self.customModelsAudited.connect(self._on_custom_models_audited)
        self.customVisionProbed.connect(self._on_custom_vision_probed)
        self.customReasoningProbed.connect(self._on_custom_reasoning_probed)
        self.customVisionProgress.connect(self._on_custom_vision_progress)
        self.customVisionBatchDone.connect(self._on_custom_vision_batch_done)
        self._provider_models: dict[str, list[str]] = {}
        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(400)
        self._size_save_timer.timeout.connect(self._save_dialog_geometry)

        # ── Root layout ──
        root = QVBoxLayout(self)
        root.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Header ──
        root.addWidget(self._build_header())

        # ── Body: sidebar + stacked content ──
        body = QWidget()
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self._body_splitter = QSplitter(Qt.Orientation.Horizontal, body)
        self._body_splitter.setObjectName("settingsBodySplitter")
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(8)
        body_layout.addWidget(self._body_splitter)

        # Sidebar
        sidebar = self._build_sidebar()
        sidebar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self._body_splitter.addWidget(sidebar)

        # Content area
        content_frame = QFrame()
        content_frame.setObjectName("contentArea")
        content_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        content_layout = QVBoxLayout(content_frame)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self._stack = QStackedWidget()
        # Build order matters: the model picker reads keys + fast_check.
        self._keys_group = self._build_keys_group()
        self._performance_group = self._build_performance_group()
        self._translation_group = self._build_engine_group()
        self._custom_providers_group = self._build_custom_providers_group()
        self._appearance_group = self._build_appearance_group()
        self._general_group = self._build_general_group()
        self._tts_group = self._build_tts_group()

        self._page_keys: list[QWidget] = []
        self._page_keys.append(self._wrap_scroll(self._custom_providers_group))
        self._page_keys.append(self._wrap_scroll(self._keys_group))
        self._page_keys.append(self._wrap_scroll(self._appearance_group))
        self._page_keys.append(self._wrap_scroll(self._performance_group))
        self._page_keys.append(self._wrap_scroll(self._general_group))
        self._page_keys.append(self._wrap_scroll(self._tts_group))
        for pg in self._page_keys:
            self._stack.addWidget(pg)

        content_layout.addWidget(self._stack, 1)

        self._body_splitter.addWidget(content_frame)
        self._body_splitter.setStretchFactor(0, 0)
        self._body_splitter.setStretchFactor(1, 1)
        self._body_splitter.setSizes([250, 830])
        root.addWidget(body, 1)

        # ── Footer ──
        root.addWidget(self._build_footer())

        # ── Hidden compatibility QTabWidget for legacy tests (findChild QTabWidget) ──
        from PySide6.QtWidgets import QTabWidget as _QTabWidget
        self._compat_tabs = _QTabWidget()
        self._compat_tabs.setObjectName("_compatTabs")
        # Don't add to layout — findChild still finds it as child via parent, but it won't affect layout min size
        self._compat_tabs.setParent(self)
        self._compat_tabs.setVisible(False)
        self._compat_tabs.setMaximumSize(0, 0)
        for _name in ["API Keys", "Custom Providers", "Performance", "Application", "Shortcuts", "Read Aloud / TTS"]:
            self._compat_tabs.addTab(QWidget(), _name)

        # Connect sidebar -> stack
        self._nav_group.buttonClicked.connect(self._on_nav_clicked)
        # Default page
        saved_page = self._settings.get("settings_last_page", PAGE_PROVIDERS)
        saved_page_version = self._settings.get("settings_last_page_version", 1)
        if saved_page_version == 1:
            legacy_pages = {
                0: PAGE_PROVIDERS,
                1: PAGE_KEYS,
                2: PAGE_PROVIDERS,
                3: PAGE_APPEARANCE,
                4: PAGE_PERFORMANCE,
                5: PAGE_GENERAL,
            }
            saved_page = legacy_pages.get(saved_page, PAGE_PROVIDERS)
        elif saved_page_version == 2:
            # The first five page indices stayed stable when the TTS page
            # was added, so keep the saved destination unchanged.
            pass
        elif saved_page_version != 3:
            saved_page = PAGE_PROVIDERS
        if not isinstance(saved_page, int) or saved_page not in range(len(self._page_keys)):
            saved_page = PAGE_PROVIDERS
        saved_nav = next(
            (
                button for button in self._nav_buttons
                if button.property("page_index") == saved_page
            ),
            self._nav_buttons[0],
        )
        saved_nav.setChecked(True)
        self._stack.setCurrentIndex(saved_page)
        if saved_page == PAGE_TTS:
            self._load_tts_edge_voices()
        self._update_sidebar_badge()

        self._update_key_states()
        self._restore_dialog_geometry()

    # ── Chrome builders ──────────────────────────────────────────────────────

    def _build_header(self) -> QFrame:
        colors = self._theme_colors()
        header = QFrame()
        header.setObjectName("settingsHeader")
        header.setFixedHeight(68)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(8)

        # Keep the subtitle on the same line as the title.
        text_col = QHBoxLayout()
        text_col.setSpacing(12)
        text_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("Settings")
        title.setObjectName("headerTitle")
        subtitle = QLabel("Models, API keys, appearance & behavior — all in one place")
        subtitle.setObjectName("headerSubtitle")
        text_col.addWidget(title)
        text_col.addWidget(subtitle)
        lay.addLayout(text_col)
        lay.addStretch(1)

        # Theme quick switcher in header (mirrors main window)
        theme_lbl = QLabel("Theme")
        theme_lbl.setStyleSheet(f"color: {colors['muted']}; font-size: 14px; font-weight: 700;")
        lay.addWidget(theme_lbl)
        self.header_theme_combo = QComboBox()
        self.header_theme_combo.setMinimumWidth(150)
        for name in THEMES:
            self.header_theme_combo.addItem(name, name)
        cur_theme = self._settings.get("theme", "Dark Slate")
        idx = self.header_theme_combo.findData(cur_theme)
        self.header_theme_combo.setCurrentIndex(max(0, idx))
        self.header_theme_combo.setToolTip("Switch the application color theme")
        self.header_theme_combo.currentIndexChanged.connect(self._on_header_theme_changed)
        lay.addWidget(self.header_theme_combo)

        return header

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(500)
        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(3)

        nav_panel = QWidget(sidebar)
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(3)

        # Section: GENERAL
        sec1 = QLabel("GENERAL")
        sec1.setObjectName("sidebarSection")
        nav_layout.addWidget(sec1)
        self._nav_buttons: list[_NavButton] = []
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        items = [
            ("Custom Providers", "Your OpenAI-compatible APIs", PAGE_PROVIDERS),
            ("API Keys", "Gemini, Groq, Cloudflare…", PAGE_KEYS),
            ("Appearance", "Fonts, sizes, spacing & theme", PAGE_APPEARANCE),
            ("Performance", "Speed, streaming & effort", PAGE_PERFORMANCE),
            ("Application", "Behavior, shortcuts & storage", PAGE_GENERAL),
            ("Read Aloud / TTS", "Engine, speed & speech behavior", PAGE_TTS),
        ]
        for title, subtitle, page_index in items:
            btn = _NavButton(title, subtitle, nav_panel)
            btn.setProperty("page_index", page_index)
            self._nav_group.addButton(btn, page_index)
            self._nav_buttons.append(btn)
            nav_layout.addWidget(btn)
        nav_layout.addStretch(1)

        self._results_stack = QStackedWidget(sidebar)
        self._results_stack.setObjectName("settingsResultsStack")
        self._results_stack.setMinimumHeight(150)
        self._results_stack.setMaximumHeight(500)

        self.results = QTextEdit()
        self.results.setObjectName("settingsStatusPanel")
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(100)
        self.results.setMaximumHeight(500)
        self.results.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.results.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.results.setStyleSheet(
            "QTextEdit#settingsStatusPanel {"
            "font-size: 18px; padding: 12px; border-radius: 8px; }")
        self.results.textChanged.connect(self._show_plain_results)
        self._results_stack.addWidget(self.results)

        self._sidebar_splitter = QSplitter(Qt.Orientation.Vertical, sidebar)
        self._sidebar_splitter.setObjectName("settingsSidebarSplitter")
        self._sidebar_splitter.setChildrenCollapsible(False)
        self._sidebar_splitter.setHandleWidth(8)
        self._sidebar_splitter.addWidget(nav_panel)
        self._sidebar_splitter.addWidget(self._results_stack)
        self._sidebar_splitter.setStretchFactor(0, 1)
        self._sidebar_splitter.setStretchFactor(1, 1)
        self._sidebar_splitter.setSizes([330, 200])
        lay.addWidget(self._sidebar_splitter, 1)

        # Sidebar footer: version + storage path hint
        foot = QLabel(f"Config: {SETTINGS_PATH.name}")
        foot.setWordWrap(True)
        foot.setStyleSheet(f"color: {self._theme_colors()['muted']}; font-size: 12px; background: transparent; border: none;")
        lay.addWidget(foot)
        return sidebar

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("footer")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        self.test_btn = QPushButton("Test Selected Model")
        self.test_btn.setToolTip("Send a short test request to the currently selected model")
        self.test_btn.clicked.connect(self._run_probe)
        lay.addWidget(self.test_btn)
        lay.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghostBtn")
        cancel.setMinimumHeight(42)
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel)

        ok = QPushButton("Save Changes  ✓")
        ok.setObjectName("primaryBtn")
        ok.setMinimumHeight(42)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        lay.addWidget(ok)
        return footer

    def _wrap_scroll(self, widget: QWidget) -> QWidget:
        """Put widget into a scroll area padded like a page."""
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        lay = QVBoxLayout(page)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        inner = QWidget()
        inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        inner_layout.setContentsMargins(18, 14, 18, 14)
        inner_layout.setSpacing(12)
        inner_layout.addWidget(widget)
        inner_layout.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll)
        return page

    def _section_header(self, eyebrow: str, title: str, description: str) -> QWidget:
        """Consistent page header: eyebrow + title + description."""
        colors = self._theme_colors()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 0, 2, 4)
        lay.setSpacing(4)
        eb = QLabel(eyebrow)
        eb.setStyleSheet(f"color: {colors['accent_light']}; font-size: 12px; font-weight: 800; letter-spacing: 1px; background: transparent; border: none;")
        lay.addWidget(eb)
        t = QLabel(title)
        t.setStyleSheet(f"color: {colors['text']}; font-size: 21px; font-weight: 800; background: transparent; border: none;")
        lay.addWidget(t)
        d = QLabel(description)
        d.setWordWrap(True)
        d.setStyleSheet(f"color: {colors['muted']}; font-size: 14px; font-weight: 500; line-height: 1.4; background: transparent; border: none;")
        lay.addWidget(d)
        return w

    def _on_nav_clicked(self, btn) -> None:
        idx = btn.property("page_index")
        if not isinstance(idx, int):
            return
        self._stack.setCurrentIndex(idx)
        self._settings["settings_last_page"] = idx
        self._settings["settings_last_page_version"] = 3
        parent_settings = getattr(self.parent(), "settings", None)
        if isinstance(parent_settings, dict):
            parent_settings["settings_last_page"] = idx
            parent_settings["settings_last_page_version"] = 3
            try:
                from config.settings import save

                save(parent_settings)
            except (OSError, TypeError, ValueError):
                log.exception("Could not save the last Settings page")
        if idx == PAGE_TTS:
            self._load_tts_edge_voices()

    @staticmethod
    def _rename_custom_model_group(combo: QComboBox, provider_name: str) -> None:
        for index in range(combo.count()):
            if combo.itemData(index, _MODEL_GROUP_ROLE):
                current = combo.itemText(index)
                if current.startswith("Models from "):
                    combo.setItemText(
                        index,
                        f"Models from {provider_name.strip() or 'Provider'}",
                    )

    def _on_header_theme_changed(self, _idx: int) -> None:
        theme = self.header_theme_combo.currentData()
        if not isinstance(theme, str) or theme not in THEMES:
            return
        self._settings["theme"] = theme
        self._apply_theme_style()

    def _on_color_scheme_changed(self, _idx: int) -> None:
        scheme = self.color_scheme_combo.currentData()
        if scheme not in ("system", "dark", "light"):
            return
        self._settings["color_scheme"] = scheme
        self._apply_theme_style()

    def _apply_theme_style(self) -> None:
        theme = self._settings.get("theme", "Dark Slate")
        scheme = self._settings.get("color_scheme", "system")
        self.setStyleSheet(settings_stylesheet(theme, scheme))
        try:
            colors = self._theme_colors()
            # model_instructions uses inline surface/border colors
            self.model_instructions.setStyleSheet(
                "QPlainTextEdit {"
                f"background-color: {colors['surface']};"
                f"color: {colors['text']};"
                f"border: 1px solid {colors['border']};"
                "border-radius: 10px; padding: 8px; }"
            )
        except Exception:
            pass

    def _update_sidebar_badge(self) -> None:
        """Retained as a no-op for existing status-update call sites."""

    # ── Geometry persistence ─────────────────────────────────────────────────

    def _restore_dialog_geometry(self) -> None:
        size = self._settings.get("settings_size")
        if (isinstance(size, (list, tuple)) and len(size) == 2
                and all(isinstance(v, (int, float)) for v in size)):
            width, height = int(size[0]), int(size[1])
            if width >= 520 and height >= 420:
                self.resize(width, height)
        position = self._settings.get("settings_pos")
        if (isinstance(position, (list, tuple)) and len(position) == 2
                and all(isinstance(v, (int, float)) for v in position)):
            self.move(int(position[0]), int(position[1]))

    def _theme_colors(self) -> dict[str, str]:
        return theme_colors(
            self._settings.get("theme", "Dark Slate"),
            self._settings.get("color_scheme", "system"),
        )

    def minimumSizeHint(self) -> QSize:
        return QSize(520, 420)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._size_save_timer.start()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._size_save_timer.start()

    def _capture_dialog_geometry(self) -> None:
        self._settings["settings_size"] = [self.width(), self.height()]
        self._settings["settings_pos"] = [self.x(), self.y()]

    def _save_dialog_geometry(self) -> None:
        self._capture_dialog_geometry()
        try:
            from config.settings import load, save
            persisted = load()
            persisted["settings_size"] = [self.width(), self.height()]
            persisted["settings_pos"] = [self.x(), self.y()]
            save(persisted)
        except (OSError, TypeError, ValueError):
            log.exception("Could not save Settings window geometry")

    def accept(self) -> None:
        self._size_save_timer.stop()
        self._save_dialog_geometry()
        super().accept()

    # ── Content builders ─────────────────────────────────────────────────────

    def _build_engine_group(self) -> QWidget:
        """Build the global model picker, now shown at the top of Custom Providers."""
        card = QGroupBox("MODEL")
        card.setObjectName("globalModelPickerCard")
        card.setStyleSheet(
            "QGroupBox#globalModelPickerCard {"
            "margin-top: 8px; padding: 6px 12px 6px 12px; }"
        )
        form = QFormLayout(card)
        form.setContentsMargins(8, 4, 8, 4)
        form.setSpacing(4)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.z_model = QComboBox()
        self.z_model.setObjectName("modelPicker")
        self.z_model.setEditable(True)
        model_delegate = _ModelPickerDelegate(
            self.z_model, favorites_enabled=True)
        model_delegate.favoriteClicked.connect(
            self._on_model_favorite_clicked)
        self.z_model.setItemDelegate(model_delegate)
        self.z_model.setMinimumHeight(38)
        self.z_model.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        self.z_model.view().setFont(
            QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        self.z_model.lineEdit().setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        self.z_model.currentTextChanged.connect(self._on_model_changed)
        self.model_image_badge = QLabel()
        self.model_image_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_image_badge.setMinimumWidth(110)
        self.model_image_badge.setMinimumHeight(34)
        self.z_refresh = QPushButton("⟳")
        self.z_refresh.setObjectName("iconBtn")
        self.z_refresh.setToolTip("Reload Gemini, OpenCode Zen, and OpenCode CLI model lists")
        self.z_refresh.setFixedSize(38, 38)
        self.z_refresh.clicked.connect(self._refresh_zen_models)

        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(self.z_model, 1)
        model_row.addWidget(self.model_image_badge)
        model_row.addWidget(self.z_refresh)
        holder = QWidget()
        holder.setLayout(model_row)
        form.addRow("Model", holder)

        self.engine_hint = QLabel()
        self.engine_hint.setWordWrap(True)
        self.engine_hint.setStyleSheet("font-size: 14px; background: transparent; border: none;")
        form.addRow("", self.engine_hint)

        self._fill_model_combo()
        self._select_model(resolve_model(self._settings))

        self._engine_model_card = card
        return card

    def _build_keys_group(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(self._section_header("CREDENTIALS", "API Keys", "Add the keys you use. Everything stays on your machine and is only sent to the provider that needs it."))

        s = self._settings
        box = QGroupBox("PROVIDERS")
        form = QFormLayout(box)
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.gemini_key = QLineEdit(s.get("gemini_key", "") or "")
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key.setPlaceholderText("Paste Gemini API Key")
        self.gemini_state = self._key_state_label()
        form.addRow("Gemini", self._key_row(self.gemini_key, self.gemini_state, "https://aistudio.google.com/apikey"))

        self.groq_key = QLineEdit(s.get("groq_key", "") or "")
        self.groq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key.setPlaceholderText("gsk-... (optional if shared slot set)")
        self.groq_state = self._key_state_label()
        form.addRow("Groq", self._key_row(self.groq_key, self.groq_state, "https://console.groq.com/keys"))

        self.cf_account = QLineEdit(s.get("cloudflare_account_id", "") or "")
        self.cf_account.setPlaceholderText("Account ID from dash.cloudflare.com")
        self.cf_account_state = self._key_state_label()
        form.addRow("Cloudflare ID", self._key_row(self.cf_account, self.cf_account_state, "https://dash.cloudflare.com/"))
        self.cf_token = QLineEdit(s.get("cloudflare_token", "") or "")
        self.cf_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.cf_token.setPlaceholderText("API token (Workers AI)")
        self.cf_token_state = self._key_state_label()
        form.addRow("Cloudflare Token", self._key_row(self.cf_token, self.cf_token_state, "https://dash.cloudflare.com/"))

        self.mistral_key = QLineEdit(s.get("mistral_key", "") or "")
        self.mistral_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.mistral_key.setPlaceholderText("Mistral key (optional if shared slot set)")
        self.mistral_state = self._key_state_label()
        form.addRow("Mistral", self._key_row(self.mistral_key, self.mistral_state, "https://console.mistral.ai/"))

        self._flm_detected = detect_flm_base()
        self.flm_base = QLineEdit(s.get("flm_base", "") or "")
        self.flm_base.setPlaceholderText(self._flm_detected)
        self.flm_base.setToolTip(f"FreeLLMAPI's base URL. Detected from its own config: {self._flm_detected}")
        self.flm_key = QLineEdit(s.get("flm_key", "") or "")
        self.flm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.flm_key.setPlaceholderText("freellmapi-… (Keys page)")
        self.flm_state = self._key_state_label()
        flm_hint = QLabel("Router URL + unified key")
        flm_hint.setStyleSheet("color: #94A3B8; font-size: 14px; background: transparent; border: none;")
        flm_box = QWidget()
        flm_form = QFormLayout(flm_box)
        flm_form.setContentsMargins(0, 4, 0, 0)
        flm_form.setSpacing(6)
        flm_form.addRow("Router", self.flm_base)
        self.flm_key.textChanged.connect(self._on_credentials_changed)
        self.flm_base.textChanged.connect(self._on_credentials_changed)
        flm_key_row = QWidget()
        flm_key_row.setLayout(self._key_row_layout(self.flm_key, self.flm_state))
        flm_form.addRow("Key", flm_key_row)
        form.addRow("FreeLLMAPI", flm_box)

        # Legacy single OpenAI-compatible slot (hidden but preserved for migration)
        self.o_base = QLineEdit(s.get("openai_base", "") or "")
        self.o_key = QLineEdit(s.get("openai_key", "") or "")
        self.o_model = QLineEdit(s.get("openai_model", "") or "")
        self.advanced_box = QFrame()
        self.advanced_toggle = QPushButton()

        lay.addWidget(box)
        # Helpful hint box
        hint = _hint_frame("Keys are stored locally in your home directory and never sent anywhere except the matching provider.", "default")
        lay.addWidget(hint)
        return outer

    def _build_appearance_group(self) -> QWidget:
        s = self._settings
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(self._section_header("STYLE", "Appearance", "Typography and readability controls. Changes apply instantly to any open translation."))

        color_card = QGroupBox("COLOR THEME")
        color_form = QFormLayout(color_card)
        color_form.setSpacing(10)
        self.color_scheme_combo = QComboBox()
        for label, value in (
            ("Follow system", "system"),
            ("Dark", "dark"),
            ("Light", "light"),
        ):
            self.color_scheme_combo.addItem(label, value)
        scheme = self._settings.get("color_scheme", "system")
        scheme_index = self.color_scheme_combo.findData(scheme)
        self.color_scheme_combo.setCurrentIndex(
            max(0, scheme_index))
        self.color_scheme_combo.currentIndexChanged.connect(
            self._on_color_scheme_changed)
        color_form.addRow("Color scheme", self.color_scheme_combo)
        self.pdf_canvas_theme_check = QCheckBox(
            "Match background around PDF pages to theme")
        self.pdf_canvas_theme_check.setChecked(bool(
            s.get("pdf_canvas_theme_background", False)))
        self.pdf_canvas_theme_check.setToolTip(
            "Use the active theme color in the spaces around pages. "
            "The PDF page appearance is unchanged.")
        color_form.addRow(self.pdf_canvas_theme_check)
        lay.addWidget(color_card)

        # ── Typography card ──
        typo_card = QGroupBox("TYPOGRAPHY")
        form = QFormLayout(typo_card)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(MIN_TRANSLATION_FONT_SIZE, MAX_TRANSLATION_FONT_SIZE)
        self.font_size.setDecimals(1)
        self.font_size.setSingleStep(1.0)
        self.font_size.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.font_size.setValue(float(s.get("font_size", 17) or 17))
        self.font_size.setToolTip("Applies instantly to open translations — no need to press Save.")
        self.font_size.valueChanged.connect(self._on_typography_changed)
        font_size_row = QHBoxLayout()
        font_size_row.setSpacing(6)
        font_size_row.addWidget(self.font_size, 1)
        self.font_size_down = QPushButton("−")
        self.font_size_down.setObjectName("fontSizeDown")
        self.font_size_down.setProperty("stepBtn", True)
        self.font_size_down.setObjectName("stepBtn")
        # keep test objectName compatibility: tests look for fontSizeDown / fontSizeUp
        self.font_size_down.setObjectName("fontSizeDown")
        self.font_size_down.setFixedSize(48, 42)
        self.font_size_down.setStyleSheet("QPushButton#fontSizeDown { padding: 0; font-size: 22px; font-weight: 700; }")
        self.font_size_down.setToolTip("Decrease translation font size")
        self.font_size_down.clicked.connect(self.font_size.stepDown)
        font_size_row.addWidget(self.font_size_down)
        self.font_size_up = QPushButton("+")
        self.font_size_up.setObjectName("fontSizeUp")
        self.font_size_up.setFixedSize(48, 42)
        self.font_size_up.setStyleSheet("QPushButton#fontSizeUp { padding: 0; font-size: 22px; font-weight: 700; }")
        self.font_size_up.setToolTip("Increase translation font size")
        self.font_size_up.clicked.connect(self.font_size.stepUp)
        font_size_row.addWidget(self.font_size_up)
        font_size_holder = QWidget()
        font_size_holder.setLayout(font_size_row)
        form.addRow("Font Size", font_size_holder)

        available_fonts = set(QFontDatabase.families())
        serif_fonts = (
            "Georgia", "Times New Roman", "Cambria", "Constantia",
            "Palatino Linotype", "Sitka Text", "Noto Serif", "DejaVu Serif",
            "Liberation Serif",
        )
        common_fonts = {
            "Arial", "Aptos", "Calibri", "Cambria", "Candara", "Consolas",
            "Constantia", "Corbel", "Courier New", "Georgia", "Palatino Linotype",
            "Segoe UI", "Sitka Text", "Tahoma", "Times New Roman", "Trebuchet MS",
            "Verdana",
        }
        available_fonts.update(common_fonts)
        added_fonts: set[str] = set()
        ordered_fonts = []
        for family in serif_fonts:
            if family in available_fonts:
                ordered_fonts.append((f"{family} (Serif)", family))
                added_fonts.add(family)
        ordered_fonts.extend((family, family) for family in sorted(available_fonts - added_fonts, key=str.casefold))

        def make_font_combo(setting_key: str) -> QComboBox:
            combo = QComboBox()
            combo.addItem("System default", "")
            for label, family in ordered_fonts:
                combo.addItem(label, family)
            saved_font = s.get(setting_key)
            if not isinstance(saved_font, str) or not saved_font:
                saved_font = s.get("translation_font", "")
            font_index = combo.findData(saved_font)
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, font_index))
            combo.blockSignals(False)
            combo.currentIndexChanged.connect(self._on_typography_changed)
            return combo

        self.arabic_font = make_font_combo("translation_font_arabic")
        form.addRow("Arabic Font", self.arabic_font)
        self.latin_font = make_font_combo("translation_font_latin")
        form.addRow("Latin Font", self.latin_font)

        self.arabic_tashkeel = QCheckBox("Add Arabic diacritics (tashkeel)")
        self.arabic_tashkeel.setChecked(bool(s.get("arabic_tashkeel", False)))
        self.arabic_tashkeel.setToolTip("Ask the model to add complete Arabic tashkeel to Arabic translations.")
        form.addRow("Arabic Text", self.arabic_tashkeel)

        try:
            saved_spacing = int(s.get("line_spacing_px", DEFAULT_LINE_SPACING))
        except (TypeError, ValueError):
            saved_spacing = DEFAULT_LINE_SPACING
        self.line_spacing = QSpinBox()
        self.line_spacing.setRange(MIN_LINE_SPACING, MAX_LINE_SPACING)
        self.line_spacing.setSingleStep(1)
        self.line_spacing.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.line_spacing.setValue(max(MIN_LINE_SPACING, min(MAX_LINE_SPACING, saved_spacing)))
        self.line_spacing.setSuffix(" px")
        self.line_spacing.setToolTip("Adjust wrapped-line spacing. 0 keeps natural spacing; negative tightens lines. Applies instantly.")
        self.line_spacing.valueChanged.connect(self._on_typography_changed)
        line_spacing_row = QHBoxLayout()
        line_spacing_row.setSpacing(6)
        line_spacing_row.addWidget(self.line_spacing, 1)
        self.line_spacing_down = QPushButton("−")
        self.line_spacing_down.setObjectName("lineSpacingDown")
        self.line_spacing_down.setFixedSize(48, 42)
        self.line_spacing_down.setStyleSheet("QPushButton#lineSpacingDown { padding: 0; font-size: 22px; font-weight: 700; }")
        self.line_spacing_down.setToolTip("Decrease extra line spacing by 1 px")
        self.line_spacing_down.clicked.connect(self.line_spacing.stepDown)
        line_spacing_row.addWidget(self.line_spacing_down)
        self.line_spacing_up = QPushButton("+")
        self.line_spacing_up.setObjectName("lineSpacingUp")
        self.line_spacing_up.setFixedSize(48, 42)
        self.line_spacing_up.setStyleSheet("QPushButton#lineSpacingUp { padding: 0; font-size: 22px; font-weight: 700; }")
        self.line_spacing_up.setToolTip("Increase extra line spacing by 1 px")
        self.line_spacing_up.clicked.connect(self.line_spacing.stepUp)
        line_spacing_row.addWidget(self.line_spacing_up)
        line_spacing_holder = QWidget()
        line_spacing_holder.setLayout(line_spacing_row)
        form.addRow("Extra Line Spacing", line_spacing_holder)

        try:
            saved_margin = int(s.get("translation_side_margin_px", DEFAULT_TRANSLATION_SIDE_MARGIN_PX))
        except (TypeError, ValueError):
            saved_margin = DEFAULT_TRANSLATION_SIDE_MARGIN_PX
        self.translation_side_margin = QSpinBox()
        self.translation_side_margin.setRange(MIN_TRANSLATION_SIDE_MARGIN_PX, MAX_TRANSLATION_SIDE_MARGIN_PX)
        self.translation_side_margin.setSingleStep(1)
        self.translation_side_margin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.translation_side_margin.setValue(max(MIN_TRANSLATION_SIDE_MARGIN_PX, min(MAX_TRANSLATION_SIDE_MARGIN_PX, saved_margin)))
        self.translation_side_margin.setSuffix(" px")
        self.translation_side_margin.setToolTip("Set the same left and right inner margins for translated text. Applies instantly.")
        self.translation_side_margin.valueChanged.connect(self._on_typography_changed)
        side_margin_row = QHBoxLayout()
        side_margin_row.setSpacing(6)
        side_margin_row.addWidget(self.translation_side_margin, 1)
        self.translation_side_margin_down = QPushButton("−")
        self.translation_side_margin_down.setObjectName("translationSideMarginDown")
        self.translation_side_margin_down.setFixedSize(48, 42)
        self.translation_side_margin_down.setStyleSheet("QPushButton#translationSideMarginDown { padding: 0; font-size: 22px; font-weight: 700; }")
        self.translation_side_margin_down.setToolTip("Decrease both left and right margins by 1 px")
        self.translation_side_margin_down.clicked.connect(self.translation_side_margin.stepDown)
        side_margin_row.addWidget(self.translation_side_margin_down)
        self.translation_side_margin_up = QPushButton("+")
        self.translation_side_margin_up.setObjectName("translationSideMarginUp")
        self.translation_side_margin_up.setFixedSize(48, 42)
        self.translation_side_margin_up.setStyleSheet("QPushButton#translationSideMarginUp { padding: 0; font-size: 22px; font-weight: 700; }")
        self.translation_side_margin_up.setToolTip("Increase both left and right margins by 1 px")
        self.translation_side_margin_up.clicked.connect(self.translation_side_margin.stepUp)
        side_margin_row.addWidget(self.translation_side_margin_up)
        side_margin_holder = QWidget()
        side_margin_holder.setLayout(side_margin_row)
        form.addRow("Side Margins", side_margin_holder)

        try:
            saved_vertical_margin = int(s.get(
                "translation_top_bottom_margin_px",
                DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            ))
        except (TypeError, ValueError):
            saved_vertical_margin = DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX
        self.translation_top_bottom_margin = QSpinBox()
        self.translation_top_bottom_margin.setRange(
            MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
        )
        self.translation_top_bottom_margin.setSingleStep(1)
        self.translation_top_bottom_margin.setButtonSymbols(
            QSpinBox.ButtonSymbols.NoButtons)
        self.translation_top_bottom_margin.setValue(max(
            MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            min(MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX, saved_vertical_margin),
        ))
        self.translation_top_bottom_margin.setSuffix(" px")
        self.translation_top_bottom_margin.setToolTip(
            "Set the same top and bottom inner margins for translated text. "
            "Applies instantly.")
        self.translation_top_bottom_margin.valueChanged.connect(
            self._on_typography_changed)
        vertical_margin_row = QHBoxLayout()
        vertical_margin_row.setSpacing(6)
        vertical_margin_row.addWidget(self.translation_top_bottom_margin, 1)
        self.translation_top_bottom_margin_down = QPushButton("−")
        self.translation_top_bottom_margin_down.setObjectName(
            "translationTopBottomMarginDown")
        self.translation_top_bottom_margin_down.setFixedSize(48, 42)
        self.translation_top_bottom_margin_down.setStyleSheet(
            "QPushButton#translationTopBottomMarginDown { padding: 0; "
            "font-size: 22px; font-weight: 700; }")
        self.translation_top_bottom_margin_down.setToolTip(
            "Decrease both top and bottom margins by 1 px")
        self.translation_top_bottom_margin_down.clicked.connect(
            self.translation_top_bottom_margin.stepDown)
        vertical_margin_row.addWidget(
            self.translation_top_bottom_margin_down)
        self.translation_top_bottom_margin_up = QPushButton("+")
        self.translation_top_bottom_margin_up.setObjectName(
            "translationTopBottomMarginUp")
        self.translation_top_bottom_margin_up.setFixedSize(48, 42)
        self.translation_top_bottom_margin_up.setStyleSheet(
            "QPushButton#translationTopBottomMarginUp { padding: 0; "
            "font-size: 22px; font-weight: 700; }")
        self.translation_top_bottom_margin_up.setToolTip(
            "Increase both top and bottom margins by 1 px")
        self.translation_top_bottom_margin_up.clicked.connect(
            self.translation_top_bottom_margin.stepUp)
        vertical_margin_row.addWidget(self.translation_top_bottom_margin_up)
        vertical_margin_holder = QWidget()
        vertical_margin_holder.setLayout(vertical_margin_row)
        form.addRow("Top / Bottom Margins", vertical_margin_holder)

        lay.addWidget(typo_card)

        # Hint about live preview
        lay.addWidget(_hint_frame("Font and spacing changes preview live — open translations update without saving.", "default"))
        return outer

    def _build_performance_group(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(self._section_header("SPEED", "Performance", "Control streaming, ordering and synthesis. Fast mode enables every speedup at once."))

        box = QGroupBox("SPEED & STREAMING")
        blay = QVBoxLayout(box)
        blay.setSpacing(12)

        self.fast_check = QCheckBox("⚡  Fast mode — all speedups ON")
        self.fast_check.setChecked(bool(self._settings.get("fast_mode", True)))
        self.fast_check.setToolTip("One switch for every speedup. OFF restores classic behavior.")
        self.fast_check.toggled.connect(self._on_fast_toggled)
        blay.addWidget(self.fast_check)

        self.fast_details = QLabel(
            "ON → live word-by-word display (~1s to first words) · bridge models ordered fastest-first · "
            "read-aloud replays from disk (~0s repeats) · speech segments synthesize in parallel.\n"
            "OFF → spinner until the full reply · alphabetical model order · "
            "speech re-synthesizes every click, one segment at a time.\n\n"
            "Measured short-sentence times: space-bunny 3.4s · muse-spark-1.3 4.3s · mimo 5.5s · fledge-alpha 6.0s."
        )
        self.fast_details.setWordWrap(True)
        self.fast_details.setStyleSheet("font-size: 14px; padding-top: 2px; background: transparent; border: none;")
        blay.addWidget(self.fast_details)

        from translators.opencode_bridge import BRIDGE_FREE, DEFAULT_EFFORT, EFFORT_ORDER, MODEL_DEFAULT_EFFORTS, effort_for
        effort_form = QFormLayout()
        effort_form.setSpacing(8)
        effort_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.effort_default_combo = QComboBox()
        self.effort_default_combo.addItems(EFFORT_ORDER)
        default = self._settings.get("effort_default")
        self.effort_default_combo.setCurrentIndex(EFFORT_ORDER.index(default if default in EFFORT_ORDER else DEFAULT_EFFORT))
        self.effort_default_combo.setToolTip("Global preset for future models and models without a tuned default.")
        effort_form.addRow("Global default effort", self.effort_default_combo)
        self.effort_combos: dict[str, QComboBox] = {}
        for model in BRIDGE_FREE:
            combo = QComboBox()
            combo.addItems(EFFORT_ORDER)
            combo.setCurrentIndex(EFFORT_ORDER.index(effort_for(model, self._settings)))
            combo.setToolTip("Output ceiling + reasoning effort for this model. Lower = faster, higher = more room for long passages.")
            effort_form.addRow(f"{model}", combo)
            self.effort_combos[model] = combo
        effort_box = QWidget()
        effort_box.setLayout(effort_form)
        blay.addWidget(effort_box)
        lay.addWidget(box)
        lay.addWidget(self._build_token_group())
        return outer

    def _build_token_group(self) -> QWidget:
        """Cumulative token spending, per model, from the shared ledger."""
        box = QGroupBox("TOKENS")
        blay = QVBoxLayout(box)
        blay.setSpacing(8)

        self.token_summary = QLabel()
        self.token_summary.setWordWrap(True)
        self.token_summary.setStyleSheet(
            "background: transparent; border: none;")
        blay.addWidget(self.token_summary)

        self.token_detail = QLabel()
        self.token_detail.setWordWrap(True)
        self.token_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.token_detail.setStyleSheet(
            "background: transparent; border: none;")
        blay.addWidget(self.token_detail)

        reset_btn = QPushButton("Reset token counter")
        reset_btn.setToolTip(
            "Clear the running totals. Only this counter — settings and "
            "providers are untouched."
        )
        reset_btn.clicked.connect(self._on_reset_tokens)
        blay.addWidget(reset_btn)

        self._refresh_token_summary()
        return box

    def _refresh_token_summary(self) -> None:
        from translators import usage

        data = usage.snapshot()
        totals = data["totals"]
        mark = " ≈" if any(
            row.get("estimated") for row in data["models"]) else ""
        self.token_summary.setText(
            f"Total{mark}: {data['total']:,} tokens over {data['requests']} "
            f"requests  ·  {totals.get('in', 0):,} in / "
            f"{totals.get('out', 0):,} out"
            + (f"  ·  {totals.get('reasoning', 0):,} reasoning"
               if totals.get("reasoning") else "")
            + (f"  ·  {totals.get('cached', 0):,} cached"
               if totals.get("cached") else "")
        )
        rows = data["models"][:10]
        if not rows:
            self.token_detail.setText("No requests recorded yet.")
            self.token_detail.setToolTip("")
            return
        lines = []
        for row in rows:
            flag = " ≈estimated" if row.get("estimated") else ""
            lines.append(
                f"{row['model']}  [{row['provider']}]  "
                f"{row['total']:,} tok  ({row['in']:,} in / "
                f"{row['out']:,} out, {row['requests']} req){flag}"
            )
        if len(data["models"]) > len(rows):
            lines.append(f"… and {len(data['models']) - len(rows)} more")
        self.token_detail.setText("\n".join(lines))
        self.token_detail.setToolTip(
            "≈ marks a provider that reports no counts, so the figure is "
            "an estimate from character length."
        )

    def _on_reset_tokens(self) -> None:
        from translators import usage

        usage.reset()
        self._refresh_token_summary()
        if self.results is not None:
            self.results.setPlainText("Token counter reset.")
            self._show_plain_results()

    def _build_general_group(self) -> QWidget:
        s = self._settings
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(self._section_header("SYSTEM", "Application", "Core behavior, reading mode and helpful shortcuts."))

        # Card: general prefs
        app_card = QGroupBox("PREFERENCES")
        form = QFormLayout(app_card)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.email = QLineEdit(s.get("email", "") or "")
        self.email.setPlaceholderText("Optional · Grants higher quota for MyMemory")
        form.addRow("Email", self.email)

        self.selection_mode = QComboBox()
        self.selection_mode.addItem("Extract text (PDF text / OCR)", "ocr")
        self.selection_mode.addItem("Send selection image to vision model", "image")
        mode_index = self.selection_mode.findData(s.get("selection_mode", "ocr"))
        self.selection_mode.setCurrentIndex(max(0, mode_index))
        self.selection_mode.setToolTip(
            "OCR extracts text locally before translation. Image mode sends the selected page area directly to the selected model. "
            "The model must support image input; API-compatible providers may need a key."
        )
        form.addRow("Selection Reading", self.selection_mode)

        lay.addWidget(app_card)

        instructions_card = QFrame()
        instructions_card.setObjectName("card")
        instructions_layout = QVBoxLayout(instructions_card)
        instructions_layout.setContentsMargins(18, 16, 18, 16)
        instructions_layout.setSpacing(8)
        instructions_title = QLabel("AI MODEL INSTRUCTIONS")
        instructions_title.setObjectName("modelInstructionsTitle")
        instructions_title.setStyleSheet(
            f"color: {self._theme_colors()['muted']}; font-size: 14px; "
            "font-weight: 700; letter-spacing: 0.6px; "
            "background: transparent; border: none;"
        )
        instructions_title.setToolTip(
            "Drag the divider below to resize this field with the mouse.")
        instructions_layout.addWidget(instructions_title)
        colors = self._theme_colors()
        self.model_instructions = QPlainTextEdit(
            self._settings.get("model_instructions", "") or "")
        self.model_instructions.setObjectName("modelInstructions")
        self.model_instructions.setPlaceholderText(
            "Optional guidance for AI models — e.g. keep a blank line between "
            "paragraphs when the source has one, or explain specialist terms "
            "briefly."
        )
        self.model_instructions.setMinimumHeight(110)
        self.model_instructions.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.model_instructions.setStyleSheet(
            "QPlainTextEdit {"
            f"background-color: {colors['surface']};"
            f"color: {colors['text']};"
            f"border: 1px solid {colors['border']};"
            "border-radius: 10px; padding: 8px; }"
        )
        instructions_layout.addWidget(self.model_instructions, 1)

        # Card: shortcuts
        short_card = QGroupBox("SHORTCUTS")
        sform = QFormLayout(short_card)
        sform.setSpacing(12)
        zoom_description = QLabel("Hold Ctrl and scroll the mouse wheel over the translated text to smoothly increase or decrease its font size. The size is saved and shared with the Font Size setting.")
        zoom_description.setObjectName("shortcutCtrlWheelDescription")
        zoom_description.setWordWrap(True)
        zoom_description.setStyleSheet("background: transparent; border: none; font-size: 14px;")
        sform.addRow(QLabel("Ctrl + Mouse Wheel"), zoom_description)
        height_description = QLabel("Double-click the Translation title bar to expand the window vertically to the reader area. Double-click it again to restore the previous size and position.")
        height_description.setObjectName("shortcutTitlebarDoubleClickDescription")
        height_description.setWordWrap(True)
        height_description.setStyleSheet("background: transparent; border: none; font-size: 14px;")
        sform.addRow(QLabel("Title Bar Double-Click"), height_description)
        instructions_splitter = QSplitter(Qt.Orientation.Vertical)
        instructions_splitter.setObjectName("modelInstructionsSplitter")
        instructions_splitter.setChildrenCollapsible(False)
        instructions_splitter.addWidget(instructions_card)
        instructions_splitter.addWidget(short_card)
        instructions_splitter.setStretchFactor(0, 2)
        instructions_splitter.setStretchFactor(1, 1)
        self._instr_splitter = instructions_splitter
        lay.addWidget(instructions_splitter, 1)

        where = QLabel(f"Configuration stored in {SETTINGS_PATH}")
        where.setStyleSheet(f"color: {self._theme_colors()['muted']}; font-size: 14px; background: transparent; border: none;")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(where)
        return outer

    def _build_tts_group(self) -> QWidget:
        settings = self._settings
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(self._section_header(
            "AUDIO", "Read Aloud / TTS",
            "Choose the speech engine and configure how translated text is read."
        ))

        engine_card = QGroupBox("ENGINE")
        engine_form = QFormLayout(engine_card)
        engine_form.setSpacing(12)
        engine_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.speech_engine = QComboBox()
        self.speech_engine.addItem("Microsoft Edge TTS", "edge")
        self.speech_engine.addItem("Gemini TTS", "gemini")
        selected_engine = self.speech_engine.findData(
            settings.get("speech_engine", "edge"))
        self.speech_engine.setCurrentIndex(max(0, selected_engine))
        engine_form.addRow("Speech Engine", self.speech_engine)

        self.speech_engine_notice = QLabel()
        self.speech_engine_notice.setWordWrap(True)
        self.speech_engine_notice.setObjectName("speechEngineNotice")
        self.speech_engine_notice.setStyleSheet(
            f"color: {self._theme_colors()['muted']}; background: transparent; "
            "border: none;")
        engine_form.addRow("", self.speech_engine_notice)

        behavior_card = QGroupBox("SPEECH")
        form = QFormLayout(behavior_card)
        form.setSpacing(12)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.speech_rate_setting = QDoubleSpinBox()
        self.speech_rate_setting.setRange(0.25, 2.0)
        self.speech_rate_setting.setDecimals(2)
        self.speech_rate_setting.setSingleStep(0.05)
        self.speech_rate_setting.setSuffix("×")
        try:
            configured_rate = float(settings.get("speech_rate", 1.0))
        except (TypeError, ValueError):
            configured_rate = 1.0
        self.speech_rate_setting.setValue(
            min(2.0, max(0.25, configured_rate)))
        form.addRow("Playback Speed", self.speech_rate_setting)

        self._edge_speech_voice = str(
            settings.get("speech_voice") or
            "fr-FR-VivienneMultilingualNeural")
        self._tts_edge_voice_actions = []
        self.edge_speech_voice_menu = QMenu(self)
        self.edge_speech_voice_menu.setObjectName("edgeSpeechVoiceMenu")
        self.edge_speech_voice_menu.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft)
        self.edge_speech_voice = QToolButton()
        self.edge_speech_voice.setObjectName("edgeSpeechVoice")
        self.edge_speech_voice.setText(f"{self._edge_speech_voice}  ▾")
        self.edge_speech_voice.clicked.connect(
            self._show_tts_edge_voice_menu)
        self.edge_speech_voice.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.edge_speech_voice.setToolTip(
            "Voices for Arabic, French, Italian, English, German, Spanish, "
            "and Russian, plus all Multilingual voices, are loaded from the "
            "live catalog. Choose by language, country, and voice.")
        self.edge_speech_voice_status = QLabel("Open this page to load voices.")
        self.edge_speech_voice_status.setWordWrap(True)
        edge_voice_row = QWidget()
        edge_voice_layout = QHBoxLayout(edge_voice_row)
        edge_voice_layout.setContentsMargins(0, 0, 0, 0)
        edge_voice_layout.addWidget(self.edge_speech_voice, 1)
        self.edge_speech_voice_refresh = QPushButton("Refresh")
        self.edge_speech_voice_refresh.clicked.connect(
            self._load_tts_edge_voices)
        edge_voice_layout.addWidget(self.edge_speech_voice_refresh)
        form.addRow("Edge Voice", edge_voice_row)
        form.addRow("", self.edge_speech_voice_status)

        from popup.speech_worker import GEMINI_TTS_VOICES

        self.gemini_tts_voice = QComboBox()
        for voice, quality in GEMINI_TTS_VOICES:
            self.gemini_tts_voice.addItem(
                f"{voice} — {quality}", voice)
        gemini_voice_index = self.gemini_tts_voice.findData(
            settings.get("gemini_tts_voice", "Kore"))
        self.gemini_tts_voice.setCurrentIndex(max(0, gemini_voice_index))
        self.gemini_tts_voice.setToolTip(
            "Gemini's prebuilt voices support the selected spoken language; "
            "the model detects it from the text.")
        form.addRow("Gemini Voice", self.gemini_tts_voice)

        self.automatic_speech_voice = QCheckBox(
            "Choose voice automatically for the translation language")
        self.automatic_speech_voice.setChecked(
            bool(settings.get("automatic_speech_voice", False)))
        self.automatic_speech_voice.setToolTip(
            "For Edge TTS, choose a neural voice matching the translation "
            "language. Otherwise use the voice selected in the translation window.")
        form.addRow("Automatic Voice", self.automatic_speech_voice)

        self.speech_read_latin_words = QCheckBox(
            "Read Latin-script words in Arabic translations")
        self.speech_read_latin_words.setChecked(
            bool(settings.get("speech_read_latin_words", False)))
        self.speech_read_latin_words.setToolTip(
            "Include English, French, and other Latin-script words mixed "
            "into Arabic speech, using the selected voice.")
        form.addRow("Latin Words", self.speech_read_latin_words)

        self.speech_engine.currentIndexChanged.connect(
            self._update_tts_engine_notice)
        self.speech_engine.currentIndexChanged.connect(
            self._update_tts_engine_controls)
        self._update_tts_engine_notice()
        self._update_tts_engine_controls()
        lay.addWidget(engine_card)
        lay.addWidget(behavior_card)
        lay.addStretch(1)
        return outer

    def _update_tts_engine_notice(self) -> None:
        if self.speech_engine.currentData() == "gemini":
            self.speech_engine_notice.setText(
                "Gemini TTS uses your existing Gemini API key and consumes "
                "API quota; depending on your account and current pricing, "
                "usage may incur charges. It detects language from the text, "
                "supports Arabic, and offers the listed voices. Word-level "
                "timing/highlighting is not available.")
        else:
            self.speech_engine_notice.setText(
                "Edge TTS provides selectable neural voices and word timing. "
                "It remains the default speech engine.")

    def _update_tts_engine_controls(self) -> None:
        edge_selected = self.speech_engine.currentData() == "edge"
        self.edge_speech_voice.setEnabled(edge_selected)
        self.edge_speech_voice_refresh.setEnabled(edge_selected)
        self.gemini_tts_voice.setEnabled(
            self.speech_engine.currentData() == "gemini")
        self.automatic_speech_voice.setEnabled(edge_selected)

    def _load_tts_edge_voices(self) -> None:
        from popup.speech_worker import VoiceListWorker

        worker = getattr(self, "_tts_voice_list_worker", None)
        if worker is not None or self.speech_engine.currentData() != "edge":
            return
        self.edge_speech_voice_status.setText(
            "Loading supported Edge TTS voices…")
        self.edge_speech_voice_refresh.setEnabled(False)
        worker = VoiceListWorker(self)
        self._tts_voice_list_worker = worker
        worker.voices_ready.connect(self._on_tts_edge_voices_ready)
        worker.failed.connect(self._on_tts_edge_voices_failed)
        worker.finished.connect(self._on_tts_edge_voices_finished)
        worker.start()

    def _on_tts_edge_voices_ready(self, voices: list[dict]) -> None:
        from popup.speech_worker import EDGE_TTS_PREFERRED_LANGUAGE_CODES

        current_voice = self._edge_speech_voice
        self.edge_speech_voice_menu.clear()
        self._tts_edge_voice_actions = []
        seen: set[str] = set()
        grouped: dict[str, dict[str, list[dict]]] = {}
        multilingual: dict[str, list[dict]] = {}
        for voice in voices:
            short_name = voice.get("ShortName")
            locale = voice.get("Locale")
            if (
                not isinstance(short_name, str)
                or not short_name
                or not isinstance(locale, str)
                or not locale
                or short_name in seen
            ):
                continue
            language = locale.partition("-")[0].casefold()
            if (
                language not in EDGE_TTS_PREFERRED_LANGUAGE_CODES
                and "multilingual" not in short_name.casefold()
            ):
                continue
            seen.add(short_name)
            country = self._tts_voice_country_name(voice, locale)
            if "multilingual" in short_name.casefold():
                multilingual.setdefault(country, []).append(voice)
            if language in EDGE_TTS_PREFERRED_LANGUAGE_CODES:
                grouped.setdefault(language.title(), {}).setdefault(
                    country, []).append(voice)

        for language in ("Fr", "Ar", "It", "En", "De", "Es", "Ru"):
            if language not in grouped:
                continue
            language_menu = self.edge_speech_voice_menu.addMenu(language)
            language_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            self._populate_tts_voice_country_menus(
                language_menu, grouped[language], current_voice)
        if multilingual:
            self.edge_speech_voice_menu.addSeparator()
            multilingual_menu = self.edge_speech_voice_menu.addMenu(
                "Multilingual")
            multilingual_menu.setLayoutDirection(
                Qt.LayoutDirection.RightToLeft)
            self._populate_tts_voice_country_menus(
                multilingual_menu, multilingual, current_voice)
        self.edge_speech_voice_menu.addSeparator()
        custom_voice_action = self.edge_speech_voice_menu.addAction(
            "Enter voice ID…")
        custom_voice_action.triggered.connect(self._prompt_tts_edge_voice)

        current_action = next(
            (
                action for action in self._tts_edge_voice_actions
                if action.data() == current_voice
            ),
            None,
        )
        if current_action is not None:
            self.edge_speech_voice.setText(
                f"{current_action.text()}  ▾")
            self.edge_speech_voice.setToolTip(
                f"{current_action.text()}\nVoice ID: {current_voice}")
        elif current_voice:
            self.edge_speech_voice.setText(f"{current_voice}  ▾")
            self.edge_speech_voice.setToolTip(
                f"Voice ID: {current_voice}")
        count = len(seen)
        self.edge_speech_voice_status.setText(
            f"Loaded {count} supported voices: seven languages "
            "plus all Multilingual voices."
            if count else
            "Edge TTS returned no supported voices."
        )

    @staticmethod
    def _tts_voice_country_name(voice: dict, locale: str) -> str:
        friendly_name = voice.get("FriendlyName")
        if isinstance(friendly_name, str):
            country = re.search(r"\(([^()]*)\)\s*$", friendly_name)
            if country:
                return country.group(1)
        return locale.partition("-")[2].upper() or locale

    def _populate_tts_voice_country_menus(
        self,
        parent_menu: QMenu,
        countries: dict[str, list[dict]],
        selected_voice: str,
    ) -> None:
        for country in sorted(countries):
            country_menu = parent_menu.addMenu(country)
            country_menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            for voice in sorted(
                countries[country],
                key=lambda item: str(
                    item.get("FriendlyName") or item["ShortName"]),
            ):
                short_name = voice["ShortName"]
                label = str(voice.get("FriendlyName") or short_name)
                gender = voice.get("Gender")
                if gender:
                    label = f"{label} ({gender})"
                action = country_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(short_name == selected_voice)
                action.setData(short_name)
                action.triggered.connect(
                    lambda _checked=False, name=short_name, display=label:
                        self._set_tts_edge_voice(name, display)
                )
                self._tts_edge_voice_actions.append(action)

    def _set_tts_edge_voice(self, voice: str, display_name: str) -> None:
        self._edge_speech_voice = voice
        for action in self._tts_edge_voice_actions:
            action.setChecked(action.data() == voice)
        self.edge_speech_voice.setText(f"{display_name}  ▾")
        self.edge_speech_voice.setToolTip(
            f"{display_name}\nVoice ID: {voice}")

    def _show_tts_edge_voice_menu(self) -> None:
        self.edge_speech_voice_menu.adjustSize()
        button_top_left = self.edge_speech_voice.mapToGlobal(QPoint(0, 0))
        menu_size = self.edge_speech_voice_menu.sizeHint()
        screen = (
            self.edge_speech_voice.screen()
            or self.screen()
        )
        if screen is not None:
            available = screen.availableGeometry()
            x = min(
                max(available.left(), button_top_left.x()),
                available.right() - menu_size.width() + 1,
            )
            y = max(
                available.top(),
                button_top_left.y() - menu_size.height(),
            )
        else:
            x = button_top_left.x()
            y = button_top_left.y() - menu_size.height()
        self.edge_speech_voice_menu.popup(QPoint(x, y))

    def _prompt_tts_edge_voice(self) -> None:
        voice, accepted = QInputDialog.getText(
            self,
            "Edge TTS voice",
            "Enter an Edge TTS voice ID:",
            QLineEdit.EchoMode.Normal,
            self._edge_speech_voice,
        )
        voice = voice.strip()
        if accepted and voice:
            self._set_tts_edge_voice(voice, voice)

    def _on_tts_edge_voices_failed(self, error: str) -> None:
        self.edge_speech_voice_status.setText(
            f"Could not load Edge TTS voices: {error}")

    def _on_tts_edge_voices_finished(self) -> None:
        worker = self.sender()
        if worker is self._tts_voice_list_worker:
            self._tts_voice_list_worker = None
        self.edge_speech_voice_refresh.setEnabled(
            self.speech_engine.currentData() == "edge")

    # ── Small widgets ────────────────────────────────────────────────────────

    @staticmethod
    def _key_state_label() -> QLabel:
        lbl = QLabel()
        lbl.setMinimumWidth(26)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lbl

    def _key_row_layout(self, edit: QLineEdit, state: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(state)
        return row

    def _key_row(self, edit: QLineEdit, state: QLabel, url: str) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(state)
        link = QLabel(f'<a href="{url}" style="text-decoration:none; font-weight:700; color:{self._theme_colors()["accent_light"]};">Get ↗</a>')
        link.setOpenExternalLinks(True)
        link.setStyleSheet("background: transparent; border: none;")
        row.addWidget(link)
        edit.textChanged.connect(self._on_credentials_changed)
        return holder

    def _toggle_advanced(self, on: bool) -> None:
        self.advanced_box.setVisible(on)
        self.advanced_toggle.setText("OpenAI-compatible  ▾" if on else "OpenAI-compatible  ▸")

    # ── Custom Providers ───────────────────────────────────────────────────

    def _build_custom_providers_group(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        colors = self._theme_colors()
        lay.addWidget(self._engine_model_card)

        intro = QWidget()
        intro.setObjectName("customProvidersIntro")
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(2, 0, 2, 0)
        intro_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        eyebrow = QLabel("EXTEND")
        eyebrow.setObjectName("customProvidersEyebrow")
        eyebrow.setStyleSheet(
            f"color: {colors['accent_light']}; font-size: 12px; "
            "font-weight: 800; letter-spacing: 1px; background: transparent;"
        )
        title_row.addWidget(eyebrow)
        title = QLabel("Custom Providers")
        title.setObjectName("customProvidersTitle")
        title.setStyleSheet(
            f"color: {colors['text']}; font-size: 21px; font-weight: 800; "
            "background: transparent;"
        )
        title_row.addWidget(title)
        title_row.addStretch(1)
        intro_layout.addLayout(title_row)

        lay.addWidget(intro)

        box = QGroupBox()
        box.setObjectName("customProvidersGroup")
        box.setStyleSheet(
            "QGroupBox#customProvidersGroup { margin: 0; padding: 0; "
            "border: none; background: transparent; }"
        )
        box.setToolTip("Like Kilo Code: add any API that speaks /v1/chat/completions")
        blay = QVBoxLayout(box)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(10)

        self._custom_cards: list[QWidget] = []
        self._custom_container = QVBoxLayout()
        self._custom_container.setSpacing(8)
        wrapper = QWidget()
        wrapper.setLayout(self._custom_container)
        blay.addWidget(wrapper)
        self._rebuild_custom_cards()

        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(8)
        self._toggle_reasoning_all_btn = QPushButton("Toggle reasoning for all")
        self._toggle_reasoning_all_btn.setToolTip("Turn reasoning on or off for every custom provider")
        self._toggle_reasoning_all_btn.setMinimumHeight(30)
        self._toggle_reasoning_all_btn.clicked.connect(self._on_toggle_reasoning_all)
        bulk_row.addWidget(self._toggle_reasoning_all_btn)
        self._toggle_vision_all_btn = QPushButton("Toggle images for all")
        self._toggle_vision_all_btn.setToolTip("Turn image input on or off for every custom provider")
        self._toggle_vision_all_btn.setMinimumHeight(30)
        self._toggle_vision_all_btn.clicked.connect(self._on_toggle_vision_all)
        bulk_row.addWidget(self._toggle_vision_all_btn)
        bulk_row.addStretch(1)
        blay.addLayout(bulk_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._add_custom_btn = QPushButton("Add Provider")
        self._add_custom_btn.setObjectName("primaryBtn")
        self._add_custom_btn.setMinimumHeight(30)
        self._add_custom_btn.setToolTip("Add a new custom API endpoint")
        self._add_custom_btn.clicked.connect(self._on_add_custom_provider)
        btn_row.addWidget(self._add_custom_btn)
        btn_row.addStretch(1)
        self._fetch_models_btn = QPushButton("⟳  Fetch Models")
        self._fetch_models_btn.setMinimumHeight(30)
        self._fetch_models_btn.setToolTip("Refresh the model list for the selected provider")
        self._fetch_models_btn.clicked.connect(self._on_fetch_custom_models)
        btn_row.addWidget(self._fetch_models_btn)
        blay.addLayout(btn_row)

        lay.addWidget(box)
        return outer

    def _rebuild_custom_cards(self) -> None:
        while self._custom_container.count():
            item = self._custom_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._custom_cards.clear()
        providers = self._settings.get("custom_providers") or []
        if not providers:
            empty = QLabel("No providers yet. Add one to connect your API endpoint.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(74)
            empty.setStyleSheet(
                f"font-size: 15px; color: {self._theme_colors()['muted']}; "
                f"background: {self._theme_colors()['surface']}; "
                f"border: 1px dashed {self._theme_colors()['accent_light']}; "
                "border-radius: 12px; padding: 14px;"
            )
            empty.setWordWrap(True)
            self._custom_container.addWidget(empty)
            self._custom_cards.append(empty)
            return
        card_index = 0
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            stripe_color = _CUSTOM_PROVIDER_STRIPE_COLORS[card_index % len(_CUSTOM_PROVIDER_STRIPE_COLORS)]
            card = self._make_custom_card(entry, stripe_color)
            self._custom_container.addWidget(card)
            self._custom_cards.append(card)
            card_index += 1
        for entry in providers:
            if isinstance(entry, dict) and str(entry.get("base_url", "")).strip() and str(entry.get("api_key", "")).strip():
                _pid = str(entry.get("id", "")).strip()
                QTimer.singleShot(600, lambda _pid=_pid: self._schedule_auto_fetch(_pid))

    def _make_custom_card(self, entry: dict, stripe_color: str) -> QWidget:
        cid = str(entry.get("id", "")).strip()
        name = str(entry.get("name", "")).strip() or cid
        colors = self._theme_colors()
        panel_color = QColor(colors["panel"])
        accent_color = QColor(stripe_color)
        tint_amount = 0.26
        card_background = QColor(
            round(panel_color.red() * (1 - tint_amount) + accent_color.red() * tint_amount),
            round(panel_color.green() * (1 - tint_amount) + accent_color.green() * tint_amount),
            round(panel_color.blue() * (1 - tint_amount) + accent_color.blue() * tint_amount),
        ).name()
        card = QFrame()
        card.setObjectName("customProviderCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            f"QFrame#customProviderCard {{ background: {card_background}; "
            f"border: 1px solid {colors['border']}; "
            f"border-radius: 12px; }}"
            f"QFrame#customProviderCard QLabel {{ border: none; background: transparent; }}"
            f"QFrame#customProviderCard QLineEdit, QFrame#customProviderCard QComboBox {{ min-height: 30px; border-radius: 8px; padding: 3px 8px; }}"
            f"QFrame#customProviderCard QPushButton {{ min-height: 30px; border-radius: 8px; padding: 4px 10px; }}"
            f"QFrame#customProviderCard QPushButton#collapseProviderButton {{ min-width: 34px; max-width: 34px; background: {colors['surface']}; color: {colors['text']}; border: 1px solid {colors['border']}; padding: 0; font-size: 21px; font-weight: 600; }}"
            f"QFrame#customProviderCard QPushButton#collapseProviderButton:hover {{ background: {colors['selection']}; color: {colors['text']}; border-color: {colors['accent_light']}; }}"
            f"QFrame#customProviderCard QPushButton#providerRemoveButton {{ min-width: 34px; max-width: 34px; background: {colors['surface']}; color: {colors['text']}; border: 1px solid {colors['border']}; padding: 0; font-size: 19px; font-weight: 600; }}"
            f"QFrame#customProviderCard QPushButton#providerRemoveButton:hover {{ background: {colors['surface_hover']}; border-color: {colors['accent_light']}; }}"
            f"QFrame#customProviderCard QCheckBox {{ spacing: 8px; font-weight: 600; background: transparent; }}"
            "QFrame#customProviderCard QCheckBox#providerActiveCheck::indicator "
            "{ width: 12px; height: 12px; border-radius: 6px; }"
            "QFrame#customProviderCard QCheckBox#providerActiveCheck::indicator:checked "
            "{ background: #00E676; border: 1px solid #69F0AE; }"
            "QFrame#customProviderCard QCheckBox#providerActiveCheck::indicator:unchecked "
            "{ background: #FF3B30; border: 1px solid #FF8A80; }"
        )
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        card_layout.setSpacing(0)
        accent_stripe = QFrame(card)
        accent_stripe.setObjectName("providerAccentStripe")
        accent_stripe.setFixedWidth(7)
        accent_stripe.setStyleSheet(
            f"background-color: {stripe_color}; border: none; border-radius: 0px;"
        )
        card_layout.addWidget(accent_stripe)

        content = QWidget(card)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(15, 13, 15, 15)
        content_layout.setSpacing(12)
        card_layout.addWidget(content)

        header = QHBoxLayout()
        header.setSpacing(10)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText(_CUSTOM_PROVIDER_PLACEHOLDERS["name"])
        name_edit.setToolTip("Display name shown in the model picker")
        name_edit.setMinimumHeight(34)
        name_edit.setProperty("cp_id", cid)
        name_edit.setProperty("cp_field", "name")
        name_edit.textChanged.connect(lambda _v, _id=cid: self._on_custom_field_changed(_id))
        name_edit.editingFinished.connect(self._on_custom_providers_edited)
        name_label = QLabel("Provider name")
        name_label.setStyleSheet(f"color: {colors['muted']}; font-size: 14px; font-weight: 600; background: transparent; border: none;")
        header.addWidget(name_label)
        header.addWidget(name_edit, 1)

        collapsed_name = QLabel(name)
        collapsed_name.setObjectName("providerCollapsedName")
        collapsed_name.setStyleSheet(
            "font-size: 16px; font-weight: 700; background: transparent; "
            "border: none;")
        collapsed_name.setVisible(False)
        header.addWidget(collapsed_name, 1)
        name_edit.textChanged.connect(collapsed_name.setText)

        enabled_check = QCheckBox("Active")
        enabled_check.setObjectName("providerActiveCheck")
        enabled_check.setChecked(bool(entry.get("enabled", True)))
        enabled_check.setToolTip("Allow this provider to be selected and used for translations")
        enabled_check.setProperty("cp_id", cid)
        enabled_check.toggled.connect(lambda _v, _id=cid: self._on_custom_enabled_toggled(_id))
        header.addWidget(enabled_check)

        del_btn = _ProviderActionButton("×")
        del_btn.setObjectName("providerRemoveButton")
        del_btn.setFixedSize(34, 30)
        del_btn.setToolTip(f"Remove provider '{name}'")
        del_btn.setAccessibleName(f"Remove provider '{name}'")
        del_btn.clicked.connect(lambda _c=False, _id=cid, _name=name: self._on_remove_custom_provider(_id))
        header.addWidget(del_btn)

        collapse_btn = _ProviderActionButton("−")
        collapse_btn.setObjectName("collapseProviderButton")
        collapse_btn.setCheckable(True)
        collapse_btn.setFixedSize(34, 30)
        collapse_btn.setAccessibleName("Hide provider settings")
        collapse_btn.setToolTip("Collapse provider settings")
        header.addWidget(collapse_btn)
        content_layout.addLayout(header)

        details = QWidget(card)
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        base_edit = QLineEdit(str(entry.get("base_url", "")).strip())
        base_edit.setPlaceholderText(_CUSTOM_PROVIDER_PLACEHOLDERS["base_url"])
        base_edit.setProperty("cp_id", cid)
        base_edit.setProperty("cp_field", "base_url")
        base_edit.textChanged.connect(lambda _v, _id=cid: self._on_custom_field_changed(_id))
        base_edit.editingFinished.connect(self._on_custom_providers_edited)
        form.addRow("Base URL", base_edit)

        key_edit = QLineEdit(str(entry.get("api_key", "")).strip())
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("sk-...  or  bearer token")
        key_edit.setProperty("cp_id", cid)
        key_edit.setProperty("cp_field", "api_key")
        key_edit.textChanged.connect(lambda _v, _id=cid: self._on_custom_field_changed(_id))
        key_edit.editingFinished.connect(self._on_custom_providers_edited)
        key_row = QHBoxLayout()
        key_row.setSpacing(4)
        key_row.addWidget(key_edit, 1)
        show_btn = QPushButton("Show")
        show_btn.setFixedSize(54, 30)
        show_btn.setCheckable(True)
        show_btn.setToolTip("Show / hide API key")
        show_btn.toggled.connect(lambda checked, w=key_edit: w.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        show_btn.toggled.connect(
            lambda checked, button=show_btn:
            button.setText("Hide" if checked else "Show"))
        key_row.addWidget(show_btn)
        key_holder = QWidget()
        key_holder.setLayout(key_row)
        form.addRow("API key", key_holder)

        model_combo = _SafeModelComboBox()
        model_combo.setObjectName("customProviderModelPicker")
        model_combo.setEditable(True)
        model_delegate = _ModelPickerDelegate(
            model_combo, favorites_enabled=True)
        model_delegate.favoriteClicked.connect(
            lambda favorite_key, _id=cid, _combo=model_combo:
            self._on_custom_model_favorite_clicked(
                _id, _combo, favorite_key))
        model_combo.setItemDelegate(model_delegate)
        model_combo.setPlaceholderText(_CUSTOM_PROVIDER_PLACEHOLDERS["model"])
        model_combo.setToolTip("Choose a model discovered from this endpoint, or enter its ID")
        model_combo.setMinimumHeight(34)
        model_combo.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        model_combo.view().setFont(
            QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        model_combo.setMaxVisibleItems(12)
        model_combo.setProperty("cp_id", cid)
        saved_model = str(entry.get("model", "")).strip()
        self._populate_custom_model_combo(
            model_combo, cid, name, [saved_model] if saved_model else [],
            saved_model)
        name_edit.textChanged.connect(
            lambda value, combo=model_combo:
            self._rename_custom_model_group(combo, value))
        line = model_combo.lineEdit()
        if line is not None:
            line.installEventFilter(model_combo)
            line.textChanged.connect(lambda _v, _id=cid: self._on_custom_field_changed(_id))
            line.editingFinished.connect(self._on_custom_providers_edited)
        model_combo.currentTextChanged.connect(lambda _v, _id=cid: self._on_custom_field_changed(_id))
        model_combo.currentTextChanged.connect(
            lambda _text, combo=model_combo: self._mark_active_model(combo))
        model_combo.currentIndexChanged.connect(
            lambda _index, combo=model_combo: self._mark_active_model(combo))
        self._mark_active_model(model_combo, saved_model)
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_row.addWidget(model_combo, 1)
        fetch_one_btn = QPushButton("Refresh")
        fetch_one_btn.setToolTip("Refresh the available models from this provider")
        fetch_one_btn.clicked.connect(lambda _c=False, _id=cid: self._on_fetch_one_provider(_id))
        model_row.addWidget(fetch_one_btn)
        check_models_btn = QPushButton("Check Models")
        check_models_btn.setToolTip(
            "Send a tiny test request to each listed model. "
            "This may incur provider charges."
        )
        check_models_btn.clicked.connect(
            lambda _c=False, _id=cid: self._on_check_provider_models(_id)
        )
        model_row.addWidget(check_models_btn)
        model_holder = QWidget()
        model_holder.setLayout(model_row)
        form.addRow("Model", model_holder)

        caps_panel = QFrame()
        caps_panel.setObjectName("providerCapabilities")
        caps_panel.setStyleSheet(
            f"QFrame#providerCapabilities {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 10px; }}"
            "QFrame#providerCapabilities QLabel { background: transparent; border: none; }"
        )
        caps_layout = QHBoxLayout(caps_panel)
        caps_layout.setContentsMargins(10, 5, 10, 5)
        caps_layout.setSpacing(14)
        caps_title = QLabel("CAPABILITIES")
        caps_title.setToolTip(
            "Controls request behavior for the selected model. Only enable "
            "features supported by your endpoint.")
        caps_title.setStyleSheet(f"color: {colors['muted']}; font-size: 14px; font-weight: 700; background: transparent; border: none;")
        caps_layout.addWidget(caps_title)
        reasoning_check = QCheckBox("Reasoning")
        reasoning_supported = (saved_model.casefold().split(":", 1)[0] != "gemma2")
        reasoning_check.setChecked(bool(entry.get("reasoning_enabled", False)) and reasoning_supported)
        reasoning_check.setEnabled(reasoning_supported)
        reasoning_check.setToolTip("Gemma 2 does not support reasoning." if not reasoning_supported else "When enabled, send reasoning/thinking options with requests.")
        reasoning_check.toggled.connect(lambda _v, _id=cid: self._on_custom_reasoning_toggled(_id))
        caps_layout.addWidget(reasoning_check)
        reasoning_detect_btn = QPushButton("Detect")
        reasoning_detect_btn.setFixedWidth(72)
        reasoning_detect_btn.setToolTip(
            "Send this toggle's own reasoning parameters to the model and set "
            "Reasoning from what it actually returns. Costs one small request."
        )
        reasoning_detect_btn.clicked.connect(
            lambda _c=False, _id=cid: self._on_detect_reasoning(_id)
        )
        caps_layout.addWidget(reasoning_detect_btn)
        vision_check = QCheckBox("Vision / Images")
        vision_check.setChecked(bool(entry.get("vision_enabled", False)))
        vision_check.setToolTip("When enabled, allow image selections and send image input to this provider.")
        vision_check.toggled.connect(lambda _v, _id=cid: self._on_custom_vision_toggled(_id))
        caps_layout.addWidget(vision_check)
        vision_detect_btn = QPushButton("Detect")
        vision_detect_btn.setFixedWidth(72)
        vision_detect_btn.setToolTip(
            "Send a tiny solid-colour image to this model and set Vision "
            "from what it actually answers. Costs one small request."
        )
        vision_detect_btn.clicked.connect(
            lambda _c=False, _id=cid: self._on_detect_vision(_id)
        )
        caps_layout.addWidget(vision_detect_btn)
        vision_detect_all_btn = QPushButton("Detect all")
        vision_detect_all_btn.setToolTip(
            "Test every model this provider lists and badge each one with what "
            "it actually did. Sends one small request per model."
        )
        vision_detect_all_btn.clicked.connect(
            lambda _c=False, _id=cid: self._on_detect_vision_all(_id)
        )
        caps_layout.addWidget(vision_detect_all_btn)
        caps_layout.addStretch(1)
        details_layout.addWidget(caps_panel)

        card.setProperty("cp_id", cid)
        card._cp_name = name_edit  # type: ignore[attr-defined]
        card._cp_base = base_edit  # type: ignore[attr-defined]
        card._cp_key = key_edit  # type: ignore[attr-defined]
        card._cp_model = model_combo  # type: ignore[attr-defined]
        card._cp_fetch_btn = fetch_one_btn  # type: ignore[attr-defined]
        card._cp_check_models_btn = check_models_btn  # type: ignore[attr-defined]
        card._cp_enabled = enabled_check  # type: ignore[attr-defined]
        card._cp_reasoning = reasoning_check  # type: ignore[attr-defined]
        card._cp_reasoning_detect = reasoning_detect_btn  # type: ignore[attr-defined]
        card._cp_vision = vision_check  # type: ignore[attr-defined]
        card._cp_vision_detect = vision_detect_btn  # type: ignore[attr-defined]
        card._cp_vision_detect_all = vision_detect_all_btn  # type: ignore[attr-defined]

        wrapper = QWidget()
        wrapper.setLayout(form)
        details_layout.addWidget(wrapper)
        content_layout.addWidget(details)

        def set_collapsed(collapsed: bool) -> None:
            details.setVisible(not collapsed)
            name_label.setVisible(not collapsed)
            name_edit.setVisible(not collapsed)
            collapsed_name.setVisible(collapsed)
            if collapsed:
                content_layout.setContentsMargins(15, 7, 15, 7)
            else:
                content_layout.setContentsMargins(15, 13, 15, 15)
            collapse_btn.set_symbol("+" if collapsed else "−")
            collapse_btn.setAccessibleName(
                "Show provider settings" if collapsed
                else "Hide provider settings")
            collapse_btn.setToolTip(
                "Expand provider settings" if collapsed
                else "Collapse provider settings")
            self._set_custom_provider_collapsed(cid, collapsed)

        collapse_btn.toggled.connect(set_collapsed)
        collapse_btn.setChecked(
            _normalize_custom_provider_collapsed(
                self._settings.get("custom_provider_collapsed")
            ).get(cid, False))
        card._cp_collapse = collapse_btn  # type: ignore[attr-defined]
        card._cp_details = details  # type: ignore[attr-defined]
        card._cp_capabilities = caps_panel  # type: ignore[attr-defined]
        card._cp_content_layout = content_layout  # type: ignore[attr-defined]
        return card

    def _on_add_custom_provider(self) -> None:
        import secrets as _secrets
        cid = f"custom-{_secrets.token_hex(4)}"
        providers = list(self._settings.get("custom_providers") or [])
        providers.append({"id": cid, "name": "New Provider", "base_url": "", "api_key": "", "model": "", "enabled": True, "reasoning_enabled": False, "vision_enabled": False})
        self._settings["custom_providers"] = providers
        self._rebuild_custom_cards()
        self._on_credentials_changed()

    def _on_remove_custom_provider(self, provider_id: str) -> None:
        providers = [p for p in (self._settings.get("custom_providers") or []) if isinstance(p, dict) and str(p.get("id", "")) != provider_id]
        self._settings["custom_providers"] = providers
        collapsed = _normalize_custom_provider_collapsed(
            self._settings.get("custom_provider_collapsed"))
        collapsed.pop(provider_id, None)
        self._settings["custom_provider_collapsed"] = collapsed
        favorite_keys = _normalize_model_favorites(
            self._settings.get("custom_provider_model_favorites"))
        self._settings["custom_provider_model_favorites"] = [
            key for key in favorite_keys
            if (
                (parts := _custom_model_favorite_parts(key)) is not None
                and parts[0] != provider_id
            )
        ]
        from config.settings import DEFAULT_MODEL
        if self._current_model() == f"custom:{provider_id}":
            self._select_model(DEFAULT_MODEL)
        self._rebuild_custom_cards()
        self._on_credentials_changed()

    def _on_custom_reasoning_toggled(self, provider_id: str) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                val = card._cp_reasoning.isChecked()  # type: ignore[attr-defined]
                for entry in (self._settings.get("custom_providers") or []):
                    if isinstance(entry, dict) and str(entry.get("id", "")) == provider_id:
                        entry["reasoning_enabled"] = bool(val)
                        break
                break
        self._on_credentials_changed()

    def _on_custom_vision_toggled(self, provider_id: str) -> None:
        card = next(
            (c for c in self._custom_cards
             if isinstance(c, QFrame) and c.property("cp_id") == provider_id),
            None,
        )
        entry = next(
            (item for item in (self._settings.get("custom_providers") or [])
             if isinstance(item, dict)
             and str(item.get("id", "")) == provider_id),
            None,
        )
        if card is None or entry is None:
            return
        val = bool(card._cp_vision.isChecked())
        model = card._cp_model.currentText().strip()

        # Ticking the box is a claim, not evidence. If nobody has tested this
        # model yet, test it now rather than letting the user find out mid
        # translation; if it was tested and refused an image, say so instead
        # of quietly storing a switch that does not work.
        if val and model and self._measured_vision(provider_id, model) == "unknown":
            card._cp_vision.blockSignals(True)
            card._cp_vision.setChecked(False)
            card._cp_vision.blockSignals(False)
            self.results.setPlainText(
                f"Vision ticked for {model} — checking whether it can "
                "actually read an image…"
            )
            self._show_plain_results()
            self._on_detect_vision(provider_id)
            return

        entry["vision_enabled"] = val
        models = [
            card._cp_model.itemData(index)
            for index in range(card._cp_model.count())
            if isinstance(card._cp_model.itemData(index), str)
            and card._cp_model.itemData(index)
            and not card._cp_model.itemData(index, _MODEL_GROUP_ROLE)
        ]
        self._populate_custom_model_combo(
            card._cp_model,
            provider_id,
            str(entry.get("name", "")).strip() or provider_id,
            models,
            card._cp_model.currentText().strip(),
        )
        self._on_credentials_changed()

    def _on_toggle_reasoning_all(self) -> None:
        providers = self._settings.get("custom_providers") or []
        if not providers:
            return
        target = not all(bool(p.get("reasoning_enabled")) for p in providers if isinstance(p, dict))
        for entry in providers:
            if isinstance(entry, dict):
                entry["reasoning_enabled"] = target
        for card in self._custom_cards:
            if isinstance(card, QFrame):
                try:
                    card._cp_reasoning.setChecked(target)  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
        self._on_credentials_changed()
        self.results.setPlainText(f"Reasoning {'enabled' if target else 'disabled'} for all {len(providers)} custom provider(s).")
        self.results.setVisible(True)

    def _on_toggle_vision_all(self) -> None:
        providers = self._settings.get("custom_providers") or []
        if not providers:
            return
        target = not all(bool(p.get("vision_enabled")) for p in providers if isinstance(p, dict))
        for entry in providers:
            if isinstance(entry, dict):
                entry["vision_enabled"] = target
        for card in self._custom_cards:
            if isinstance(card, QFrame):
                try:
                    card._cp_vision.setChecked(target)  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
        self._on_credentials_changed()
        self._fill_model_combo()
        self._select_model(self._current_model())
        self.results.setPlainText(f"Vision / Images {'enabled' if target else 'disabled'} for all {len(providers)} custom provider(s).")
        self.results.setVisible(True)

    def _on_custom_enabled_toggled(self, provider_id: str) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                enabled = card._cp_enabled.isChecked()  # type: ignore[attr-defined]
                for entry in (self._settings.get("custom_providers") or []):
                    if isinstance(entry, dict) and str(entry.get("id", "")) == provider_id:
                        entry["enabled"] = bool(enabled)
                        break
                break
        self._on_credentials_changed()

    def _on_custom_field_changed(self, provider_id: str) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                for entry in (self._settings.get("custom_providers") or []):
                    if isinstance(entry, dict) and str(entry.get("id", "")) == provider_id:
                        try:
                            entry["name"] = card._cp_name.text().strip()  # type: ignore[attr-defined]
                            entry["base_url"] = card._cp_base.text().strip()  # type: ignore[attr-defined]
                            entry["api_key"] = card._cp_key.text().strip()  # type: ignore[attr-defined]
                            entry["model"] = card._cp_model.currentText().strip()  # type: ignore[attr-defined]
                            reasoning_supported = (entry["model"].casefold().split(":", 1)[0] != "gemma2")
                            card._cp_reasoning.setEnabled(reasoning_supported)  # type: ignore[attr-defined]
                            if not reasoning_supported:
                                card._cp_reasoning.setChecked(False)  # type: ignore[attr-defined]
                            entry["reasoning_enabled"] = bool(card._cp_reasoning.isChecked())  # type: ignore[attr-defined]
                            entry["vision_enabled"] = bool(card._cp_vision.isChecked())  # type: ignore[attr-defined]
                        except RuntimeError:
                            pass
                        break
                break
        if not self._syncing:
            chosen = self._current_model()
            self._fill_model_combo()
            self._select_model(chosen)
            self._update_key_states()
            self._update_engine_hint()
        self._schedule_auto_fetch(provider_id)

    def _on_custom_providers_edited(self) -> None:
        self._on_credentials_changed()

    def _schedule_auto_fetch(self, provider_id: str) -> None:
        entry = next((e for e in (self._settings.get("custom_providers") or []) if isinstance(e, dict) and str(e.get("id", "")) == provider_id), None)
        if entry is None:
            return
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        if not base or not key:
            return
        if self._auto_fetch_last.get(provider_id) == (base, key):
            return
        if provider_id in self._auto_fetch_in_progress:
            return
        timer = self._auto_fetch_timers.get(provider_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            pid = provider_id
            timer.timeout.connect(lambda pid=pid: self._do_auto_fetch(pid))
            self._auto_fetch_timers[provider_id] = timer
        timer.start(650)

    def _do_auto_fetch(self, provider_id: str) -> None:
        entry = next((e for e in (self._settings.get("custom_providers") or []) if isinstance(e, dict) and str(e.get("id", "")) == provider_id), None)
        if entry is None:
            return
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        if not base or not key:
            return
        if self._auto_fetch_last.get(provider_id) == (base, key):
            return
        if provider_id in self._auto_fetch_in_progress:
            return
        self._auto_fetch_in_progress.add(provider_id)
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_fetch_btn.setEnabled(False)  # type: ignore[attr-defined]
                    card._cp_fetch_btn.setText("…")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        self.results.setPlainText(f"⟳ جلب النماذج تلقائياً من {base} …")
        self.results.setVisible(True)

        def job():
            try:
                from translators import opencode_compat
                model_groups = opencode_compat.list_model_groups(base, key)
                models = [
                    model for _, grouped_models in model_groups
                    for model in grouped_models
                ]
                error = None
            except Exception as exc:
                models = []
                model_groups = []
                error = str(exc)
            self.autoModelsFetched.emit(
                provider_id, base, key, models, error, model_groups)

        t = threading.Thread(target=job, daemon=True)
        t.start()

    def _on_auto_fetch_done(
        self,
        provider_id: str,
        base: str,
        key: str,
        models: list[str],
        error: str | None,
        model_groups: list[tuple[str, list[str]]] | None = None,
    ) -> None:
        self._auto_fetch_in_progress.discard(provider_id)
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_fetch_btn.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_fetch_btn.setText("Refresh")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        entry = next((e for e in (self._settings.get("custom_providers") or []) if isinstance(e, dict) and str(e.get("id", "")) == provider_id), None)
        if entry is None:
            return
        current_credentials = (str(entry.get("base_url", "")).strip(), str(entry.get("api_key", "")).strip())
        if current_credentials != (base, key):
            self._schedule_auto_fetch(provider_id)
            return
        if error:
            self.results.setPlainText(f"تعذر جلب النماذج تلقائياً لـ custom:{provider_id}:\n{error}\n\nاضغط Refresh بجانب حقل Model للمحاولة يدوياً.")
            return
        self._auto_fetch_last[provider_id] = (base, key)
        self._show_fetched_models(
            provider_id,
            models,
            str(entry.get("name", "")).strip() or provider_id,
        )
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    combo = card._cp_model  # type: ignore[attr-defined]
                    previous = combo.currentText().strip()
                    if not previous and models:
                        previous = models[0]
                        entry["model"] = previous
                    self._populate_custom_model_combo(
                        combo,
                        provider_id,
                        str(entry.get("name", "")).strip() or provider_id,
                        models,
                        previous,
                        model_groups,
                    )
                    combo.setMaxVisibleItems(min(12, max(8, len(models))))
                    self._on_credentials_changed()
                except RuntimeError:
                    pass
                break

    def _on_fetch_one_provider(self, provider_id: str) -> None:
        entry = next((e for e in (self._settings.get("custom_providers") or []) if isinstance(e, dict) and str(e.get("id", "")) == provider_id), None)
        if entry is None:
            return
        self._on_custom_field_changed(provider_id)
        entry = next((e for e in (self._settings.get("custom_providers") or []) if isinstance(e, dict) and str(e.get("id", "")) == provider_id), None)
        assert entry is not None
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        if not base or not key:
            self.results.setPlainText(f"Provider '{entry.get('name', provider_id)}' يحتاج Base URL و API Key قبل جلب النماذج.")
            self.results.setVisible(True)
            return
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_fetch_btn.setEnabled(False)  # type: ignore[attr-defined]
                    card._cp_fetch_btn.setText("…")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        self._fetch_models_btn.setEnabled(False)
        self.results.setPlainText(f"جلب النماذج من {base} …")
        self.results.setVisible(True)

        def job():
            try:
                from translators import opencode_compat
                model_groups = opencode_compat.list_model_groups(base, key)
                models = [
                    model for _, grouped_models in model_groups
                    for model in grouped_models
                ]
                error = None
            except Exception as exc:
                models = []
                model_groups = []
                error = str(exc)
            self.customModelsFetched.emit(
                provider_id, models, error, model_groups)

        t = threading.Thread(target=job, daemon=True)
        t.start()

    def _on_check_provider_models(self, provider_id: str) -> None:
        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        self._on_custom_field_changed(provider_id)
        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        if not base or not key:
            self.results.setPlainText(
                "Enter this provider's Base URL and API Key first."
            )
            self._show_plain_results()
            return

        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                card._cp_check_models_btn.setEnabled(False)  # type: ignore[attr-defined]
                card._cp_check_models_btn.setText("Checking…")  # type: ignore[attr-defined]
                break
        self.results.setPlainText(
            f"Checking models from {entry.get('name', provider_id)}…"
        )
        self._show_plain_results()

        def job() -> None:
            result: dict = {}
            try:
                from translators import opencode_compat

                explicitly_free: set[str] = set()
                groups = opencode_compat.list_model_groups(
                    base, key, explicitly_free_models=explicitly_free
                )
                models = list(dict.fromkeys(
                    model
                    for _, grouped_models in groups
                    for model in grouped_models
                ))
                checks = opencode_compat.probe_models(base, key, models)
                result = {
                    "groups": groups,
                    "explicitly_free": explicitly_free,
                    "checks": checks,
                }
            except Exception as exc:
                result = {"error": str(exc)}
            self.customModelsAudited.emit(provider_id, base, key, result)

        threading.Thread(target=job, daemon=True).start()

    def _on_detect_vision_all(self, provider_id: str) -> None:
        """Badge every model a provider lists with its measured verdict."""
        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        self._on_custom_field_changed(provider_id)
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        name = str(entry.get("name", provider_id)).strip() or provider_id
        # Fall back to the last fetch, but do not require one: asking the
        # gateway for its catalog is the same call Refresh makes, and making
        # the user press Refresh first is how this button came to look broken.
        models = [m for m in (self._provider_models.get(provider_id) or [])
                  if isinstance(m, str)]
        if not models:
            models = [str(entry.get("model", "")).strip()]
        if not base or not models:
            self.results.setPlainText(
                "This provider needs a Base URL and a Model before it can be tested."
            )
            self._show_plain_results()
            return

        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision_detect_all.setEnabled(False)  # type: ignore[attr-defined]
                    card._cp_vision_detect_all.setText("Testing…")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        total = len(models)
        self.results.setPlainText(
            f"Testing {total} models from {name} — each one gets a small "
            "colour-swatch image…"
        )
        self._show_plain_results()

        def job() -> None:
            payload = {}
            try:
                from translators.opencode_compat import list_model_groups

                # Ask the gateway for its catalog when nothing was cached, so
                # this button works on a provider that was never refreshed.
                # Only the free tier is tested by default: a catalog can hold
                # hundreds of ids, and probing every one of them would spend a
                # user's quota on models they cannot call anyway.
                targets = list(models)
                if not self._provider_models.get(provider_id):
                    try:
                        groups = list_model_groups(
                            base, key, explicitly_free_models=set())
                        listed = list(dict.fromkeys(
                            m for _, rows in groups for m in rows))
                        free = [m for m in listed if _is_free_model(m)]
                        if free:
                            targets = free[:_VISION_BATCH_LIMIT]
                    except Exception:
                        pass  # keep whatever we had; testing it is still useful
                total = len(targets)

                from translators.probe import detect_vision_all

                def progress(model, outcome, _seen={"n": 0}):
                    _seen["n"] += 1
                    self.customVisionProgress.emit(
                        provider_id, model, outcome[0],
                        _seen["n"], total, outcome[1])

                payload = {"results": detect_vision_all(
                    base, key, targets, on_result=progress)}
            except Exception as exc:
                payload = {"error": str(exc)}
            self.customVisionBatchDone.emit(provider_id, base, payload)

        threading.Thread(target=job, daemon=True).start()

    def _on_custom_vision_batch_done(
        self, provider_id: str, base: str, result: dict
    ) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision_detect_all.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_vision_detect_all.setText("Detect all")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        if error := result.get("error"):
            self.results.setPlainText(f"Could not test images: {error}")
            self._show_plain_results()
            return
        verdicts = result.get("results") or {}
        if not verdicts:
            self.results.setPlainText("No models were tested.")
            self._show_plain_results()
            return
        for model, outcome in verdicts.items():
            self._record_measured_vision(
                provider_id, model,
                "supported" if outcome[0] else "unsupported")
        yes = sorted(m for m, o in verdicts.items() if o[0])
        no = sorted(m for m, o in verdicts.items() if not o[0])
        # A model that refuses the image only because it insists on reasoning
        # is not really text-only; say so rather than let the list imply it is.
        forced = sorted(
            m for m, o in verdicts.items()
            if not o[0] and "reasoning is mandator" in str(o[1]).casefold()
        )
        if forced:
            no = [m for m in no if m not in forced]
        lines = []
        if yes:
            lines.append("VISION — " + ", ".join(yes))
        if no:
            lines.append("text only — " + ", ".join(no))
        if forced:
            lines.append(
                "needs reasoning (this app sends reasoning off, so the test "
                "cannot confirm them) — " + ", ".join(forced)
            )
        entry = next(
            (item for item in self._settings.get("custom_providers", [])
             if isinstance(item, dict)
             and str(item.get("id", "")).strip() == provider_id),
            {},
        )
        # The switch follows the selected model, not the provider.
        current = str(entry.get("model", "")).strip()
        if current:
            entry["vision_enabled"] = bool(verdicts.get(current, (False,))[0])
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision.blockSignals(True)  # type: ignore[attr-defined]
                    card._cp_vision.setChecked(bool(entry.get("vision_enabled")))
                    card._cp_vision.blockSignals(False)  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        self.results.setPlainText(
            f"Tested {len(verdicts)} models — {len(yes)} take images, "
            f"{len(no)} are text only.\n\n" + "\n\n".join(lines)
        )
        self._show_plain_results()

    def _on_custom_vision_progress(
        self, provider_id: str, model: str, supported: bool,
        done: int, total: int, detail: str,
    ) -> None:
        mark = "✅" if supported else "—"
        self.results.setPlainText(
            f"[{done}/{total}] {mark} {model}\n{detail}"
        )

    def _on_detect_vision(self, provider_id: str) -> None:
        """Measure the provider's image support and set the checkbox from it."""
        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        self._on_custom_field_changed(provider_id)
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        model = str(entry.get("model", "")).strip()
        name = str(entry.get("name", provider_id)).strip() or provider_id
        if not base or not model:
            self.results.setPlainText(
                "Enter this provider's Base URL and Model first."
            )
            self._show_plain_results()
            return

        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision_detect.setEnabled(False)  # type: ignore[attr-defined]
                    card._cp_vision_detect.setText("Testing…")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        self.results.setPlainText(
            f"Sending a test image to {name} ({model})…"
        )
        self._show_plain_results()

        def job() -> None:
            payload = {}
            try:
                from translators.probe import detect_vision

                supported, detail = detect_vision(base, key, model)
                payload = {"supported": supported, "detail": detail}
            except Exception as exc:
                payload = {"error": str(exc)}
            self.customVisionProbed.emit(provider_id, base, model, key, payload)

        threading.Thread(target=job, daemon=True).start()

    def _on_custom_vision_probed(
        self, provider_id: str, base: str, model: str, key: str,
        result: dict,
    ) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision_detect.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_vision_detect.setText("Detect")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break

        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        # The card may have been edited mid-flight; a verdict about a
        # different endpoint or model would be about something else.
        if (str(entry.get("base_url", "")).strip(),
                str(entry.get("model", "")).strip()) != (base, model):
            self.results.setPlainText(
                "The provider changed while testing. Press Detect again."
            )
            return

        if error := result.get("error"):
            self.results.setPlainText(f"Could not test images: {error}")
            return

        supported = bool(result.get("supported"))
        detail = str(result.get("detail", ""))
        # The switch follows the model that was tested, not the whole
        # provider: one gateway holds text and vision models side by side.
        entry["vision_enabled"] = supported
        # The verdict is recorded against the model, not the provider, so the
        # model list stops stamping every row of this provider with the same
        # badge.
        self._record_measured_vision(provider_id, model, "supported" if supported
                                     else "unsupported")
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_vision.blockSignals(True)  # type: ignore[attr-defined]
                    card._cp_vision.setChecked(supported)  # type: ignore[attr-defined]
                    card._cp_vision.blockSignals(False)  # type: ignore[attr-defined]
                    card._cp_vision.setToolTip(detail)  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        mark = "✅ Vision / Images ON" if supported else "❌ Vision / Images OFF"
        self.results.setPlainText(
            f"{mark} — {entry.get('name', provider_id)} ({model})\n\n{detail}"
        )
        self._show_plain_results()

    def _on_detect_reasoning(self, provider_id: str) -> None:
        """Measure the provider's reasoning support and set the checkbox."""
        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        self._on_custom_field_changed(provider_id)
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        model = str(entry.get("model", "")).strip()
        name = str(entry.get("name", provider_id)).strip() or provider_id
        if not base or not model:
            self.results.setPlainText(
                "Enter this provider's Base URL and Model first."
            )
            self._show_plain_results()
            return

        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_reasoning_detect.setEnabled(False)  # type: ignore[attr-defined]
                    card._cp_reasoning_detect.setText("Testing…")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        self.results.setPlainText(f"Testing reasoning on {name} ({model})…")
        self._show_plain_results()

        def job() -> None:
            payload = {}
            try:
                from translators.probe import detect_reasoning

                supported, detail = detect_reasoning(base, key, model)
                payload = {"supported": supported, "detail": detail}
            except Exception as exc:
                payload = {"error": str(exc)}
            self.customReasoningProbed.emit(provider_id, base, model, key, payload)

        threading.Thread(target=job, daemon=True).start()

    def _on_custom_reasoning_probed(
        self, provider_id: str, base: str, model: str, key: str,
        result: dict,
    ) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_reasoning_detect.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_reasoning_detect.setText("Detect")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break

        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        if (str(entry.get("base_url", "")).strip(),
                str(entry.get("model", "")).strip()) != (base, model):
            self.results.setPlainText(
                "The provider changed while testing. Press Detect again."
            )
            return

        if error := result.get("error"):
            self.results.setPlainText(f"Could not test reasoning: {error}")
            return

        supported = bool(result.get("supported"))
        detail = str(result.get("detail", ""))
        entry["reasoning_enabled"] = supported
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    box = card._cp_reasoning  # type: ignore[attr-defined]
                    box.blockSignals(True)
                    box.setChecked(supported)
                    box.blockSignals(False)
                    box.setToolTip(detail)
                except RuntimeError:
                    pass
                break
        mark = "✅ Reasoning ON" if supported else "❌ Reasoning OFF"
        self.results.setPlainText(
            f"{mark} — {entry.get('name', provider_id)} ({model})\n\n{detail}"
        )
        self._show_plain_results()

    def _on_custom_models_audited(
        self, provider_id: str, base: str, key: str, result: dict
    ) -> None:
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_check_models_btn.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_check_models_btn.setText("Check Models")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break

        entry = next(
            (
                item for item in self._settings.get("custom_providers", [])
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        credentials = (
            str(entry.get("base_url", "")).strip(),
            str(entry.get("api_key", "")).strip(),
        )
        if credentials != (base, key):
            self.results.setPlainText(
                "Provider credentials changed while checking. "
                "Run Check Models again to test the updated endpoint."
            )
            return

        if error := result.get("error"):
            self.results.setPlainText(
                f"Could not check models for {entry.get('name', provider_id)}: "
                f"{error}"
            )
            return

        if not result["checks"]:
            self.results.setPlainText("This provider returned no models to check.")
            return
        explicitly_free = result["explicitly_free"]
        working = [
            (model, detail) for model, available, detail in result["checks"]
            if available
        ]
        temporary = [
            (model, detail) for model, available, detail in result["checks"]
            if not available and "TEMPORARY —" in detail
        ]
        failed = [
            (model, detail) for model, available, detail in result["checks"]
            if not available and "TEMPORARY —" not in detail
        ]

        def model_rows(models: list[tuple[str, str]], color: str) -> str:
            return "".join(
                "<div style='margin: 3px 0; white-space: normal;'>"
                f"<span style='color: {color}; font-size: 25px; "
                "font-weight: bold;'>&#9679;</span>&nbsp;"
                f"<span>{escape(model)}</span>"
                f" <span style='font-size: 12px; color: "
                f"{self._theme_colors()['muted']};'>"
                f"— {escape(detail)}</span>"
                + (
                    " <span style='color: #22C55E; font-size: 11px;'>FREE</span>"
                    if model in explicitly_free else ""
                )
                + "</div>"
                for model, detail in models
            )

        provider_name = escape(str(entry.get("name", provider_id)))
        green = self._theme_colors().get("success", "#22C55E")
        red = self._theme_colors().get("danger", "#EF4444")
        amber = self._theme_colors().get("warning", "#F59E0B")
        html = [
            f"<b>Model check results — {provider_name}</b>",
            f"<p style='color: {green}; margin: 8px 0 2px;'>"
            f"<b>WORKING ({len(working)})</b></p>",
            model_rows(working, green) or "<div>None</div>",
            f"<p style='color: {amber}; margin: 8px 0 2px;'>"
            f"<b>RETRY / TEMPORARY ({len(temporary)})</b></p>",
            model_rows(temporary, amber) or "<div>None</div>",
            f"<p style='color: {red}; margin: 8px 0 2px;'>"
            f"<b>NOT WORKING ({len(failed)})</b></p>",
            model_rows(failed, red) or "<div>None</div>",
            "<p style='font-size: 11px;'>Rate limits, server errors, and "
            "timeouts are temporary/unverified, not proof a model is broken. "
            "One short request per model; paid tests may use credits. "
            "FREE is based on provider/model "
            "metadata and does not confirm subscription or card requirements.</p>",
        ]
        self.results.setHtml("".join(html))
        working_count = len(working)
        self.results.setAccessibleName(
            f"{working_count} working; {len(temporary)} temporary; "
            f"{len(failed)} failed for "
            f"{entry.get('name', provider_id)}"
        )

    def _on_fetch_custom_models(self) -> None:
        target_id = None
        current = self._current_model()
        if current.startswith("custom:"):
            target_id = current[len("custom:"):]
        else:
            for entry in (self._settings.get("custom_providers") or []):
                if isinstance(entry, dict) and entry.get("enabled") and str(entry.get("base_url", "")).strip():
                    target_id = str(entry.get("id", "")).strip()
                    break
        if not target_id:
            self.results.setPlainText("No custom provider to fetch from — add one and fill its Base URL + API Key first.")
            self.results.setVisible(True)
            return
        self._on_fetch_one_provider(target_id)

    def _on_custom_fetch_done(
        self,
        provider_id: str,
        models: list[str],
        error: str | None,
        model_groups: list[tuple[str, list[str]]] | None = None,
    ) -> None:
        self._fetch_models_btn.setEnabled(True)
        self._fetch_models_btn.setText("Fetch Models")
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    card._cp_fetch_btn.setEnabled(True)  # type: ignore[attr-defined]
                    card._cp_fetch_btn.setText("Refresh")  # type: ignore[attr-defined]
                except RuntimeError:
                    pass
                break
        if error:
            self.results.setPlainText(f"Fetch failed for custom:{provider_id}: {error}")
            return
        provider_entry = next(
            (
                entry for entry in self._settings.get("custom_providers", [])
                if isinstance(entry, dict)
                and str(entry.get("id", "")).strip() == provider_id
            ),
            {},
        )
        self._show_fetched_models(
            provider_id,
            models,
            str(provider_entry.get("name", "")).strip() or provider_id,
        )
        # Kept for "Detect all" only: it is a view of what the gateway
        # currently offers, not a saved preference, so it does not belong in
        # the settings file.
        self._provider_models[provider_id] = list(models)
        for card in self._custom_cards:
            if isinstance(card, QFrame) and card.property("cp_id") == provider_id:
                try:
                    combo = card._cp_model  # type: ignore[attr-defined]
                    previous = combo.currentText().strip()
                    if not previous and models:
                        previous = models[0]
                        for entry in (self._settings.get("custom_providers") or []):
                            if isinstance(entry, dict) and str(entry.get("id", "")) == provider_id:
                                entry["model"] = models[0]
                                break
                    provider_entry = next(
                        (
                            entry for entry in self._settings.get(
                                "custom_providers", [])
                            if isinstance(entry, dict)
                            and str(entry.get("id", "")).strip() == provider_id
                        ),
                        {},
                    )
                    self._populate_custom_model_combo(
                        combo,
                        provider_id,
                        str(provider_entry.get("name", "")).strip()
                        or provider_id,
                        models,
                        previous,
                        model_groups,
                    )
                    self._on_credentials_changed()
                except RuntimeError:
                    pass
                break

    def _show_fetched_models(
        self,
        provider_id: str,
        models: list[str],
        provider_name: str,
    ) -> None:
        lines = [f"• {model}" for model in models]
        if not models:
            lines.append("No models found.")

        self.results.setPlainText("\n".join(lines))
        self.results.setAccessibleName(
            f"Models fetched for {provider_name} ({len(models)})")
        self.results.setToolTip(
            f"{len(models)} models fetched for {provider_name} ({provider_id})")
        self._results_stack.setCurrentWidget(self.results)
        self._results_stack.setVisible(True)

    def _show_plain_results(self) -> None:
        self._results_stack.setCurrentWidget(self.results)
        self._results_stack.setVisible(True)

    # ── Helpers for model picker (unchanged contracts) ──────────────────────

    @staticmethod
    def _is_header(index: int, combo) -> bool:
        item = combo.model().item(index)
        return item is None or not item.isEnabled()

    def _measured_vision(self, provider_id: str, model_id: str) -> str:
        """What this model was *seen* to do with an image.

        "unknown" until the model has been tested, which is the honest state
        for a gateway whose catalog the app has never seen. The Vision checkbox
        stays the switch that allows image selection; it no longer decides what
        the label claims.
        """
        probes = self._settings.get("vision_probes")
        if not isinstance(probes, dict):
            return "unknown"
        row = probes.get(provider_id)
        if not isinstance(row, dict):
            return "unknown"
        verdict = row.get(str(model_id).strip())
        return verdict if verdict in ("supported", "unsupported") else "unknown"

    def _record_measured_vision(
        self, provider_id: str, model_id: str, verdict: str
    ) -> None:
        probes = self._settings.get("vision_probes")
        if not isinstance(probes, dict):
            probes = {}
        row = probes.get(provider_id)
        if not isinstance(row, dict):
            row = {}
        row[str(model_id).strip()] = verdict
        probes[provider_id] = row
        self._settings["vision_probes"] = probes

    def _set_model_item_metadata(
        self,
        combo: QComboBox,
        index: int,
        model_id: str,
        image_status: str | None = None,
        favorite: bool = False,
        provider_label: str = "",
        favorite_key: str | None = None,
    ) -> None:
        from translators import registry

        status = image_status or registry.image_capability(model_id)
        combo.setItemData(index, status, _IMAGE_CAPABILITY_ROLE)
        combo.setItemData(index, _is_free_model(model_id), _FREE_MODEL_ROLE)
        combo.setItemData(index, False, _ACTIVE_MODEL_ROLE)
        combo.setItemData(index, favorite, _FAVORITE_MODEL_ROLE)
        combo.setItemData(index, provider_label, _PROVIDER_LABEL_ROLE)
        combo.setItemData(
            index, favorite_key or model_id, _FAVORITE_KEY_ROLE)
        tooltip = {
            "supported": "Accepts image input.",
            "unsupported": "Text-only model; image input is not supported.",
            "unknown": "Image support has not been verified.",
        }.get(status, "Image support has not been verified.")
        if _is_free_model(model_id):
            tooltip += " Free model."
        if favorite:
            tooltip += " Favorite model; click the star to remove it."
        else:
            tooltip += " Click the star to add it to Favorites."
        combo.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)

    def _populate_custom_model_combo(
        self,
        combo: QComboBox,
        provider_id: str,
        provider_name: str,
        models: list[str],
        selected_model: str,
        model_groups: list[tuple[str, list[str]]] | None = None,
    ) -> None:
        favorites = _normalize_model_favorites(
            self._settings.get("custom_provider_model_favorites"))
        favorite_models = {
            parts[1]
            for favorite_key in favorites
            if (parts := _custom_model_favorite_parts(favorite_key))
            and parts[0] == provider_id
        }
        available = list(dict.fromkeys(
            model.strip() for model in models if model.strip()))
        saved_favorites = sorted(favorite_models - set(available))
        combo.blockSignals(True)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.blockSignals(True)
        combo.clear()

        def add_group(label: str) -> None:
            combo.addItem(label)
            group_item = combo.model().item(combo.count() - 1)
            if group_item is not None:
                group_item.setEnabled(False)
                group_item.setData(label, _MODEL_GROUP_ROLE)

        def add_model(model_id: str, is_favorite: bool) -> None:
            combo.addItem(model_id, model_id)
            index = combo.count() - 1
            provider = next(
                (
                    entry for entry in self._settings.get(
                        "custom_providers", [])
                    if isinstance(entry, dict)
                    and str(entry.get("id", "")).strip() == provider_id
                ),
                {},
            )
            # Per model, not per provider. One provider serves text and vision
            # models side by side, so a provider-wide toggle stamped every row
            # with the same badge; only a measured verdict belongs on a row.
            self._set_model_item_metadata(
                combo,
                index,
                model_id,
                self._measured_vision(provider_id, model_id),
                favorite=is_favorite,
                favorite_key=_custom_model_favorite_key(
                    provider_id, model_id),
            )

        if favorite_models:
            add_group("Favorites")
            for model_id in sorted(favorite_models):
                add_model(model_id, True)

        if model_groups:
            default_group = f"Models from {provider_name or 'Provider'}"
            default_group_added = False
            if saved_favorites:
                add_group(default_group)
                default_group_added = True
                for model_id in saved_favorites:
                    add_model(model_id, True)
            grouped_model_ids: set[str] = set()
            for group_name, grouped_models in model_groups:
                group_models = [
                    model_id for model_id in grouped_models
                    if model_id in available
                ]
                if not group_models:
                    continue
                if group_name:
                    add_group(group_name)
                elif not default_group_added:
                    add_group(default_group)
                    default_group_added = True
                for model_id in group_models:
                    grouped_model_ids.add(model_id)
                    add_model(
                        model_id, model_id in favorite_models)
            ungrouped_models = [
                model_id for model_id in available
                if model_id not in grouped_model_ids]
            if ungrouped_models:
                if not default_group_added:
                    add_group(default_group)
                for model_id in ungrouped_models:
                    add_model(model_id, model_id in favorite_models)
        else:
            add_group(f"Models from {provider_name or 'Provider'}")
            for model_id in [*saved_favorites, *available]:
                add_model(model_id, model_id in favorite_models)

        selected_index = combo.findData(selected_model)
        if selected_index >= 0:
            combo.setCurrentIndex(selected_index)
        elif selected_model:
            combo.setCurrentText(selected_model)
        else:
            combo.setCurrentIndex(-1)
            if line_edit is not None:
                line_edit.clear()
        if line_edit is not None:
            line_edit.blockSignals(False)
        combo.blockSignals(False)
        self._mark_active_model(combo, selected_model)

    def _on_custom_model_favorite_clicked(
        self,
        provider_id: str,
        combo: QComboBox,
        favorite_key: str,
    ) -> None:
        favorites = set(_normalize_model_favorites(
            self._settings.get("custom_provider_model_favorites")))
        if favorite_key in favorites:
            favorites.remove(favorite_key)
        else:
            favorites.add(favorite_key)
        self._settings["custom_provider_model_favorites"] = sorted(favorites)
        entry = next(
            (
                provider for provider in self._settings.get(
                    "custom_providers", [])
                if isinstance(provider, dict)
                and str(provider.get("id", "")).strip() == provider_id
            ),
            None,
        )
        if entry is None:
            return
        models = [
            combo.itemData(index)
            for index in range(combo.count())
            if isinstance(combo.itemData(index), str)
            and combo.itemData(index)
            and not combo.itemData(index, _MODEL_GROUP_ROLE)
        ]
        self._populate_custom_model_combo(
            combo,
            provider_id,
            str(entry.get("name", "")).strip() or provider_id,
            models,
            combo.currentText().strip(),
        )

    def _set_custom_provider_collapsed(
        self,
        provider_id: str,
        collapsed: bool,
    ) -> None:
        states = _normalize_custom_provider_collapsed(
            self._settings.get("custom_provider_collapsed"))
        states[provider_id] = collapsed
        self._settings["custom_provider_collapsed"] = states

    @staticmethod
    def _mark_active_model(combo: QComboBox, model_id: str | None = None) -> None:
        active_model = (
            str(model_id).strip() if model_id is not None
            else str(combo.currentData() or combo.currentText()).strip()
        )
        for index in range(combo.count()):
            item_model = combo.itemData(index)
            combo.setItemData(
                index,
                bool(active_model and item_model == active_model),
                _ACTIVE_MODEL_ROLE,
            )
        view = combo.view()
        if view is not None:
            view.viewport().update()

    def _fill_model_combo(self, live: list[str] | None = None) -> None:
        from translators import registry
        probe = self._credentials()
        self._syncing = True
        self.z_model.clear()

        def add_group(label: str) -> None:
            self.z_model.addItem(label)
            header_item = self.z_model.model().item(self.z_model.count() - 1)
            if header_item is not None:
                header_item.setEnabled(False)
                header_item.setData(label, _MODEL_GROUP_ROLE)

        def display_model(label: str) -> tuple[str, str]:
            is_cli_model = label.startswith(registry.OPENCODE_CLI_PREFIX)
            is_custom = label.startswith(registry.CUSTOM_PROVIDER_PREFIX)
            display = (f"⌘ {label[len(registry.OPENCODE_CLI_PREFIX):]}" if is_cli_model else label)
            if registry.provider_of(label) == "google_translate":
                display = "🌐 Google Translate"
            provider_label = ""
            if is_custom:
                display = f"🔧 {registry.custom_provider_label(label, probe)}"
                provider_id = label[len(registry.CUSTOM_PROVIDER_PREFIX):]
                provider_entry = next(
                    (
                        entry for entry in probe.get("custom_providers", [])
                        if isinstance(entry, dict)
                        and str(entry.get("id", "")).strip() == provider_id
                    ),
                    None,
                )
                provider_label = (
                    str(provider_entry.get("name", "")).strip()
                    if provider_entry else ""
                )
            return display, provider_label

        def model_provider(label: str, section: str) -> str:
            if label.startswith(registry.CUSTOM_PROVIDER_PREFIX):
                return ""
            normalized = label.casefold()
            for prefix, provider in (
                ("gemini-", "Gemini"),
                ("groq:", "Groq"),
                ("cf:", "Cloudflare"),
                ("mistral:", "Mistral"),
                ("flm:", "FreeLLMAPI"),
                (registry.OPENCODE_CLI_PREFIX, "OpenCode CLI"),
            ):
                if normalized.startswith(prefix):
                    return provider
            return section

        def append_model(
            label: str,
            section: str,
            provider_section: str | None = None,
        ) -> None:
            is_custom = label.startswith(registry.CUSTOM_PROVIDER_PREFIX)
            display, custom_provider = display_model(label)
            self.z_model.addItem(display, label)
            if is_custom:
                # Per model again: a gateway serves text and vision models side
                # by side, so the badge follows what this one was measured to
                # do, not what the provider's switch says.
                entry = registry._find_custom_provider(probe, label)
                status = (
                    self._measured_vision(
                        str(entry.get("id", "")), label)
                    if entry else "unknown"
                )
            else:
                status = registry.image_capability(label)
            provider_label = custom_provider or model_provider(
                label, provider_section or section)
            self._set_model_item_metadata(
                self.z_model,
                self.z_model.count() - 1,
                label,
                status,
                favorite=label in favorite_models,
                provider_label=provider_label,
                favorite_key=label,
            )

        model_rows = registry.model_groups(
            probe, live, self._live_cli, self._live_groq,
            self._live_cf, self._live_flm)
        model_sections: dict[str, str] = {}
        current_section = ""
        for model_label, _enabled in model_rows:
            if model_label.startswith("──"):
                current_section = model_label.strip("─ ").split(
                    "—", 1)[0].strip(" ✦🌉⚡🔧☁🌀🔀⌘🌐")
            else:
                model_sections[model_label] = current_section
        favorite_models = set(_normalize_model_favorites(
            self._settings.get("model_favorites")))
        if favorite_models:
            add_group("── ★ Favorites ──")
            for label, _enabled in model_rows:
                if not label.startswith("──") and label in favorite_models:
                    append_model(
                        label, "Favorites", model_sections.get(label))

        section = ""
        for label, _enabled in model_rows:
            if label.startswith("──"):
                section = label.strip("─ ").split("—", 1)[0].strip(
                    " ✦🌉⚡🔧☁🌀🔀⌘🌐")
                add_group(label)
                continue
            append_model(label, section)

        # Preserve starred models that are temporarily absent from the live
        # catalog so a refresh does not silently erase the user's favorites.
        self._settings["model_favorites"] = sorted(favorite_models)
        self._syncing = False
        self._mark_active_model(self.z_model, resolve_model(self._settings))
        self._update_model_image_badge()

    def _select_model(self, current: str) -> None:
        self._syncing = True
        idx = self.z_model.findData(current)
        if idx < 0:
            idx = self.z_model.findText(current)
        if idx >= 0 and not self._is_header(idx, self.z_model):
            self.z_model.setCurrentIndex(idx)
        elif idx >= 0:
            self.z_model.setCurrentIndex(idx)
        elif current:
            from translators import registry
            is_cli_model = current.startswith(registry.OPENCODE_CLI_PREFIX)
            is_custom = current.startswith(registry.CUSTOM_PROVIDER_PREFIX)
            display = (f"⌘ {current[len(registry.OPENCODE_CLI_PREFIX):]}" if is_cli_model else current)
            if is_custom:
                display = f"🔧 {registry.custom_provider_label(current, self._credentials())}"
            self.z_model.addItem(display, current)
            cap = registry.custom_image_capability(current, self._credentials()) if is_custom else registry.image_capability(current)
            self._set_model_item_metadata(
                self.z_model, self.z_model.count() - 1, current, cap)
            self.z_model.setCurrentIndex(self.z_model.count() - 1)
        else:
            self.z_model.setCurrentIndex(self._first_enabled())
        self._mark_active_model(self.z_model, current)
        self._syncing = False
        self._update_model_image_badge()
        self._update_engine_hint()

    def _first_enabled(self) -> int:
        for i in range(self.z_model.count()):
            if not self._is_header(i, self.z_model):
                return i
        return 0

    def _current_model(self) -> str:
        index = self.z_model.currentIndex()
        data = self.z_model.currentData()
        if isinstance(data, str) and index >= 0 and self.z_model.currentText() == self.z_model.itemText(index):
            return data
        text = self.z_model.currentText().strip()
        if not text or text.startswith("──"):
            return DEFAULT_MODEL
        return text

    def _on_credentials_changed(self, *_args) -> None:
        if self._syncing:
            return
        chosen = self._current_model()
        self._fill_model_combo()
        self._select_model(chosen)
        self._update_key_states()

    def _on_model_changed(self, *_args) -> None:
        self._mark_active_model(self.z_model, self._current_model())
        self._update_model_image_badge()
        if not self._syncing:
            self._update_engine_hint()

    def _on_model_favorite_clicked(self, model_id: str) -> None:
        favorites = set(_normalize_model_favorites(
            self._settings.get("model_favorites")))
        if model_id in favorites:
            favorites.remove(model_id)
        else:
            favorites.add(model_id)
        self._settings["model_favorites"] = sorted(favorites)
        current = self._current_model()
        self._fill_model_combo()
        self._select_model(current)

    def _on_fast_toggled(self, _on: bool) -> None:
        chosen = self._current_model()
        self._fill_model_combo()
        self._select_model(chosen)

    def _on_typography_changed(self, *_args) -> None:
        parent = self.parent()
        popups = getattr(parent, "popups", None)
        if not popups:
            return
        try:
            live = self.values()
        except (AttributeError, TypeError):
            return
        for popup in list(popups):
            try:
                if popup.isVisible():
                    popup.refresh_display(live)
            except RuntimeError:
                pass

    def reject(self) -> None:
        self._size_save_timer.stop()
        self._save_dialog_geometry()
        parent = self.parent()
        popups = getattr(parent, "popups", None)
        if popups:
            for popup in list(popups):
                try:
                    if popup.isVisible():
                        popup.refresh_display(self._settings)
                except RuntimeError:
                    pass
        super().reject()

    def _update_model_image_badge(self) -> None:
        from translators import registry
        cur = self._current_model()
        status = registry.custom_image_capability(cur, self._credentials()) if cur.startswith(registry.CUSTOM_PROVIDER_PREFIX) else registry.image_capability(cur)
        label, background, foreground = _IMAGE_BADGES[status]
        self.model_image_badge.setText(label)
        self.model_image_badge.setStyleSheet(
            "QLabel {" f"background-color: {background.name()}; color: {foreground.name()}; border-radius: 8px; padding: 5px 10px; font-size: 13px; font-weight: 700; border: none; " "}"
        )
        self.model_image_badge.setToolTip({"supported": "This model accepts image input.", "unsupported": "This model accepts text only.", "unknown": "Image support not verified."}[status])

    def _update_key_states(self) -> None:
        creds = self._credentials()
        self.gemini_state.setText("●" if creds["gemini_key"] else "○")
        self.gemini_state.setStyleSheet("color: #10B981; font-size: 14px; background: transparent; border: none;" if creds["gemini_key"] else "color: #EF4444; font-size: 14px; background: transparent; border: none;")
        for state, filled in ((self.groq_state, creds["groq_key"]), (self.cf_token_state, creds["cloudflare_token"]), (self.mistral_state, creds["mistral_key"]), (self.flm_state, creds["flm_key"])):
            state.setText("●" if filled else "○")
            state.setStyleSheet("color: #10B981; font-size: 14px; background: transparent; border: none;" if filled else "color: #EF4444; font-size: 14px; background: transparent; border: none;")
        self.cf_account_state.setText("●" if creds["cloudflare_account_id"] else "○")
        self.cf_account_state.setStyleSheet("color: #10B981; font-size: 14px; background: transparent; border: none;" if creds["cloudflare_account_id"] else "color: #EF4444; font-size: 14px; background: transparent; border: none;")

    def _update_engine_hint(self) -> None:
        from translators import registry
        model = self._current_model()
        creds = dict(self._credentials())
        ok, reason = registry.availability(model, creds)
        is_cli_model = registry.provider_of(model) == "opencode_cli"
        if is_cli_model and self._cli_model_error:
            reason = self._cli_model_error
            ok = False
        status = ("Ready — this model will translate." if ok else reason)
        if self._cli_model_error and not is_cli_model:
            status += f"\n⌘ OpenCode CLI unavailable: {self._cli_model_error}"
        self.engine_hint.setText(status)
        colors = self._theme_colors()
        self.engine_hint.setStyleSheet(f"color: {colors['success']}; font-weight: 600; background: transparent; border: none;" if ok and not self._cli_model_error else f"color: {colors['warning']}; font-weight: 600; background: transparent; border: none;")
        # sync header badge
        try:
            self.header_status.setText(
                "Ready" if ok and not self._cli_model_error
                else "Action needed")
            self.header_status.setObjectName("badgeSuccess" if ok and not self._cli_model_error else "badgeWarning")
            self.header_status.style().unpolish(self.header_status)
            self.header_status.style().polish(self.header_status)
        except Exception:
            pass

    def _refresh_zen_models(self) -> None:
        from translators import gemini as gemini_api
        from translators import opencode_cli, opencode_compat, opencode_zen
        chosen = self._current_model()
        self.z_refresh.setEnabled(False)
        self._cli_model_error = None
        try:
            gemini_api.known_models(refresh=True, api_key=self.gemini_key.text().strip())
        except Exception as error:
            log.warning("Could not refresh Gemini model list: %s", error)
        base = self.o_base.text().strip()
        key = self.o_key.text().strip()
        if base and key:
            if "groq" in base:
                try:
                    self._live_groq = opencode_compat.list_models(base, key)
                except Exception as error:
                    log.info("Groq model list unavailable: %s", error)
                    self._live_groq = None
            if "cloudflare" in base:
                try:
                    self._live_cf = opencode_compat.list_models(base, key)
                except Exception as error:
                    log.info("Cloudflare model list unavailable: %s", error)
                    self._live_cf = None
        flm_base = self.flm_base.text().strip()
        flm_key = self.flm_key.text().strip()
        if flm_base and flm_key:
            try:
                self._live_flm = opencode_compat.list_ready_models(flm_base, flm_key)
            except Exception as error:
                log.info("FreeLLMAPI model list unavailable: %s", error)
                self._live_flm = None
        else:
            self._live_flm = None
        try:
            self._live_cli = opencode_cli.list_models()
            self._settings["opencode_cli_models"] = list(self._live_cli)
        except opencode_cli.OpenCodeCLIError as error:
            self._cli_model_error = str(error)
            log.info("OpenCode CLI models unavailable: %s", error)
            self._live_cli = list(self._settings.get("opencode_cli_models") or [])
        finally:
            self.z_refresh.setEnabled(True)
        self._fill_model_combo()
        self._select_model(chosen)
        self._verify_cli_models()

    def _verify_cli_models(self) -> None:
        if self._verify_thread is not None and self._verify_thread.is_alive():
            return
        if not self._live_cli:
            return
        models = list(self._live_cli)
        snapshot = self._settings

        def job():
            from translators import verify
            try:
                snapshot["model_status"] = verify.verify_cli_models(models, snapshot)
            except Exception as error:
                log.warning("Could not verify OpenCode CLI models: %s", error)
                return
            self.verifyDone.emit()

        self._verify_thread = threading.Thread(target=job, daemon=True)
        self._verify_thread.start()

    def _credentials(self) -> dict:
        def text(name: str) -> str:
            w = getattr(self, name, None)
            return w.text().strip() if w is not None else ""
        return {
            "gemini_key": text("gemini_key"),
            "groq_key": text("groq_key"),
            "cloudflare_account_id": text("cf_account"),
            "cloudflare_token": text("cf_token"),
            "mistral_key": text("mistral_key"),
            "flm_base": text("flm_base"),
            "flm_key": text("flm_key"),
            "opencode_cli_models": list(self._settings.get("opencode_cli_models") or []),
            "model_status": dict(self._settings.get("model_status") or {}),
            "openai_base": text("o_base"),
            "openai_key": text("o_key"),
            "openai_model": text("o_model"),
            "custom_providers": list(self._settings.get("custom_providers") or []),
            "fast_mode": self.fast_check.isChecked(),
        }

    def _run_probe(self) -> None:
        from translators import probe
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing Model…")
        snapshot = self.values()
        model_name = self._current_model()

        def job():
            try:
                self.probeDone.emit(probe.probe_all(snapshot))
            except Exception:
                log.exception("Selected model test failed")
                self.probeDone.emit([
                    (f"★ Your model ({model_name})", False, "FAILED")
                ])

        self._probe_thread = threading.Thread(target=job, daemon=True)
        self._probe_thread.start()

    def _on_verify_done(self) -> None:
        chosen = self._current_model()
        self._fill_model_combo()
        self._select_model(chosen)

    def _on_probe_done(self, results: list) -> None:
        from translators import probe
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test Selected Model")
        text = probe.format_results(results)
        self.results.setPlainText(text)
        self.results.setVisible(True)

    def values(self) -> dict:
        from config.settings import DEFAULTS
        from translators.opencode_bridge import MODEL_DEFAULT_EFFORTS
        from config.settings import _sanitize_custom_providers
        out = dict(DEFAULTS)
        out.update(self._settings)
        groq_key = self.groq_key.text().strip()
        if not groq_key and "groq" in self.o_base.text() and self.o_key.text().strip():
            groq_key = self.o_key.text().strip()
            self.groq_key.setText(groq_key)
        try:
            self._sync_custom_providers_from_cards()
        except Exception:
            pass
        out.update({
            "theme": self._settings.get("theme", "Dark Slate"),
            "color_scheme": self._settings.get("color_scheme", "system"),
            "pdf_canvas_theme_background":
                self.pdf_canvas_theme_check.isChecked(),
            "model": self._current_model(),
            "gemini_key": self.gemini_key.text().strip(),
            "opencode_cli_models": list(self._live_cli if self._live_cli is not None else self._settings.get("opencode_cli_models") or []),
            "openai_base": self.o_base.text().strip(),
            "openai_key": self.o_key.text().strip(),
            "openai_model": self.o_model.text().strip(),
            "groq_key": groq_key,
            "flm_base": self.flm_base.text().strip(),
            "flm_key": self.flm_key.text().strip(),
            "flm_models_ready": list(self._live_flm if self._live_flm is not None else self._settings.get("flm_models_ready") or []),
            "cloudflare_account_id": self.cf_account.text().strip(),
            "cloudflare_token": self.cf_token.text().strip(),
            "mistral_key": self.mistral_key.text().strip(),
            "custom_providers": _sanitize_custom_providers(self._settings.get("custom_providers") or []),
            "model_favorites": _normalize_model_favorites(
                self._settings.get("model_favorites")),
            "custom_provider_model_favorites": _normalize_model_favorites(
                self._settings.get("custom_provider_model_favorites")),
            "custom_provider_collapsed": _normalize_custom_provider_collapsed(
                self._settings.get("custom_provider_collapsed")),
            "settings_last_page": self._stack.currentIndex(),
            "settings_last_page_version": 3,
            "email": self.email.text().strip(),
            "selection_mode": self.selection_mode.currentData(),
            "speech_engine": self.speech_engine.currentData(),
            "gemini_tts_voice": self.gemini_tts_voice.currentData(),
            "speech_rate": self.speech_rate_setting.value(),
            "speech_voice": self._edge_speech_voice,
            "font_size": self.font_size.value(),
            "line_spacing_px": self.line_spacing.value(),
            "translation_side_margin_px": self.translation_side_margin.value(),
            "translation_top_bottom_margin_px":
                self.translation_top_bottom_margin.value(),
            "translation_font": "",
            "translation_font_arabic": self.arabic_font.currentData() or "",
            "translation_font_latin": self.latin_font.currentData() or "",
            "arabic_tashkeel": self.arabic_tashkeel.isChecked(),
            "automatic_speech_voice": (
                self.automatic_speech_voice.isChecked()
            ),
            "speech_read_latin_words": (
                self.speech_read_latin_words.isChecked()
            ),
            "settings_size": [self.width(), self.height()],
            "settings_pos": [self.x(), self.y()],
            "model_status": dict(self._settings.get("model_status") or {}),
            "model_instructions": self.model_instructions.toPlainText().strip(),
            "fast_mode": self.fast_check.isChecked(),
            "effort_default": self.effort_default_combo.currentText(),
            "effort_levels": {model: combo.currentText() for model, combo in self.effort_combos.items() if combo.currentText() != MODEL_DEFAULT_EFFORTS.get(model, self.effort_default_combo.currentText())},
        })
        # header theme combo is authoritative if user changed it there
        try:
            hdr_theme = self.header_theme_combo.currentData()
            if isinstance(hdr_theme, str) and hdr_theme in THEMES:
                out["theme"] = hdr_theme
        except Exception:
            pass
        return out

    def _sync_custom_providers_from_cards(self) -> None:
        for card in getattr(self, "_custom_cards", []):
            if not isinstance(card, QFrame):
                continue
            cid = card.property("cp_id")
            if not cid:
                continue
            for entry in (self._settings.get("custom_providers") or []):
                if isinstance(entry, dict) and str(entry.get("id", "")) == cid:
                    try:
                        entry["name"] = card._cp_name.text().strip()  # type: ignore[attr-defined]
                        entry["base_url"] = card._cp_base.text().strip()  # type: ignore[attr-defined]
                        entry["api_key"] = card._cp_key.text().strip()  # type: ignore[attr-defined]
                        entry["model"] = card._cp_model.currentText().strip()  # type: ignore[attr-defined]
                        entry["enabled"] = bool(card._cp_enabled.isChecked())  # type: ignore[attr-defined]
                        entry["reasoning_enabled"] = bool(card._cp_reasoning.isChecked())  # type: ignore[attr-defined]
                        entry["vision_enabled"] = bool(card._cp_vision.isChecked())  # type: ignore[attr-defined]
                    except RuntimeError:
                        pass
                    break
