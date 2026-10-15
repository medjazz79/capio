"""PDF open / render / text-in-rect via PyMuPDF."""
from __future__ import annotations

import re
from typing import NamedTuple

import pymupdf
from PySide6.QtGui import QImage

MIN_SCAN_IMAGE_COVERAGE = 0.5
MAX_SCAN_TEXT_WORDS = 80


def open_doc(path: str) -> pymupdf.Document:
    return pymupdf.open(path)


def render_page(page: pymupdf.Page, zoom: float) -> QImage:
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(zoom, zoom),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    img = QImage(
        pix.samples_mv, pix.width, pix.height, pix.stride,
        QImage.Format.Format_RGB888,
    )
    return img.copy()

def render_page_at_dpi(page: pymupdf.Page, dpi: float) -> QImage:
    if dpi <= 0:
        raise ValueError("DPI must be positive")
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    image = QImage(
        pix.samples_mv, pix.width, pix.height, pix.stride,
        QImage.Format.Format_RGB888,
    )
    return image.copy()


def render_crop(page: pymupdf.Page, pdf_rect, dpi: float = 300) -> QImage:
    """Render a PDF rect at high resolution for OCR."""
    z = dpi / 72.0
    clip = pymupdf.Rect(pdf_rect[0], pdf_rect[1], pdf_rect[2], pdf_rect[3])
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(z, z),
        clip=clip,
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    img = QImage(
        pix.samples_mv, pix.width, pix.height, pix.stride,
        QImage.Format.Format_RGB888,
    )
    return img.copy()


# QImage format -> (PIL mode, PIL raw decoder mode). Only formats whose memory
# layout maps 1:1 onto a Pillow raw decoder are listed; anything else falls
# back to the PNG round-trip, which is always correct and merely slow.
#
# Format_ARGB32_Premultiplied is deliberately absent: its stored colour values
# are scaled by alpha, so the buffer is not what PNG decodes to and the fast
# path would silently return darkened pixels.
_QIMAGE_TO_PIL = {
    QImage.Format.Format_RGB888: ("RGB", "RGB"),
    QImage.Format.Format_RGBA8888: ("RGBA", "RGBA"),
    QImage.Format.Format_Grayscale8: ("L", "L"),
    # 32-bit formats are stored byte-wise as B, G, R, A on every platform Qt
    # supports here. Format_RGB32 has no alpha channel (BGRX matches its
    # always-opaque fourth byte) and reports as RGB; Format_ARGB32 does, and
    # matches PNG's RGBA.
    QImage.Format.Format_RGB32: ("RGB", "BGRX"),
    QImage.Format.Format_ARGB32: ("RGBA", "BGRA"),
}


def _qimage_to_pil_via_png(img: QImage):
    """Correct-for-every-format fallback: encode to PNG and decode it again.

    Kept as the reference implementation: `qimage_to_pil` is verified to be
    pixel-identical to this for every format it maps.
    """
    from io import BytesIO
    from PySide6.QtCore import QBuffer, QIODevice
    from PIL import Image
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(BytesIO(bytes(buf.data())))


