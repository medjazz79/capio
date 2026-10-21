"""Background worker for the translation popup's follow-up chat."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class ChatWorker(QThread):
    done = Signal(str)
    failed = Signal(str)
    partial = Signal(str)

    def __init__(
        self,
        messages: list[dict[str, str]],
        settings: dict,
        source_text: str,
        translated_text: str,
    ):
        super().__init__()
        self._messages = [dict(message) for message in messages]
        self._settings = dict(settings)
        self._settings["_cancelled"] = self.isInterruptionRequested
        self._source_text = source_text
        self._translated_text = translated_text

    def run(self) -> None:
        from translators.http_client import cancellation_scope, RequestCancelled
        if self.isInterruptionRequested():
            return
        try:
            with cancellation_scope(self.isInterruptionRequested):
                self._reply()
        except RequestCancelled:
            return

    def _reply(self) -> None:
        chunks: list[str] = []
        try:
            from translators.chat import reply_stream

            for chunk in reply_stream(
                self._messages,
                self._settings,
                self._source_text,
                self._translated_text,
            ):
                if self.isInterruptionRequested():
                    return
                chunks.append(chunk)
                self.partial.emit("".join(chunks))
            answer = "".join(chunks)
            if not answer.strip():
                raise RuntimeError("The selected model returned an empty reply.")
            if not self.isInterruptionRequested():
                self.done.emit(answer.strip())
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))
