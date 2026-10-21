"""Keep background MuPDF calls in a spawned process with its own document."""
from __future__ import annotations

import multiprocessing
import threading
import time


def _serve(connection):
    import pymupdf
    from pathlib import Path
    from reader import pdf_utils

    document = None
    try:
        while True:
            operation, args = connection.recv()
            if operation == "close":
                return
            try:
                if operation == "open":
                    if document is not None:
                        document.close()
                        document = None
                    path, data = args
                    # Do not hold a Windows file handle across a Save/Save As.
                    source = data if data is not None else Path(path).read_bytes()
                    document = pymupdf.open(stream=source, filetype="pdf")
                    result = None
                else:
                    if document is None:
                        raise RuntimeError("Background PDF document is closed")
                    page = document[args[0]]
                    if operation == "render":
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(args[1], args[1]),
                                             colorspace=pymupdf.csRGB, alpha=False)
                        result = (pix.width, pix.height, pix.stride, pix.samples)
                    elif operation == "body":
                        result = pdf_utils.page_body_text(page)
                    elif operation == "text":
                        result = pdf_utils.page_text(page)
                    elif operation == "needs_ocr":
                        result = pdf_utils.has_sparse_text_over_scan(page)
                    else:
                        raise ValueError(f"Unknown PDF operation: {operation}")
                connection.send((True, result))
                result = None
                if operation == "render":
                    pix = None
            except Exception as error:
                connection.send((False, str(error)))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if document is not None:
            document.close()
        connection.close()


class PdfProcess:
    """Lazy, serial IPC. The GUI never waits for a render while closing a book."""

    def __init__(self, path):
        self._source = (str(path), None)
        self._generation = 0
        self._loaded_generation = -1
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = threading.Event()
        self._process = None
        self._connection = None

    def set_source(self, path, data=None):
        self._source = (str(path), data)
        self._generation += 1

    def _exchange(self, operation, args):
        connection = self._connection
        connection.send((operation, args))
        deadline = time.monotonic() + 60
        while not connection.poll(0.05):
            if self._closed.is_set():
                raise RuntimeError("Background PDF operation cancelled")
            if not self._process.is_alive():
                raise RuntimeError("The background PDF process stopped unexpectedly")
            if time.monotonic() >= deadline:
                self._process.terminate()
                raise RuntimeError("The background PDF operation timed out")
        success, result = connection.recv()
        if not success:
            raise RuntimeError(result)
        return result

    def request(self, operation, *args):
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("Background PDF document is closed")
            if self._process is None:
                with self._lifecycle_lock:
                    if self._closed.is_set():
                        raise RuntimeError("Background PDF document is closed")
                    context = multiprocessing.get_context("spawn")
                    parent, child = context.Pipe()
                    process = context.Process(target=_serve, args=(child,), daemon=True)
                    process.start()
                    self._connection, self._process = parent, process
                    child.close()
            try:
                generation, source = self._generation, self._source
                if generation != self._loaded_generation:
                    self._exchange("open", source)
                    self._loaded_generation = generation
                return self._exchange(operation, args)
            except (OSError, EOFError, ValueError) as error:
                raise RuntimeError("Background PDF communication stopped") from error

    def close(self):
        self._closed.set()
        with self._lifecycle_lock:
            process = self._process
            if process is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
            if self._connection is not None:
                self._connection.close()
