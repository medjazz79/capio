"""Shared application theme palettes and Settings-window styling — professional edition."""
import math

_DARK_SLATE = {
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
}

_MIDNIGHT_BLUE = {
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
}

_NORDIC_GRAY = {
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
}

_PURE_LIGHT = {
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
}


def _palette(
    *,
    window: str,
    toolbar: str,
    panel: str,
    surface: str,
    surface_hover: str,
    canvas: str,
    text: str,
    muted: str,
    border: str,
    accent: str,
    accent_hover: str,
    accent_light: str,
    selection: str,
    scroll: str,
    scroll_hover: str,
    success: str = "#2EA043",
    warning: str = "#D29922",
) -> dict[str, str]:
    return {
        "window": window,
        "toolbar": toolbar,
        "panel": panel,
        "surface": surface,
        "surface_hover": surface_hover,
        "canvas": canvas,
        "text": text,
        "muted": muted,
        "border": border,
        "accent": accent,
        "accent_hover": accent_hover,
        "accent_light": accent_light,
        "selection": selection,
        "scroll": scroll,
        "scroll_hover": scroll_hover,
        "success": success,
        "warning": warning,
    }


def _family_palettes(
    dark_background: str,
    dark_panel: str,
    dark_surface: str,
    dark_text: str,
    dark_muted: str,
    dark_border: str,
    dark_accent: str,
    dark_accent_hover: str,
    dark_accent_light: str,
    dark_selection: str,
    light_accent: str,
    light_accent_hover: str,
    light_accent_light: str,
    light_selection: str,
) -> dict[str, dict[str, str]]:
    return {
        "dark": _palette(
            window=dark_background,
            toolbar=dark_panel,
            panel=dark_panel,
            surface=dark_surface,
            surface_hover=dark_border,
            canvas=dark_background,
            text=dark_text,
            muted=dark_muted,
            border=dark_border,
            accent=dark_accent,
            accent_hover=dark_accent_hover,
            accent_light=dark_accent_light,
            selection=dark_selection,
            scroll=dark_border,
            scroll_hover=dark_border,
        ),
        "light": _palette(
            window="#F8FAFC",
            toolbar="#FFFFFF",
            panel="#FFFFFF",
            surface="#F1F5F9",
            surface_hover="#E2E8F0",
            canvas="#F1F5F9",
            text="#0F172A",
            muted="#64748B",
            border="#E2E8F0",
            accent=light_accent,
            accent_hover=light_accent_hover,
            accent_light=light_accent_light,
            selection=light_selection,
            scroll="#CBD5E1",
            scroll_hover="#94A3B8",
            success="#16A34A",
            warning="#D97706",
        ),
    }


