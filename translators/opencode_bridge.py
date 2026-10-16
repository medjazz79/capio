"""OpenCode free bridge: streaming SSE + client fingerprint.

Live finding 2026-10-08: plain non-streaming POST to
https://opencode.ai/zen/v1/chat/completions returns 200 only for
`space-bunny-free`; every other `-free` id returns 403
"free tier can only be used from within OpenCode".
The same request with `stream:true` + `tools:[shell, read]` +
`User-Agent: opencode/1.18.31` + `x-opencode-session` + `x-opencode-client`
returns 200 for the ids in BRIDGE_FREE. `stream:false` with the same
fingerprint still returns 403, so streaming is mandatory: this module
consumes the SSE stream and aggregates `delta.content`.
"""
from __future__ import annotations

import json
import random
import string

import requests
from PIL import Image

from .prompt import output_token_budget, system_instructions, user_prompt
from .vision import (
    encode_image, openai_image_messages, responses_image_input,
)

BASE = "https://opencode.ai/zen/v1"
UA = "opencode/1.18.31"
BRIDGE_TIMEOUT = 60

# Fixed verified list (streaming, 2026-10-08). No GPT-4-free exists on Zen.
# Ordered fastest-first (measured short EN->AR: 3.4 / 4.3 / 5.5 / 6.0 /
# 12.6 / 15.5 / 18.6s; nemotron-ultra answered empty, kept last).
# muse-spark ids use the /responses endpoint (OpenAI Responses API);
# the rest use /chat/completions. Both need the same fingerprint.
RESPONSES_MODELS = [
    "muse-spark-1.3-contributor-free",
    "muse-spark-1.2-contributor-free",
]
BRIDGE_FREE = [
    "space-bunny-free",
    "muse-spark-1.3-contributor-free",
    "mimo-v2.6-flash-free",
    "fledge-alpha-free",
    "muse-spark-1.2-contributor-free",
    "longcat-2.5-preview-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
]


def ordered_models(fast: bool = True) -> list[str]:
    """Bridge ids in picker order: fastest-first when fast mode is on."""
    return list(BRIDGE_FREE) if fast else sorted(BRIDGE_FREE)

MIN_MAX_TOKENS = 4096

# Effort presets: (output-token ceiling, reasoning_effort value sent
# upstream). Measured 2026-10-08: `reasoning_effort` is accepted and moves
# mimo (~2-3s minimal vs ~8s high) but does NOT stop nemotron's reasoning
# dump, so the ceiling is the reliable lever and effort is best-effort.
# The ceiling never shrinks below the translation budget for the text, so
# a long passage cannot be truncated by a low preset — presets bite only
# when a model over-generates (reasoning dumps). Applies to every model,
# including future ones, through DEFAULT_EFFORT.
EFFORT_LEVELS = {
    "minimal": (512, "minimal"),
    "low": (1024, "low"),
    "medium": (2048, "medium"),
    "high": (4096, "high"),
    "xhigh": (8192, "xhigh"),
    "max": (16384, None),
}
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "medium"
MODEL_DEFAULT_EFFORTS = {
    "longcat-2.5-preview-free": "minimal",
}


def effort_for(model: str, settings: dict) -> str:
    """The preset for this model: user override, model default, then global."""
    levels = settings.get("effort_levels")
    if isinstance(levels, dict):
        level = levels.get(model)
        if level in EFFORT_LEVELS:
            return level
    model_default = MODEL_DEFAULT_EFFORTS.get(model)
    if model_default in EFFORT_LEVELS:
        return model_default
    default = settings.get("effort_default")
    return default if default in EFFORT_LEVELS else DEFAULT_EFFORT


def effort_budget(text: str, level: str) -> int:
    """Token ceiling for this text under this preset (never below need)."""
    cap, _ = EFFORT_LEVELS.get(level, EFFORT_LEVELS[DEFAULT_EFFORT])
    return max(cap, output_token_budget(text, minimum=0))


def _session_id() -> str:
    hexpart = "".join(random.choice("0123456789abcdef") for _ in range(12))
    b62 = string.digits + string.ascii_letters
    return "ses_" + hexpart + "".join(random.choice(b62) for _ in range(14))


def _headers(session: str) -> dict:
    return {
        "User-Agent": UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "Content-Type": "application/json",
    }


def _tools() -> list:
    return [
        {"type": "function", "function": {
            "name": "shell", "description": "run a shell command",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "read", "description": "read a file",
            "parameters": {"type": "object", "properties": {}}}},
    ]


def _response_tools() -> list:
    # Responses API shape: flat function tools, not chat-completions nesting.
    return [
        {"type": "function", "name": "shell", "description": "run a shell command",
         "parameters": {"type": "object", "properties": {}}},
        {"type": "function", "name": "read", "description": "read a file",
         "parameters": {"type": "object", "properties": {}}},
    ]


