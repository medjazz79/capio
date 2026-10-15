"""OpenCode Zen translator: https://opencode.ai/zen/v1/chat/completions (OpenAI-compatible)."""
from __future__ import annotations

import json

import requests
from PIL import Image

from .prompt import output_token_budget, system_instructions, user_prompt
from .vision import encode_image, openai_image_messages

BASE = "https://opencode.ai/zen/v1"
TIMEOUT = 25

# Measured on 2026-10-07: `space-bunny-free` answers 200 with no Authorization
# header at all. Every other id in the Zen catalog that ends in `-free` answers
# 403 "free tier can only be used from within OpenCode" -- from plain HTTP and
# from inside the authenticated OpenCode CLI alike, so it is an account
# entitlement rather than something a proxy could route around. The one id is
# listed in `registry.ZEN_FREE`; keep a single copy of it.
#
# The catalog endpoint is no longer consulted. It still advertises ids that
# answer 403, so a live list is not a list of models that work, and offering a
# model that cannot answer costs the user a failed translation and a confusing
# badge.

# Reasoning models emit `reasoning_content` alongside `content`. Measured: at
# 2048 the model spends the whole budget on reasoning and replies with
# finish_reason="length" -- which surfaces as "reply was cut off" on exactly
# the fully-vocalised Arabic output this app asks for. 4096 measured reliably.
MIN_MAX_TOKENS = 4096


def translate(
    text: str,
    model: str,
    max_tokens: int = MIN_MAX_TOKENS,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    """Translate through OpenCode Zen. No API key: the free tier needs none."""
    r = requests.post(
        f"{BASE}/chat/completions",
        json={
            "model": model,
            "temperature": 0.3,
            "max_tokens": max(
                max_tokens, output_token_budget(text, minimum=MIN_MAX_TOKENS)),
            "messages": [
                {"role": "system", "content": system_instructions(
                    instructions, source_language, target_language)},
                {"role": "user", "content": user_prompt(
                    text, source_language, target_language)},
            ],
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("Unexpected reply from OpenCode Zen") from e
    if choice.get("finish_reason") == "length":
        raise RuntimeError("OpenCode Zen reply was cut off — try a shorter selection")
    out = (message.get("content") or "").strip()
    if not out:
        # Reasoning models can burn the whole budget before answering.
        out = (message.get("reasoning_content") or "").strip()
    if not out:
        raise RuntimeError(f"OpenCode Zen returned an empty reply for {model}")
    return out


def _stream_response(payload: dict, model: str):
    response = requests.post(
        f"{BASE}/chat/completions",
        json={**payload, "stream": True},
        timeout=TIMEOUT,
        stream=True,
    )
    received_text = False
    truncated = False
    try:
        if response.status_code != 200:
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
            data = chunk[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                choice = json.loads(data)["choices"][0]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise RuntimeError(
                    f"Unexpected streamed reply from OpenCode Zen ({model})"
                ) from error
            if choice.get("finish_reason") == "length":
                truncated = True
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content") or ""
            if isinstance(text, list):
                text = "".join(
                    part.get("text", "") for part in text
                    if isinstance(part, dict))
            if isinstance(text, str) and text:
                received_text = True
                yield text
        if truncated:
            raise RuntimeError(
                "OpenCode Zen reply was cut off — try a shorter selection")
        if not received_text:
            raise RuntimeError(f"OpenCode Zen returned an empty reply for {model}")
    finally:
        response.close()


def stream_translate(
    text: str,
    model: str,
    max_tokens: int = MIN_MAX_TOKENS,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
):
    """Yield OpenCode Zen text deltas."""
    yield from _stream_response({
        "model": model,
        "temperature": 0.3,
        "max_tokens": max(
            max_tokens, output_token_budget(text, minimum=MIN_MAX_TOKENS)),
        "messages": [
            {"role": "system", "content": system_instructions(
                instructions, source_language, target_language)},
            {"role": "user", "content": user_prompt(
                text, source_language, target_language)},
        ],
    }, model)


def translate_image(
    image: Image.Image,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    response = requests.post(
        f"{BASE}/chat/completions",
        json={
            "model": model,
            "temperature": 0.3,
            "max_tokens": MIN_MAX_TOKENS,
            "messages": openai_image_messages(
                encode_image(image), instructions,
                source_language, target_language),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(
            "Unexpected image-translation response from OpenCode Zen"
        ) from error
    if isinstance(body, dict) and body.get("usage"):
        from . import usage as _usage

        _usage.note(model, body["usage"], "zen")
    if choice.get("finish_reason") == "length":
        raise RuntimeError("OpenCode Zen image reply was cut off")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            f"OpenCode Zen returned no image translation for {model}")
    return content.strip()


def stream_translate_image(
    image: Image.Image,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
):
    """Yield OpenCode Zen vision deltas."""
    yield from _stream_response({
        "model": model,
        "temperature": 0.3,
        "max_tokens": MIN_MAX_TOKENS,
        "messages": openai_image_messages(
            encode_image(image), instructions,
            source_language, target_language),
    }, model)


def chat(
    messages: list[dict[str, str]],
    model: str,
    system_prompt: str,
) -> str:
    response = requests.post(
        f"{BASE}/chat/completions",
        json={
            "model": model,
            "temperature": 0.3,
            "max_tokens": 2048,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Unexpected OpenCode chat response") from error
    if isinstance(body, dict) and body.get("usage"):
        from . import usage as _usage

        _usage.note(model, body["usage"], "zen")
    if choice.get("finish_reason") == "length":
        raise RuntimeError("The chat reply was cut off; ask a shorter question.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"OpenCode returned an empty chat reply for {model}")
    return content.strip()