THEME_VARIANTS = {
    "Dark Slate": {"dark": _DARK_SLATE, "light": _PURE_LIGHT},
    "Midnight Blue": {
        **_family_palettes(
            "#0F172A", "#1E293B", "#334155", "#F8FAFC", "#94A3B8",
            "#334155", "#0284C7", "#0369A1", "#38BDF8", "#1E3A8A",
            "#0369A1", "#075985", "#0284C7", "#DBEAFE",
        ),
        "dark": _MIDNIGHT_BLUE,
    },
    "Nordic Gray": {
        **_family_palettes(
            "#242933", "#2E3440", "#3B4252", "#ECEFF4", "#D8DEE9",
            "#4C566A", "#88C0D0", "#81A1C1", "#8FBCBB", "#3B4252",
            "#456A79", "#345A78", "#557D8E", "#D7EAF0",
        ),
        "dark": _NORDIC_GRAY,
    },
    "Pure Light": {"dark": _DARK_SLATE, "light": _PURE_LIGHT},
    "Amoled": _family_palettes(
        "#000000", "#090909", "#171717", "#F5F5F5", "#A3A3A3",
        "#262626", "#22C55E", "#16A34A", "#4ADE80", "#052E16",
        "#15803D", "#166534", "#22C55E", "#DCFCE7",
    ),
    "Catppuccin": _family_palettes(
        "#1E1E2E", "#24243A", "#313244", "#CDD6F4", "#A6ADC8",
        "#45475A", "#CBA6F7", "#B4BEFE", "#F5C2E7", "#45475A",
        "#8839EF", "#7287FD", "#EA76CB", "#E6D9FC",
    ),
    "Dracula": _family_palettes(
        "#282A36", "#21222C", "#343746", "#F8F8F2", "#B8B8C8",
        "#44475A", "#BD93F9", "#A77BF3", "#FF79C6", "#44475A",
        "#7C3AED", "#6D28D9", "#A78BFA", "#EDE9FE",
    ),
    "Everforest": _family_palettes(
        "#2D353B", "#343F44", "#3D484D", "#D3C6AA", "#9DA9A0",
        "#4F585E", "#A7C080", "#83C092", "#D699B6", "#3D484D",
        "#527A3A", "#3F682F", "#6B8F45", "#E5F0D8",
    ),
    "Gruvbox": _family_palettes(
        "#282828", "#32302F", "#3C3836", "#EBDBB2", "#BDAE93",
        "#504945", "#FABD2F", "#D79921", "#FE8019", "#504945",
        "#9A6700", "#7A5200", "#B58105", "#F5E8C5",
    ),
    "Matrix": _family_palettes(
        "#050A05", "#0B140B", "#132013", "#D8F5D0", "#8BBF83",
        "#253B25", "#39FF14", "#20D60A", "#8AFF72", "#173517",
        "#16821B", "#116B17", "#229B25", "#DDF5D7",
    ),
    "Monokai": _family_palettes(
        "#272822", "#2D2E27", "#3E3D32", "#F8F8F2", "#C2C2B8",
        "#49483E", "#A6E22E", "#7FB51A", "#F92672", "#49483E",
        "#548500", "#426A00", "#6D9F00", "#E8F2CC",
    ),
    "Nord": _family_palettes(
        "#2E3440", "#3B4252", "#434C5E", "#ECEFF4", "#D8DEE9",
        "#4C566A", "#88C0D0", "#81A1C1", "#8FBCBB", "#434C5E",
        "#456A79", "#345A78", "#557D8E", "#D7EAF0",
    ),
    "Rose Pine": _family_palettes(
        "#191724", "#1F1D2E", "#26233A", "#E0DEF4", "#908CAA",
        "#3D3952", "#EBBCBA", "#E5A7A5", "#C4A7E7", "#3D3952",
        "#B4637A", "#9E4D63", "#D7827E", "#F5DCE1",
    ),
    "Solarized": _family_palettes(
        "#002B36", "#073642", "#0B3F4C", "#FDF6E3", "#93A1A1",
        "#586E75", "#2AA198", "#268BD2", "#6C71C4", "#073642",
        "#147D74", "#0E665E", "#268BD2", "#D4EFEC",
    ),
    "Tokyo Night": _family_palettes(
        "#1A1B26", "#1F2335", "#292E42", "#C0CAF5", "#A9B1D6",
        "#3B4261", "#7AA2F7", "#5D83D6", "#BB9AF7", "#292E42",
        "#3459A5", "#294986", "#5D83D6", "#DCE6FC",
    ),
    "Vercel": _family_palettes(
        "#000000", "#111111", "#1C1C1C", "#FAFAFA", "#A1A1A1",
        "#333333", "#0070F3", "#0059C9", "#3291FF", "#102A4C",
        "#0059C9", "#0047A3", "#0070F3", "#DCEBFF",
    ),
    "Vesper": _family_palettes(
        "#101010", "#171717", "#242424", "#E8E6E3", "#A09B94",
        "#353330", "#FFC799", "#E6A875", "#FFD9B8", "#3A3027",
        "#9B5D2E", "#80491F", "#B57949", "#F5E4D5",
    ),
}

THEMES = {
    name: variants["light" if name == "Pure Light" else "dark"]
    for name, variants in THEME_VARIANTS.items()
}

_BUILTIN_THEME_NAMES = frozenset(THEME_VARIANTS)
THEME_COLOR_KEYS = tuple(THEMES["Dark Slate"])
_USER_THEME_NAMES: set[str] = set()

