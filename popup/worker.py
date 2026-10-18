"""Background worker for PDF text, OCR, and direct image translation."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Signal


class TranslateWorker(QThread):
    done = Signal(str, str)   # translated text, engine badge
    stage = Signal(str)       # status text for the spinner
    failed = Signal(str)
    source_ready = Signal(str)
    partial = Signal(str)     # live accumulated text while streaming

    def __init__(self, settings: dict, text: str | None = None,
                 pil_image=None, ocr_langs=("ru", "en")):
        super().__init__()
        self._text = text
        self._pil_image = pil_image
        # None (or ("auto",)) = detect the language automatically.
        self._ocr_langs = None if ocr_langs in (None, ("auto",)) else tuple(ocr_langs)
        self._settings = dict(settings)
        self.translate_secs: float = 0.0  # proof of which layer answered + how long

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            if self._pil_image is not None:
                if self._settings.get("selection_mode", "ocr") == "image":
                    self._translate_image()
                    return
                from translators import ocr
                if not ocr.is_warmed():
                    self.stage.emit("First run: preparing the reading engine (once)…")
                else:
                    self.stage.emit(f"Reading with {ocr.backend_name()}…")
                if self._ocr_langs is None:
                    self._text, _used = ocr.auto_read(self._pil_image)
                else:
                    self._text = ocr.read_text(self._pil_image, self._ocr_langs)
                if self.isInterruptionRequested():
                    return
                if not (self._text or "").strip():
                    self.failed.emit(
                        "Could not read any text from this image.\n"
                        "Try selecting a bigger area with larger print."
                    )
                    return
            self.source_ready.emit(self._text or "")
            self.stage.emit("Translating…")
            import time
            from translators import chain
            t0 = time.time()
            translated_text, badge = "", "—"
            for kind, partial, badge in chain.translate_stream(
                    self._text, self._settings):
                if self.isInterruptionRequested():
                    return
                if kind == "partial" and partial:
                    self.partial.emit(partial)
                elif kind == "done":
                    translated_text, badge = partial, badge
            if self.isInterruptionRequested():
                return
            self.translate_secs = time.time() - t0
            self.done.emit(translated_text, badge)
        except Exception as e:  # safety net
            self.failed.emit(str(e))

    def _translate_image(self) -> None:
        from translators import chain
        from config.settings import resolve_model

        self.stage.emit(f"Sending image to {resolve_model(self._settings)}…")
        import time
        started = time.time()
        translated_text, badge = "", "—"
        for kind, partial, current_badge in chain.translate_image_stream(
                self._pil_image, self._settings):
            if self.isInterruptionRequested():
                return
            if kind == "partial" and partial:
                self.partial.emit(partial)
            elif kind == "done":
                translated_text, badge = partial, current_badge
        if self.isInterruptionRequested():
            return
        self.translate_secs = time.time() - started
        self.done.emit(translated_text, badge)


class PageTranslationWorker(TranslateWorker):
    """Prepare a PDF page and translate it without blocking the GUI thread."""

    def __init__(
        self,
        settings: dict,
        prepare_input: Callable[
            [], tuple[str | None, object | None, tuple[str, ...] | None]
        ],
    ):
        super().__init__(settings)
        self._prepare_input = prepare_input

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            self.stage.emit("Preparing the next page…")
            self._text, self._pil_image, ocr_langs = self._prepare_input()
            self._ocr_langs = (
                None if ocr_langs in (None, ("auto",))
                else tuple(ocr_langs)
            )
        except Exception as error:
            self.failed.emit(str(error))
            return
        super().run()
