"""One-shot engine diagnostics: does the chosen model answer right now?

Answers the only question that matters in the settings screen — "if I press
Translate right now, what will run?" — using the same routing rules as
`chain.translate`, so the test can never disagree with reality.

Deliberately quick: one tiny "OK" probe against the chosen model only
(~one round-trip), not a full sample translation per engine.
"""
from __future__ import annotations

import re
import time

from . import registry

# One probe definition for the whole app, imported from `translators.verify`
# (which imports nothing at module level, so this stays a leaf import). Two
# copies of the prompt and the timeout let the Settings screen and the picker
# verdict disagree about the same model; `verify.PROBE_TIMEOUT` is the stricter
# of the two and is the one kept.
from .verify import PROBE_TIMEOUT
from .verify import PROMPT as PROBE_TEXT


def probe_all(settings: dict) -> list[tuple[str, bool, str]]:
    """Returns [(engine_name, ok, detail)]. Slow engines run with their own timeouts."""
    from . import (
        fallback,
        gemini,
        opencode_bridge,
        opencode_cli,
        opencode_compat,
        opencode_zen,
    )

    results: list[tuple[str, bool, str]] = []

    def run(name: str, fn) -> None:
        t0 = time.time()
        try:
            out = fn(PROBE_TEXT)
            results.append((name, True, f"OK in {time.time() - t0:.1f}s — {out[:70]}"))
        except Exception as e:
            results.append((name, False, f"FAILED: {str(e)[:160]}"))

    s = settings
    from config.settings import resolve_model
    model = resolve_model(s)
    provider = registry.provider_of(model)

    # The chosen model: tested exactly as the chain will call it, but with
    # a one-word probe instead of a full passage.
    if provider == "gemini":
        if not (s.get("gemini_key") or "").strip():
            results.append((f"★ Your model ({model})", False,
                            "SKIPPED: no Gemini key — free at aistudio.google.com/apikey"))
        else:
            run(f"★ Your model ({model})",
                lambda t: gemini.translate(t, s["gemini_key"], model))
    elif provider == "google_translate":
        run(f"★ Your model ({model})",
            lambda t: fallback.gtx_translate(t, "en", "en"))
    elif provider == "bridge":
        run(f"★ Your model ({model})",
            lambda t: opencode_bridge.translate(
                t, model, max_tokens=30, timeout=PROBE_TIMEOUT))
    elif provider == "opencode":
        run(f"★ Your model ({model})",
            lambda t: opencode_zen.translate(t, model))
    elif provider == "freellm":
        base, key = registry.provider_creds("freellm", s)
        bare = model[len(registry.FLM_PREFIX):]
        if not key:
            results.append((f"★ Your model ({bare})", False,
                            "SKIPPED: no FreeLLMAPI unified key — copy it from "
                            f"the Keys page of {base}"))
        else:
            def _flm_probe(t):
                _, via = opencode_compat.translate_with_engine(
                    t, base, key, bare)
                return via or bare
            run(f"★ Your model ({bare})", _flm_probe)
    elif provider in ("groq", "cf", "mistral"):
        base, key = registry.provider_creds(provider, s)
        bare = model[len({"groq": registry.GROQ_PREFIX,
                          "cf": registry.CF_PREFIX,
                          "mistral": registry.MISTRAL_PREFIX}[provider]):]
        if not (base and key):
            results.append((f"★ Your model ({bare})", False,
                            "SKIPPED: provider token is not configured"))
        else:
            run(f"★ Your model ({bare})",
                lambda t: opencode_compat.translate(t, base, key, bare))
    elif provider == "custom_provider":
        cfg = registry.custom_provider_config(model, s)
        if cfg is None:
            results.append((f"★ Your model ({model})", False,
                            "SKIPPED: custom provider not found — re-add it in Settings → Custom Providers"))
        else:
            base, key, provider_model = cfg
            if not base or not key or not provider_model:
                results.append((f"★ Your model ({model})", False,
                                "SKIPPED: custom provider incomplete — fill Base URL, API Key and Model"))
            else:
                label = registry.custom_provider_label(model, s)
                entry = registry.get_custom_provider(s, model)
                reasoning = bool(entry.get("reasoning_enabled")) if entry else False
                run(f"★ Your model ({label})",
                    lambda t: opencode_compat.translate(t, base, key, provider_model, reasoning_enabled=reasoning))
    elif provider == "opencode_cli":
        cli_model = model[len(registry.OPENCODE_CLI_PREFIX):]
        run(f"★ Your model ({cli_model})",
            lambda t: opencode_cli.translate(t, cli_model,
                                             timeout=PROBE_TIMEOUT))
    else:
        base, key = (s.get("openai_base") or "").strip(), (s.get("openai_key") or "").strip()
        if not (base and key):
            results.append((f"★ Your model ({model})", False,
                            "SKIPPED: OpenAI-compatible is not configured"))
        else:
            run(f"★ Your model ({model})",
                lambda t: opencode_compat.translate(t, base, key, model))

    # Hints: mismatches that produce a confusing result later.
    if provider not in ("gemini", "google_translate") \
            and (s.get("gemini_key") or "").strip():
        results.append(("💡 Hint", True,
                        f"You have a Gemini key but chose {model}. The chain "
                        f"never switches to it on its own: if {model} fails, "
                        "the free translation services answer (Google "
                        "Translate, then MyMemory). To use your Gemini key, "
                        "pick a Gemini "
                        "model in Settings → Translation."))
    elif provider == "gemini" and not (s.get("gemini_key") or "").strip():
        results.append(("💡 Hint", True,
                        f"{model} needs a Gemini key: aistudio.google.com/apikey"))
    elif provider == "opencode":
        results.append(("💡 Hint", True,
                        f"{model} is the one free model that needs no API key. "
                        "The other '-free' ids answer 403 even from inside "
                        "OpenCode, so they are not offered."))
    elif provider == "bridge":
        results.append(("💡 Hint", True,
                        f"{model} uses the streaming bridge (no key). "
                        "If the gate changes, it falls back to free services."))
    elif provider == "freellm":
        results.append(("💡 Hint", True,
                        f"{model} lets your local FreeLLMAPI router pick. "
                        "It fails over to another free tier on its own, so a "
                        "rate-limited upstream does not surface here. The "
                        "badge names the engine that actually answered."))
    elif provider == "custom_provider":
        label = registry.custom_provider_label(model, s)
        cfg = registry.custom_provider_config(model, s)
        base_info = f" via {cfg[0]}" if cfg and cfg[0] else ""
        entry = registry.get_custom_provider(s, model)
        caps = []
        if entry and entry.get("reasoning_enabled"):
            caps.append("reasoning")
        if entry and entry.get("vision_enabled"):
            caps.append("vision")
        cap_info = f" [{', '.join(caps)}]" if caps else ""
        results.append(("💡 Hint", True,
                        f"{label}{base_info}{cap_info} — your custom OpenAI-compatible endpoint. "
                        "Uses /chat/completions like Kilo Code providers. "
                        "Streaming available if the endpoint supports it."))
    return results


