"""Gemini translator (free key from Google AI Studio). REST only, no SDK."""
from __future__ import annotations

import base64
from io import BytesIO
import json
import threading
import time

import requests
from PIL import Image

from .prompt import (
    output_token_budget, system_instructions, user_prompt,
)
from .vision import image_system_instructions, image_translation_prompt

TIMEOUT = 12
CHAT_TIMEOUT = 30
IMAGE_TIMEOUT = 45
IMAGE_MAX_OUTPUT_TOKENS = 8192
TEXT_MIN_OUTPUT_TOKENS = 8192
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Google answers 503 ("high demand") and 429 (quota) for moments at a time --
# verified on this key, where a plain retry minutes later succeeded. 400/404
# never clear on their own: 404 is how Google retires a model, so retrying it
# only burns latency.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (1, 3)  # seconds to wait before attempts 2 and 3

# Verified available on a plain AI Studio key. `gemini-2.0-flash` (404) and
# `gemini-1.5-flash` (retired) used to be listed here and always failed.
STARTER_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

# Text-in / text-out only. The picker must not offer image or TTS models.
_EXCLUDE = ("-image", "-tts", "-transcribe", "robotics", "computer-use",
            "deep-research", "nano-banana", "omni", "antigravity")

_lock = threading.Lock()
# Catalogue per API key. Keying it is the whole point: one shared list meant a
# second key kept being answered with the first key's models, and every model
# the new key supports but the first did not showed disabled with
# "not available on your Google key".
_cache: dict[str, list[str]] = {}
# Which key the *keyless* picker render serves. The rows a picker offers come
# from `known_models()` with no key, so the most recently fetched catalogue is
# what the ⟳ button's result has to keep driving. `availability` never reads
# this — see `catalogue_for`.
_last_key = ""


def known_models(refresh: bool = False, api_key: str = "",
                 timeout: int = 12) -> list[str]:
    """Chat-capable models for this key, cached. Falls back to the starters.

    With a key, the catalogue is fetched once and then remembered per key: that
    is the list the picker offers and `registry.availability` judges against.
    Without a key nothing is fetched and nothing is stored — the keyless call
    only decides which rows to draw.
    """
    key = (api_key or "").strip()
    if not refresh:
        cached = _cached_for(key)
        if cached is not None:
            return list(cached)
        if not key:
            return list(STARTER_MODELS)  # nothing to fetch with; store nothing
    models = _fetch(key, timeout)
    if not models:
        # A fetch that failed, or a keyless refresh, is not a catalogue. Storing
        # the starters here is what made one bad response hide every real model
        # behind the starters until the user pressed ⟳ again.
        return list(STARTER_MODELS)
    global _last_key
    with _lock:
        _cache[key] = list(models)
        _last_key = key
    return list(models)


def catalogue_for(api_key: str = "") -> list[str]:
    """What this key supports, from cache only: never a network call.

    `registry.availability` asks this once per picker row on every repaint, so a
    request per row would freeze the Settings dialog. An unknown key falls back
    to the starters, which are verified working on a plain AI Studio key — a
    conservative answer, but never another account's catalogue.
    """
    cached = _cached_for((api_key or "").strip())
    return list(cached) if cached is not None else list(STARTER_MODELS)


def _cached_for(key: str) -> list[str] | None:
    """The remembered catalogue for this key, or for the last one fetched when
    no key is given. None means "nothing remembered", which is not the same as
    the starter fallback."""
    with _lock:
        return _cache.get(key) if key else _cache.get(_last_key)


def _fetch(api_key: str, timeout: int) -> list[str]:
    """The models Google reports for this key; [] when there is no key or the
    request fails."""
    if not api_key:
        return []
    try:
        r = requests.get(f"{API_ROOT}/models",
                          headers=_auth(api_key), timeout=timeout)
        r.raise_for_status()
    except Exception:
        return []
    models: list[str] = []
    for m in r.json().get("models", []):
        name = str(m.get("name", "")).split("/")[-1]
        methods = m.get("supportedGenerationMethods") or []
        if not name or "generateContent" not in methods:
            continue
        if any(bad in name.lower() for bad in _EXCLUDE):
            continue
        models.append(name)
    if not models:
        return []
    return sorted(set(models), key=_model_rank)


