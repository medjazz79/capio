"""Offline persistence tests, including stale windows and portable recovery."""
import shutil
from pathlib import Path
from unittest import mock

from tests.test_app import _TempSettingsCase, _GuiCase, _make_pdf_file
from reader.bookmarks import BookmarkStore


class TestBookmarkStorage(_TempSettingsCase):
    def source(self, name="book.pdf"):
        source = Path(self._scratch) / name
        source.write_bytes(b"a book whose contents identify it")
        return source

    def test_survives_settings_removal_and_book_rename(self):
        source = self.source()
        store = BookmarkStore(str(source))
        store.save({1, 7})
        self._settings_tmp.write_text("{}")
        self._settings_tmp.unlink()
        moved = source.with_name("renamed.pdf")
        source.rename(moved)
        self.assertEqual(BookmarkStore(str(moved)).pages, {1, 7})

    def test_sidecar_recovers_without_user_profile_records(self):
        source = self.source()
        store = BookmarkStore(str(source))
        store.save({3})
        shutil.rmtree(store.directory)
        recovered = BookmarkStore(str(source))
        self.assertTrue(recovered.exists)
        self.assertEqual(recovered.pages, {3})

    def test_corrupt_primary_uses_backup(self):
        store = BookmarkStore(str(self.source()))
        store.save({1})
        store.save({1, 2})
        store.sidecar_path.unlink()
        store.sidecar_path.with_suffix(".json.bak").unlink()
        store.record_path.write_text("broken JSON")
        self.assertEqual(BookmarkStore(str(store.source)).pages, {1})

    def test_corrupt_one_copy_recovers_latest_from_other_copy(self):
        store = BookmarkStore(str(self.source()))
        store.save({1})
        store.save({1, 2})
        store.record_path.write_text("broken JSON")
        self.assertEqual(BookmarkStore(str(store.source)).pages, {1, 2})

    def test_removed_bookmark_does_not_return_from_backup_or_stale_window(self):
        source = str(self.source())
        initial = BookmarkStore(source)
        initial.save({1})
        stale = BookmarkStore(source)
        initial.save(set())
        stale.save({1, 4})
        self.assertEqual(BookmarkStore(source).pages, {4})
        BookmarkStore(source).save(set())
        self.assertEqual(BookmarkStore(source).pages, set())

    def test_two_windows_merge_additions(self):
        source = str(self.source())
        first, second = BookmarkStore(source), BookmarkStore(source)
        first.save({2})
        self.assertEqual(second.save({5}), {2, 5})
        self.assertEqual(BookmarkStore(source).pages, {2, 5})

    def test_read_only_book_directory_still_saves_user_copy(self):
        from config import settings
        store = BookmarkStore(str(self.source()))
        write = settings._write_json_atomically

        def protected(path, data):
            if path == store.sidecar_path:
                raise PermissionError("read-only book directory")
            return write(path, data)

        with mock.patch.object(settings, "_write_json_atomically", side_effect=protected):
            store.save({2})
        self.assertEqual(BookmarkStore(str(store.source)).pages, {2})

    def test_both_write_failures_are_reported(self):
        store = BookmarkStore(str(self.source()))
        with mock.patch("config.settings._write_json_atomically", side_effect=PermissionError):
            with self.assertRaises(OSError):
                store.save({2})

    def test_unrelated_replacement_file_does_not_inherit_sidecar(self):
        source = self.source()
        BookmarkStore(str(source)).save({1})
        source.write_bytes(b"a different book")
        self.assertFalse(BookmarkStore(str(source)).exists)


class TestBookViewBookmarkPersistence(_GuiCase):
    def test_toggle_saves_immediately_without_session_save(self):
        from reader.bookview import BookView
        source = _make_pdf_file("immediate_bookmarks.pdf", pages=4)
        book = BookView()
        try:
            book.open(source, defer_render=True)
            book.toggle_bookmark(2)
            self.assertEqual(BookmarkStore(source).pages, {2})
            book.close_doc()
            book.open(source, defer_render=True)
            self.assertEqual(book.bookmarked_pages, {2})
            book.toggle_bookmark(2)
            book.close_doc()
            book.open(source, defer_render=True)
            self.assertTrue(book.has_saved_bookmarks)
            self.assertEqual(book.bookmarked_pages, set())
        finally:
            book.close()

    def test_durable_bookmarks_override_stale_session_in_main_window(self):
        from main import MainWindow
        source = _make_pdf_file("stale_session_bookmarks.pdf", pages=4)
        BookmarkStore(source).save({2})
        window = MainWindow()
        try:
            book = window._add_tab(source, bookmarks=[1])
            self.assertEqual(book.bookmarked_pages, {2})
        finally:
            window.close()

    def test_save_as_preserves_bookmarks_under_new_identity(self):
        from reader.bookview import BookView
        book = BookView()
        destination = str(Path(self._scratch) / "saved.pdf")
        try:
            book.open(_make_pdf_file("bookmark_save_as.pdf", pages=4), defer_render=True)
            book.toggle_bookmark(2)
            book.save_as_pdf(destination)
            self.assertEqual(BookmarkStore(destination).pages, {2})
            book.close_doc()
            book.open(destination, defer_render=True)
            self.assertEqual(book.bookmarked_pages, {2})
        finally:
            book.close()
