"""Generic OpenAI-compatible translator (OpenRouter / opencode / any free endpoint)."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
import json
import time
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from time import monotonic
from urllib.parse import urlsplit

import requests
from PIL import Image

from .prompt import output_token_budget, system_instructions, user_prompt
from .vision import encode_image, openai_image_messages

TIMEOUT = 20
TRANSLATION_TIMEOUT = 120
RATE_LIMIT_RETRIES = 2
MAX_RATE_LIMIT_WAIT = 30
RATE_LIMIT_BACKOFF = (2, 5)

# Gateways like FreeLLMAPI answer with this header naming the provider that
# actually served the call. It is what makes an "auto" model honest in the
# badge: the user picked a strategy, the gateway names the engine.
ROUTED_VIA_HEADER = "X-Routed-Via"
FALLBACK_ATTEMPTS_HEADER = "X-Fallback-Attempts"
FALLBACK_TRAIL_HEADER = "X-Fallback-Trail"
_QWEN3_VL_2B_IMAGE_OUTPUT_TOKENS = 8192


def _reasoning_parameters(model: str, enabled: bool, base_url: str) -> dict:
    """Build provider-specific thinking controls without breaking Gemma 2."""
    normalized_model = model.strip().casefold().split(":", 1)[0]
    if normalized_model == "gemma2":
        return {}
    try:
        is_ollama = urlsplit(base_url).port == 11434
    except ValueError:
        is_ollama = False
    if is_ollama:
        return {"think": enabled}
    if not enabled:
        # Silence reasoning explicitly. Omitting the field is not the same
        # thing: gateways fall back to the model's own default, which on
        # the open-weight vision models is thinking ON. Measured on a full
        # page through Kilo, "dots-studio/dots-3-note-preview:free" spent
        # 14,128 reasoning tokens, returned 0 characters, and took 52.6s;
        # with "none" it returned 1,548 characters in 11.6s.
        return {"reasoning_effort": "none"}
    return {
        "reasoning_effort": "medium",
        "extra_body": {"reasoning": {"enabled": True}},
    }


def _image_output_token_limit(model: str) -> int:
    """Give the small local Qwen vision model more room for page translation."""
    if model.strip().lower().startswith("qwen3-vl:2b"):
        return _QWEN3_VL_2B_IMAGE_OUTPUT_TOKENS
    return 4096


def routed_via(response) -> str:
    """The `platform/model` that served this reply, or "" when absent.

    Gateways percent-encode non-ASCII ids in the header, so it is unquoted
    before display. Anything unexpected returns "" rather than raising: a
    missing header costs us a badge detail, never a translation.
    """
    try:
        raw = (response.headers.get(ROUTED_VIA_HEADER) or "").strip()
    except AttributeError:
        return ""
    if not raw:
        return ""
    try:
        from urllib.parse import unquote
        return unquote(raw)
    except Exception:
        return raw


def fallback_note(response) -> str:
    """`fell over N times` detail, or "" when the router served it directly."""
    try:
        count = int(
            (response.headers.get(FALLBACK_ATTEMPTS_HEADER) or "0").strip())
    except (AttributeError, TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    trail = (response.headers.get(FALLBACK_TRAIL_HEADER) or "").strip()
    return f"after {count} failover{'s' if count > 1 else ''}" + (
        f" ({trail[:80]})" if trail else "")

# Ids that can never translate a passage: audio, guard, moderation.
_NON_TEXT = ("whisper", "guard", "safeguard", "moderation", "tts",
             "transcribe", "embedding", "image", "video")


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    return RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]


def _release(response) -> None:
    """Hand a rate-limited response's socket back before doing anything else.

    `requests` keeps the connection until the body is consumed or the response
    is closed. A 429 body is never read on the paths that lead to a raise, and
    on the retry path the next request is issued straight after the sleep, so
    every attempt would hold its socket for the whole backoff — during exactly
    the traffic spike the retry exists to survive.
    """
    try:
        response.close()
    except Exception:
        pass


def _post_with_rate_limit_retry(
    url: str, *, headers: dict, json: dict, timeout: int,
    give_up_on_gateway_exhaustion: bool = False,
) -> requests.Response:
    """POST, retrying 429 a couple of times with backoff.

    `give_up_on_gateway_exhaustion` is for gateways like FreeLLMAPI, which
    already fail over across every model it knows before answering. Its 429
    means "every route is blocked, and here is when the soonest one clears" —
    retrying the same request immediately cannot change that, it only adds
    seconds of waiting before the same answer. A plain provider (Groq, Mistral)
    does need the retries, because one key hitting its own limit may clear.
    """
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        response = requests.post(
            url, headers=headers, json=json, timeout=timeout)
        if response.status_code != 429:
            return response
        if give_up_on_gateway_exhaustion and _is_gateway_exhaustion(response):
            _release(response)
            return response

        delay = _retry_after_seconds(response, attempt)
        # Whatever happens next this attempt is over: give the socket back
        # before sleeping, and before either of the two ways out below.
        _release(response)
        if attempt >= RATE_LIMIT_RETRIES:
            raise RuntimeError(
                "The translation provider is still rate-limiting requests "
                f"(HTTP 429) after {attempt + 1} tries. Wait and try again, "
                "or check your provider quota."
            )
        if delay > MAX_RATE_LIMIT_WAIT:
            raise RuntimeError(
                "The translation provider is rate-limiting requests "
                f"(HTTP 429) and asks for a {delay:.0f}-second wait. "
                "Wait and try again, or check your provider quota."
            )
        time.sleep(delay)

    raise RuntimeError("Rate-limit retry loop ended unexpectedly.")


def _is_gateway_exhaustion(response) -> bool:
    """True when a 429 came from a gateway that already exhausted its routes.

    FreeLLMAPI answers `routing_exhausted` and, in the same message, says how
    long the soonest key is on cooldown. Retrying cannot outrun a cooldown the
    gateway itself just measured, so the caller must stop.
    """
    if response.headers.get(ROUTED_VIA_HEADER):
        return False  # something did answer; this is a per-key limit
    try:
        error = response.json().get("error", {})
    except ValueError:
        return False
    if str(error.get("code", "")).lower() == "routing_exhausted":
        return True
    return "models exhausted" in str(error.get("message", "")).lower()


def list_model_groups(
    base_url: str,
    api_key: str,
    timeout: int = TIMEOUT,
    explicitly_free_models: set[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Text-model ids grouped by category metadata returned by `/models`.

    Kilo Code tries base + /models then falls back to stripped/added /v1 variants;
    this does the same but without the old rstrip('/v1') character-set bug.
    """
    base = base_url.strip().rstrip("/")
    # Build candidates like Kilo Code: try exactly what the user typed first,
    # then the alternate with/without /v1 — never using str.rstrip('/v1').
    candidates: list[str] = [f"{base}/models"]
    if base.endswith("/v1"):
        without_v1 = base[:-3].rstrip("/")
        if without_v1:
            candidates.append(f"{without_v1}/models")
            candidates.append(f"{without_v1}/v1/models")
    else:
        candidates.append(f"{base}/v1/models")
    # Deduplicate preserving order
    seen: set[str] = set()
    urls: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    last_error: Exception | None = None
    last_status: str = ""
    for url in urls:
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if r.status_code == 404:
                last_error = RuntimeError(f"404 Not Found at {url}")
                last_status = f"{r.status_code} at {url}"
                continue
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError as je:
                raise RuntimeError(f"Invalid JSON from {url}: {je}") from je
            # OpenAI: {"data": [{"id": ...}]} ; some gateways: {"models": [...]} ; some return plain list
            entries = None
            if isinstance(data, dict):
                entries = data.get("data")
                if entries is None:
                    entries = data.get("models") or data.get("items") or data.get("models_data")
                if entries is None and any(
                    isinstance(value, list) for value in data.values()
                ):
                    entries = data
            elif isinstance(data, list):
                entries = data
            if isinstance(entries, dict):
                grouped_entries = [
                    (str(group).strip(), group_entries)
                    for group, group_entries in entries.items()
                    if isinstance(group_entries, list)
                ]
                if not grouped_entries:
                    raise RuntimeError(
                        f"Unexpected /models reply from {url}: "
                        f"{str(data)[:300]}")
            elif isinstance(entries, list):
                grouped_entries = [("", entries)]
            else:
                raise RuntimeError(f"Unexpected /models reply from {url}: {str(data)[:300]}")
            groups: list[tuple[str, list[str]]] = []
            entry_count = 0
            for default_group, group_entries in grouped_entries:
                for entry in group_entries:
                    entry_count += 1
                    if isinstance(entry, str):
                        model_id = entry.strip()
                        group = default_group
                        if (
                            explicitly_free_models is not None
                            and (
                                _group_is_free(group)
                                or model_id.casefold().endswith(
                                    ("-free", ":free", "/free")
                                )
                            )
                        ):
                            explicitly_free_models.add(model_id)
                    elif isinstance(entry, dict):
                        model_id = str(
                            entry.get(
                                "id",
                                entry.get("name", entry.get("model", "")),
                            )
                        ).strip()
                        group = default_group
                        if (
                            explicitly_free_models is not None
                            and (
                                _model_entry_is_free(entry, model_id)
                                or _group_is_free(default_group)
                            )
                        ):
                            explicitly_free_models.add(model_id)
                        if not group:
                            group = next(
                                (
                                    value.strip()
                                    for key in (
                                        "group",
                                        "category",
                                        "tier",
                                        "model_group",
                                        "group_name",
                                    )
                                    if isinstance(
                                        value := entry.get(key), str
                                    )
                                    and value.strip()
                                ),
                                "",
                            )
                    else:
                        continue
                    if not model_id:
                        continue
                    lowered = model_id.lower()
                    if any(bad in lowered for bad in _NON_TEXT):
                        continue
                    if not groups or groups[-1][0] != group:
                        groups.append((group, []))
                    groups[-1][1].append(model_id)
            if not any(group_models for _, group_models in groups):
                # Return raw list size hint so user knows endpoint did reply
                raise RuntimeError(f"No text models on this endpoint ({url} returned {entry_count} entries, all filtered)")
            return groups
        except requests.RequestException as e:
            body = ""
            try:
                if e.response is not None:
                    body = f" | body: {e.response.text[:300]}"
                    last_status = f"{e.response.status_code} at {url}{body}"
            except Exception:
                pass
            last_error = RuntimeError(f"Request failed for {url}: {e}{body}") if body else e
            continue
        except Exception as e:
            # Keep the most specific message — include URL
            if url not in str(e):
                last_error = RuntimeError(f"{e} (url: {url})")
                last_error.__cause__ = e
            else:
                last_error = e
            continue
    assert last_error is not None
    detail = f" Last tried: {last_status}" if last_status else ""
    raise RuntimeError(f"Could not list models: {last_error}.{detail}") from last_error


