"""Shared application theme palettes and Settings-window styling — professional edition."""

THEMES = {
    "Dark Slate": {
        "window": "#0D1117",
        "toolbar": "#161B22",
        "panel": "#161B22",
        "surface": "#21262D",
        "surface_hover": "#30363D",
        "canvas": "#090D12",
        "text": "#F0F6FC",
        "muted": "#8B949E",
        "border": "#30363D",
        "accent": "#4F46E5",
        "accent_hover": "#4338CA",
        "accent_light": "#6366F1",
        "selection": "#26354A",
        "scroll": "#30363D",
        "scroll_hover": "#484F58",
        "success": "#2EA043",
        "warning": "#D29922",
    },
    "Midnight Blue": {
        "window": "#0F172A",
        "toolbar": "#1E293B",
        "panel": "#1E293B",
        "surface": "#334155",
        "surface_hover": "#475569",
        "canvas": "#0A0F1D",
        "text": "#F8FAFC",
        "muted": "#94A3B8",
        "border": "#334155",
        "accent": "#0284C7",
        "accent_hover": "#0369A1",
        "accent_light": "#38BDF8",
        "selection": "#1E3A8A",
        "scroll": "#334155",
        "scroll_hover": "#475569",
        "success": "#10B981",
        "warning": "#F59E0B",
    },
    "Nordic Gray": {
        "window": "#242933",
        "toolbar": "#2E3440",
        "panel": "#2E3440",
        "surface": "#3B4252",
        "surface_hover": "#434C5E",
        "canvas": "#1E222A",
        "text": "#ECEFF4",
        "muted": "#D8DEE9",
        "border": "#4C566A",
        "accent": "#88C0D0",
        "accent_hover": "#81A1C1",
        "accent_light": "#8FBCBB",
        "selection": "#3B4252",
        "scroll": "#434C5E",
        "scroll_hover": "#4C566A",
        "success": "#A3BE8C",
        "warning": "#EBCB8B",
    },
    "Pure Light": {
        "window": "#F8FAFC",
        "toolbar": "#FFFFFF",
        "panel": "#FFFFFF",
        "surface": "#F1F5F9",
        "surface_hover": "#E2E8F0",
        "canvas": "#F1F5F9",
        "text": "#0F172A",
        "muted": "#64748B",
        "border": "#E2E8F0",
        "accent": "#2563EB",
        "accent_hover": "#1D4ED8",
        "accent_light": "#3B82F6",
        "selection": "#DBEAFE",
        "scroll": "#CBD5E1",
        "scroll_hover": "#94A3B8",
        "success": "#16A34A",
        "warning": "#D97706",
    },
}