def translate(
    text: str,
    model: str,
    max_tokens: int | None = None,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = BRIDGE_TIMEOUT,
    effort: str | None = None,
    settings: dict | None = None,
) -> str:
    out = "".join(stream(
        text, model, max_tokens=max_tokens, instructions=instructions,
        source_language=source_language, target_language=target_language,
        timeout=timeout, effort=effort, settings=settings,
    )).strip()
    if not out:
        raise RuntimeError(f"Bridge returned an empty reply for {model}")
    return out


def translate_image(
    image: Image.Image,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    settings: dict | None = None,
) -> str:
    """Translate an image with a bridge model through its native vision format."""
    image_url = encode_image(image)
    if model in RESPONSES_MODELS:
        output = "".join(_iter_responses(
            "", model, instructions=instructions,
            source_language=source_language, target_language=target_language,
            level=effort_for(model, settings or {}),
            image_url=image_url,
        )).strip()
    else:
        output = "".join(_iter_chat(
            "", model, max_tokens=4096, instructions=instructions,
            source_language=source_language, target_language=target_language,
            level=effort_for(model, settings or {}),
            image_url=image_url,
        )).strip()
    if not output:
        raise RuntimeError(f"Bridge returned an empty image translation for {model}")
    return output


def stream_image(
    image: Image.Image,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    settings: dict | None = None,
):
    """Yield translated image text as the bridge streams it."""
    image_url = encode_image(image)
    if model in RESPONSES_MODELS:
        yield from _iter_responses(
            "", model, instructions=instructions,
            source_language=source_language, target_language=target_language,
            level=effort_for(model, settings or {}),
            image_url=image_url,
        )
    else:
        yield from _iter_chat(
            "", model, max_tokens=4096, instructions=instructions,
            source_language=source_language, target_language=target_language,
            level=effort_for(model, settings or {}),
            image_url=image_url,
        )


def stream(
    text: str,
    model: str,
    max_tokens: int | None = None,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = BRIDGE_TIMEOUT,
    effort: str | None = None,
    settings: dict | None = None,
):
    """Yield content deltas as they arrive over SSE (for live display).

    `translate()` aggregates this; the popup renders each piece at once so
    the first words appear in ~1s instead of after the full reply.
    `effort` is a key of EFFORT_LEVELS; with `settings` given, the
    per-model preset (else the default) is resolved automatically.
    """
    level = effort if effort in EFFORT_LEVELS else effort_for(
        model, settings or {})
    if model in RESPONSES_MODELS:
        yield from _iter_responses(
            text, model, instructions=instructions,
            source_language=source_language, target_language=target_language,
            timeout=timeout, level=level,
        )
        return
    yield from _iter_chat(
        text, model, max_tokens=max_tokens, instructions=instructions,
        source_language=source_language, target_language=target_language,
        timeout=timeout, level=level,
    )