def _model_rank(name: str) -> tuple:
    """Keep the starters first and on top; everything else alphabetically."""
    try:
        return (0, STARTER_MODELS.index(name)) if name in STARTER_MODELS else (1, name)
    except ValueError:
        return (1, name)


def _auth(api_key: str) -> dict:
    """Send the key as a header, never as a query parameter.

    `?key=...` ends up in proxy logs, browser history and crash reports.
    """
    return {"x-goog-api-key": api_key} if api_key else {}


def _backoff(attempt: int) -> float:
    """Seconds to wait before attempt N (1-based): 1s, then 3s, then hold."""
    return RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]


def _validate_finish_reason(finish_reason: str | None) -> None:
    if finish_reason == "STOP":
        return
    if finish_reason == "MAX_TOKENS":
        raise RuntimeError("reply was cut off — try a shorter selection")
    if not finish_reason:
        raise RuntimeError("Gemini ended the reply without a completion reason")
    raise RuntimeError(
        f"Gemini ended the reply before completion ({finish_reason})")


def translate(
    text: str,
    api_key: str,
    model: str = STARTER_MODELS[0],
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    url = f"{API_ROOT}/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_instructions(
            instructions, source_language, target_language)}]},
        "contents": [{"parts": [{"text": user_prompt(
            text, source_language, target_language)}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": output_token_budget(
                text, minimum=TEXT_MIN_OUTPUT_TOKENS),
        },
    }

    last: Exception | None = None
    attempts_used = 0
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_backoff(attempt))
        attempts_used = attempt + 1
        try:
            r = requests.post(url, headers=_auth(api_key), json=payload,
                            timeout=TIMEOUT)
        except requests.RequestException as e:
            # Already slow (connect/read timeout): retrying would compound it.
            last = e
            break

        if r.status_code == 404:
            raise RuntimeError(f"model {model} is not available on this key")
        if r.status_code in RETRY_STATUSES:
            last = requests.HTTPError(f"{r.status_code} from Gemini", response=r)
            continue
        r.raise_for_status()
        return _parse(r, model)

    unit = "attempt" if attempts_used == 1 else "attempts"
    raise RuntimeError(
        f"Gemini {model} failed after {attempts_used} {unit}: {last}"
    )


def stream_translate(
    text: str,
    api_key: str,
    model: str = STARTER_MODELS[0],
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
):
    """Yield Gemini text deltas from its streaming GenerateContent endpoint."""
    url = f"{API_ROOT}/models/{model}:streamGenerateContent?alt=sse"
    payload = {
        "system_instruction": {"parts": [{"text": system_instructions(
            instructions, source_language, target_language)}]},
        "contents": [{"parts": [{"text": user_prompt(
            text, source_language, target_language)}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": output_token_budget(
                text, minimum=TEXT_MIN_OUTPUT_TOKENS),
        },
    }

    last: Exception | None = None
    attempts_used = 0
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_backoff(attempt))
        attempts_used = attempt + 1
        try:
            response = requests.post(
                url, headers=_auth(api_key), json=payload,
                timeout=TIMEOUT, stream=True)
        except requests.RequestException as error:
            last = error
            break

        if response.status_code == 404:
            response.close()
            raise RuntimeError(f"model {model} is not available on this key")
        if response.status_code in RETRY_STATUSES:
            last = requests.HTTPError(
                f"{response.status_code} from Gemini", response=response)
            response.close()
            continue

        received_text = False
        finish_reason = None
        # Gemini repeats the same usageMetadata on every chunk, so it is kept
        # and recorded once when the stream ends, not once per chunk.
        pending_usage = None
        try:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = (
                    line.decode("utf-8", "replace")
                    if isinstance(line, bytes) else line
                )
                if not chunk.startswith("data:"):
                    continue
                try:
                    event = json.loads(chunk[len("data:"):].strip())
                    candidate = event["candidates"][0]
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    raise RuntimeError(
                        f"Unexpected streamed reply from Gemini ({model})"
                    ) from error
                if event.get("usageMetadata"):
                    pending_usage = event["usageMetadata"]
                if candidate.get("finishReason"):
                    finish_reason = candidate["finishReason"]
                content = candidate.get("content") or {}
                for part in content.get("parts") or []:
                    delta = part.get("text", "")
                    if delta:
                        received_text = True
                        yield delta
        finally:
            response.close()
        if pending_usage:
            from . import usage as _usage

            _usage.note(model, pending_usage, "gemini")
        _validate_finish_reason(finish_reason)
        if not received_text:
            raise RuntimeError(f"Gemini returned an empty reply for {model}")
        return

    unit = "attempt" if attempts_used == 1 else "attempts"
    raise RuntimeError(
        f"Gemini {model} failed after {attempts_used} {unit}: {last}"
    )


