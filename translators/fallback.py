"""Free keyless fallback (Google GTX, auto-detect)."""
from __future__ import annotations

import requests

GTX_URL = "https://translate.googleapis.com/translate_a/single"
TIMEOUT = 18
TRIES = 2


def gtx_translate(text: str, source_language: str = "auto",
                  target_language: str = "ar") -> str:
    last: Exception | None = None
    for _ in range(TRIES):
        try:
            params = {
                "client": "gtx",
                "sl": "auto" if source_language == "auto" else source_language,
                "tl": target_language,
                "dt": "t",
                "q": text,
            }
            r = requests.get(GTX_URL, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
            try:
                out = "".join(seg[0] for seg in data[0] if seg[0]).strip()
            except (TypeError, IndexError) as e:
                raise RuntimeError(
                    "Unexpected reply from the free translation service"
                ) from e
            if out:
                # The free service reports no token counts, so record an
                # estimate instead. It is marked as one so the UI can show
                # it differently from a measured number.
                from . import usage as _usage

                _usage.note_estimate(
                    "google-translate", "free",
                    prompt_text=text, completion_text=out)
                return out
            last = RuntimeError("Empty reply from the free translation service")
        except Exception as e:
            last = e
    raise last if last is not None else RuntimeError("Free translation failed")
