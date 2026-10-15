"""Provider selection for the Windows 11 native OneOCR engine."""
from __future__ import annotations

import threading

from .base import OcrResult, OcrUnavailable, OCRProvider

# `all_providers()` is reached from MainWindow._warm_ocr's threading.Thread and
# from popup/worker.py's TranslateWorker (a QThread) at the same time. The old
# unguarded `if _providers is None: _providers = _build()` let both win the
# race: two OneOCRProvider objects, each with its own Lock, so two native
# engines loaded and the recognition model was held twice with no way to
# release the loser. `_preferred` was written from the same threads with no
# synchronisation at all.
_lock = threading.RLock()

_providers: list[OCRProvider] | None = None
_preferred: OCRProvider | None = None


def _build() -> list[OCRProvider]:
    from .oneocr_backend import OneOCRProvider
    return [OneOCRProvider()]


def all_providers() -> list[OCRProvider]:
    global _providers
    with _lock:
        if _providers is None:
            _providers = _build()
        return _providers


def get_provider() -> OCRProvider | None:
    """Return OneOCR when installed, without warming the engine."""
    for p in all_providers():
        if p.is_available():
            return p
    return None


def read(pil_image, langs: tuple | None = None) -> OcrResult:
    """Read an image with OneOCR; propagate its errors instead of hiding them."""
    provider = get_provider()
    if provider is None:
        raise OcrUnavailable(
            "OneOCR is unavailable. OCR requires the Windows 11 Snipping Tool."
        )
    result = provider.read(pil_image, langs)
    global _preferred
    with _lock:
        _preferred = provider
    return result


def backend_name() -> str:
    with _lock:
        preferred = _preferred
    if preferred is not None:
        return preferred.name
    p = get_provider()
    return p.name if p else "none"


def close_all() -> None:
    """Close every live provider and forget them."""
    global _providers, _preferred
    with _lock:
        providers, _providers, _preferred = _providers, None, None
    for provider in providers or ():
        try:
            provider.close()
        except Exception:
            # A provider that fails to release must not stop the others.
            pass


def reset_for_tests() -> None:
    close_all()