def translate_image(
    image: Image.Image,
    api_key: str,
    model: str = STARTER_MODELS[0],
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    """Translate text visible in an image using Gemini's multimodal endpoint."""
    if not isinstance(image, Image.Image):
        raise TypeError("Image translation requires a PIL image")

    image = image.convert("RGB")
    image.thumbnail((3072, 3072), Image.Resampling.LANCZOS)
    image_buffer = BytesIO()
    image.save(image_buffer, format="JPEG", quality=90, optimize=True)
    encoded_image = base64.b64encode(image_buffer.getvalue()).decode("ascii")

    prompt = image_translation_prompt(source_language, target_language)
    url = f"{API_ROOT}/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{
            "text": image_system_instructions(
                instructions, source_language, target_language),
        }]},
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {
                "mime_type": "image/jpeg",
                "data": encoded_image,
            }},
        ]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": IMAGE_MAX_OUTPUT_TOKENS,
        },
    }

    last: Exception | None = None
    attempts_used = 0
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_backoff(attempt))
        attempts_used = attempt + 1
        try:
            response = requests.post(
                url, headers=_auth(api_key), json=payload, timeout=IMAGE_TIMEOUT)
        except requests.RequestException as error:
            last = error
            break

        if response.status_code == 404:
            raise RuntimeError(f"model {model} is not available on this key")
        if response.status_code in RETRY_STATUSES:
            last = requests.HTTPError(
                f"{response.status_code} from Gemini", response=response)
            continue
        response.raise_for_status()
        return _parse(response, model)

    unit = "attempt" if attempts_used == 1 else "attempts"
    raise RuntimeError(
        f"Gemini image translation failed after {attempts_used} {unit}: {last}"
    )


def stream_translate_image(
    image: Image.Image,
    api_key: str,
    model: str = STARTER_MODELS[0],
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
):
    """Yield text deltas while Gemini reads and translates an image."""
    if not isinstance(image, Image.Image):
        raise TypeError("Image translation requires a PIL image")

    image = image.convert("RGB")
    image.thumbnail((3072, 3072), Image.Resampling.LANCZOS)
    image_buffer = BytesIO()
    image.save(image_buffer, format="JPEG", quality=90, optimize=True)
    encoded_image = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    payload = {
        "system_instruction": {"parts": [{
            "text": image_system_instructions(
                instructions, source_language, target_language),
        }]},
        "contents": [{"parts": [
            {"text": image_translation_prompt(source_language, target_language)},
            {"inline_data": {
                "mime_type": "image/jpeg",
                "data": encoded_image,
            }},
        ]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": IMAGE_MAX_OUTPUT_TOKENS,
        },
    }
    url = f"{API_ROOT}/models/{model}:streamGenerateContent?alt=sse"
    last: Exception | None = None
    attempts_used = 0
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_backoff(attempt))
        attempts_used = attempt + 1
        try:
            response = requests.post(
                url, headers=_auth(api_key), json=payload,
                timeout=IMAGE_TIMEOUT, stream=True)
        except requests.RequestException as error:
            last = error
            break

        if response.status_code == 404:
            response.close()
            raise RuntimeError(f"model {model} is not available on this key")
        if response.status_code in RETRY_STATUSES:
            last = requests.HTTPError(
                f"{response.status_code} from Gemini", response=response)
            response.close()
            continue

        received_text = False
        truncated = False
        # Same as the text stream: recorded once, after the loop.
        pending_usage = None
        try:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = (
                    line.decode("utf-8", "replace")
                    if isinstance(line, bytes) else line
                )
                if not chunk.startswith("data:"):
                    continue
                try:
                    event = json.loads(chunk[len("data:"):].strip())
                    candidate = event["candidates"][0]
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    raise RuntimeError(
                        f"Unexpected streamed image reply from Gemini ({model})"
                    ) from error
                if event.get("usageMetadata"):
                    pending_usage = event["usageMetadata"]
                if candidate.get("finishReason") == "MAX_TOKENS":
                    truncated = True
                content = candidate.get("content") or {}
                for part in content.get("parts") or []:
                    delta = part.get("text", "")
                    if delta:
                        received_text = True
                        yield delta
        finally:
            response.close()
        if pending_usage:
            from . import usage as _usage

            _usage.note(model, pending_usage, "gemini")
        if truncated:
            raise RuntimeError("reply was cut off — try a smaller image")
        if not received_text:
            raise RuntimeError(f"Gemini returned an empty image reply for {model}")
        return

    unit = "attempt" if attempts_used == 1 else "attempts"
    raise RuntimeError(
        f"Gemini image translation failed after {attempts_used} {unit}: {last}"
    )


