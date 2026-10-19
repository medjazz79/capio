"""The theme is applied application-wide, and must stay that way.

A per-window version of this was tried and reverted. It measured faster
(1714 ms -> 663 ms for a switch) but it left the theme applied only partly: the
toolbar kept the previous theme's colours, and the Settings dialog lost the
application rules it used to inherit and rendered white pages until something
forced a repaint. Two regressions in the most visible area of the app are not
worth one second, so the application-wide application is the contract now.

These tests pin that contract so the optimisation is not silently reintroduced.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QToolBar  # noqa: E402


class ApplicationWideSkinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from main import MainWindow

        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.resize(1400, 800)
        self.window.show()
        self.app.processEvents()

    def _switch(self, theme):
        self.window.settings["theme"] = theme
        self.window._applied_app_stylesheet = None
        self.window._applied_theme = None
        self.window._apply_theme_skin(theme)
        self.window._flush_app_skin()
        self.app.processEvents()

    def test_a_switch_sets_the_application_stylesheet(self):
        from main import app_stylesheet

        self._switch("Amoled")
        self.assertEqual(
            self.app.styleSheet(),
            app_stylesheet("Amoled", self.window.settings["color_scheme"]))

    def test_the_toolbar_follows_the_theme_through_the_application_sheet(self):
        """The reported symptom: the toolbar kept the previous colours.

        Pixels are not sampled here: an offscreen grab of this window does not
        reliably reflect a restyle. The contract that fixes the toolbar is that
        the skin is delivered application-wide, so the toolbar's colours have to
        change inside the stylesheet it inherits.
        """
        from ui.theme import theme_colors

        bar = self.window.findChild(QToolBar)
        self.assertIsNotNone(bar, "the toolbar was not found")
        self.assertEqual(
            bar.styleSheet(), "",
            "the toolbar should inherit its colours from the application")

        scheme = self.window.settings["color_scheme"]
        rendered = {}
        for theme in ("Amoled", "Nordic Gray"):
            self._switch(theme)
            sheet = self.app.styleSheet()
            rendered[theme] = sheet
            colors = theme_colors(theme, scheme)
            self.assertIn(
                colors["toolbar"].upper(), sheet.upper(),
                f"{theme}'s toolbar colour is missing from the application "
                f"stylesheet, so the toolbar cannot take the new theme")

        self.assertNotEqual(
            rendered["Amoled"], rendered["Nordic Gray"],
            "switching the theme left the application stylesheet unchanged")

    def test_a_message_box_opened_after_a_switch_uses_the_new_theme(self):
        from PySide6.QtWidgets import QMessageBox

        from main import app_stylesheet

        self._switch("Amoled")
        expected = app_stylesheet("Amoled", self.window.settings["color_scheme"])

        box = QMessageBox(QMessageBox.Icon.Information, "t", "m", parent=self.window)
        self.addCleanup(box.deleteLater)
        # It inherits from the application stylesheet rather than carrying one.
        self.assertEqual(box.styleSheet(), "")
        self.assertEqual(self.app.styleSheet(), expected)

    def test_no_window_scoped_skin_filter_is_installed(self):
        """The reverted optimisation installed an application event filter."""
        self.assertFalse(
            hasattr(self.window, "_late_window_skin"),
            "a late-window skin filter is installed again")
        self.assertFalse(
            hasattr(self.window, "_apply_stylesheet_to_open_windows"),
            "per-window styling came back; it applies the theme only partly")


if __name__ == "__main__":
    unittest.main()