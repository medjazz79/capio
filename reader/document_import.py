"""Prepare supported book and document formats for the paginated reader."""
from __future__ import annotations

import logging
import os
import tempfile
import unicodedata
from html import escape
from pathlib import Path
from typing import NamedTuple

import pymupdf
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont, QTextCursor, QTextDocument, QTextFormat

log = logging.getLogger("harmony_translator.reader.document_import")

_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".xhtml"}
_PYMUPDF_EXTENSIONS = {
    ".epub", ".xps", ".oxps", ".cbz", ".fb2",
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp",
    ".svg",
}
SUPPORTED_EXTENSIONS = frozenset(
    {".pdf", ".docx"} | _TEXT_EXTENSIONS | _PYMUPDF_EXTENSIONS
)
SUPPORTED_FILE_FILTER = (
    "Supported books and documents "
    "(*.pdf *.epub *.docx *.txt *.md *.markdown *.html *.htm *.xhtml "
    "*.xps *.oxps *.cbz *.fb2 *.png *.jpg *.jpeg *.bmp *.gif *.tif "
    "*.tiff *.webp *.svg)"
)


class PreparedDocument(NamedTuple):
    view_path: str
    temporary_path: str | None


def prepare_document(path: str) -> PreparedDocument:
    """Return a PDF path for the reader and any temporary file to clean up."""
    source = Path(path).expanduser().resolve()
    extension = source.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported document format: {extension or '(no extension)'}")
    if extension == ".pdf":
        return PreparedDocument(str(source), None)

    descriptor, temporary_path = tempfile.mkstemp(
        prefix="harmony-reader-", suffix=".pdf")
    os.close(descriptor)
    try:
        Path(temporary_path).unlink()
        if extension in _TEXT_EXTENSIONS:
            _write_text_document_pdf(source, Path(temporary_path))
        elif extension == ".docx":
            _write_docx_pdf(source, Path(temporary_path))
        else:
            _write_pymupdf_document_pdf(source, Path(temporary_path))
        with pymupdf.open(temporary_path) as rendered:
            if rendered.page_count == 0:
                raise ValueError(f"The document contains no readable pages: {source}")
        return PreparedDocument(temporary_path, temporary_path)
    except Exception:
        Path(temporary_path).unlink(missing_ok=True)
        raise


def _read_text_document(source: Path) -> str:
    data = source.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    return data.decode("utf-8")


def _write_text_document_pdf(source: Path, destination: Path) -> None:
    extension = source.suffix.lower()
    if extension in {".md", ".markdown"}:
        markdown_document = QTextDocument()
        markdown_document.setDefaultFont(QFont("Arial", 10))
        markdown_document.setBaseUrl(QUrl.fromLocalFile(str(source)))
        markdown_document.setMarkdown(_read_text_document(source))
        _apply_markdown_directions(markdown_document)
        html = markdown_document.toHtml()
    elif extension in {".html", ".htm", ".xhtml"}:
        html = _read_text_document(source)
    else:
        lines = _read_text_document(source).replace(
            "\r\n", "\n").replace("\r", "\n")
        paragraphs = []
        for line in lines.split("\n"):
            direction = text_direction(line)
            paragraphs.append(
                f'<p dir="{direction}">{escape(line) or "&nbsp;"}</p>'
            )
        html = "<html><body>" + "".join(paragraphs) + "</body></html>"
    _write_html_pdf(source, html, destination)


def text_direction(text: str) -> str:
    for character in text:
        bidi_type = unicodedata.bidirectional(character)
        if bidi_type in {"R", "AL"}:
            return "rtl"
        if bidi_type == "L":
            return "ltr"
    return "ltr"


def _apply_markdown_directions(document: QTextDocument) -> None:
    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()
        is_code = block_format.hasProperty(
            QTextFormat.Property.BlockCodeFence)
        direction = "ltr" if is_code else text_direction(block.text())
        if direction == "rtl":
            block_format.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        else:
            block_format.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        cursor = QTextCursor(block)
        cursor.setBlockFormat(block_format)
        block = block.next()


def _write_docx_pdf(source: Path, destination: Path) -> None:
    import mammoth

    with source.open("rb") as docx_file:
        result = mammoth.convert_to_html(docx_file)
    for message in result.messages:
        log.warning("DOCX conversion notice for %s: %s", source, message)
    _write_html_pdf(source, result.value, destination)


def _write_html_pdf(source: Path, html: str, destination: Path) -> None:
    page = pymupdf.paper_rect("a4")
    content_area = page + (42, 42, -42, -42)
    story = pymupdf.Story(
        html=html,
        user_css=(
            "body { font-family: sans-serif; font-size: 10pt; "
            "line-height: 1.35; } "
            "p { margin-top: 0; margin-bottom: 8pt; } "
            "img { max-width: 100%; }"
        ),
        archive=pymupdf.Archive(str(source.parent)),
    )
    writer = pymupdf.DocumentWriter(str(destination))
    try:
        more = True
        while more:
            device = writer.begin_page(page)
            more, _ = story.place(content_area)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()
    del writer
    if not destination.is_file() or destination.stat().st_size == 0:
        raise OSError(f"Could not render document as PDF: {destination}")


def _write_pymupdf_document_pdf(source: Path, destination: Path) -> None:
    with pymupdf.open(str(source)) as document:
        if document.is_reflowable:
            document.layout(width=595, height=842)
        pdf_data = document.convert_to_pdf()
    destination.write_bytes(pdf_data)