# Keep the editor's element groups aligned with the established theme model.
THEME_GROUPS = (
    ("Background", (
        "window", "toolbar", "panel", "canvas", "surface", "surface_hover",
    )),
    ("Border", ("border",)),
    ("Text", ("text", "muted")),
    ("Accent", (
        "accent", "accent_hover", "accent_light", "selection",
        "scroll", "scroll_hover",
    )),
    ("Status", ("success", "warning")),
)


def register_user_themes(raw) -> dict[str, dict[str, dict[str, str]]]:
    """Register validated user palettes so every app surface resolves them."""
    clean: dict[str, dict[str, dict[str, str]]] = {}
    if isinstance(raw, dict):
        for raw_name, raw_variants in raw.items():
            name = str(raw_name).strip()[:40]
            if (
                not name
                or name in _BUILTIN_THEME_NAMES
                or not isinstance(raw_variants, dict)
            ):
                continue
            variants = {}
            for scheme in ("dark", "light"):
                raw_palette = raw_variants.get(scheme)
                fallback = THEME_VARIANTS["Dark Slate"][scheme]
                palette = dict(fallback)
                if isinstance(raw_palette, dict):
                    for key in THEME_COLOR_KEYS:
                        value = raw_palette.get(key)
                        if (
                            isinstance(value, str)
                            and len(value) == 7
                            and value.startswith("#")
                            and all(
                                char in "0123456789abcdefABCDEF"
                                for char in value[1:]
                            )
                        ):
                            palette[key] = value.upper()
                variants[scheme] = palette
            clean[name] = variants

    for name in _USER_THEME_NAMES - clean.keys():
        THEME_VARIANTS.pop(name, None)
        THEMES.pop(name, None)
    for name, variants in clean.items():
        THEME_VARIANTS[name] = variants
        THEMES[name] = variants["dark"]
    _USER_THEME_NAMES.clear()
    _USER_THEME_NAMES.update(clean)
    return clean


def apply_color_grading(
    palette: dict[str, str],
    controls: dict[str, tuple[float, float, float]],
) -> dict[str, str]:
    """Apply Lift/Gamma/Gain/Offset controls according to each color's luma."""
    from PySide6.QtGui import QColor

    corrections = {
        "lift": (0.0, 0.0, 0.0),
        "gamma": (0.0, 0.0, 0.0),
        "gain": (0.0, 0.0, 0.0),
        "offset": (0.0, 0.0, 0.0),
    }
    corrections.update(controls)
    if not any(any(values) for values in corrections.values()):
        return dict(palette)

    result: dict[str, str] = {}
    for key, color_value in palette.items():
        color = QColor(color_value)
        luma = color.lightnessF()
        tonal_weights = {
            "lift": (1 - luma) ** 2,
            "gamma": 2 * luma * (1 - luma),
            "gain": luma ** 2,
            "offset": 1.0,
        }
        lightness_delta = 0.0
        for name in ("lift", "gamma", "gain", "offset"):
            x, y, master = corrections[name]
            weight = tonal_weights[name]
            radius = min(1.0, math.hypot(x, y))
            if radius:
                angle = math.degrees(math.atan2(y, x))
                tint_hue = round((240 - angle) % 360)
                base_hue = color.hslHue()
                strength = radius * weight * 0.55
                if base_hue < 0:
                    base_hue = tint_hue
                hue_delta = (tint_hue - base_hue + 180) % 360 - 180
                hue = round((base_hue + hue_delta * strength) % 360)
                saturation = color.hslSaturation()
                saturation += round((220 - saturation) * strength)
                color.setHsl(
                    hue,
                    max(0, min(255, saturation)),
                    color.lightness(),
                    color.alpha(),
                )
            lightness_delta += master * 64 * weight

        if lightness_delta:
            color.setHsl(
                color.hslHue(),
                color.hslSaturation(),
                max(0, min(
                    255, round(color.lightness() + lightness_delta))),
                color.alpha(),
            )
        result[key] = color.name().upper()
    return result


