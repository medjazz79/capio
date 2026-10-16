"""Second free layer: MyMemory (auto source detection). Falls through on any error."""
from __future__ import annotations

import requests

TIMEOUT = 15


def detect_source(text: str) -> str:
    if any("\u0400" <= c <= "\u04FF" for c in text):
        return "ru"
    try:
        from langdetect import detect
        code = (detect(text) or "en").split("-")[0].lower()
    except Exception:
        return "en"
    return {"en": "en", "fr": "fr", "de": "de", "it": "it",
            "es": "es", "ru": "ru", "ar": "ar", "la": "la"}.get(code, "en")


def translate(
    text: str,
    timeout: int = TIMEOUT,
    email: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    src = detect_source(text) if source_language == "auto" else source_language
    params = {"q": text, "langpair": f"{src}|{target_language}"}
    if (email or "").strip():
        params["de"] = email.strip()  # raises the free quota (~10x)
    last: Exception | None = None
    for _ in range(2):
        try:
            r = requests.get(
                "https://api.mymemory.translated.net/get",
                params=params,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError(
                    "Unexpected reply format from MyMemory translation service")
            status = data.get("responseStatus")
            response_data = data.get("responseData")
            if not isinstance(response_data, dict):
                raise RuntimeError(
                    "Unexpected reply format from MyMemory translation service")
            translated_text = response_data.get("translatedText")
            out = translated_text.strip() if isinstance(translated_text, str) else ""
            bad_markers = ("MYMEMORY WARNING", "INVALID", "QUERY LENGTH LIMIT",
                           "NO QUERY SPECIFIED", "PLEASE SELECT")
            if status != 200 or not out or out.upper().startswith(bad_markers):
                raise RuntimeError(f"MyMemory refused the request: {out[:120]}")
            return out
        except Exception as e:
            last = e
            import time
            time.sleep(2)
    raise last if last is not None else RuntimeError("MyMemory failed")
