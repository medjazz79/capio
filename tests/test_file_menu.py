"""File menu, recent-book history, and PDF Save As coverage."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestFileMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import config.settings as settings
        self._scratch = Path(tempfile.mkdtemp(prefix="harmony-file-menu-"))
        self._settings_path = mock.patch.object(
            settings, "SETTINGS_PATH", self._scratch / "settings.json")
        self._settings_path.start()
        self.addCleanup(self._settings_path.stop)
        self.addCleanup(shutil.rmtree, self._scratch, True)
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.app.processEvents()

    def _pdf(self, filename, pages=1):
        import pymupdf
        path = self._scratch / filename
        document = pymupdf.open()
        for page_index in range(pages):
            page = document.new_page()
            page.insert_text((72, 100), f"{filename} page {page_index + 1}")
        document.save(path)
        document.close()
        return str(path)

    def _window(self):
        from main import MainWindow
        self.window = MainWindow()
        self.window.show()
        self.app.processEvents()
        return self.window

    def test_file_menu_has_requested_actions_and_save_requires_edits(self):
        from PySide6.QtGui import QKeySequence

        window = self._window()
        self.assertEqual(window.file_button.text(), "File")
        self.assertEqual(
            [action.text() for action in window.file_menu.actions()[:4]],
            ["Open file…", "Save", "Save as PDF…", ""])
        self.assertEqual(
            window.open_file_action.shortcut(),
            QKeySequence(QKeySequence.StandardKey.Open))
        self.assertEqual(
            window.save_file_action.shortcut(),
            QKeySequence(QKeySequence.StandardKey.Save))
        self.assertEqual(
            window.save_as_action.shortcut(),
            QKeySequence(QKeySequence.StandardKey.SaveAs))
        self.assertFalse(window.save_file_action.isEnabled())
        self.assertFalse(window.pdf_location_action.isEnabled())
        self.assertFalse(window.copy_path_action.isEnabled())

        path = self._pdf("menu.pdf")
        with mock.patch(
                "main.QFileDialog.getOpenFileNames",
                return_value=([path], "Supported books and documents")) as open_dialog:
            window.open_file_action.trigger()
        file_filter = open_dialog.call_args.args[3]
        for extension in ("*.pdf", "*.epub", "*.docx", "*.md", "*.html"):
            self.assertIn(extension, file_filter)
        self.app.processEvents()
        self.assertEqual(window.current_book().path, str(Path(path).resolve()))
        self.assertTrue(window.save_as_action.isEnabled())
        self.assertFalse(window.save_file_action.isEnabled())
        self.assertTrue(window.pdf_location_action.isEnabled())
        self.assertTrue(window.copy_path_action.isEnabled())

    def test_pdf_location_opens_folder_and_copy_as_path_uses_absolute_path(self):
        window = self._window()
        path = self._pdf("location.pdf")
        window.open_paths([path])

        with mock.patch("main.QDesktopServices.openUrl", return_value=True) as open_url:
            window.pdf_location_action.trigger()
        opened_url = open_url.call_args.args[0]
        self.assertEqual(
            Path(opened_url.toLocalFile()),
            Path(path).resolve().parent)

        window.copy_path_action.trigger()
        self.assertEqual(
            self.app.clipboard().text(), str(Path(path).resolve()))

    def test_text_source_can_be_edited_and_saved_in_place(self):
        from PySide6.QtCore import Qt

        window = self._window()
        source = self._scratch / "editable.md"
        source.write_text(
            "# Before\n\nمرحبا بالعالم\n\nEnglish text",
            encoding="utf-8",
        )
        window.open_paths([str(source)])
        book = window.current_book()

        self.assertTrue(window.edit_source_action.isEnabled())
        self.assertFalse(book.is_editing_source)
        window.edit_source_action.trigger()
        self.assertTrue(book.is_editing_source)
        self.assertTrue(book.source_editor.isVisible())
        self.assertEqual(book._source_editor_font_size, 18)
        self.assertIn("background-color: #FFFFFF", book.source_editor.styleSheet())
        self.assertIn("color: #202124", book.source_editor.styleSheet())
        self.assertIn("font-size: 18px", book.source_editor.styleSheet())
        self.assertEqual(
            book._source_editor_text(),
            "# Before\n\nمرحبا بالعالم\n\nEnglish text")
        arabic_block = book.source_editor.document().findBlockByNumber(2)
        english_block = book.source_editor.document().findBlockByNumber(4)
        self.assertEqual(
            arabic_block.blockFormat().layoutDirection(),
            Qt.LayoutDirection.RightToLeft)
        self.assertTrue(
            arabic_block.blockFormat().alignment()
            & Qt.AlignmentFlag.AlignRight)
        self.assertEqual(
            english_block.blockFormat().layoutDirection(),
            Qt.LayoutDirection.LeftToRight)
        self.assertTrue(
            english_block.blockFormat().alignment()
            & Qt.AlignmentFlag.AlignLeft)
        self.assertNotEqual(
            book.source_editor.palette().color(
                book.source_editor.foregroundRole()),
            book.source_editor.palette().color(
                book.source_editor.backgroundRole()),
        )
        book._set_source_editor_text(
            "# بعد التعديل\n\nUpdated text")
        book._on_source_text_changed()
        self.app.processEvents()
        self.assertTrue(book.is_modified)
        self.assertTrue(window.save_file_action.isEnabled())

        window.save_file_action.trigger()
        self.app.processEvents()

        self.assertEqual(
            source.read_text(encoding="utf-8"),
            "# بعد التعديل\n\nUpdated text",
        )
        self.assertFalse(book.is_modified)
        self.assertIn("Updated text", book.page_text(0))
        self.assertTrue(book.is_editing_source)

    def test_discarding_text_source_edits_restores_the_saved_source(self):
        from PySide6.QtWidgets import QMessageBox

        window = self._window()
        source = self._scratch / "discard.md"
        source.write_text("# Saved text", encoding="utf-8")
        window.open_paths([str(source)])
        book = window.current_book()
        window.edit_source_action.trigger()
        book._set_source_editor_text("# Unsaved text")
        book._on_source_text_changed()

        with mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Discard):
            window.edit_source_action.setChecked(False)

        self.assertFalse(book.is_editing_source)
        self.assertFalse(book.is_modified)
        self.assertEqual(book._source_editor_text(), "# Saved text")
        self.assertEqual(source.read_text(encoding="utf-8"), "# Saved text")

    def test_editor_switches_paragraph_direction_when_language_changes(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextCursor
        from PySide6.QtWidgets import QMessageBox

        warning_patch = mock.patch(
            "main.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Discard,
        )
        warning_patch.start()
        self.addCleanup(warning_patch.stop)
        window = self._window()
        source = self._scratch / "direction.md"
        source.write_text("English paragraph", encoding="utf-8")
        window.open_paths([str(source)])
        book = window.current_book()
        window.edit_source_action.trigger()
        block = book.source_editor.document().begin()
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.insertText("فقرة عربية")
        self.app.processEvents()

        block = book.source_editor.document().begin()
        self.assertEqual(
            block.blockFormat().layoutDirection(),
            Qt.LayoutDirection.RightToLeft)
        self.assertTrue(
            block.blockFormat().alignment()
            & Qt.AlignmentFlag.AlignRight)
        self.assertIn('align="right"', book.source_editor.toHtml())
        self.assertEqual(book._source_editor_text(), "فقرة عربية")

        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.insertText("English paragraph")
        self.app.processEvents()
        block = book.source_editor.document().begin()
        self.assertEqual(
            block.blockFormat().layoutDirection(),
            Qt.LayoutDirection.LeftToRight)
        self.assertTrue(
            block.blockFormat().alignment()
            & Qt.AlignmentFlag.AlignLeft)
        self.assertEqual(book._source_editor_text(), "English paragraph")

    def test_ctrl_wheel_changes_source_editor_font_size_only_in_edit_mode(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        window = self._window()
        source = self._scratch / "zoom-source.md"
        source.write_text("Editable text", encoding="utf-8")
        window.open_paths([str(source)])
        book = window.current_book()
        book.apply_theme({
            "window": "#111111", "panel": "#222222", "surface": "#333333",
            "text": "#EEEEEE", "border": "#444444", "accent": "#5555FF",
        })
        window.edit_source_action.trigger()
        self.app.processEvents()
        original_size = book._source_editor_font_size

        event = QWheelEvent(
            QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(book.source_editor.viewport(), event)
        self.assertEqual(book._source_editor_font_size, original_size + 1)
        self.assertIn(
            f"font-size: {original_size + 1}px",
            book.source_editor.styleSheet())

        plain_scroll = QWheelEvent(
            QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(book.source_editor.viewport(), plain_scroll)
        self.assertEqual(book._source_editor_font_size, original_size + 1)

        window.edit_source_action.trigger()
        self.app.processEvents()
        ctrl_scroll_in_reader = QWheelEvent(
            QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(book.source_editor.viewport(), ctrl_scroll_in_reader)
        self.assertEqual(book._source_editor_font_size, original_size + 1)

    def test_open_dialog_remembers_last_opened_directory(self):
        import config.settings as settings

        window = self._window()
        first_dir = self._scratch / "first"
        second_dir = self._scratch / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "first.txt"
        second = second_dir / "second.txt"
        first.write_text("First", encoding="utf-8")
        second.write_text("Second", encoding="utf-8")

        with mock.patch(
                "main.QFileDialog.getOpenFileNames",
                return_value=([str(first)], "Supported books and documents")
        ) as dialog:
            window.open_file_action.trigger()
        self.assertEqual(dialog.call_args.args[2], str(Path.home()))
        self.assertEqual(window.settings["last_open_dir"], str(first_dir))
        self.assertEqual(settings.load()["last_open_dir"], str(first_dir))

        window.open_paths([str(second)])
        window.tabs.setCurrentIndex(0)
        self.app.processEvents()
        with mock.patch(
                "main.QFileDialog.getOpenFileNames",
                return_value=([], "Supported books and documents")
        ) as dialog:
            window.open_file_action.trigger()

        self.assertEqual(dialog.call_args.args[2], str(second_dir))
        self.assertEqual(settings.load()["last_open_dir"], str(second_dir))

    def test_file_actions_export_selection_and_show_pdf_properties(self):
        window = self._window()
        path = self._pdf("export.pdf", pages=3)
        window.open_paths([path])
        book = window.current_book()
        book.set_thumbs_visible(True)
        self.app.processEvents()
        for item_index in (0, 2):
            book.thumbs.list.item(item_index).setSelected(True)
        self.app.processEvents()
        self.assertTrue(window.export_pages_action.isEnabled())

        exported = self._scratch / "selected.pdf"
        with mock.patch(
                "main.QFileDialog.getSaveFileName",
                return_value=(str(exported), "PDF files (*.pdf)")):
            window.export_pages_action.trigger()
        import pymupdf
        with pymupdf.open(exported) as document:
            self.assertEqual(len(document), 2)
            self.assertIn("page 1", document[0].get_text())
            self.assertIn("page 3", document[1].get_text())

        with mock.patch("main.QMessageBox.information") as show_info:
            window.pdf_properties_action.trigger()
        properties = show_info.call_args.args[2]
        self.assertIn("Pages: 3", properties)
        self.assertIn("Location:", properties)

    def test_reload_discards_changes_only_after_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        window = self._window()
        path = self._pdf("reload.pdf", pages=3)
        window.open_paths([path])
        book = window.current_book()
        self.app.processEvents()
        book.reader._current = 2
        book.delete_pages([1])
        self.assertTrue(book.is_modified)

        with mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Cancel):
            window.reload_pdf_action.trigger()
        self.assertEqual(book.page_count(), 2)
        self.assertTrue(book.is_modified)

        with mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Discard):
            window.reload_pdf_action.trigger()
        self.assertEqual(book.page_count(), 3)
        self.assertFalse(book.is_modified)
        self.assertEqual(book.path, str(Path(path).resolve()))

    def test_print_can_be_cancelled_without_starting_a_print_job(self):
        from PySide6.QtPrintSupport import QPrintDialog

        window = self._window()
        window.open_paths([self._pdf("print.pdf")])
        with mock.patch.object(
                QPrintDialog, "exec",
                return_value=QPrintDialog.DialogCode.Rejected) as print_dialog:
            window.print_action.trigger()
        print_dialog.assert_called_once()

    def test_print_renders_all_pdf_pages_to_selected_printer(self):
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter

        window = self._window()
        window.open_paths([self._pdf("print-pages.pdf", pages=2)])
        output = self._scratch / "printed.pdf"
        original_init = QPrinter.__init__

        def initialize_pdf_printer(printer, *args, **kwargs):
            original_init(printer, *args, **kwargs)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(output))

        with mock.patch.object(
                QPrinter, "__init__", initialize_pdf_printer), mock.patch.object(
                    QPrintDialog, "exec",
                    return_value=QPrintDialog.DialogCode.Accepted):
            window.print_action.trigger()

        import pymupdf
        with pymupdf.open(output) as printed:
            self.assertEqual(len(printed), 2)
            self.assertGreater(printed[0].get_pixmap().width, 0)

    def test_closing_modified_tab_requires_discard_or_save_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        window = self._window()
        path = self._pdf("close-modified.pdf", pages=2)
        window.open_paths([path])
        book = window.current_book()
        book.delete_pages([1])
        self.assertTrue(book.is_modified)

        with mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Cancel):
            window._close_tab(window._book_tab_index(book))
        self.assertEqual(window.tabs.count(), 1)
        self.assertTrue(book.is_modified)

        with mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Discard):
            window._close_tab(window._book_tab_index(book))
        self.assertEqual(window.tabs.count(), 0)

    def test_close_other_tabs_keeps_target_tab_and_stays_out_of_file_menu(self):
        window = self._window()
        window.open_paths([
            self._pdf("close-a.pdf"),
            self._pdf("close-b.pdf"),
            self._pdf("close-c.pdf"),
        ])
        keep = window.tabs.widget(1)
        close_others = [
            window.tabs.widget(index)
            for index in range(window.tabs.count())
            if index != 1
        ]
        window._close_books(close_others, "closing other tabs")

        self.assertEqual(window.tabs.count(), 1)
        self.assertIs(window.tabs.widget(0), keep)
        file_action_labels = [
            action.text().casefold() for action in window.file_menu.actions()
        ]
        self.assertFalse(any("close" in label for label in file_action_labels))

    def test_recent_files_are_persisted_deduplicated_and_reordered(self):
        from config.settings import load

        window = self._window()
        first = self._pdf("first.pdf")
        second = self._pdf("second.pdf")
        window.open_paths([first, second])
        self.assertEqual(
            window.settings["recent_files"],
            [str(Path(second).resolve()), str(Path(first).resolve())])

        window.open_paths([first])
        self.assertEqual(
            window.settings["recent_files"],
            [str(Path(first).resolve()), str(Path(second).resolve())])
        self.assertEqual(load()["recent_files"], window.settings["recent_files"])

        window._populate_recent_files_menu()
        self.assertEqual(
            [action.text() for action in window.recent_files_menu.actions()
             if action.isEnabled() and not action.isSeparator()],
            ["first.pdf", "second.pdf", "Clear recent files"])
        window.open_paths([second])
        window._populate_recent_files_menu()
        next(action for action in window.recent_files_menu.actions()
             if action.text() == "first.pdf").trigger()
        self.assertEqual(window.current_book().path,
                         str(Path(first).resolve()))

    def test_recent_files_menu_is_a_below_item_dropdown_limited_to_15(self):
        window = self._window()
        self.assertIs(self.recent_menu_submenu(window), window.recent_files_menu)

        for index in range(20):
            window._record_recent_file(str(self._scratch / f"recent-{index}.pdf"))

        window._populate_recent_files_menu()
        actions = window.recent_files_menu.actions()
        file_actions = [
            action for action in actions
            if action.text().startswith("recent-")
        ]
        self.assertEqual(len(file_actions), 15)
        self.assertEqual(file_actions[0].text(), "recent-19.pdf (not found)")
        self.assertEqual(actions[-1].text(), "Clear recent files")
        self.assertEqual(len(window.settings["recent_files"]), 15)

    @staticmethod
    def recent_menu_submenu(window):
        return next(
            (action.menu() for action in window.file_menu.actions()
             if action.text() == "Open recent"),
            None,
        )

    def test_save_as_writes_pdf_and_switches_current_book_to_saved_copy(self):
        window = self._window()
        source = self._pdf("source.pdf")
        destination = self._scratch / "saved-copy"
        window.open_paths([source])
        book = window.current_book()

        with mock.patch(
                "main.QFileDialog.getSaveFileName",
                return_value=(str(destination), "PDF files (*.pdf)")):
            window.save_file_as()

        saved_path = destination.with_suffix(".pdf")
        self.assertTrue(saved_path.is_file())
        self.assertTrue(Path(source).is_file())
        self.assertEqual(book.path, str(saved_path.resolve()))
        self.assertEqual(window.tabs.tabToolTip(window.tabs.currentIndex()),
                         str(saved_path.resolve()))
        self.assertEqual(window.settings["recent_files"][0],
                         str(saved_path.resolve()))

        import pymupdf
        with pymupdf.open(saved_path) as saved:
            self.assertEqual(len(saved), 1)
            self.assertEqual(
                saved[0].get_text().strip(), "source.pdf page 1")

    def test_saving_modified_markdown_creates_pdf_and_preserves_source(self):
        from PySide6.QtWidgets import QMessageBox

        window = self._window()
        source = self._scratch / "notes.md"
        source.write_text(
            "\n\n".join(
                f"Paragraph {index}: " + ("Readable source text. " * 24)
                for index in range(80)
            ),
            encoding="utf-8",
        )
        original = source.read_bytes()
        window.open_paths([str(source)])
        book = window.current_book()
        self.assertIsNotNone(book)
        self.assertGreater(book.page_count(), 1)
        book.delete_pages([0])
        destination = self._scratch / "notes-export"

        with (
            mock.patch(
                "main.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Save,
            ),
            mock.patch(
                "main.QFileDialog.getSaveFileName",
                return_value=(str(destination), "PDF files (*.pdf)"),
            ) as save_dialog,
        ):
            self.assertTrue(window._confirm_pending_edits(book, "closing"))

        self.assertEqual(save_dialog.call_args.args[2], str(source.with_suffix(".pdf")))
        self.assertTrue(destination.with_suffix(".pdf").is_file())
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(book.can_save_in_place)

    def test_delete_pages_updates_reader_bookmarks_and_saved_pdf(self):
        import pymupdf

        window = self._window()
        path = self._pdf("pages.pdf", pages=3)
        window.open_paths([path])
        book = window.current_book()
        self.app.processEvents()
        book.set_bookmarks({0, 2})
        book.reader._current = 2
        self.assertEqual(book.delete_pages([1]), 1)
        self.assertEqual(book.page_count(), 2)
        self.assertEqual(book.bookmarked_pages, {0, 1})
        self.assertEqual(book.current_page(), 1)
        self.assertTrue(book.is_modified)
        self.assertTrue(window.save_file_action.isEnabled())
        self.assertEqual(book.thumbs._count, 2)

        with self.assertRaisesRegex(ValueError, "at least one page"):
            book.delete_pages([0, 1])
        self.assertEqual(book.page_count(), 2)

        book.save()
        self.assertEqual(book.thumbs._bookmarks, {0, 1})
        with pymupdf.open(path) as saved:
            self.assertEqual(len(saved), 2)
            self.assertIn("page 1", saved[0].get_text())
            self.assertIn("page 3", saved[1].get_text())


if __name__ == "__main__":
    unittest.main()
