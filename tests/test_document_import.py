from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PIL import Image
from PySide6.QtWidgets import QApplication

from reader.document_import import (
    SUPPORTED_EXTENSIONS,
    prepare_document,
)


class TestDocumentImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="harmony-import-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def _assert_rendered_text(self, source: Path, expected: str) -> None:
        prepared = prepare_document(str(source))
        if prepared.temporary_path:
            self.addCleanup(Path(prepared.temporary_path).unlink, missing_ok=True)
        with pymupdf.open(prepared.view_path) as document:
            self.assertGreater(document.page_count, 0)
            self.assertIn(expected, "\n".join(page.get_text() for page in document))

    def test_plain_text_markdown_and_html_render_as_readable_pages(self):
        sources = (
            (".txt", "Plain text document"),
            (".md", "# Markdown heading\n\nMarkdown paragraph"),
            (".html", "<html><body><h1>HTML heading</h1><p>HTML paragraph</p></body></html>"),
        )
        for extension, contents in sources:
            with self.subTest(extension=extension):
                source = self.root / f"sample{extension}"
                source.write_text(contents, encoding="utf-8")
                expected = {
                    ".txt": "Plain text document",
                    ".md": "Markdown heading",
                    ".html": "HTML heading",
                }[extension]
                self._assert_rendered_text(source, expected)

    def test_markdown_opening_renders_heading_emphasis_and_list(self):
        source = self.root / "formatted.md"
        source.write_text(
            "# A Big Heading\n\nRegular paragraph with **bold text**.\n\n"
            "- first item\n- second item",
            encoding="utf-8",
        )
        prepared = prepare_document(str(source))
        self.addCleanup(
            Path(prepared.temporary_path).unlink, missing_ok=True)
        with pymupdf.open(prepared.view_path) as document:
            page = document[0]
            rendered_text = page.get_text().replace("\ufb01", "fi")
            blocks = page.get_text("dict")["blocks"]
            spans = [
                span
                for block in blocks
                for line in block.get("lines", [])
                for span in line["spans"]
            ]
            self.assertIn("A Big Heading", rendered_text)
            self.assertIn("first item", rendered_text)
            heading = next(
                span for span in spans if "A Big Heading" in span["text"])
            paragraph = next(
                span for span in spans if "Regular paragraph" in span["text"])
            bold = next(span for span in spans if span["text"] == "bold text")
            self.assertGreater(heading["size"], paragraph["size"])
            self.assertIn("Bold", bold["font"])

    def test_markdown_and_text_render_rtl_and_ltr_paragraphs(self):
        contents = (
            "# عنوان عربي\n\n"
            "فقرة عربية مع English words\n\n"
            "English heading\n\n"
            "English paragraph with العربية mixed"
        )
        for extension in (".md", ".txt"):
            with self.subTest(extension=extension):
                source = self.root / f"bilingual{extension}"
                source.write_text(contents, encoding="utf-8")
                prepared = prepare_document(str(source))
                self.addCleanup(
                    Path(prepared.temporary_path).unlink, missing_ok=True)
                with pymupdf.open(prepared.view_path) as document:
                    page = document[0]
                    spans = [
                        span
                        for block in page.get_text("dict")["blocks"]
                        for line in block.get("lines", [])
                        for span in line["spans"]
                    ]
                    arabic_heading = next(
                        span for span in spans
                        if "\ufecb\ufee8\ufeee\ufe8d\ufee5" in span["text"])
                    english_heading = next(
                        span for span in spans
                        if span["text"] == "English heading")
                    self.assertGreater(
                        arabic_heading["bbox"][0], page.rect.width * 0.7)
                    self.assertLess(
                        english_heading["bbox"][0], page.rect.width * 0.2)

    def test_docx_is_rendered_without_changing_the_source(self):
        source = self.root / "sample.docx"
        with zipfile.ZipFile(source, "w") as docx:
            docx.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>',
            )
            docx.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                '</Relationships>',
            )
            docx.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>DOCX sample text</w:t></w:r></w:p>'
                '<w:sectPr/></w:body></w:document>',
            )
        original = source.read_bytes()
        self._assert_rendered_text(source, "DOCX sample text")
        self.assertEqual(source.read_bytes(), original)

    def test_epub_comic_archive_and_image_are_readable(self):
        epub = self.root / "sample.epub"
        with zipfile.ZipFile(epub, "w") as book:
            book.writestr(
                "mimetype", "application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            book.writestr(
                "META-INF/container.xml",
                '<container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            book.writestr(
                "OEBPS/content.opf",
                '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
                'unique-identifier="BookId"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:title>Sample</dc:title><dc:language>en</dc:language>'
                '<dc:identifier id="BookId">sample</dc:identifier></metadata>'
                '<manifest><item id="chapter" href="chapter.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="chapter"/></spine></package>',
            )
            book.writestr(
                "OEBPS/chapter.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                '<p>EPUB sample text</p></body></html>',
            )
        self._assert_rendered_text(epub, "EPUB sample text")

        image_path = self.root / "page.png"
        Image.new("RGB", (64, 64), "red").save(image_path)
        prepared_image = prepare_document(str(image_path))
        if prepared_image.temporary_path:
            self.addCleanup(
                Path(prepared_image.temporary_path).unlink, missing_ok=True)
        with pymupdf.open(prepared_image.view_path) as image_pdf:
            self.assertGreater(image_pdf.page_count, 0)
            pixmap = image_pdf[0].get_pixmap()
            red, green, blue = pixmap.pixel(
                pixmap.width // 2, pixmap.height // 2)
            self.assertGreater(red, green + 100)
            self.assertGreater(red, blue + 100)

        comic = self.root / "sample.cbz"
        with zipfile.ZipFile(comic, "w") as archive:
            archive.write(image_path, "page.png")
        prepared_comic = prepare_document(str(comic))
        if prepared_comic.temporary_path:
            self.addCleanup(
                Path(prepared_comic.temporary_path).unlink, missing_ok=True)
        with pymupdf.open(prepared_comic.view_path) as comic_pdf:
            self.assertGreater(comic_pdf.page_count, 0)
            comic_pixmap = comic_pdf[0].get_pixmap()
            red, green, blue = comic_pixmap.pixel(
                comic_pixmap.width // 2, comic_pixmap.height // 2)
            self.assertGreater(red, green + 100)
            self.assertGreater(red, blue + 100)

        fb2 = self.root / "sample.fb2"
        fb2.write_text(
            '<?xml version="1.0"?><FictionBook '
            'xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            '<description><title-info><book-title>Sample</book-title>'
            '</title-info></description><body><section><p>FB2 sample text'
            '</p></section></body></FictionBook>',
            encoding="utf-8",
        )
        self._assert_rendered_text(fb2, "FB2 sample text")

        svg = self.root / "sample.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            '<text x="2" y="20">SVG sample text</text></svg>',
            encoding="utf-8",
        )
        self._assert_rendered_text(svg, "SVG sample text")

    def test_pdf_is_used_directly_and_extension_list_includes_requested_formats(self):
        source = self.root / "source.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Source PDF")
            document.save(source)
        prepared = prepare_document(str(source))
        self.assertEqual(prepared.view_path, str(source.resolve()))
        self.assertIsNone(prepared.temporary_path)
        self.assertTrue({
            ".pdf", ".epub", ".docx", ".txt", ".md", ".html", ".svg",
        }.issubset(SUPPORTED_EXTENSIONS))

    def test_book_view_reads_markdown_and_exports_pdf_without_changing_source(self):
        from reader.bookview import BookView

        source = self.root / "notes.md"
        source.write_text("# Original Markdown\n\nDo not modify this source.", encoding="utf-8")
        original = source.read_bytes()
        book = BookView()
        self.addCleanup(book.close_doc)
        book.open(str(source))

        self.assertEqual(book.path, str(source.resolve()))
        self.assertFalse(book.can_save_in_place)
        self.assertIn("Original Markdown", book.page_text(0))
        self.assertTrue(book.can_edit_source)
        self.assertIn("Original Markdown", book._source_editor_text())

        temporary_view_path = book._temporary_view_path
        self.assertIsNotNone(temporary_view_path)
        assert temporary_view_path is not None
        temporary_view = Path(temporary_view_path)
        destination = self.root / "notes-export.pdf"
        book.save_as_pdf(str(destination))
        self.assertTrue(destination.is_file())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(book.path, str(destination.resolve()))
        self.assertEqual(book._document_path, str(destination.resolve()))
        self.assertFalse(temporary_view.exists())

    def test_text_source_editing_saves_utf8_bom_and_line_endings(self):
        from reader.bookview import BookView

        source = self.root / "notes.txt"
        source.write_bytes(b"\xef\xbb\xbfFirst line\r\nSecond line\r\n")
        book = BookView()
        self.addCleanup(book.close_doc)
        book.open(str(source))
        book.set_source_editing(True)
        book._set_source_editor_text("Changed line\nAnother line")
        book._on_source_text_changed()
        self.assertTrue(book.is_modified)

        book.save()

        self.assertEqual(
            source.read_bytes(),
            b"\xef\xbb\xbfChanged line\r\nAnother line",
        )
        self.assertFalse(book.is_modified)
        self.assertTrue(book.is_editing_source)
        self.assertEqual(
            book._source_editor_text(), "Changed line\nAnother line")
        self.assertIn("Changed line", book.page_text(0))
