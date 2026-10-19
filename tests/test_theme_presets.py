import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestThemePresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_every_builtin_theme_has_light_and_dark_variants(self):
        from main import app_stylesheet
        from ui.theme import THEMES, theme_colors
        import re

        self.assertGreaterEqual(len(THEMES), 16)
        for name in THEMES:
            with self.subTest(theme=name):
                dark = theme_colors(name, "dark")
                light = theme_colors(name, "light")
                self.assertEqual(set(dark), set(THEMES["Dark Slate"]))
                self.assertEqual(set(light), set(THEMES["Dark Slate"]))
                for scheme, colors in (("dark", dark), ("light", light)):
                    default_style = app_stylesheet(name, scheme)
                    themed_style = app_stylesheet(name, scheme, True)
                    canvas_pattern = (
                        r"QScrollArea#pdfCanvas\s*\{\s*"
                        r"background-color:\s*([^;]+);"
                    )
                    self.assertEqual(
                        re.search(canvas_pattern, default_style).group(1),
                        "#F1F3F5",
                    )
                    self.assertEqual(
                        re.search(canvas_pattern, themed_style).group(1),
                        colors["canvas"],
                    )
                if name != "Pure Light":
                    self.assertEqual(dark, THEMES[name])

    def test_chat_splitter_and_dock_marker_follow_every_theme(self):
        from main import app_stylesheet
        from ui.chat_panel import ChatPanel
        from ui.theme import THEMES, theme_colors

        panel = ChatPanel(lambda: {})
        try:
            for name in THEMES:
                for scheme in ("dark", "light"):
                    with self.subTest(theme=name, scheme=scheme):
                        colors = theme_colors(name, scheme)
                        app_style = app_stylesheet(name, scheme)
                        separator_style = app_style.split(
                            "QMainWindow::separator {", 1)[1].split("}", 1)[0]
                        self.assertIn(
                            "background-color: transparent",
                            separator_style,
                        )
                        self.assertIn("width: 1px", separator_style)

                        panel.apply_theme(colors)
                        panel_style = panel.styleSheet()
                        # The divider between the chat action buttons and
                        # the composer was removed on request. It used to be
                        # the one thing here painted in the Border colour,
                        # and the panel's own stylesheet meant the
                        # application's transparent rule never reached it.
                        divider = panel_style.split(
                            "QSplitter#standaloneChatSplitter::handle:vertical {",
                            1,
                        )[1].split("}", 1)[0]
                        self.assertNotIn(colors["border"], divider)
                        self.assertIn("transparent", divider)
                        self.assertIn(
                            f"background-color: {colors['accent']}",
                            panel_style,
                        )
                        self.assertIn(
                            f"color: {colors['text']}",
                            panel_style,
                        )
        finally:
            panel.close()

    def test_settings_dialog_exposes_the_system_dark_light_preference(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            self.assertEqual(
                [dialog.color_scheme_combo.itemData(index)
                 for index in range(dialog.color_scheme_combo.count())],
                ["system", "dark", "light"],
            )
            dialog.color_scheme_combo.setCurrentIndex(
                dialog.color_scheme_combo.findData("light"))
            self.assertEqual(dialog.values()["color_scheme"], "light")
            self.assertFalse(dialog.pdf_canvas_theme_check.isChecked())
            dialog.pdf_canvas_theme_check.setChecked(True)
            self.assertTrue(
                dialog.values()["pdf_canvas_theme_background"])
        finally:
            dialog.close()

    def test_legacy_theme_settings_keep_their_existing_appearance(self):
        from config.settings import load

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            with mock.patch("config.settings.SETTINGS_PATH", settings_path):
                self.assertEqual(load()["color_scheme"], "system")

                settings_path.write_text(
                    json.dumps({"theme": "Dark Slate"}), encoding="utf-8")
                self.assertEqual(load()["color_scheme"], "dark")

                settings_path.write_text(
                    json.dumps({"theme": "Pure Light"}), encoding="utf-8")
                self.assertEqual(load()["color_scheme"], "light")

    def test_user_themes_register_both_schemes_and_reject_invalid_colors(self):
        from ui.theme import (
            THEME_VARIANTS, apply_color_grading, register_user_themes,
            theme_colors,
        )

        source = THEME_VARIANTS["Dark Slate"]
        custom = register_user_themes({
            "Reading Room": {
                "dark": dict(source["dark"]),
                "light": dict(source["light"]),
            },
            "Dark Slate": {"dark": {}, "light": {}},
        })
        dark = apply_color_grading(
            custom["Reading Room"]["dark"],
            {"offset": (0.8, -0.2, 0.0)},
        )
        light = apply_color_grading(
            custom["Reading Room"]["light"],
            {"offset": (-0.5, 0.3, 0.0)},
        )
        custom["Reading Room"]["dark"] = dark
        custom["Reading Room"]["light"] = light
        register_user_themes(custom)

        self.assertEqual(theme_colors("Reading Room", "dark"), dark)
        self.assertEqual(theme_colors("Reading Room", "light"), light)
        self.assertNotEqual(dark, source["dark"])
        self.assertNotEqual(
            theme_colors("Reading Room", "light")["window"],
            theme_colors("Reading Room", "light")["surface"],
        )
        self.assertNotIn("Dark Slate", custom)

    def test_color_grading_targets_luminance_ranges_and_offset(self):
        from PySide6.QtGui import QColor
        from ui.theme import apply_color_grading

        palette = {
            "dark": "#181818",
            "middle": "#808080",
            "bright": "#F0F0F0",
        }
        neutral = apply_color_grading(
            palette, {"lift": (0, 0, 0), "gamma": (0, 0, 0)})
        self.assertEqual(neutral, palette)

        lift = apply_color_grading(palette, {"lift": (0, 0, 0.8)})
        self.assertGreater(
            QColor(lift["dark"]).lightness(),
            QColor(palette["dark"]).lightness(),
        )
        self.assertEqual(lift["bright"], palette["bright"])

        gamma = apply_color_grading(palette, {"gamma": (0, 0, 0.8)})
        self.assertGreater(
            QColor(gamma["middle"]).lightness(),
            QColor(palette["middle"]).lightness(),
        )
        self.assertNotEqual(gamma["dark"], palette["dark"])

        offset = apply_color_grading(palette, {"offset": (0.5, 0, 0)})
        self.assertNotEqual(offset["dark"], palette["dark"])
        self.assertNotEqual(offset["middle"], palette["middle"])
        self.assertNotEqual(offset["bright"], palette["bright"])

    def test_master_tone_curve_maps_rgb_channels_and_preserves_neutral_curve(self):
        from PySide6.QtGui import QColor
        from ui.theme import apply_color_curve

        palette = {
            "shadow": "#404040",
            "midtone": "#808080",
            "highlight": "#E0E0E0",
        }
        identity = ((0.0, 0.0), (1.0, 1.0))
        self.assertEqual(apply_color_curve(palette, identity), palette)

        lifted_midtones = apply_color_curve(
            palette,
            ((0.0, 0.0), (0.5, 0.8), (1.0, 1.0)),
        )
        self.assertGreater(
            QColor(lifted_midtones["midtone"]).red(),
            QColor(palette["midtone"]).red(),
        )
        self.assertGreater(
            QColor(lifted_midtones["shadow"]).red(),
            QColor(palette["shadow"]).red(),
        )

    def test_lift_gamma_gain_have_distinct_tonal_responses(self):
        from PySide6.QtGui import QColor
        from ui.theme import apply_color_grading

        palette = {
            "shadow": "#1A1A1A",
            "midtone": "#808080",
            "highlight": "#E6E6E6",
        }
        changes = {}
        for wheel in ("lift", "gamma", "gain"):
            graded = apply_color_grading(
                palette, {wheel: (0, 0, 0.5)})
            changes[wheel] = {
                key: (
                    QColor(graded[key]).lightness()
                    - QColor(palette[key]).lightness()
                )
                for key in palette
            }

        self.assertGreater(
            changes["lift"]["shadow"], changes["gamma"]["shadow"])
        self.assertGreater(
            changes["gamma"]["midtone"], changes["lift"]["midtone"])
        self.assertGreater(
            changes["gamma"]["midtone"], changes["gain"]["midtone"])
        self.assertGreater(
            changes["gain"]["highlight"],
            changes["gamma"]["highlight"],
        )

    def test_color_balance_wheel_selects_balance_inside_the_ring(self):
        from PySide6.QtCore import QPointF
        from ui.theme_editor import ColorBalanceWheel

        wheel = ColorBalanceWheel()
        wheel.resize(260, 220)
        puck_radius = ((220 / 2 - 2) * 0.78 - 1) * 0.88
        wheel._select_balance(QPointF(
            130 + puck_radius * 0.5,
            110,
        ))
        self.assertAlmostEqual(wheel._balance_x, 0.5, delta=0.005)
        self.assertAlmostEqual(wheel._balance_y, 0, delta=0.005)

    def test_color_balance_wheel_supports_fine_keyboard_adjustments(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from ui.theme_editor import ColorBalanceWheel

        wheel = ColorBalanceWheel()
        changes = []
        wheel.balanceChanged.connect(
            lambda x, y: changes.append((x, y)))
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Right,
            Qt.KeyboardModifier.ControlModifier,
        )
        wheel.keyPressEvent(event)
        self.assertEqual(wheel.balance, (0.002, 0.0))
        self.assertEqual(changes, [(0.002, 0.0)])

    def test_color_balance_wheel_caches_ring_between_repaints(self):
        from PySide6.QtGui import QColor
        from ui.theme_editor import ColorBalanceWheel, _wheel_image

        wheel = ColorBalanceWheel()
        wheel.resize(170, 170)
        image = _wheel_image(156)
        self.assertGreater(
            QColor(image.pixelColor(78, 5)).alpha(), 0)
        wheel.grab()
        cached_image = _wheel_image(156)
        wheel.setBalance(0.5, -0.25)
        wheel.grab()
        self.assertIs(_wheel_image(156), cached_image)

    def test_tone_curve_editor_adds_and_removes_control_points(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from ui.theme_editor import ToneCurveEditor

        editor = ToneCurveEditor()
        editor.resize(320, 220)
        editor.show()
        self.app.processEvents()
        try:
            plot = editor._plot_rect()
            added_point = QPoint(
                round(plot.left() + plot.width() * 0.5),
                round(plot.top() + plot.height() * 0.35),
            )
            QTest.mouseClick(editor, Qt.MouseButton.LeftButton, pos=added_point)
            self.assertEqual(len(editor.points), 3)
            QTest.mouseClick(
                editor, Qt.MouseButton.RightButton, pos=added_point)
            self.assertEqual(editor.points, ((0.0, 0.0), (1.0, 1.0)))
        finally:
            editor.close()

    def test_personal_theme_wheels_only_grade_the_selected_color_group(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import register_user_themes, theme_colors

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            dialog._ensure_personal_theme()
            base = dict(theme_colors("My Theme", "dark"))
            group_roles = dialog._theme_color_groups
            assigned_roles = [
                role for roles in group_roles.values() for role in roles
            ]
            self.assertEqual(set(assigned_roles), set(base))
            self.assertEqual(len(assigned_roles), len(set(assigned_roles)))

            for group, roles in group_roles.items():
                dialog._theme_group_checkboxes[group].setChecked(True)
                for wheel_name in dialog._theme_grade_names:
                    with self.subTest(group=group, wheel=wheel_name):
                        dialog._reset_all_theme_grades()
                        dialog._theme_group_checkboxes[group].setChecked(True)
                        self.assertEqual(
                            sum(
                                checkbox.isChecked()
                                for checkbox
                                in dialog._theme_group_checkboxes.values()
                            ),
                            1,
                        )
                        dialog._theme_grade_wheels[wheel_name].setBalance(
                            0.6, -0.3, emit=True)
                        changed = theme_colors("My Theme", "dark")
                        changed_roles = {
                            key for key in base if changed[key] != base[key]
                        }
                        self.assertTrue(changed_roles)
                        self.assertTrue(changed_roles.issubset(set(roles)))

            dialog._reset_all_theme_grades()
            dialog._theme_group_checkboxes["accent"].setChecked(True)
            dialog._theme_grade_wheels["gamma"].setBalance(
                0.4, -0.2, emit=True)
            accent_state = dialog._theme_grade_states["accent"]["gamma"]
            dialog._theme_group_checkboxes["background"].setChecked(True)
            dialog._theme_grade_wheels["gamma"].setBalance(
                -0.3, 0.5, emit=True)
            dialog._theme_group_checkboxes["accent"].setChecked(True)
            self.assertEqual(
                dialog._theme_grade_wheels["gamma"].balance,
                accent_state[:2],
            )
            self.assertEqual(set(dialog._theme_grade_wheels), {
                "lift", "gamma", "gain", "offset",
            })
            self.assertEqual([
                dialog._theme_wheels_layout.getItemPosition(index)[:2]
                for index in range(4)
            ], [(0, 0), (0, 1), (1, 0), (1, 1)])
            self.assertTrue(all(
                wheel.maximumWidth() > 156
                for wheel in dialog._theme_grade_wheels.values()
            ))
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()
            register_user_themes(DEFAULTS.get("user_themes"))

    def test_first_grade_event_keeps_selected_group_for_existing_personal_theme(
        self,
    ):
        from PySide6.QtCore import Qt
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import (
            THEME_VARIANTS, register_user_themes, theme_colors,
        )

        palette = {
            scheme: dict(colors)
            for scheme, colors in THEME_VARIANTS["Dark Slate"].items()
        }
        register_user_themes({"My Theme 2": palette})
        dialog = SettingsDialog(dict(
            DEFAULTS,
            theme="My Theme 2",
            color_scheme="dark",
            user_themes={"My Theme 2": palette},
        ))
        try:
            dialog._theme_group_checkboxes["background"].click()
            dialog._theme_grade_wheels["offset"].setBalance(
                0.6, -0.3, emit=True)
            self.assertEqual(
                dialog._active_theme_color_group, "background")
            self.assertTrue(
                dialog._theme_group_checkboxes["background"].isChecked())
            changed = theme_colors("My Theme 2", "dark")
            changed_roles = {
                key for key in palette["dark"]
                if changed[key] != palette["dark"][key]
            }
            self.assertTrue(changed_roles)
            self.assertTrue(changed_roles.issubset(
                set(dialog._theme_color_groups["background"])))
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()
            register_user_themes(DEFAULTS.get("user_themes"))

    def test_personal_theme_curve_only_changes_and_tracks_selected_group(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import register_user_themes, theme_colors

        register_user_themes({})
        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            dialog._ensure_personal_theme()
            base = dict(theme_colors("My Theme", "dark"))
            points = ((0.0, 0.0), (0.5, 0.8), (1.0, 1.0))

            dialog._theme_group_checkboxes["text"].setChecked(True)
            dialog._theme_curve_editor.setPoints(points, emit=True)
            changed = theme_colors("My Theme", "dark")
            changed_roles = {
                key for key in base if changed[key] != base[key]
            }
            self.assertTrue(changed_roles)
            self.assertTrue(changed_roles.issubset({"text", "muted"}))

            dialog._theme_group_checkboxes["accent"].setChecked(True)
            self.assertEqual(
                dialog._theme_curve_editor.points,
                ((0.0, 0.0), (1.0, 1.0)),
            )
            dialog._theme_group_checkboxes["text"].setChecked(True)
            self.assertEqual(dialog._theme_curve_editor.points, points)
            dialog._reset_theme_curve()
            reset = theme_colors("My Theme", "dark")
            self.assertEqual(reset["text"], base["text"])
            self.assertEqual(reset["muted"], base["muted"])
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()
            register_user_themes(DEFAULTS.get("user_themes"))

    def test_creating_personal_theme_keeps_selected_group_and_wheel_position(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import register_user_themes, theme_colors

        register_user_themes({})
        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            dialog._theme_group_checkboxes["background"].setChecked(True)
            dialog._theme_grade_wheels["offset"].setBalance(
                0.6, -0.3, emit=True)
            self.assertEqual(dialog._settings["theme"], "My Theme")
            self.assertEqual(dialog._active_theme_color_group, "background")
            self.assertEqual(
                dialog._theme_grade_wheels["offset"].balance,
                (0.6, -0.3),
            )
            self.assertEqual(
                dialog._theme_grade_states["background"]["offset"][:2],
                (0.6, -0.3),
            )
            changed = theme_colors("My Theme", "dark")
            self.assertNotEqual(
                changed["window"],
                dialog._theme_grade_base["window"],
            )
            self.assertEqual(
                changed["accent"],
                dialog._theme_grade_base["accent"],
            )

            from PySide6.QtTest import QTest
            QTest.qWait(75)
            dialog._begin_theme_grade("My Theme", "dark")
            self.assertEqual(
                dialog._theme_grade_wheels["offset"].balance,
                (0.6, -0.3),
            )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()
            register_user_themes(DEFAULTS.get("user_themes"))

    def test_theme_role_groups_are_disjoint_and_cover_every_builtin_palette(self):
        from ui.settings_dialog import SettingsDialog
        from ui.theme import THEME_GROUPS, THEME_VARIANTS
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            assigned_roles = [
                role
                for roles in dialog._theme_color_groups.values()
                for role in roles
            ]
            self.assertEqual(len(assigned_roles), len(set(assigned_roles)))
            for theme, variants in THEME_VARIANTS.items():
                for scheme, palette in variants.items():
                    with self.subTest(theme=theme, scheme=scheme):
                        self.assertEqual(set(assigned_roles), set(palette))
            self.assertEqual(
                tuple(
                    (label, dialog._theme_color_groups[label.lower()])
                    for label, _roles in THEME_GROUPS
                ),
                (
                    ("Background", (
                        "window", "toolbar", "panel", "canvas", "surface",
                        "surface_hover",
                    )),
                    ("Border", ("border",)),
                    ("Text", ("text", "muted")),
                    ("Accent", (
                        "accent", "accent_hover", "accent_light", "selection",
                        "scroll", "scroll_hover",
                    )),
                    ("Status", ("success", "warning")),
                ),
            )
            self.assertEqual(
                tuple(dialog._theme_group_checkboxes),
                ("background", "border", "text", "accent", "status"),
            )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()

    def test_personal_theme_editor_previews_and_saves_palette_values(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import PAGE_MY_THEME, SettingsDialog
        from ui.theme import (
            THEME_VARIANTS, register_user_themes, theme_colors,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            with mock.patch("config.settings.SETTINGS_PATH", settings_path):
                register_user_themes({})
                dialog = SettingsDialog(dict(
                    DEFAULTS, color_scheme="dark"))
                previews = []
                try:
                    dialog._stack.setCurrentIndex(PAGE_MY_THEME)
                    dialog.show()
                    self.app.processEvents()
                    dialog._ensure_personal_theme()
                    dialog.previewSettingsChanged.connect(
                        lambda settings, restoring:
                            previews.append((settings, restoring))
                    )
                    dialog._theme_group_checkboxes["text"].setChecked(True)
                    dialog._theme_grade_wheels["offset"].setBalance(
                        0.8, -0.3, emit=True)
                    self.assertTrue(dialog._theme_preview_timer.isActive())
                    self.assertFalse(previews)
                    dialog._theme_preview_timer.stop()
                    dialog._flush_theme_grade_preview()
                    saved = dialog.values()
                    self.assertEqual(saved["theme"], "My Theme")
                    self.assertNotEqual(
                        theme_colors("My Theme", "dark"),
                        THEME_VARIANTS["Dark Slate"]["dark"],
                    )
                    self.assertTrue(previews)
                    self.assertFalse(previews[-1][1])
                    self.assertIn("light", saved["user_themes"]["My Theme"])
                    from config.settings import load, save
                    save(saved)
                    self.assertEqual(
                        load()["user_themes"]["My Theme"]["dark"],
                        saved["user_themes"]["My Theme"]["dark"],
                    )

                    dialog.reject()
                    self.assertTrue(previews[-1][1])
                    self.assertEqual(
                        dialog._settings["theme"],
                        DEFAULTS["theme"],
                    )
                    self.assertNotIn(
                        "My Theme",
                        dialog._settings["user_themes"],
                    )
                finally:
                    dialog.close()
                    register_user_themes({})

    def test_theme_grade_flush_avoids_reapplying_full_settings_stylesheet(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import PAGE_MY_THEME, SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        previews = []
        dialog.previewSettingsChanged.connect(
            lambda settings, restoring: previews.append(
                    (settings, restoring)))
        try:
            dialog._stack.setCurrentIndex(PAGE_MY_THEME)
            self.assertIsNotNone(dialog._theme_role_widget_cache)
            page = dialog._stack.currentWidget()
            initial_page_style = page.styleSheet()
            dialog._theme_group_checkboxes["text"].setChecked(True)
            dialog._theme_grade_wheels["offset"].setBalance(
                0.8, -0.3, emit=True)
            dialog._theme_preview_timer.stop()
            with mock.patch.object(dialog, "_apply_theme_style") as apply_style:
                    dialog._flush_theme_grade_preview()
            apply_style.assert_not_called()
            self.assertEqual(len(previews), 1)
            self.assertFalse(previews[0][1])
            self.assertEqual(page.styleSheet(), initial_page_style)
            dialog._apply_live_theme_styles()
            colors = dialog._theme_colors()
            self.assertIn(
                f"color: {colors['text']}",
                page.styleSheet(),
            )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()

    def test_theme_group_selectors_are_exclusive_radio_buttons(self):
        from PySide6.QtWidgets import QRadioButton
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            self.assertTrue(all(
                isinstance(button, QRadioButton)
                for button in dialog._theme_group_checkboxes.values()
            ))
            self.assertTrue(dialog._theme_group_button_group.exclusive())
            dialog._theme_group_checkboxes["background"].setChecked(True)
            dialog._theme_group_checkboxes["text"].setChecked(True)
            self.assertEqual(
                dialog._active_theme_color_group, "text")
            self.assertEqual(
                sum(button.isChecked()
                    for button in dialog._theme_group_checkboxes.values()),
                1,
            )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()

    def test_grade_preview_defers_sample_restyle_until_preview_timer(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        previews = []
        dialog.previewSettingsChanged.connect(
            lambda settings, restoring: previews.append(
                (settings, restoring)))
        try:
            self.assertEqual(dialog._theme_preview_timer.interval(), 16)
            dialog._ensure_personal_theme()
            with mock.patch.object(
                dialog, "_update_theme_editor_sample"
            ) as update_sample:
                dialog._theme_grade_wheels["lift"].setBalance(
                    0.4, -0.2, emit=True)
                update_sample.assert_not_called()
                dialog._flush_theme_grade_preview()

            update_sample.assert_called_once()
            self.assertEqual(len(previews), 1)
            self.assertTrue(previews[0][0]["_grade_editing"])
            self.assertFalse(previews[0][1])
            self.assertNotIn("_grade_editing", dialog.values())
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()

    def test_save_flushes_pending_border_grade_preview_before_accepting(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import theme_colors

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        previews = []
        dialog.previewSettingsChanged.connect(
            lambda settings, restoring: previews.append(
                (settings, restoring)))
        try:
            dialog._ensure_personal_theme()
            dialog._select_theme_color_group("border", True)
            dialog._on_theme_grade_changed("offset", master=0.4)
            self.assertTrue(dialog._theme_preview_timer.isActive())
            values = dialog.values()
            saved_border = values["user_themes"][
                values["theme"]]["dark"]["border"]

            with mock.patch("config.settings.save"):
                dialog.accept()

            self.assertTrue(previews)
            latest_settings, restoring = previews[-1]
            self.assertFalse(restoring)
            self.assertTrue(latest_settings["_grade_editing"])
            self.assertEqual(
                latest_settings["user_themes"][
                    latest_settings["theme"]]["dark"]["border"],
                saved_border,
            )
            self.assertNotEqual(
                saved_border,
                theme_colors("Dark Slate", "dark")["border"],
            )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()

    def test_user_theme_palette_change_requires_full_skin_apply(self):
        from main import MainWindow

        previous = {"theme": "My Theme", "user_themes": {"My Theme": {}}}
        current = {"theme": "My Theme", "user_themes": {"My Theme": {
            "dark": {"accent": "#123456"},
        }}}
        self.assertIn("user_themes", MainWindow._APP_SKIN_KEYS)
        self.assertTrue(MainWindow._settings_changed(
            previous, current, MainWindow._APP_SKIN_KEYS))

    def test_grade_preview_updates_all_fast_theme_surfaces(self):
        from unittest import mock
        from main import MainWindow
        from ui.theme import THEMES

        app = mock.Mock()
        app.settings = {"theme": "My Theme", "color_scheme": "dark"}
        app._theme_actions = {name: object() for name in THEMES}
        app._apply_theme_preview_fast = mock.Mock()
        app._apply_theme_grade_preview_fast = lambda theme: (
            MainWindow._apply_theme_grade_preview_fast(app, theme))
        preview = {
            "theme": "My Theme",
            "color_scheme": "dark",
            "user_themes": {
                "My Theme": {
                    "dark": {"accent": "#123456"},
                    "light": {"accent": "#123456"},
                },
            },
            "_grade_editing": True,
        }

        MainWindow._preview_theme_from_settings(app, preview, False)

        app._apply_theme_preview_fast.assert_called_once_with("My Theme")
        app._queue_theme_preview_refresh.assert_not_called()
        self.assertTrue(app._theme_preview_requires_popup_refresh)
        self.assertNotIn("_grade_editing", app.settings)

    def test_live_theme_preview_restyles_all_role_groups_and_pages(self):
        from PySide6.QtWidgets import QCheckBox, QLabel
        from config.settings import DEFAULTS
        from ui.settings_dialog import PAGE_MY_THEME, SettingsDialog
        from ui.theme import (
            THEME_COLOR_KEYS, THEME_GROUPS, register_user_themes,
            settings_stylesheet, theme_colors,
        )

        palette = dict(theme_colors("Dark Slate", "dark"))
        for index, role in enumerate(THEME_COLOR_KEYS, start=1):
            palette[role] = f"#{(0x102030 + index * 0x010101):06X}"
        user_themes = {
            "Role Coverage": {
                "dark": dict(palette),
                "light": dict(palette),
            },
        }
        register_user_themes(user_themes)
        provider = {
            "id": "theme-test",
            "name": "Theme Test",
            "base_url": "",
            "api_key": "",
            "model": "",
            "enabled": True,
            "reasoning_enabled": False,
            "vision_enabled": False,
        }
        dialog = SettingsDialog(dict(
            DEFAULTS,
            theme="Role Coverage",
            color_scheme="dark",
            user_themes=user_themes,
            custom_providers=[provider],
        ))
        try:
            dialog._stack.setCurrentIndex(PAGE_MY_THEME)
            dialog._apply_live_theme_styles()
            page = dialog._stack.currentWidget()
            page_style = settings_stylesheet("Role Coverage", "dark")
            self.assertEqual(page.styleSheet(), page_style)

            for label, roles in THEME_GROUPS:
                with self.subTest(group=label):
                    dialog._theme_group_checkboxes[
                        label.lower()].setChecked(True)
                    self.assertEqual(
                        dialog._active_theme_color_group, label.lower())
                    for role in roles:
                        if role != "canvas":
                            self.assertIn(palette[role], page_style)

            eyebrow = page.findChild(QLabel, "sectionEyebrow")
            if eyebrow is not None:
                self.assertIn(
                    palette["accent_light"], eyebrow.styleSheet())
            self.assertIn(
                f"background: {palette['accent']}",
                dialog.theme_preview_button.styleSheet(),
            )
            self.assertIn(
                f"color: {palette['text']}",
                dialog.theme_preview_button.styleSheet(),
            )
            provider_card = dialog._custom_cards[0]
            self.assertIn(palette["border"], provider_card.styleSheet())
            self.assertIn(palette["success"], provider_card.styleSheet())
            self.assertIn(palette["warning"], provider_card.styleSheet())
            active_toggle = provider_card.findChild(
                QCheckBox, "providerActiveCheck")
            self.assertIsNotNone(active_toggle)
            self.assertIn(
                palette["success"], provider_card.styleSheet())

            dialog._update_key_states()
            self.assertIn(
                palette["warning"], dialog.gemini_state.styleSheet())
            badge = dialog.model_image_badge
            for role_property in (
                "theme_background_role",
                "theme_color_role",
                "theme_border_role",
            ):
                role = badge.property(role_property)
                if role in palette:
                    self.assertIn(palette[role], badge.styleSheet())

            dialog._stack.setCurrentIndex(1)
            self.assertEqual(
                dialog._stack.currentWidget().styleSheet(), page_style)
            dialog._clear_live_theme_styles()
            self.assertTrue(all(
                not stacked_page.styleSheet()
                for stacked_page in dialog._page_keys
            ))
            dialog._settings["theme"] = "Dark Slate"
            dialog._apply_theme_style()
            eyebrow = dialog._page_keys[
                PAGE_MY_THEME].findChild(QLabel, "sectionEyebrow")
            if eyebrow is not None:
                self.assertIn(
                    theme_colors("Dark Slate", "dark")["accent_light"],
                    eyebrow.styleSheet(),
                )
        finally:
            dialog._theme_preview_timer.stop()
            dialog.close()
            register_user_themes(DEFAULTS.get("user_themes"))

    def test_main_live_theme_preview_updates_welcome_buttons_and_menus(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from PySide6.QtWidgets import (
            QComboBox, QMenu, QTabWidget, QToolBar, QToolButton, QWidget,
        )
        from config.settings import DEFAULTS
        from main import MainWindow
        from ui.theme import theme_colors

        app_style_before = self.app.styleSheet()
        central = QWidget()
        host = SimpleNamespace(
            settings=dict(DEFAULTS, theme="Everforest", color_scheme="dark"),
            _toolbar=QToolBar(),
            tabs=QTabWidget(),
            centralWidget=lambda: central,
            welcome=QWidget(),
            file_menu=QMenu(),
            fit_menu=QMenu(),
            theme_menu=QMenu(),
            _iter_books=lambda: (),
            _apply_chat_theme=Mock(),
            _apply_model_readout_theme=Mock(),
        )
        colors = theme_colors("Everforest", "dark")
        toolbar_button = QToolButton(host._toolbar)
        toolbar_combo = QComboBox(host._toolbar)
        host._toolbar.addWidget(toolbar_button)
        host._toolbar.addWidget(toolbar_combo)
        book = SimpleNamespace(apply_theme=Mock())
        host._iter_books = Mock(return_value=[book])
        try:
            MainWindow._apply_theme_preview_fast(host, "Everforest")
            book.apply_theme.assert_called_with(dict(
                colors, canvas="#F1F3F5"))
            toolbar_style = host._toolbar.styleSheet()
            self.assertNotIn("QToolBar QToolButton:checked", toolbar_style)
            for role in (
                "surface", "text", "border", "surface_hover",
                "accent_light", "selection", "panel",
            ):
                self.assertIn(colors[role], toolbar_style)
            self.assertIn("QToolBar QToolButton", toolbar_style)
            self.assertIn("QToolBar QComboBox", toolbar_style)
            self.assertTrue(central.autoFillBackground())
            self.assertEqual(
                central.palette().color(central.backgroundRole()).name(),
                colors["window"].lower(),
            )
            self.assertIn(
                f"background-color: {colors['canvas']}",
                host.tabs.styleSheet(),
            )
            self.assertIn(
                f"background-color: {colors['toolbar']}",
                host.tabs.tabBar().styleSheet(),
            )
            self.assertIn(
                f"background-color: {colors['surface']}",
                host.tabs.tabBar().styleSheet(),
            )
            if colors["surface"] != colors["selection"]:
                self.assertNotIn(
                    f"background-color: {colors['selection']}",
                    host.tabs.tabBar().styleSheet(),
                )
            welcome_style = host.welcome.styleSheet()
            self.assertIn(colors["panel"], welcome_style)
            self.assertIn(colors["surface"], welcome_style)
            self.assertIn(colors["accent"], welcome_style)
            self.assertIn(colors["accent_hover"], welcome_style)
            self.assertIn(colors["text"], welcome_style)
            self.assertIn(colors["muted"], welcome_style)
            for menu in (host.file_menu, host.fit_menu, host.theme_menu):
                self.assertIn(colors["panel"], menu.styleSheet())
                self.assertIn(colors["selection"], menu.styleSheet())
            host.settings["pdf_canvas_theme_background"] = True
            MainWindow._apply_theme_preview_fast(host, "Everforest")
            book.apply_theme.assert_called_with(colors)
            MainWindow._clear_theme_preview_fast_styles(host)
            self.assertEqual(host._toolbar.styleSheet(), "")
            self.assertEqual(host.tabs.styleSheet(), "")
            self.assertEqual(host.tabs.tabBar().styleSheet(), "")
            self.assertEqual(host.welcome.styleSheet(), "")
            self.assertTrue(all(
                not menu.styleSheet()
                for menu in (
                    host.file_menu, host.fit_menu, host.theme_menu)
            ))
            self.assertEqual(self.app.styleSheet(), app_style_before)
        finally:
            for widget in (
                toolbar_button, toolbar_combo, host._toolbar, host.tabs,
                host.welcome, host.file_menu,
                host.fit_menu, host.theme_menu,
            ):
                widget.close()

    def test_main_window_applies_and_reverts_live_theme_preview(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from config.settings import DEFAULTS
        from main import MainWindow
        from ui.theme import (
            THEME_VARIANTS, register_user_themes, theme_colors,
        )

        baseline = dict(DEFAULTS, color_scheme="dark")
        host = SimpleNamespace(
            settings=dict(baseline),
            _theme_actions={
                name: Mock() for name in THEME_VARIANTS
            },
            theme_menu=Mock(),
            _sync_language_combos=Mock(),
            _sync_selection_mode_button=Mock(),
            _refresh_engine_label=Mock(),
            _sync_theme_button=Mock(),
            _apply_theme_skin=Mock(),
            _apply_theme_preview_fast=Mock(),
            _clear_theme_preview_fast_styles=Mock(),
            _queue_theme_preview_refresh=Mock(),
            popups=[],
        )
        palette = {
            scheme: dict(colors)
            for scheme, colors in THEME_VARIANTS["Dark Slate"].items()
        }
        palette["dark"]["accent"] = "#D020A0"
        preview = dict(
            baseline,
            theme="Reading Room",
            user_themes={"Reading Room": palette},
        )

        MainWindow._preview_theme_from_settings(host, preview, False)
        self.assertEqual(host.settings["theme"], "Reading Room")
        self.assertEqual(host.settings["user_themes"], preview["user_themes"])
        host._apply_theme_skin.assert_not_called()
        host._apply_theme_preview_fast.assert_called_with("Reading Room")
        self.assertIn("Reading Room", host._theme_actions)

        MainWindow._preview_theme_from_settings(host, baseline, True)
        self.assertEqual(host.settings["theme"], baseline["theme"])
        host._apply_theme_preview_fast.assert_called_with(baseline["theme"])
        host._clear_theme_preview_fast_styles.assert_called_once()
        host._apply_theme_skin.assert_called_with(baseline["theme"])
        self.assertNotIn("Reading Room", host._theme_actions)

        live_palette = {
            scheme: dict(colors)
            for scheme, colors in THEME_VARIANTS["Dark Slate"].items()
        }
        live_palette["dark"]["accent"] = "#D020A0"
        live_preview = dict(
            baseline,
            theme="Reading Room",
            color_scheme="dark",
            user_themes={"Reading Room": live_palette},
        )
        MainWindow._preview_theme_from_settings(host, live_preview, False)
        stale_disk = dict(
            baseline,
            theme="Dark Slate",
            user_themes={},
            fast_mode=not baseline["fast_mode"],
        )
        register_user_themes(stale_disk["user_themes"])
        kept = MainWindow._keep_live_theme_preview(host, stale_disk)
        self.assertEqual(kept["theme"], "Reading Room")
        self.assertEqual(kept["fast_mode"], stale_disk["fast_mode"])
        self.assertEqual(
            theme_colors("Reading Room", "dark")["accent"], "#D020A0")
        self.assertEqual(
            kept["user_themes"]["Reading Room"]["dark"]["accent"],
            "#D020A0",
        )
        host.settings = dict(live_preview)
        stale_theme_file = dict(
            host.settings, theme="Dark Slate", user_themes={})
        register_user_themes(stale_theme_file["user_themes"])
        kept_after_reload = MainWindow._keep_live_theme_preview(
            host, stale_theme_file)
        self.assertEqual(kept_after_reload["theme"], "Reading Room")
        self.assertEqual(
            kept_after_reload["user_themes"]["Reading Room"]["dark"]["accent"],
            "#D020A0",
        )
        self.assertEqual(
            theme_colors("Reading Room", "dark")["accent"], "#D020A0")
        MainWindow._preview_theme_from_settings(host, baseline, True)
        register_user_themes(baseline.get("user_themes"))

    def test_book_border_group_colors_thumbnail_frames_but_draws_no_lines(self):
        """The Border group paints the thumbnail frames, nothing else.

        It used to also paint the splitter handle, the bookmark rail's
        right edge, the thumbnail panel's right edge and the list's own
        left and right edges. The user asked for those vertical lines to be
        removed: they were decoration, not information. The per-thumbnail
        rounded frames stay - they are cards, not lines - and so do the
        scrollbars.
        """
        from main import app_stylesheet
        from reader.bookview import BookView
        from ui.theme import theme_colors

        app = self.app
        previous_style = app.styleSheet()
        colors = dict(theme_colors("Everforest", "dark"))
        book = BookView()
        try:
            book.apply_theme(colors)
            book.resize(700, 500)
            book.show()
            app.setStyleSheet(app_stylesheet("Everforest", "dark"))
            book.apply_theme(colors)
            app.processEvents()
            delegate = book.thumbs.list.itemDelegate()
            bookmark_delegate = book.thumbs.bookmarks_list.itemDelegate()
            thumbs_style = book.thumbs.styleSheet()
            rendered_list = book.thumbs.list.grab().toImage()
            self.assertGreater(
                sum(
                    rendered_list.pixelColor(x, y).name().lower()
                    == colors["border"].lower()
                    for y in range(rendered_list.height())
                    for x in range(rendered_list.width())
                ),
                0,
            )
            self.assertEqual(
                delegate._border_color.name(), colors["border"].lower())
            self.assertEqual(
                bookmark_delegate._border_color.name(),
                colors["border"].lower(),
            )
            self.assertIn(
                "background-color: transparent",
                book.splitter.styleSheet(),
            )
            self.assertNotIn(
                "border-right",
                thumbs_style,
                "the rail and panel edges were standing vertical lines",
            )
            self.assertIn(
                "QListWidget#pdfThumbnails, QListWidget#pdfBookmarks "
                f"{{ background-color: {colors['panel']}; "
                f"color: {colors['text']}; border: none; }}",
                thumbs_style,
            )
            self.assertNotIn("QScrollBar#pdfThumbnailScrollBar", thumbs_style)
            for scrollbar in (
                book.reader.verticalScrollBar(),
                book.thumbs.list.verticalScrollBar(),
                book.thumbs.bookmarks_list.verticalScrollBar(),
            ):
                self.assertIn(
                    f"background-color: {colors['scroll']}",
                    scrollbar.styleSheet(),
                )
                self.assertIn(
                    f"background-color: {colors['scroll_hover']}",
                    scrollbar.styleSheet(),
                )
        finally:
            book.close()
            app.setStyleSheet(previous_style)

    def test_main_thumbnail_list_has_no_box_around_it(self):
        """The list's own left and right edges were removed on request.

        The per-thumbnail frames are painted by the delegate and are
        covered by test_thumbnail_delegate_paints_the_selected_border_color.
        """
        from main import app_stylesheet

        style = app_stylesheet("Everforest", "dark")
        self.assertIn(
            "QListWidget#pdfThumbnails,\n"
            "QListWidget#pdfBookmarks {\n",
            style,
        )
        block = style.split("QListWidget#pdfThumbnails,\n"
                            "QListWidget#pdfBookmarks {", 1)[1]
        self.assertIn("border: none;", block.split("}", 1)[0])

    def test_thumbnail_delegate_paints_the_selected_border_color(self):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QColor, QImage, QIcon, QPixmap
        from PySide6.QtWidgets import QListWidgetItem
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        border = QColor("#12AB34")
        try:
            page_list = panel.list
            page_list.setIconSize(QSize(80, 100))
            page_list.setGridSize(QSize(100, 130))
            page = QImage(80, 100, QImage.Format.Format_ARGB32)
            page.fill(QColor("white"))
            page_list.addItem(QListWidgetItem(
                QIcon(QPixmap.fromImage(page)), "test page"))
            panel.set_border_color(border.name())
            panel.resize(140, 170)
            panel.show()
            self.app.processEvents()

            rendered = page_list.viewport().grab().toImage()
            self.assertTrue(any(
                rendered.pixelColor(x, y) == border
                for y in range(rendered.height())
                for x in range(rendered.width())
            ))
        finally:
            panel.close()

    def test_theme_save_forces_surface_apply_when_stylesheet_is_unchanged(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from main import MainWindow, app_stylesheet

        theme = "Dark Slate"
        scheme = "dark"
        stylesheet = app_stylesheet(theme, scheme)
        timer = Mock()
        host = SimpleNamespace(
            settings={"theme": theme, "color_scheme": scheme},
            _applied_app_stylesheet=stylesheet,
            _applied_theme=theme,
            _theme_preview_requires_full_apply=True,
            _pending_skin=None,
            _skin_already_applied=lambda candidate, selected: (
                candidate == stylesheet and selected == theme
            ),
            _ensure_app_stylesheet_timer=Mock(return_value=timer),
        )

        MainWindow._apply_theme_skin(host, theme)

        self.assertEqual(host._pending_skin, (stylesheet, theme))
        self.assertTrue(host._pending_skin_force_apply)
        timer.start.assert_called_once()

        host._clear_theme_preview_fast_styles = Mock()
        host._apply_theme_to_books = Mock()
        host._apply_chat_theme = Mock()
        host._apply_model_readout_theme = Mock()
        host._pending_skin = (stylesheet, theme)
        with patch("main.QApplication.instance") as app_instance:
            MainWindow._flush_app_skin(host)

        app_instance.assert_not_called()
        host._clear_theme_preview_fast_styles.assert_called_once()
        host._apply_theme_to_books.assert_called_once_with(theme)
        host._apply_chat_theme.assert_called_once()
        host._apply_model_readout_theme.assert_called_once()
        self.assertFalse(host._pending_skin_force_apply)

    def test_border_and_scrollbar_roles_are_in_separate_grade_groups(self):
        from ui.theme import THEME_GROUPS

        groups = {label.lower(): roles for label, roles in THEME_GROUPS}
        self.assertEqual(groups["border"], ("border",))
        self.assertIn("scroll", groups["accent"])
        self.assertIn("scroll_hover", groups["accent"])

    def test_pdf_page_surroundings_render_the_selected_canvas_color(self):
        from PySide6.QtGui import QImage
        from main import app_stylesheet
        from reader.continuous import ContinuousReader
        from ui.theme import theme_colors

        app = self.app
        previous_style = app.styleSheet()
        reader = ContinuousReader(
            lambda _index, _zoom: QImage(
                80, 100, QImage.Format.Format_RGB32))
        try:
            app.setStyleSheet(
                app_stylesheet("Everforest", "dark", True))
            reader.resize(300, 220)
            reader.set_document([(80.0, 100.0)], render=False)
            reader.show()
            app.processEvents()

            canvas_pixel = reader.viewport().grab().toImage().pixelColor(
                10, 10).name().upper()
            self.assertEqual(
                canvas_pixel,
                theme_colors("Everforest", "dark")["canvas"].upper(),
            )
        finally:
            reader.close()
            app.setStyleSheet(previous_style)

    def test_book_view_applies_theme_colors_without_global_stylesheet(self):
        from reader.bookview import BookView
        from ui.theme import theme_colors

        book = BookView()
        colors = theme_colors("Everforest", "dark")
        try:
            book.apply_theme(colors)
            self.assertEqual(
                book.reader.viewport().styleSheet(),
                f"background-color: {colors['canvas']};",
            )
            self.assertEqual(
                book.reader._container.styleSheet(),
                f"background-color: {colors['panel']};",
            )
        finally:
            book.close()