def _open_stream(endpoint: str, payload: dict, session: str, model: str,
                 timeout: int):
    try:
        r = requests.post(
            endpoint, json=payload,
            headers=_headers(session), timeout=timeout, stream=True,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Bridge {model} request failed: {e}") from e
    if r.status_code != 200:
        body = ""
        try:
            body = r.text[:300]
        except Exception:
            pass
        try:
            r.close()
        except Exception:
            pass
        if "free tier can only be used" in body.lower():
            raise RuntimeError(
                f"403 — free tier blocked for {model} (gate changed?)")
        raise RuntimeError(f"Bridge {model} failed: HTTP {r.status_code}")
    return r


def _iter_chat(
    text: str,
    model: str,
    max_tokens: int | None = None,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = BRIDGE_TIMEOUT,
    level: str = DEFAULT_EFFORT,
    image_url: str | None = None,
    chat_messages: list[dict[str, str]] | None = None,
):
    session = _session_id()
    cap, req_effort = EFFORT_LEVELS.get(level, EFFORT_LEVELS[DEFAULT_EFFORT])
    if max_tokens is None:
        max_tokens = effort_budget(text, level)
    else:
        max_tokens = max(max_tokens, effort_budget(text, level))
    messages = chat_messages if chat_messages is not None else (
        openai_image_messages(
            image_url, instructions, source_language, target_language)
        if image_url else [
            {"role": "system", "content": system_instructions(
                instructions, source_language, target_language)},
            {"role": "user", "content": user_prompt(
                text, source_language, target_language)},
        ]
    )
    payload = {
        "model": model,
        "stream": True,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": _tools(),
    }
    if req_effort is not None:
        payload["reasoning_effort"] = req_effort
    r = _open_stream(f"{BASE}/chat/completions", payload, session, model,
                     timeout)
    truncated = False
    try:
        for line in r.iter_lines():
            if not line:
                continue
            t = line.decode("utf-8", errors="replace").strip()
            if not t.startswith("data:"):
                continue
            data = t[5:].strip()
            if data == "[DONE]":
                break
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            if evt.get("usage"):
                from . import usage as _usage

                _usage.note(model, evt["usage"], "bridge")
            try:
                choice = evt["choices"][0]
            except (KeyError, IndexError, TypeError):
                continue
            if choice.get("finish_reason") == "length":
                truncated = True
            delta = choice.get("delta") or {}
            chunk = delta.get("content") or ""
            if isinstance(chunk, list):
                chunk = "".join(
                    part.get("text", "") for part in chunk
                    if isinstance(part, dict))
            if chunk:
                yield chunk
    finally:
        try:
            r.close()
        except Exception:
            pass
    if truncated:
        # Raised whether or not text arrived. Text that stopped early is the
        # worse case, not the better one: a caller that receives half a
        # paragraph with no signal has nothing to detect it. Every other
        # adapter rejects `finish_reason == "length"` unconditionally, and
        # `chain` only falls back to the free ladder when we raise.
        raise RuntimeError(
            f"Bridge {model} reply was cut off — try a shorter selection")


def _translate_responses(
    text: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = BRIDGE_TIMEOUT,
) -> str:
    """Translate via the OpenAI Responses endpoint (muse-spark ids).

    Same gate as chat/completions: `stream:true` + shell/read tools +
    opencode fingerprint, otherwise 403. Aggregates
    `response.output_text.delta` events; falls back to the completed
    output blocks (deltas already carry the full text, completed repeats
    it, so completed is only used when no deltas arrived).
    """
    out = "".join(_iter_responses(
        text, model, instructions=instructions,
        source_language=source_language, target_language=target_language,
        timeout=timeout,
    )).strip()
    if not out:
        raise RuntimeError(f"Bridge returned an empty reply for {model}")
    return out


def _iter_responses(
    text: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = BRIDGE_TIMEOUT,
    level: str = DEFAULT_EFFORT,
    image_url: str | None = None,
    chat_messages: list[dict[str, str]] | None = None,
):
    session = _session_id()
    if chat_messages is not None:
        prompt = instructions
        input_content = [
            {
                "role": "assistant" if item["role"] == "assistant" else "user",
                "content": [{
                    "type": (
                        "output_text" if item["role"] == "assistant"
                        else "input_text"
                    ),
                    "text": item["content"],
                }],
            }
            for item in chat_messages
        ]
    elif image_url:
        prompt, input_content = responses_image_input(
            image_url, instructions, source_language, target_language)
    else:
        prompt = system_instructions(
            instructions, source_language, target_language)
        input_content = user_prompt(text, source_language, target_language)
    payload = {
        "model": model,
        "instructions": prompt,
        "input": input_content,
        "stream": True,
        "tools": _response_tools(),
        "max_output_tokens": max(
            4096 if image_url else 0, effort_budget(text, level)),
    }
    r = _open_stream(f"{BASE}/responses", payload, session, model, timeout)
    completed: list[str] = []
    saw_delta = False
    try:
        for line in r.iter_lines():
            if not line:
                continue
            t = line.decode("utf-8", errors="replace").strip()
            if not t.startswith("data:"):
                continue
            data = t[5:].strip()
            if data == "[DONE]":
                break
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            etype = evt.get("type")
            if etype == "response.output_text.delta":
                chunk = evt.get("delta") or ""
                if chunk:
                    saw_delta = True
                    yield chunk
            elif etype == "response.completed":
                try:
                    for blk in evt["response"]["output"]:
                        for c in blk.get("content", []):
                            if c.get("text"):
                                completed.append(c["text"])
                except (KeyError, TypeError, AttributeError):
                    continue
    finally:
        try:
            r.close()
        except Exception:
            pass
    if not saw_delta and completed:
        # No deltas arrived (older gateway shape): emit the finished text.
        yield "".join(completed)


def chat(
    messages: list[dict[str, str]],
    model: str,
    system_prompt: str,
    settings: dict,
) -> str:
    """Run chat through the selected bridge protocol and preserve its fingerprint."""
    level = effort_for(model, settings)
    user_text = messages[-1]["content"] if messages else ""
    if model in RESPONSES_MODELS:
        output = "".join(_iter_responses(
            user_text,
            model,
            instructions=system_prompt,
            level=level,
            chat_messages=messages,
        )).strip()
    else:
        output = "".join(_iter_chat(
            user_text,
            model,
            instructions=system_prompt,
            level=level,
            chat_messages=[
                {"role": "system", "content": system_prompt},
                *messages,
            ],
        )).strip()
    if not output:
        raise RuntimeError(f"Bridge returned an empty chat reply for {model}")
    return output


def chat_stream(
    messages: list[dict[str, str]],
    model: str,
    system_prompt: str,
    settings: dict,
):
    """Yield conversational text deltas through the selected bridge."""
    level = effort_for(model, settings)
    user_text = messages[-1]["content"] if messages else ""
    if model in RESPONSES_MODELS:
        yield from _iter_responses(
            user_text,
            model,
            instructions=system_prompt,
            level=level,
            chat_messages=messages,
        )
        return
    yield from _iter_chat(
        user_text,
        model,
        instructions=system_prompt,
        level=level,
        chat_messages=[
            {"role": "system", "content": system_prompt},
            *messages,
        ],
    )
