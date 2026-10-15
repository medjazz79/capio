"""Public surface of the OCR provider package."""
from .base import OcrBox, OcrLine, OcrResult, OcrUnavailable, OCRProvider, OcrWord
from .manager import backend_name, get_provider, reset_for_tests

__all__ = ["OcrBox", "OcrLine", "OcrResult", "OcrUnavailable", "OCRProvider",
           "OcrWord", "backend_name", "get_provider", "reset_for_tests"]