def apply_color_curve(
    palette: dict[str, str],
    points: tuple[tuple[float, float], ...],
) -> dict[str, str]:
    """Apply a master RGB tone curve to each color in a palette."""
    from PySide6.QtGui import QColor

    if len(points) < 2:
        raise ValueError("A tone curve requires at least two control points")
    ordered = sorted(points, key=lambda point: point[0])
    if (
        ordered[0][0] != 0
        or ordered[-1][0] != 1
        or any(
            right[0] <= left[0]
            for left, right in zip(ordered, ordered[1:])
        )
    ):
        raise ValueError("A tone curve must include input endpoints 0 and 1")

    if all(x == y for x, y in ordered):
        return dict(palette)

    result: dict[str, str] = {}
    for key, color_value in palette.items():
        color = QColor(color_value)
        channels = tuple(
            round(tone_curve_value(ordered, channel / 255) * 255)
            for channel in (color.red(), color.green(), color.blue())
        )
        result[key] = QColor(*channels, color.alpha()).name().upper()
    return result


def tone_curve_slopes(
    points: tuple[tuple[float, float], ...],
) -> tuple[float, ...]:
    """Return shape-preserving tangents for a cubic tone curve."""
    secants = tuple(
        (right[1] - left[1]) / (right[0] - left[0])
        for left, right in zip(points, points[1:])
    )
    if len(points) == 2:
        return secants[0], secants[0]

    slopes = [secants[0]]
    for index in range(1, len(points) - 1):
        before, after = secants[index - 1], secants[index]
        if before == 0 or after == 0 or before * after < 0:
            slopes.append(0.0)
            continue
        left_width = points[index][0] - points[index - 1][0]
        right_width = points[index + 1][0] - points[index][0]
        weight_left = 2 * right_width + left_width
        weight_right = right_width + 2 * left_width
        slopes.append(
            (weight_left + weight_right)
            / (weight_left / before + weight_right / after)
        )
    slopes.append(secants[-1])
    return tuple(slopes)


def tone_curve_value(
    points: tuple[tuple[float, float], ...],
    value: float,
) -> float:
    """Evaluate the smooth, shape-preserving curve at one normalized input."""
    slopes = tone_curve_slopes(points)
    for index, (left, right) in enumerate(zip(points, points[1:])):
        x0, y0 = left
        x1, y1 = right
        if value <= x1:
            width = x1 - x0
            t = (value - x0) / width
            t_squared = t * t
            t_cubed = t_squared * t
            output = (
                (2 * t_cubed - 3 * t_squared + 1) * y0
                + (t_cubed - 2 * t_squared + t) * width * slopes[index]
                + (-2 * t_cubed + 3 * t_squared) * y1
                + (t_cubed - t_squared) * width * slopes[index + 1]
            )
            return max(0.0, min(1.0, output))
    return points[-1][1]


def system_color_scheme(app=None) -> str:
    """Return the desktop color scheme, using the application palette as fallback."""
    if app is None:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
    if app is None:
        return "dark"
    scheme = app.styleHints().colorScheme()
    if scheme.name.lower() in ("dark", "light"):
        return scheme.name.lower()
    return (
        "light"
        if app.palette().window().color().lightness() >= 128
        else "dark"
    )


def resolved_color_scheme(scheme: str = "system", app=None) -> str:
    if scheme == "system":
        return system_color_scheme(app)
    return scheme if scheme in ("dark", "light") else "dark"


def theme_colors(
    theme: str = "Dark Slate",
    scheme: str = "dark",
    app=None,
) -> dict[str, str]:
    variants = THEME_VARIANTS.get(theme, THEME_VARIANTS["Dark Slate"])
    return variants[resolved_color_scheme(scheme, app)]


def settings_stylesheet(
    theme: str = "Dark Slate",
    scheme: str = "dark",
) -> str:
    """Professional Settings dialog stylesheet — sidebar navigation + card system."""
    colors = theme_colors(theme, scheme)
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
            color: {colors["text"]};
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
        QRadioButton {{
            color: {colors["text"]};
            spacing: 10px;
            font-size: 15px;
            font-weight: 600;
        }}
        QRadioButton::indicator {{
            width: 12px; height: 12px;
            border: 1.5px solid {colors["border"]};
            border-radius: 7px;
            background-color: {colors["surface"]};
        }}
        QRadioButton::indicator:hover {{
            border-color: {colors["accent_light"]};
        }}
        QRadioButton::indicator:checked {{
            background-color: {colors["accent"]};
            border-color: {colors["accent"]};
        }}
        QRadioButton:disabled {{
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