def qimage_to_pil(img: QImage):
    """Convert a QImage to a PIL image.

    The obvious implementation round-trips through PNG, which costs a full
    deflate plus inflate: 357 ms measured on the GUI thread for a single
    300 DPI scanned-page selection. Almost every image this app produces is
    Format_RGB888 straight out of `render_crop`, so those are mapped straight
    from the image buffer via `constBits()` and Pillow's raw decoder, which is
    a memcpy. Formats without a direct mapping still go through PNG.
    """
    from PIL import Image

    if img.isNull() or img.width() <= 0 or img.height() <= 0:
        # Return a 1x1 placeholder instead of PNG round-trip on empty image
        from PIL import Image
        return Image.new("RGB", (1, 1), color="white")

    if img.format() == QImage.Format.Format_Indexed8:
        # Indexed needs the colour table carried across as a PIL palette.
        pil = Image.frombytes(
            "P", (img.width(), img.height()),
            img.constBits(), "raw", "P", img.bytesPerLine(), 1,
        )
        table = img.colorTable()
        flat = bytearray()
        for entry in table:
            flat += bytes(((entry >> 16) & 0xFF, (entry >> 8) & 0xFF, entry & 0xFF))
        if len(flat) < 768:                     # Pillow wants 256 entries
            flat += bytes(768 - len(flat))
        pil.putpalette(bytes(flat[:768]))
        return pil

    mapping = _QIMAGE_TO_PIL.get(img.format())
    if mapping is None:
        return _qimage_to_pil_via_png(img)

    pil_mode, raw_mode = mapping
    try:
        return Image.frombytes(
            pil_mode, (img.width(), img.height()),
            img.constBits(), "raw", raw_mode, img.bytesPerLine(), 1,
        )
    except (TypeError, ValueError):
        # Fallback for Pillow versions that don't handle voidptr
        try:
            data = bytes(img.constBits().asstring(img.sizeInBytes()))  # type: ignore
        except AttributeError:
            try:
                data = img.constBits().tobytes()  # type: ignore
            except AttributeError:
                return _qimage_to_pil_via_png(img)
        return Image.frombytes(
            pil_mode, (img.width(), img.height()),
            data, "raw", raw_mode, img.bytesPerLine(), 1,
        )


def page_text(page: pymupdf.Page) -> str:
    return page.get_text().strip()