def detect_vision_all(base_url: str, api_key: str, models: list[str],
                      on_result=None) -> dict[str, tuple[bool, str]]:
    """Test a whole model list, one worker per model, bounded concurrency.

    A gateway's catalog carries no capability information, so this is the only
    way to know which of its models can read an image. Results are returned
    as they finish through `on_result` so the caller can show progress
    instead of waiting for the slowest model.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    unique = list(dict.fromkeys(
        model.strip() for model in models if model and model.strip()))
    if not unique:
        return {}
    results: dict[str, tuple[bool, str]] = {}
    workers = min(4, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(detect_vision, base_url, api_key, model): model
            for model in unique
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                outcome = future.result()
            except Exception as error:
                outcome = (False, f"The test failed: {error}")
            results[model] = outcome
            if on_result is not None:
                on_result(model, outcome)
    return results


def format_results(results: list[tuple[str, bool, str]]) -> str:
    """Render only the selected model's result and measured response time."""
    for name, ok, detail in results:
        if name.startswith("★ Your model (") and name.endswith(")"):
            model = name[len("★ Your model ("):-1]
            if not ok:
                return f"❌ {model} — Not OK"
            duration = re.search(r"\bOK in ([\d.]+)s\b", detail)
            if duration is None:
                return f"✅ {model} — OK"
            return f"✅ {model} — OK — {duration.group(1)}s"
    return "❌ Model test — Not tested"