def _parse(r, model: str) -> str:
    data = r.json()
    # Gemini reports counts as `usageMetadata` with its own key names; the
    # shared ledger reads that shape as readily as OpenAI's.
    if isinstance(data, dict) and data.get("usageMetadata"):
        from . import usage as _usage

        _usage.note(model, data["usageMetadata"], "gemini")
    try:
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
        out = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected reply from Gemini ({model})") from e
    _validate_finish_reason(candidate.get("finishReason"))
    if not out:
        raise RuntimeError(f"Gemini returned an empty reply for {model}")
    return out


def chat(
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    system_prompt: str,
) -> str:
    """Reply to a follow-up conversation using the selected Gemini model."""
    contents = [
        {
            "role": "model" if message["role"] == "assistant" else "user",
            "parts": [{"text": message["content"]}],
        }
        for message in messages
    ]
    response = requests.post(
        f"{API_ROOT}/models/{model}:generateContent",
        headers=_auth(api_key),
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return _parse(response, model)


def chat_stream(
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    system_prompt: str,
):
    """Yield conversational reply text as Gemini generates it."""
    url = f"{API_ROOT}/models/{model}:streamGenerateContent?alt=sse"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": [{"text": item["content"]}],
            }
            for item in messages
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
        },
    }
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(_backoff(attempt))
        response = None
        received_text = False
        try:
            response = requests.post(
                url,
                headers=_auth(api_key),
                json=payload,
                timeout=CHAT_TIMEOUT,
                stream=True,
            )
            if response.status_code in RETRY_STATUSES:
                if attempt + 1 == MAX_ATTEMPTS:
                    response.raise_for_status()
                response.close()
                response = None
                continue

            response.raise_for_status()
            finish_reason = None
            pending_usage = None
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = (
                    line.decode("utf-8", "replace")
                    if isinstance(line, bytes) else line
                )
                if not chunk.startswith("data:"):
                    continue
                try:
                    event = json.loads(chunk[len("data:"):].strip())
                    candidate = event["candidates"][0]
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    raise RuntimeError(
                        f"Unexpected streamed reply from Gemini ({model})"
                    ) from error
                if event.get("usageMetadata"):
                    pending_usage = event["usageMetadata"]
                if candidate.get("finishReason"):
                    finish_reason = candidate["finishReason"]
                content = candidate.get("content") or {}
                for part in content.get("parts") or []:
                    delta = part.get("text", "")
                    if delta:
                        received_text = True
                        yield delta
            if pending_usage:
                from . import usage as _usage

                _usage.note(model, pending_usage, "gemini")
            _validate_finish_reason(finish_reason)
            if not received_text:
                raise RuntimeError(
                    f"Gemini returned an empty reply for {model}")
            return
        except requests.exceptions.Timeout:
            if received_text or attempt + 1 == MAX_ATTEMPTS:
                raise
        finally:
            if response is not None:
                response.close()