def settings_stylesheet(theme: str = "Dark Slate") -> str:
    """Professional Settings dialog stylesheet — sidebar navigation + card system."""
    colors = THEMES.get(theme, THEMES["Dark Slate"])
    is_light = theme == "Pure Light"
    # subtle card shadow via border + background separation
    return f"""
        /* ── Base ── */
        QDialog {{
            background-color: {colors["window"]};
            color: {colors["text"]};
            font-size: 14px;
        }}
        QWidget {{
            color: {colors["text"]};
            font-size: 14px;
        }}
        QLabel {{
            color: {colors["text"]};
            font-size: 14px;
        }}

        /* ── Header ── */
        QFrame#settingsHeader {{
            background-color: {colors["toolbar"]};
            border: none;
            border-bottom: 1px solid {colors["border"]};
        }}
        QLabel#headerTitle {{
            color: {colors["text"]};
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.3px;
        }}
        QLabel#headerSubtitle {{
            color: {colors["muted"]};
            font-size: 14px;
            font-weight: 500;
        }}
        QLineEdit#headerSearch {{
            background-color: {colors["surface"]};
            border: 1px solid {colors["border"]};
            border-radius: 9px;
            padding: 9px 12px 9px 12px;
            font-size: 14px;
            color: {colors["muted"]};
        }}
        QLineEdit#headerSearch:focus {{
            border-color: {colors["accent_light"]};
            color: {colors["text"]};
        }}

        /* ── Sidebar ── */
        QFrame#sidebar {{
            background-color: {colors["panel"]};
            border: none;
            border-right: 1px solid {colors["border"]};
        }}
        QLabel#sidebarSection {{
            color: {colors["muted"]};
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 1px;
        }}
        QPushButton[nav="true"] {{
            background-color: transparent;
            color: {colors["muted"]};
            border: 1px solid transparent;
            border-radius: 9px;
            text-align: left;
            padding: 12px 16px;
            min-height: 22px;
            font-size: 15px;
            font-weight: 600;
        }}
        QPushButton[nav="true"]:hover {{
            background-color: {colors["surface"]};
            color: {colors["text"]};
            border-color: {colors["border"]};
        }}
        QPushButton[nav="true"]:checked {{
            background-color: {colors["selection"]};
            color: {colors["text"]};
            border-color: {colors["accent_light"]};
            font-weight: 700;
        }}
        QPushButton[nav="true"]:checked:hover {{
            background-color: {colors["selection"]};
        }}

        /* ── Content area ── */
        QFrame#contentArea {{
            background-color: {colors["window"]};
            border: none;
        }}
        QScrollArea {{
            background-color: transparent;
            border: none;
        }}
        QScrollArea > QWidget > QWidget {{
            background-color: transparent;
        }}

        /* ── Cards ── */
        QGroupBox {{
            background-color: {colors["panel"]};
            border: 1px solid {colors["border"]};
            border-radius: 12px;
            margin-top: 10px;
            padding: 18px 18px 16px 18px;
            font-size: 14px;
            font-weight: 700;
            letter-spacing: 0.6px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 8px;
            color: {colors["muted"]};
            background-color: transparent;
        }}
        /* Custom card frame (for provider cards etc) */
        QFrame#card {{
            background-color: {colors["panel"]};
            border: 1px solid {colors["border"]};
            border-radius: 12px;
        }}
        QFrame#cardElevated {{
            background-color: {colors["surface"]};
            border: 1px solid {colors["border"]};
            border-radius: 12px;
        }}

        /* ── Inputs ── */
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QAbstractSpinBox,
        QTextEdit, QPlainTextEdit {{
            background-color: {colors["surface"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 10px;
            padding: 10px 12px;
            min-height: 22px;
            font-size: 15px;
            selection-background-color: {colors["accent"]};
            selection-color: #FFFFFF;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
        QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {colors["accent_light"]};
            background-color: {colors["surface"]};
        }}
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
            border-color: {colors["accent_light"]};
        }}
        QLineEdit:disabled, QComboBox:disabled {{
            color: {colors["muted"]};
            background-color: {colors["window"]};
        }}
        QToolButton#edgeSpeechVoice {{
            background-color: {colors["surface"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 10px;
            padding: 10px 12px;
            min-height: 22px;
            font-size: 15px;
            text-align: left;
        }}
        QToolButton#edgeSpeechVoice:hover,
        QToolButton#edgeSpeechVoice:focus {{
            border-color: {colors["accent_light"]};
            background-color: {colors["surface"]};
        }}
        QToolButton#edgeSpeechVoice:disabled {{
            color: {colors["muted"]};
            background-color: {colors["window"]};
        }}
        QMenu#edgeSpeechVoiceMenu {{
            background-color: {colors["panel"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            padding: 5px;
        }}
        QMenu#edgeSpeechVoiceMenu::item {{
            padding: 7px 24px;
            border-radius: 5px;
        }}
        QMenu#edgeSpeechVoiceMenu::item:selected {{
            background-color: {colors["selection"]};
            color: {colors["text"]};
        }}
        QComboBox {{
            padding-right: 28px;
            min-height: 18px;
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 28px;
            border: none;
            background: transparent;
        }}
        QComboBox::down-arrow {{
            image: none;
            width: 0; height: 0;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid {colors["muted"]};
            margin-right: 6px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {colors["panel"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 10px;
            padding: 6px;
            selection-background-color: {colors["selection"]};
            selection-color: {colors["text"]};
            outline: none;
        }}
        QComboBox QAbstractItemView::item {{
            padding: 10px 12px;
            border-radius: 6px;
            min-height: 26px;
            font-size: 14px;
        }}
        QComboBox#modelPicker QAbstractItemView {{
            padding: 2px;
            font-size: 16px;
        }}
        QComboBox#customProviderModelPicker QAbstractItemView {{
            padding: 2px;
            font-size: 16px;
        }}
        QComboBox#modelPicker QAbstractItemView::item,
        QComboBox#customProviderModelPicker QAbstractItemView::item {{
            padding: 1px 8px;
            min-height: 22px;
            border-radius: 5px;
            font-size: 16px;
        }}

        /* ── Buttons ── */
        QPushButton {{
            background-color: {colors["surface"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 8px;
            padding: 4px 12px;
            min-height: 18px;
            font-size: 14px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background-color: {colors["surface_hover"]};
            border-color: {colors["accent_light"]};
        }}
        QPushButton:pressed {{
            background-color: {colors["selection"]};
        }}
        QPushButton:disabled {{
            color: {colors["muted"]};
            background-color: {colors["window"]};
            border-color: {colors["border"]};
        }}
        QPushButton#primaryBtn, QPushButton#translateBtn {{
            background-color: {colors["accent"]};
            color: #FFFFFF;
            border: 1px solid {colors["accent"]};
            font-weight: 700;
            padding: 4px 15px;
            border-radius: 8px;
        }}
        QPushButton#primaryBtn:hover, QPushButton#translateBtn:hover {{
            background-color: {colors["accent_hover"]};
            border-color: {colors["accent_hover"]};
        }}
        QPushButton#primaryBtn:pressed, QPushButton#translateBtn:pressed {{
            background-color: {colors["accent_hover"]};
        }}
        QPushButton#ghostBtn {{
            background-color: transparent;
            border: 1px solid {colors["border"]};
            color: {colors["text"]};
            padding: 4px 14px;
            border-radius: 8px;
            font-weight: 600;
        }}
        QPushButton#ghostBtn:hover {{
            background-color: {colors["surface"]};
            border-color: {colors["accent_light"]};
        }}
        QPushButton#collapseProviderButton {{
            background-color: {colors["surface"]};
            color: {colors["muted"]};
            border: 1px solid {colors["border"]};
            padding: 5px 8px;
            min-height: 30px;
            font-size: 13px;
        }}
        QPushButton#collapseProviderButton:hover {{
            background-color: {colors["selection"]};
            color: {colors["text"]};
            border-color: {colors["accent_light"]};
        }}
        QPushButton#dangerBtn {{
            background-color: transparent;
            color: #EF6B73;
            border: 1px solid #71343A;
            font-weight: 600;
        }}
        QPushButton#dangerBtn:hover {{
            background-color: #B4232E;
            color: #FFFFFF;
            border-color: #B4232E;
        }}
        QPushButton#iconBtn {{
            background-color: {colors["surface"]};
            border: 1px solid {colors["border"]};
            border-radius: 8px;
            padding: 6px 10px;
            font-size: 15px;
            min-width: 36px;
            max-width: 44px;
        }}
        QPushButton#iconBtn:hover {{
            background-color: {colors["surface_hover"]};
            border-color: {colors["accent_light"]};
        }}

        /* ── Checkboxes ── */
        QCheckBox {{
            color: {colors["text"]};
            spacing: 10px;
            font-size: 15px;
            font-weight: 600;
        }}
        QCheckBox::indicator {{
            width: 12px; height: 12px;
            border: 1.5px solid {colors["border"]};
            border-radius: 3px;
            background-color: {colors["surface"]};
        }}
        QCheckBox::indicator:hover {{
            border-color: {colors["accent_light"]};
        }}
        QCheckBox::indicator:checked {{
            background-color: {colors["accent"]};
            border-color: {colors["accent"]};
            image: url(none);
        }}
        QCheckBox:disabled {{
            color: {colors["muted"]};
        }}

        /* ── Double Spin / helper buttons ── */
        QPushButton#stepBtn {{
            background-color: {colors["surface"]};
            border: 1px solid {colors["border"]};
            border-radius: 9px;
            font-size: 18px;
            font-weight: 700;
            padding: 0;
            min-width: 44px;
            max-width: 44px;
            min-height: 38px;
        }}
        QPushButton#stepBtn:hover {{
            background-color: {colors["surface_hover"]};
            border-color: {colors["accent_light"]};
        }}

        /* ── Toggle pill / badges ── */
        QLabel#badgeSuccess {{
            background-color: {colors["success"]};
            color: #FFFFFF;
            border-radius: 7px;
            padding: 4px 9px;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }}
        QLabel#badgeWarning {{
            background-color: {colors["warning"]};
            color: #0F172A;
            border-radius: 7px;
            padding: 4px 9px;
            font-size: 12px;
            font-weight: 700;
        }}
        QLabel#badgeMuted {{
            background-color: {colors["surface"]};
            color: {colors["muted"]};
            border: 1px solid {colors["border"]};
            border-radius: 7px;
            padding: 4px 9px;
            font-size: 12px;
            font-weight: 700;
        }}

        QToolTip {{
            background-color: {colors["panel"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            padding: 6px 9px;
            font-size: 14px;
        }}

        /* ── Info / hint boxes ── */
        QFrame#hintBox {{
            background-color: {colors["surface"]};
            border: 1px solid {colors["border"]};
            border-radius: 10px;
        }}
        QFrame#hintBoxSuccess {{
            background-color: {colors["selection"]};
            border: 1px solid {colors["success"]};
            border-radius: 10px;
        }}
        QFrame#hintBoxWarning {{
            background-color: rgba(211,153,34,0.12);
            border: 1px solid {colors["warning"]};
            border-radius: 10px;
        }}

        /* ── Footer ── */
        QFrame#footer {{
            background-color: {colors["toolbar"]};
            border: none;
            border-top: 1px solid {colors["border"]};
        }}

        /* ── Scrollbars ── */
        QScrollBar:vertical {{
            background-color: transparent;
            width: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background-color: {colors["scroll"]};
            border-radius: 5px;
            min-height: 36px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical:hover {{
            background-color: {colors["scroll_hover"]};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            height: 10px;
            background: transparent;
        }}
        QScrollBar::handle:horizontal {{
            background-color: {colors["scroll"]};
            border-radius: 5px;
            min-width: 36px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}

        /* ── Tab bar fallback (kept for any inner tabs) ── */
        QTabWidget::pane {{
            border: none;
            background-color: transparent;
        }}
        QTabBar::tab {{
            background-color: {colors["surface"]};
            color: {colors["muted"]};
            border: 1px solid {colors["border"]};
            border-bottom: none;
            padding: 8px 14px;
            margin-right: 4px;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            font-weight: 600;
        }}
        QTabBar::tab:selected {{
            background-color: {colors["panel"]};
            color: {colors["accent_light"]};
        }}

        /* ── Splitter ── */
        QSplitter::handle {{
            background-color: {colors["border"]};
        }}
        QSplitter::handle:horizontal {{
            width: 1px;
        }}
        QSplitter::handle:vertical {{
            height: 1px;
        }}
        QSplitter#settingsBodySplitter::handle:horizontal {{
            width: 8px;
        }}
        QSplitter#settingsSidebarSplitter::handle:vertical {{
            height: 8px;
        }}
        QSplitter::handle:hover {{
            background-color: {colors["accent_light"]};
        }}
    """