# --- Vision detection -------------------------------------------------------
# A custom provider's capabilities are whatever its endpoint actually does.
# The Vision checkbox is a claim the user makes; this measures it, so the
# label on the model says what the endpoint did rather than what was hoped.

_VISION_PROBE_COLOR = (255, 0, 255)  # magenta: not a colour a text model guesses
_VISION_PROBE_NAMES = ("magenta", "fuchsia", "purple", "pink", "وردي", "أرجواني")


def detect_vision(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Ask a model to name a solid magenta swatch.

    Returns (supported, detail). A text-only model either refuses the image
    field or answers blind, and a blind answer is rejected here because it
    does not name the colour. Reasoning is switched off explicitly: these
    endpoints default to thinking, and a thinking model burns its whole
    budget before answering anything.
    """
    import base64
    from io import BytesIO

    import requests
    from PIL import Image

    from .opencode_compat import TRANSLATION_TIMEOUT

    image = Image.new("RGB", (64, 64), _VISION_PROBE_COLOR)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(
        buffer.getvalue()).decode("ascii")

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 64,
        "reasoning_effort": "none",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "What single colour fills this image? "
                    "Answer with the colour name only.")},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    try:
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers=headers, json=payload,
            timeout=TRANSLATION_TIMEOUT,
        )
    except Exception as error:  # network, TLS, DNS — anything that is not a verdict
        return False, f"Could not reach the endpoint: {error}"

    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message", ""))
        except Exception:
            pass
        detail = detail or f"HTTP {response.status_code}"
        return False, f"The endpoint refused the image: {detail}"

    try:
        message = response.json()["choices"][0]["message"]
    except Exception:
        return False, "The endpoint replied without a readable message."
    answer = (message.get("content") or "").strip().lower()
    if not answer:
        return False, "The model returned no answer to the image."
    named = [name for name in _VISION_PROBE_NAMES if name in answer]
    if named:
        return True, f"The model named the colour ({named[0]})."
    return False, (
        "The model answered blind — it replied without looking at the "
        "image, so treat it as text-only."
    )


# --- Reasoning detection ---------------------------------------------------
# Same contract as `detect_vision`: send exactly what the Reasoning toggle
# sends, and report what the endpoint did with it.

_REASONING_PROBE_QUESTION = (
    "A shop sells pens at 3 for 11. How much do 7 pens cost? "
    "Answer with the number only."
)
_REASONING_TRACE_KEYS = ("reasoning_content", "reasoning", "thinking")


def detect_reasoning(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Send the Reasoning toggle's own payload and report what came back.

    Returns (supported, detail). A "true" verdict means the endpoint accepted
    the parameters and the model produced a reasoning trace — the only
    outcome where ticking the box changes anything.
    """
    import requests

    from .opencode_compat import TRANSLATION_TIMEOUT, _reasoning_parameters

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 256,
        "messages": [{
            "role": "user",
            "content": _REASONING_PROBE_QUESTION,
        }],
    }
    # Exactly what the enabled checkbox sends, so a pass here is a pass in
    # the real translation path too.
    payload.update(_reasoning_parameters(model, True, base_url))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    try:
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers=headers, json=payload,
            timeout=TRANSLATION_TIMEOUT,
        )
    except Exception as error:
        return False, f"Could not reach the endpoint: {error}"

    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message", ""))
        except Exception:
            pass
        detail = detail or f"HTTP {response.status_code}"
        return False, (
            "The endpoint rejected the reasoning parameters, so sending them "
            f"would break every request: {detail}"
        )

    try:
        message = response.json()["choices"][0]["message"]
    except Exception:
        return False, "The endpoint replied without a readable message."

    trace = ""
    for key in _REASONING_TRACE_KEYS:
        value = message.get(key)
        if value:
            trace = str(value)
            break
    if trace.strip():
        return True, (
            f"The endpoint accepted the parameters and the model returned a "
            f"reasoning trace ({len(trace)} characters)."
        )
    if not (message.get("content") or "").strip():
        return False, "The model returned neither an answer nor a trace."
    return False, (
        "The endpoint accepted the parameters but the model returned no "
        "reasoning trace, so the box would have no effect here."
    )