def page_body_text(page: pymupdf.Page) -> tuple[str, str | None]:
    """Extract page text while removing a clearly styled running top header.

    A standalone page number in the page margins is returned separately so
    callers can keep it small at the end of a translation.
    """
    page_height = page.rect.height
    if page_height <= 0:
        return page_text(page), None

    has_text = False
    body_sizes: list[float] = []
    blocks = page.get_text("dict").get("blocks", [])
    for block in blocks:
        for line in block.get("lines", []):
            spans = [
                span for span in line.get("spans", [])
                if str(span.get("text", "")).strip()
            ]
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            has_text = True
            rect = pymupdf.Rect(line["bbox"])
            if page.rotation:
                rect *= page.rotation_matrix
            if (rect.y0 + rect.y1) / 2 >= page_height * 0.16:
                body_sizes.extend(
                    float(span.get("size", 0))
                    for span in spans
                    if float(span.get("size", 0)) > 0
                )

    if not has_text:
        return "", None

    body_sizes.sort()
    typical_size = body_sizes[len(body_sizes) // 2] if body_sizes else 0.0
    page_number: str | None = None
    body_lines: list[tuple[int, int, str, pymupdf.Rect]] = []
    for block_index, block in enumerate(blocks):
        for line_index, line in enumerate(block.get("lines", [])):
            spans = [
                span for span in line.get("spans", [])
                if str(span.get("text", "")).strip()
            ]
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            rect = pymupdf.Rect(line["bbox"])
            if page.rotation:
                rect *= page.rotation_matrix
            center_y = (rect.y0 + rect.y1) / 2
            at_page_edge = (
                center_y <= page_height * 0.16
                or center_y >= page_height * 0.88
            )
            if at_page_edge and re.fullmatch(r"\d{1,4}", text):
                if page_number is None:
                    page_number = text
                continue

            is_top_margin = center_y <= page_height * 0.16
            is_italic = any(int(span.get("flags", 0)) & 2 for span in spans)
            sizes = [
                float(span.get("size", 0))
                for span in spans
                if float(span.get("size", 0)) > 0
            ]
            is_small = bool(
                sizes and typical_size
                and max(sizes) <= typical_size * 0.9
            )
            is_bold = any(int(span.get("flags", 0)) & 16 for span in spans)
            if is_top_margin and (is_italic or is_small) and not is_bold:
                continue
            body_lines.append((block_index, line_index, text, rect))

    body_line_map = {
        (line[0], line[1]): line[2]
        for line in body_lines
    }
    table_lines = _aligned_markdown_tables(body_lines)
    kept_blocks: list[str] = []
    for block_index, block in enumerate(blocks):
        kept_lines: list[str] = []
        for line_index in range(len(block.get("lines", []))):
            table = table_lines.get((block_index, line_index))
            if table is not None:
                kept_lines.append(table)
            elif (block_index, line_index) not in table_lines:
                text = body_line_map.get((block_index, line_index), "")
                if text:
                    kept_lines.append(text)
        if kept_lines:
            kept_blocks.append("\n".join(kept_lines))

    return "\n\n".join(kept_blocks).strip(), page_number


def _aligned_markdown_tables(
    lines: list[tuple[int, int, str, pymupdf.Rect]],
) -> dict[tuple[int, int], str | None]:
    """Reconstruct simple, consistently aligned PDF tables as Markdown."""
    if len(lines) < 6:
        return {}

    heights = sorted(line[3].height for line in lines if line[3].height)
    if not heights:
        return {}
    median_height = heights[len(heights) // 2]
    y_tolerance = max(2.0, median_height * 0.35)
    x_tolerance = max(12.0, median_height * 1.5)
    min_column_gap = max(18.0, median_height * 1.5)

    rows: list[tuple[float, list[tuple[int, int, str, pymupdf.Rect]]]] = []
    for item in sorted(lines, key=lambda line: line[3].y0 + line[3].height / 2):
        center_y = item[3].y0 + item[3].height / 2
        if rows and abs(rows[-1][0] - center_y) <= y_tolerance:
            rows[-1][1].append(item)
            row, members = rows[-1]
            rows[-1] = (
                (row * (len(members) - 1) + center_y) / len(members),
                members,
            )
        else:
            rows.append((center_y, [item]))

    candidate_rows = []
    for center_y, members in rows:
        cells = sorted(members, key=lambda item: item[3].x0)
        if len(cells) < 2:
            continue
        if any(
            cells[index + 1][3].x0 - cells[index][3].x1 < min_column_gap
            for index in range(len(cells) - 1)
        ):
            continue
        candidate_rows.append((center_y, cells))

    groups: list[list[tuple[float, list[tuple[int, int, str, pymupdf.Rect]]]]] = []
    current = []
    for candidate in candidate_rows:
        center_y, cells = candidate
        if current:
            prev_y, prev_cells = current[-1]
            aligned = (
                len(cells) == len(prev_cells)
                and all(
                    abs(cell[3].x0 - previous[3].x0) <= x_tolerance
                    for cell, previous in zip(cells, prev_cells)
                )
                and center_y - prev_y <= max(36.0, median_height * 4)
            )
            if not aligned:
                groups.append(current)
                current = []
        current.append(candidate)
    if current:
        groups.append(current)

    result: dict[tuple[int, int], str | None] = {}
    for group in groups:
        if len(group) < 3:
            continue
        columns = len(group[0][1])
        compact_columns = sum(
            sum(
                len(re.sub(r"\s+", "", row[1][column][2])) <= 24
                for row in group
            ) / len(group) >= 0.6
            for column in range(columns)
        )
        if not compact_columns:
            continue

        markdown_rows = []
        rtl_table = sum(
            bool(re.search(r"[\u0590-\u08FF]", cell[2]))
            for row in group
            for cell in row[1]
        ) > len(group) * columns / 2
        for row in group:
            row_cells = row[1]
            texts = [cell[2] for cell in row_cells]
            if rtl_table:
                texts.reverse()
            markdown_rows.append(
                "| " + " | ".join(text.replace("|", r"\|") for text in texts)
                + " |"
            )
        markdown_rows.insert(
            1, "| " + " | ".join("---" for _ in range(columns)) + " |")
        table_markdown = "\n".join(markdown_rows)
        member_keys = [
            (cell[0], cell[1])
            for row in group
            for cell in row[1]
        ]
        first_key = min(member_keys)
        for key in member_keys:
            result[key] = table_markdown if key == first_key else None
    return result


def has_sparse_text_over_scan(page: pymupdf.Page) -> bool:
    """Detect a page-sized scan with only a small, incomplete text layer."""
    words = page.get_text("words")
    if len(words) >= MAX_SCAN_TEXT_WORDS:
        return False
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return False
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image):
            if rect.width * rect.height / page_area >= MIN_SCAN_IMAGE_COVERAGE:
                return True
    return False


def _word_centre(w) -> tuple[float, float]:
    return ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)