def list_models(base_url: str, api_key: str,
                timeout: int = TIMEOUT) -> list[str]:
    """Text-model ids from an OpenAI-compatible `/models` endpoint."""
    return [
        model
        for _, models in list_model_groups(base_url, api_key, timeout)
        for model in models
    ]


def free_model_groups(
    model_groups: list[tuple[str, list[str]]],
    explicitly_free_models: set[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Group models marked free by provider metadata, category, or model id."""
    explicitly_free_models = explicitly_free_models or set()
    free_groups = []
    for group, models in model_groups:
        free_models = [
            model for model in models if model in explicitly_free_models
        ]
        if free_models:
            free_groups.append((group or "Provider-marked free", free_models))
    return free_groups


def _model_entry_is_free(entry: dict, model_id: str = "") -> bool:
    pricing = entry.get("pricing")
    if isinstance(pricing, dict) and pricing:
        try:
            prices = [
                Decimal(str(value).strip())
                for value in pricing.values()
            ]
        except (InvalidOperation, TypeError, ValueError):
            prices = []
        if prices:
            return all(price == 0 for price in prices)

    for key in ("free", "is_free"):
        value = entry.get(key)
        if value is True or (
            isinstance(value, str) and value.strip().casefold() == "true"
        ):
            return True
        if value is False or (
            isinstance(value, str) and value.strip().casefold() == "false"
        ):
            return False
    for key in ("group", "category", "tier", "model_group", "group_name"):
        value = entry.get(key)
        if isinstance(value, str) and _group_is_free(value):
            return True
    if model_id.strip().casefold().endswith(("-free", ":free", "/free")):
        return True
    return False


def _group_is_free(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        "free" in normalized
        and not any(label in normalized for label in (
            "not free", "non-free", "paid",
        ))
    )


def probe_models(
    base_url: str,
    api_key: str,
    models: list[str],
    timeout: float = 25,
    max_workers: int = 4,
) -> list[tuple[str, bool, str]]:
    """Probe every model with a tiny compatible request and bounded concurrency."""
    unique_models = list(dict.fromkeys(
        model.strip() for model in models if model.strip()
    ))

    def probe(model: str) -> tuple[str, bool, str]:
        started = monotonic()
        response = None
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": "Reply only with OK."}
                ],
                "max_completion_tokens": 256,
            }
            payload.update(_reasoning_parameters(model, False, base_url))
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=timeout,
            )
            try:
                response.raise_for_status()
            except requests.HTTPError:
                error_text = ""
                try:
                    error_text = response.text.casefold()
                except (AttributeError, TypeError, ValueError):
                    pass
                if (
                    "max_completion_tokens" not in error_text
                    or not any(word in error_text for word in (
                        "unsupported", "unrecognized", "unknown", "not support",
                    ))
                ):
                    raise
                response.close()
                fallback_payload = dict(payload)
                fallback_payload.pop("max_completion_tokens")
                fallback_payload["max_tokens"] = 256
                response = requests.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=fallback_payload,
                    timeout=timeout,
                )
                response.raise_for_status()
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                detail = (
                    data.get("error", "No choices returned")
                    if isinstance(data, dict)
                    else "Unexpected response format"
                )
                return model, False, (
                    f"{monotonic() - started:.2f}s — {str(detail)[:140]}"
                )
            choice = choices[0]
            if not isinstance(choice, dict):
                return model, False, (
                    f"{monotonic() - started:.2f}s — Unexpected choice format"
                )
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            has_content = (
                isinstance(content, str) and bool(content.strip())
            ) or (
                isinstance(content, list)
                and any(
                    isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and part["text"].strip()
                    for part in content
                )
            )
            has_reasoning = isinstance(message, dict) and bool(
                message.get("reasoning_content")
            )
            has_refusal = isinstance(message, dict) and bool(
                message.get("refusal")
            )
            if not (has_content or has_reasoning or has_refusal):
                if choice.get("finish_reason") == "length":
                    return model, False, (
                        f"{monotonic() - started:.2f}s — TEMPORARY — "
                        "test output hit its token limit"
                    )
                return model, False, (
                    f"{monotonic() - started:.2f}s — Model returned no text"
                )
            return model, True, f"{monotonic() - started:.2f}s"
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            detail = str(exc) or type(exc).__name__
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if response is not None:
                try:
                    body = response.text.strip()
                    if body:
                        detail = f"{detail}: {body[:120]}"
                except (AttributeError, TypeError, ValueError):
                    pass
            if (
                status_code == 429
                or (isinstance(status_code, int) and status_code >= 500)
                or isinstance(
                    exc,
                    (requests.Timeout, requests.ConnectionError),
                )
            ):
                detail = f"TEMPORARY — {detail}"
            return model, False, (
                f"{monotonic() - started:.2f}s — {detail[:140]}"
            )
        finally:
            if response is not None:
                response.close()

    with ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, 4))
    ) as executor:
        return list(executor.map(probe, unique_models))


def list_ready_models(base_url: str, api_key: str,
                      timeout: int = TIMEOUT) -> list[str]:
    """Model ids a gateway can serve *right now*.

    A router's plain `/models` lists its whole catalog, including entries with
    no usable key behind them, so the picker would fill with models that cannot
    answer. FreeLLMAPI exposes `?execution_status=ready` for exactly this
    question; `ready` rows are also ranked, so the fastest usable models come
    first. Endpoints without that filter fall back to the full list.
    """
    base = base_url.rstrip("/")
    try:
        r = requests.get(
            f"{base}/models?execution_status=ready",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if r.status_code == 200:
            entries = r.json().get("data") or []
            ids = [
                str(e.get("id", "")).strip() for e in entries
                if isinstance(e, dict) and str(e.get("id", "")).strip()
            ]
            ids = [
                m for m in ids
                if not any(bad in m.lower() for bad in _NON_TEXT)
            ]
            if ids:
                return ids
    except (requests.RequestException, KeyError, TypeError, ValueError,
            AttributeError):
        pass
    return list_models(base_url, api_key, timeout)


def _messages_for(text: str, instructions: str, source_language: str,
                  target_language: str) -> list[dict]:
    return [
        {"role": "system", "content": system_instructions(
            instructions, source_language, target_language)},
        {"role": "user", "content": user_prompt(
            text, source_language, target_language)},
    ]


def translate_with_engine(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> tuple[str, str]:
    """`translate()` plus the engine that answered.

    The second element is the gateway's `X-Routed-Via` value (`platform/model`),
    or "" when the endpoint does not report one. Keeping it separate from the
    translation is what lets the caller name the real engine in the badge
    instead of echoing back the pseudo-model the user typed.

    `give_up_on_gateway_exhaustion` is on because this entry point only ever
    talks to the user's own router (see `chain` and `probe`): it has already
    failed over across every route it knows before answering, so a 429 here is
    the router's own exhaustion and retrying only delays the same answer.
    """
    base = base_url.rstrip("/")
    r = _post_with_rate_limit_retry(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0.3,
            "max_tokens": output_token_budget(text, minimum=4096),
            "messages": _messages_for(
                text, instructions, source_language, target_language),
        },
        timeout=TRANSLATION_TIMEOUT,
        give_up_on_gateway_exhaustion=True,
    )
    r.raise_for_status()
    return _reply_text(r, model), routing_badge(r)


def routing_badge(response) -> str:
    """`platform/model (after N failovers)`, or "" when nothing is reported.

    Separate from `routed_via` so the failover count — the part that explains a
    slow answer — reaches the badge too.
    """
    via = routed_via(response)
    note = fallback_note(response)
    if not via:
        return ""
    return f"{via} {note}".strip()


def _record_usage(raw, model: str, provider: str = "") -> dict:
    """Hand a provider's `usage` block to the shared token ledger.

    `model` is passed explicitly rather than remembered in module state:
    translations run on worker threads, so a module-level "last model" would
    attribute one thread's tokens to another's model.
    """
    from . import usage

    return usage.note(model, raw, provider)


def _reply_text(r, model: str = "") -> str:
    try:
        data = r.json()
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise RuntimeError("Unexpected reply from the OpenAI-compatible endpoint") from e
    if isinstance(data, dict) and data.get("usage"):
        _record_usage(data["usage"], model)
    if choice.get("finish_reason") == "length":
        raise RuntimeError(
            "OpenAI-compatible reply was cut off — try a shorter selection"
        )
    try:
        return choice["message"]["content"].strip()
    except (KeyError, TypeError, AttributeError) as e:
        raise RuntimeError("Unexpected reply from the OpenAI-compatible endpoint") from e


def _token_totals(usage: dict) -> dict:
    """Pull the counts out of a provider's `usage` block, tolerating variants.

    Endpoints disagree on which keys exist and on whether `total` is
    present, so each number falls back to the sum of its parts.
    """
    def count(*names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    prompt = count("prompt_tokens", "input_tokens")
    completion = count("completion_tokens", "output_tokens")
    total = count("total_tokens")
    if not total:
        total = prompt + completion
    reasoning = count("reasoning_tokens")
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = count("cached_tokens") or 0
        details = usage.get("completion_tokens_details")
    if not reasoning and isinstance(details, dict):
        reasoning = count("reasoning_tokens")
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "reasoning": reasoning,
        "cached": cached,
    }


def _post_stream(payload: dict, base: str, api_key: str, timeout: int):
    """POST a streaming request, retrying once without `stream_options`.

    `stream_options` is how the token counts are asked for, but not every
    OpenAI-compatible endpoint knows the field and some reject the whole
    request over it. Losing the count is acceptable; losing the translation
    is not, so the retry drops the field rather than failing.
    """
    def send(body: dict):
        return requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=timeout,
            stream=True,
        )

    response = send(payload)
    if "stream_options" not in payload or response.status_code == 200:
        return response
    error_text = ""
    try:
        error_text = response.text.casefold()
    except (AttributeError, TypeError, ValueError):
        pass
    if not any(word in error_text for word in (
        "stream_options", "unsupported", "unrecognized", "unknown",
        "not support", "invalid",
    )):
        return response
    response.close()
    retry = dict(payload)
    retry.pop("stream_options")
    return send(retry)


def stream_translate(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = TRANSLATION_TIMEOUT,
    report: dict | None = None,
    reasoning_enabled: bool = False,
) -> Iterator[str]:
    """Yield the translation as SSE deltas arrive.

    The popup renders each delta, so the first words appear in about a second
    instead of after the whole passage. Servers that reject `stream: true` (or
    ignore it) simply yield one delta containing the full reply, so this is safe
    against every OpenAI-compatible endpoint, not just the streaming ones.

    `report`, when given, is filled with {"routed_via", "fallback"} once the
    stream finishes. It exists because a generator cannot return a value: the
    caller needs the gateway's routing header for the badge, and asking for it
    with a second request would double the tokens and the latency of every
    translation.

    A stream the endpoint cut off (`finish_reason == "length"`) raises once it
    ends, exactly as the non-streaming `_reply_text` does for the same reply.
    The deltas are yielded as they arrive, so the popup has already painted the
    partial text; the raise is what tells it not to keep it.
    """
    base = base_url.rstrip("/")
    payload: dict = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": output_token_budget(text, minimum=4096),
        "stream": True,
        # Ask for the token counts the badge reports. Endpoints that do not
        # know this field reject it, and the retry below drops it.
        "stream_options": {"include_usage": True},
        "messages": _messages_for(
            text, instructions, source_language, target_language),
    }
    payload.update(_reasoning_parameters(model, reasoning_enabled, base))
    response = _post_stream(payload, base, api_key, timeout)
    if report is not None:
        report["routed_via"] = routing_badge(response)
    truncated = False
    pending_usage = None
    try:
        if response.status_code != 200:
            response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            chunk = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if not chunk.startswith("data:"):
                continue
            data = chunk[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            # The usage frame carries an empty `choices` list, so it has to be
            # read before the choice lookup below would throw it away. A page
            # is split into chunks and each is its own request, so the counts
            # are added up — the badge has to report the whole translation.
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                totals = _token_totals(usage)
                # Gateways that repeat the block on every frame would multiply
                # the total by the length of the reply; keep the latest and
                # record it once the stream ends.
                pending_usage = usage
                if report is not None:
                    running = report.get("tokens")
                    if isinstance(running, dict):
                        report["tokens"] = {
                            key: int(running.get(key, 0)) + int(totals.get(key, 0))
                            for key in totals
                        }
                    else:
                        report["tokens"] = totals
            try:
                choice = event["choices"][0]
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            if choice.get("finish_reason") == "length":
                truncated = True
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                yield delta
    finally:
        try:
            response.close()
        except Exception:
            pass
    if pending_usage:
        _record_usage(pending_usage, model)
    if truncated:
        raise RuntimeError(
            "OpenAI-compatible reply was cut off — try a shorter selection")


def translate(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    reasoning_enabled: bool = False,
) -> str:
    base = base_url.rstrip("/")
    payload: dict = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": output_token_budget(text, minimum=4096),
        "messages": [
            {"role": "system", "content": system_instructions(
                instructions, source_language, target_language)},
            {"role": "user", "content": user_prompt(
                text, source_language, target_language)},
        ],
    }
    payload.update(_reasoning_parameters(model, reasoning_enabled, base))
    r = _post_with_rate_limit_retry(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=TRANSLATION_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("usage"):
        _record_usage(data["usage"], model)
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("رد غير متوقع من الخادم المتوافق مع OpenAI") from e
    if choice.get("finish_reason") == "length":
        raise RuntimeError(
            "OpenAI-compatible reply was cut off — try a shorter selection"
        )
    try:
        return choice["message"]["content"].strip()
    except (KeyError, TypeError) as e:
        raise RuntimeError("رد غير متوقع من الخادم المتوافق مع OpenAI") from e


def translate_image(
    image: Image.Image,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    report: dict | None = None,
    reasoning_enabled: bool = False,
) -> str:
    payload: dict = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": _image_output_token_limit(model),
        "messages": openai_image_messages(
            encode_image(image), instructions,
            source_language, target_language),
    }
    payload.update(_reasoning_parameters(model, reasoning_enabled, base_url))
    response = _post_with_rate_limit_retry(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=TRANSLATION_TIMEOUT,
        give_up_on_gateway_exhaustion=True,
    )
    if report is not None:
        report["routed_via"] = routing_badge(response)
    response.raise_for_status()
    try:
        choice = response.json()["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(
            "Unexpected image-translation response from the OpenAI-compatible endpoint"
        ) from error
    if choice.get("finish_reason") == "length":
        raise RuntimeError("Image translation reply was cut off")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The selected model returned no image translation")
    return content.strip()


def stream_translate_image(
    image: Image.Image,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    report: dict | None = None,
    reasoning_enabled: bool = False,
) -> Iterator[str]:
    """Yield OpenAI-compatible vision response deltas as they arrive."""
    payload: dict = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": _image_output_token_limit(model),
        "stream": True,
        # Same ask as `stream_translate`: without it the endpoint reports no
        # counts and the badge cannot say what a page cost.
        "stream_options": {"include_usage": True},
        "messages": openai_image_messages(
            encode_image(image), instructions,
            source_language, target_language),
    }
    payload.update(_reasoning_parameters(model, reasoning_enabled, base_url))
    base = base_url.rstrip("/")
    response = _post_stream(payload, base, api_key, TRANSLATION_TIMEOUT)
    if report is not None:
        report["routed_via"] = routing_badge(response)

    received_text = False
    truncated = False
    pending_usage = None
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
                event = json.loads(data)
            except ValueError:
                continue
            # The usage frame carries an empty `choices` list, so read it
            # before the lookup below would skip it.
            block = event.get("usage") if isinstance(event, dict) else None
            if isinstance(block, dict):
                pending_usage = block
                if report is not None:
                    report["tokens"] = _token_totals(block)
            try:
                choice = event["choices"][0]
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            if choice.get("finish_reason") == "length":
                truncated = True
            delta = (choice.get("delta") or {}).get("content")
            # reasoning models may send reasoning_content separately — ignore it
            if isinstance(delta, list):
                delta = "".join(
                    part.get("text", "") for part in delta
                    if isinstance(part, dict))
            if isinstance(delta, str) and delta:
                received_text = True
                yield delta
    finally:
        response.close()
    if pending_usage:
        _record_usage(pending_usage, model)
    if truncated:
        if model.strip().lower().startswith("qwen3-vl:2b"):
            raise RuntimeError(
                "Qwen3-VL:2B image reply reached its output limit. Increase "
                "Ollama's context length to at least 16K in Ollama Settings, "
                "then retry with the same or a smaller page selection.")
        raise RuntimeError(
            "OpenAI-compatible image reply reached its output limit and was "
            "cut off; try a smaller image or check the provider's context limit")
    if not received_text:
        raise RuntimeError("The selected model returned no streamed image translation")


def chat(
    messages: list[dict[str, str]],
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    reasoning_enabled: bool = False,
) -> str:
    payload: dict = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": system_prompt},
            *messages,
        ],
    }
    payload.update(_reasoning_parameters(model, reasoning_enabled, base_url))
    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=TRANSLATION_TIMEOUT,
    )
    response.raise_for_status()
    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(
            "Unexpected chat response from the OpenAI-compatible endpoint"
        ) from error
    if isinstance(body, dict) and body.get("usage"):
        _record_usage(body["usage"], model)
    if choice.get("finish_reason") == "length":
        raise RuntimeError("The chat reply was cut off; ask a shorter question.")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The selected model returned an empty chat reply.")
    return content.strip()
