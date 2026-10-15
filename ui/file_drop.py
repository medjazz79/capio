"""Helpers for accepting PDF files dropped onto application windows."""
from __future__ import annotations

from pathlib import Path

from reader.document_import import SUPPORTED_EXTENSIONS


def _local_paths_with_extensions(mime_data, extensions: frozenset[str]) -> list[str]:
    if not mime_data.hasUrls():
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for url in mime_data.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if Path(path).suffix.lower() not in extensions:
            continue
        key = str(Path(path).resolve()).casefold()
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def local_document_paths(mime_data) -> list[str]:
    """Return unique local paths in formats supported by the main reader."""
    return _local_paths_with_extensions(mime_data, SUPPORTED_EXTENSIONS)


def local_pdf_paths(mime_data) -> list[str]:
    """Return unique local PDF paths for legacy PDF-only entry points."""
    return _local_paths_with_extensions(mime_data, frozenset({".pdf"}))