def _group_words_into_lines(
    words: list, rotation_matrix: pymupdf.Matrix | None = None
) -> list[list]:
    """Group selected words into lines, respecting rotated page coordinates.

    Centre-based, not top-based: two words on the same row whose fonts
    differ (heading next to body text) share a centre even when their top
    edges differ by several points -- the old top-based rule split them.

    On rotated pages, PyMuPDF's word line/word indexes reflect the PDF text
    stream, while the text bounding boxes are in unrotated page coordinates.
    Use those indexes for membership and reading order, and the rotation
    matrix only to order lines as they appear on screen.
    """
    if not words:
        return []
    if rotation_matrix is not None:
        grouped: dict[tuple[int, int], list] = {}
        for word in words:
            grouped.setdefault((word[5], word[6]), []).append(word)

        def visual_position(line: list) -> tuple[float, float]:
            rects = [
                pymupdf.Rect(word[:4]) * rotation_matrix
                for word in line
            ]
            return (
                sum((rect.y0 + rect.y1) / 2 for rect in rects) / len(rects),
                sum((rect.x0 + rect.x1) / 2 for rect in rects) / len(rects),
            )

        lines = [
            sorted(line, key=lambda word: word[7])
            for line in grouped.values()
        ]
        return sorted(lines, key=visual_position)

    heights = sorted(w[3] - w[1] for w in words if w[3] > w[1])
    median_h = heights[len(heights) // 2] if heights else 12.0
    tol = max(3.0, median_h * 0.6)
    ordered = sorted(words, key=lambda w: _word_centre(w)[1])
    lines: list[list] = []
    current: list = []
    current_c: float | None = None
    for w in ordered:
        c = _word_centre(w)[1]
        if current_c is None or abs(c - current_c) <= tol:
            current.append(w)
            if current_c is None:
                current_c = c
            else:
                current_c = (current_c * (len(current) - 1) + c) / len(current)
        else:
            lines.append(sorted(current, key=lambda w: w[0]))
            current, current_c = [w], c
    if current:
        lines.append(sorted(current, key=lambda w: w[0]))
    return lines


def _within(word, x0: float, y0: float, x1: float, y1: float) -> bool:
    """A word counts as selected when its centre is inside the rect.

    The old rule (>=40% of the word's height overlapping the rect) gave
    inconsistent results across font sizes and could deliver a half-line
    while the app reported complete success. Centre inclusion is what the
    user sees as "I put the box around this text".
    """
    cx, cy = _word_centre(word)
    return x0 <= cx <= x1 and y0 <= cy <= y1


class Extraction:
    """Result of a selection. Complete means every touched line was delivered."""

    def __init__(self, text: str, requested_lines: int, returned_lines: int):
        self.text = text
        self.requested_lines = requested_lines
        self.returned_lines = returned_lines
        self.complete = requested_lines == returned_lines

    def __iter__(self):  # unpacks as (text, complete) for legacy use
        yield self.text
        yield self.complete


def extract_region(page: pymupdf.Page, rect) -> Extraction:
    """Whole-line extraction over a rect in PDF points.

    Invariant, asserted by the test suite: every visual line the rect brushes is
    returned *whole* — `requested_lines == returned_lines`, so `complete` is
    always True. That is the guarantee, not a measured statistic: the earlier
    rule could hand back half a line and still report success.

    `requested_lines`/`returned_lines` are kept because callers and tests read
    them; treat a False `complete` as a regression in this function, never as a
    normal outcome.
    """
    x0, y0, x1, y1 = (min(rect[0], rect[2]), min(rect[1], rect[3]),
                      max(rect[0], rect[2]), max(rect[1], rect[3]))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return Extraction("", 0, 0)
    picked = [w for w in page.get_text("words") if _within(w, x0, y0, x1, y1)]
    rotation_matrix = page.rotation_matrix if page.rotation else None
    lines = _group_words_into_lines(picked, rotation_matrix)
    text = "\n".join(" ".join(w[4] for w in line) for line in lines)
    return Extraction(text, len(lines), len(lines))


def extract_in_rect(page: pymupdf.Page, rect) -> tuple[str, object | None]:
    """Back-compatible 2-tuple wrapper around `extract_region`."""
    out = extract_region(page, rect)
    return out.text, pymupdf.Rect(*rect) if out.text else None
