"""The vertical splitter lines are decoration, not information.

The user asked for them gone: they are a 1 px bar in the Border colour
running down both sides of the document area. They must not be painted in
the Border colour any more, while staying exactly as wide so the layout
does not move and the panes remain draggable.

The hover hint is kept on purpose: with a fully invisible handle there is
no longer any sign that the divider can be dragged.
"""
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QSplitter


class SplitterLinesAreHiddenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        scratch = Path(tempfile.mkdtemp(prefix="harmony-split-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        patcher = mock.patch("config.settings.SETTINGS_PATH",
                             scratch / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        import config.settings as settings_module

        settings_module.save({
            "theme": "Dark Slate",
            "color_scheme": "dark",
            "font_size": 17,
        })

        from main import MainWindow

        self.window = MainWindow()
        self.addCleanup(self.window.close)

    @staticmethod
    def _handle_rule(text):
        match = re.search(r"QSplitter::handle\s*\{([^}]*)\}", text)
        return " ".join(match.group(1).split()) if match else ""

    def test_the_app_stylesheet_does_not_paint_the_handle_in_the_border(self):
        from main import APP_STYLE_TEMPLATE, app_stylesheet

        rule = self._handle_rule(APP_STYLE_TEMPLATE)
        self.assertNotIn(
            "@border@", rule,
            "the app stylesheet still paints the splitter in the Border "
            "colour, which is the line the user wants gone")

    def test_the_built_stylesheet_paints_the_handle_transparently(self):
        from main import app_stylesheet

        rule = self._handle_rule(app_stylesheet("Dark Slate", "dark"))
        self.assertIn(
            "transparent", rule,
            f"expected a transparent handle, got: {rule}")

    def test_the_handle_keeps_its_width_so_the_layout_does_not_move(self):
        from main import APP_STYLE_TEMPLATE

        rule = self._handle_rule(APP_STYLE_TEMPLATE)
        self.assertIn(
            "width: 1px", rule,
            "the handle must stay 1 px wide or the panes will shift")

    def test_the_hover_hint_survives(self):
        from main import APP_STYLE_TEMPLATE

        self.assertIn(
            "QSplitter::handle:hover", APP_STYLE_TEMPLATE,
            "with an invisible handle there is no sign it can be dragged")

    def test_the_reader_does_not_repaint_the_handle_in_the_border(self):
        from reader.bookview import BookView
        from ui.theme import theme_colors

        book = BookView()
        self.addCleanup(book.deleteLater)
        colors = theme_colors("Dark Slate", "dark")
        book.apply_theme(colors)
        rule = self._handle_rule(book.splitter.styleSheet())
        self.assertNotIn(
            colors["border"], rule,
            "the reader sets its own splitter stylesheet and had to be "
            "changed too, or the line stays")

    def test_no_visible_splitter_edge_is_painted_in_the_border_colour(self):
        from PySide6.QtGui import QColor

        from ui.theme import theme_colors

        border = QColor(theme_colors("Dark Slate", "dark")["border"])
        self.window.resize(1200, 800)
        self.app.processEvents()
        image = self.window.grab().toImage()
        checked = 0
        for splitter in self.window.findChildren(QSplitter):
            if not splitter.isVisible() or splitter.objectName() in (
                    "settingsBodySplitter",):
                continue
            for x in (0, splitter.width() - 1):
                point = splitter.mapTo(self.window,
                                       QPoint(x, splitter.height() // 2))
                if not (0 <= point.x() < image.width()
                        and 0 <= point.y() < image.height()):
                    continue
                colour = image.pixelColor(point.x(), point.y())
                checked += 1
                with self.subTest(splitter=splitter.objectName() or "(reader)",
                                  x=x):
                    self.assertFalse(
                        colour == border,
                        f"the splitter edge is still painted {colour.name()}")
        if checked == 0:
            self.skipTest("no visible splitter edge to sample")


class ChatActionDividerIsHiddenTest(unittest.TestCase):
    """The thick bar between the chat buttons and the composer.

    The chat panel sets its own stylesheet, so the application's
    `QSplitter::handle { background-color: transparent }` never reached it.
    `input_splitter` is named `standaloneChatSplitter`, and the panel
    painted that handle in the Border colour with a 2 px vertical margin,
    which is the thick bar the user pointed at.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import config.settings as settings_module

        settings_module.save({"theme": "Dark Slate", "color_scheme": "dark",
                              "font_size": 17})
        from ui.chat_panel import ChatPanel
        from ui.theme import theme_colors

        self.settings = settings_module.load()
        self.colors = theme_colors("Dark Slate", "dark")
        self.panel = ChatPanel(lambda: self.settings)
        self.panel.apply_theme(self.colors)
        self.addCleanup(self.panel.deleteLater)

    def test_the_divider_is_not_painted_in_the_border_colour(self):
        sheet = self.panel.styleSheet()
        match = re.search(
            r"QSplitter#standaloneChatSplitter::handle:vertical\s*\{([^}]*)\}",
            sheet)
        self.assertIsNotNone(match, "the handle rule disappeared")
        body = " ".join(match.group(1).split())
        self.assertNotIn(
            self.colors["border"], body,
            f"the chat divider is still drawn in the Border colour: {body}")
        self.assertIn("transparent", body)

    def test_the_divider_keeps_its_hover_hint(self):
        self.assertIn(
            "QSplitter#standaloneChatSplitter::handle:vertical:hover",
            self.panel.styleSheet())

    def test_the_divider_keeps_its_grab_area(self):
        sheet = self.panel.styleSheet()
        body = re.search(
            r"QSplitter#standaloneChatSplitter::handle:vertical\s*\{([^}]*)\}",
            sheet).group(1)
        self.assertIn(
            "margin", body,
            "removing the margin would move the panes; only the colour "
            "was asked to change")

    def test_the_splitter_is_still_draggable(self):
        splitter = self.panel.input_splitter
        self.assertEqual(splitter.count(), 2,
                         "the divider must remain a draggable handle")


if __name__ == "__main__":
    unittest.main()


class ThumbnailPanelLinesAreHiddenTest(unittest.TestCase):
    """The other three vertical lines around the thumbnail panel.

    With the splitter handle transparent, three lines were still standing:
    the bookmark rail's right edge, the thumbnail list's own left and right
    edges, and the panel's right edge. All of them are drawn in the Border
    colour by `reader.bookview.apply_theme` and by the app template, so
    both had to change.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import config.settings as settings_module

        settings_module.save({"theme": "Dark Slate", "color_scheme": "dark",
                              "font_size": 17})
        from reader.bookview import BookView
        from ui.theme import theme_colors

        self.book = BookView()
        self.addCleanup(self.book.deleteLater)
        self.colors = theme_colors("Dark Slate", "dark")
        self.book.apply_theme(self.colors)

    def _rule_for(self, selector):
        sheet = self.book.thumbs.styleSheet()
        pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
        match = re.search(pattern, sheet)
        return " ".join(match.group(1).split()) if match else ""

    def test_the_bookmark_rail_has_no_right_border(self):
        self.assertNotIn(
            "border-right", self._rule_for("QWidget#bookmarkRail"),
            "the bookmark rail's right edge is one of the standing lines")

    def test_the_thumbnail_panel_has_no_right_border(self):
        self.assertNotIn(
            "border-right", self._rule_for("QWidget#pdfThumbnailsPanel"))

    def test_the_thumbnail_lists_have_no_border(self):
        rule = self._rule_for("QListWidget#pdfThumbnails, "
                              "QListWidget#pdfBookmarks")
        self.assertNotIn(
            "solid", rule,
            f"the thumbnail list's own left and right edges are still drawn: "
            f"{rule}")

    def test_the_app_template_agrees_with_the_reader(self):
        from main import APP_STYLE_TEMPLATE

        self.assertIn(
            "QWidget#thumbsPanel", APP_STYLE_TEMPLATE)
        panel = re.search(
            r"QWidget#thumbsPanel\s*\{([^}]*)\}", APP_STYLE_TEMPLATE)
        body = panel.group(1) if panel else ""
        self.assertNotIn(
            "solid", body,
            "the app template still draws a vertical line beside the "
            "thumbnail panel; the reader and the template must agree")
        self.assertIn("border-right: none;", body)

    def test_each_thumbnail_keeps_its_own_outline(self):
        """Guard against over-removing: the items are cards, not lines."""
        from main import APP_STYLE_TEMPLATE

        item = re.search(r"QLabel#thumbItem\s*\{([^}]*)\}",
                         APP_STYLE_TEMPLATE)
        self.assertIsNotNone(item)
        self.assertIn(
            "border", item.group(1),
            "the per-thumbnail rounded outline should stay; it is a card, "
            "not one of the vertical lines")


if __name__ == "__main__":
    unittest.main()