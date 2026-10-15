"""Clean OCR abstraction: providers are swappable without touching document logic."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class OcrBox:
    """Four-corner polygon in IMAGE pixels (source crop coordinates)."""
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    x3: float = 0.0
    y3: float = 0.0
    x4: float = 0.0
    y4: float = 0.0

    def as_rect(self) -> tuple[float, float, float, float]:
        xs = (self.x1, self.x2, self.x3, self.x4)
        ys = (self.y1, self.y2, self.y3, self.y4)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class OcrWord:
    text: str
    box: OcrBox | None = None
    confidence: float | None = None


@dataclass
class OcrLine:
    text: str
    box: OcrBox | None = None
    words: list[OcrWord] = field(default_factory=list)


@dataclass
class OcrResult:
    text: str
    lines: list[OcrLine] = field(default_factory=list)
    backend: str = ""
    angle: float | None = None

    @property
    def words(self) -> list[OcrWord]:
        return [w for line in self.lines for w in line.words]

    @property
    def mean_confidence(self) -> float:
        confs = [w.confidence for w in self.words if w.confidence is not None]
        return sum(confs) / len(confs) if confs else 0.0


class OcrUnavailable(RuntimeError):
    pass


class OCRProvider(ABC):
    """One OCR engine. Coordinates are always in source-image pixels."""

    name: str = "?"

    @abstractmethod
    def is_available(self) -> bool:
        """True if this engine can run on this machine right now."""

    @abstractmethod
    def read(self, pil_image, langs: tuple | None = None) -> OcrResult:
        """OCR a PIL image. `langs` is a hint; providers may ignore it."""

    def close(self) -> None:
        """Release anything the provider holds. Called on shutdown/reset.

        Deliberately concrete rather than abstract: a provider that holds no
        native resources has nothing to release, and making this abstract would
        break every such provider for no benefit. Providers that do hold
        resources must override it, or they pin them for the process lifetime
        because nothing in the app knows to call anything else.
        """
