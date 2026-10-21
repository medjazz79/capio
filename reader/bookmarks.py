"""Durable, portable bookmarks, independent of session/settings snapshots.

Each book has a content-addressed record in the user's Capio directory and a
sidecar next to the source. Neither belongs to the application installation.
Per-page timestamps merge edits from two windows, including explicit removals.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)


class BookmarkStore:
    def __init__(self, source: str):
        self.source = Path(source).resolve()
        digest = hashlib.sha256()
        with self.source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        self.identity = digest.hexdigest()
        self.directory = settings.SETTINGS_PATH.parent / "Capio" / "Bookmarks"
        self.record_path = self.directory / (self.identity + ".json")
        self.sidecar_path = self.source.with_name(self.source.name + ".capio-bookmarks.json")
        self.marks: dict[str, dict] = {}
        self.exists = False
        self.pages = self._load()

    def _read(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("document") != self.identity or data.get("version") != 1:
                return None
            marks = data["marks"]
            if not isinstance(marks, dict):
                return None
            for page, mark in marks.items():
                if (not page.isdecimal() or not isinstance(mark, dict)
                        or type(mark.get("marked")) is not bool
                        or type(mark.get("updated")) is not int):
                    return None
            return marks
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.warning("Could not read bookmark record %s", path, exc_info=True)
            return None

    def _load(self) -> set[int]:
        for path in (self.record_path, self.sidecar_path):
            # A valid empty primary is authoritative; old backups must not
            # resurrect a bookmark the user explicitly removed.
            marks = self._read(path)
            if marks is None:
                marks = self._read(path.with_suffix(path.suffix + ".bak"))
            if marks is not None:
                self.exists = True
                for page, mark in marks.items():
                    if mark["updated"] > self.marks.get(page, {}).get("updated", -1):
                        self.marks[page] = mark
        return {int(page) for page, mark in self.marks.items() if mark["marked"]}

    def save(self, pages: set[int]) -> set[int]:
        """Commit only this window's edits, preserving other windows' marks."""
        edits = self.pages ^ pages
        if not edits and self.exists:
            return set(self.pages)
        with settings._settings_transaction():
            self._load()
            updated = max(time.time_ns(), max(
                (mark["updated"] + 1 for mark in self.marks.values()), default=0))
            for page in edits:
                self.marks[str(page)] = {"marked": page in pages, "updated": updated}
            data = {"version": 1, "document": self.identity, "marks": self.marks}
            successes = 0
            errors = []
            for path in (self.record_path, self.sidecar_path):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # Preserve the latest valid primary before replacing it.
                    previous = self._read(path)
                    if previous is not None:
                        settings._write_json_atomically(
                            path.with_suffix(path.suffix + ".bak"),
                            {"version": 1, "document": self.identity, "marks": previous})
                    settings._write_json_atomically(path, data)
                    successes += 1
                except OSError as error:
                    errors.append(error)
                    log.warning("Could not save bookmarks to %s", path, exc_info=True)
            if not successes:
                raise OSError("Could not save bookmarks to either location") from errors[-1]
            self.exists = True
            self.pages = {int(page) for page, mark in self.marks.items() if mark["marked"]}
            return set(self.pages)
