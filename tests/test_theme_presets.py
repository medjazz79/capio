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


class SettingsMenusWearTheirOwnThemeTest(unittest.TestCase):
    """Every menu in Settings must wear the theme it belongs to.

    A light reset (`#FFFFFF` / `#1B1F27`) once slipped into the dialog's popup
    rules, and because a widget's own stylesheet beats the app-wide
    `QComboBox QAbstractItemView { @panel@ }` rule (main.py), every dropdown in
    Settings -- the model list in the report -- painted a light panel inside a
    dark theme. The popup rules must keep taking their colours from the theme.
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import shutil

        import config.settings as settings_module

        scratch = Path(tempfile.mkdtemp(prefix="harmony-menus-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        patcher = mock.patch("config.settings.SETTINGS_PATH",
                             scratch / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        settings_module.save({
            "theme": "Dark Slate",
            "color_scheme": "dark",
            "font_size": 17,
        })

    def _dialog(self):
        """A Settings window opened the way the app opens it: from a live
        MainWindow, so the app-wide stylesheet is in force too."""
        from main import MainWindow
        from ui.settings_dialog import SettingsDialog

        window = MainWindow()
        self.addCleanup(window.close)
        settings = dict(window.settings)
        settings["custom_providers"] = [{
            "id": "custom-test", "name": "Gemini",
            "base_url": "https://example.invalid/v1",
            "api_key": "test", "model": "models/example",
            "enabled": True, "reasoning_enabled": False,
            "vision_enabled": False,
        }]
        dialog = SettingsDialog(settings, window)
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        return dialog

    def _popup_colours(self, combo):
        from PySide6.QtGui import QPalette

        combo.addItems(["vendor/one-model", "vendor/two-model"])
        combo.showPopup()
        self.app.processEvents()
        self.app.processEvents()
        view = combo.view()
        image = view.window().grab().toImage()
        # The current row is painted with the selection colour; ask about an
        # ordinary row, which is the popup's own background.
        row = view.visualRect(combo.model().index(combo.count() - 1, 0))
        painted = image.pixelColor(
            row.left() + 6, row.center().y()).name().upper()
        text = view.palette().color(QPalette.ColorRole.Text).name().upper()
        combo.hidePopup()
        return painted, text

    def test_the_model_picker_popup_paints_the_theme_colours(self):
        from PySide6.QtWidgets import QComboBox
        from ui.theme import theme_colors

        colors = theme_colors("Dark Slate", "dark")
        dialog = self._dialog()
        combo = dialog.findChild(QComboBox, "customProviderModelPicker")
        self.assertIsNotNone(combo, "the provider model picker is missing")

        painted, text = self._popup_colours(combo)

        self.assertEqual(
            painted, colors["panel"].upper(),
            "the model popup painted outside the theme it belongs to")
        self.assertEqual(
            text, colors["text"].upper(),
            "the model popup text colour is not the theme's")

    def test_a_plain_settings_combo_popup_paints_the_theme_colours(self):
        """Every dropdown in the dialog, not just the delegated model list."""
        from PySide6.QtWidgets import QComboBox
        from ui.theme import theme_colors

        colors = theme_colors("Dark Slate", "dark")
        dialog = self._dialog()
        combo = QComboBox(dialog)
        combo.setObjectName("plainProbeCombo")
        combo.move(10, 10)

        painted, text = self._popup_colours(combo)

        self.assertEqual(painted, colors["panel"].upper())
        self.assertEqual(text, colors["text"].upper())
