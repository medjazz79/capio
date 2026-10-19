"""OCR facade: stable API for the app, engines live in ocr_providers/ (swappable)."""
from __future__ import annotations

from ocr_providers import manager
from ocr_providers.base import OcrResult

LATIN_LANGS = ("en", "it", "fr", "de", "es")
RU_LANGS = ("ru", "en")

# Reader and floating translator share one source-language -> OCR hint map.
OCR_LANGUAGE_HINTS = {
    "ar": ("ar",),
    "fr": ("fr", "en"),
    "en": ("en",),
    "ru": ("ru", "en"),
    "it": ("it", "en"),
    "es": ("es", "en"),
    "de": ("de", "en"),
}


def languages_for(source_language: str) -> tuple[str, ...] | None:
    """OCR hints for a chosen source language (None lets the engine auto-detect)."""
    return OCR_LANGUAGE_HINTS.get(source_language)


def is_available() -> bool:
    return manager.get_provider() is not None


def backend_name() -> str:
    return manager.backend_name()


def is_warmed(langs=("ru", "en")) -> bool:
    del langs
    p = manager.get_provider()
    return bool(p is not None and getattr(p, "warmed", False))


def parse_langs(spec: str) -> tuple:
    langs = tuple(l.strip().lower() for l in (spec or "").split(",") if l.strip())
    return langs or RU_LANGS


def prepare_image(pil_image):
    """Clean a crop for OCR: upscale small print, boost contrast, add a border."""
    from PIL import Image, ImageOps
    img = pil_image.convert("L")
    w, h = img.size
    if w < 1600:
        scale = 1600 / max(1, w)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageOps.expand(img, border=24, fill=255)
    return img.convert("RGB")


def clean_ocr_text(text: str) -> str:
    """Make a raw OCR dump safe to send to the model.

    Join broken hyphens (both `-` and OneOCR's `_` form), normalise spaces,
    and drop blank line runs -- the provider returns all three.
    """
    from . import chain as _chain
    text = _chain.dehyphenate(text)
    lines: list[str] = []
    blank = False
    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if line:
            lines.append(line)
            blank = False
        elif not blank:
            lines.append("")
            blank = True
    while lines and lines[-1] == "":
        lines.pop()
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines)


def read_with_boxes(pil_image, langs: tuple | None = None) -> OcrResult:
    """Full result: text + line/word boxes in source-image pixels. For overlays."""
    result = manager.read(pil_image, langs)
    result.text = clean_ocr_text(result.text)
    return result


def read_text(pil_image, langs=RU_LANGS) -> str:
    """Run OCR on a PIL image, return lines of text (may be empty)."""
    return read_with_boxes(pil_image, tuple(langs)).text


def auto_read(pil_image) -> tuple[str, tuple]:
    """Read multilingual text with OneOCR; its language hint is automatic."""
    provider = manager.get_provider()
    if provider is None:
        raise manager.OcrUnavailable(
            "OneOCR is unavailable. Install or repair the Windows 11 Snipping "
            "Tool to use OCR."
        )
    return clean_ocr_text(provider.read(pil_image).text), ("auto",)
