"""Provider catalogue and routing rules for configured API services.

The model picker is credential-driven: a service contributes models only after
its connection details are complete. Custom providers use the OpenAI-compatible
protocol, so users can add any number of services without new fixed key fields.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CUSTOM = "custom"
CUSTOM_PROVIDER_PREFIX = "custom:"
_CUSTOM_MODEL_SEPARATOR = "::"
GOOGLE_TRANSLATE_MODEL = "google-translate"

GROQ_PREFIX = "groq:"
CF_PREFIX = "cf:"
MISTRAL_PREFIX = "mistral:"
FLM_PREFIX = "flm:"

GROQ_BASE = "https://api.groq.com/openai/v1"
MISTRAL_BASE = "https://api.mistral.ai/v1"
FLM_DEFAULT_BASE = "http://localhost:3001/v1"
_CLOUDFLARE_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)

GEMINI_HDR = "── ✦ Gemini ──"
GROQ_HDR = "── ⚡ Groq ──"
CF_HDR = "── ☁ Cloudflare Workers AI ──"
MISTRAL_HDR = "── 🌀 Mistral ──"
FLM_HDR = "── 🔀 FreeLLMAPI ──"
CUSTOM_HDR = "── 🔧 Your API providers ──"

# Built-in router strategies, not a provider catalogue. Live model names come
# from the configured endpoint and are stored in `provider_models`.
FLM_ROUTER_MODELS = [
    "auto", "auto:fast", "auto:smart", "auto:reliable", "auto:balanced",
    "fusion",
]

_FLM_CONFIG_PATHS = [
    Path.home() / "AppData" / "Roaming" / "freellmapi" / "config.json",
    Path.home() / ".freellmapi" / "config.json",
]

FLM_MEASURED_VISION = [
    "gemma-4-26b-a4b-it", "gemma-4-26b-it", "gemini-3.1-flash-lite",
    "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.8-flash",
]
FLM_MEASURED_TEXT_ONLY = frozenset({
    "llava-1.5-7b", "llama-4-scout", "llama-4-maverick",
    "moondream-3.1-9b-a2b", "gemma-2b-it-lora", "gemma-7b-it-lora",
    "llama-3.2-11b-vision", "llama-3.2-90b-vision", "aya-vision-32b",
    "command-a-vision", "glm-4.6v-flash", "qwen2.5-vl-72b",
})

_IMAGE_SUPPORTED_MODELS = frozenset({
    "groq:qwen/qwen3.8-27b",
    "cf:@cf/meta/llama-3.2-11b-vision-instruct",
    "cf:@cf/mistralai/mistral-small-3.1-24b-instruct",
    "mistral:mistral-small-latest", "mistral:mistral-large-latest",
    "mistral:pixtral-12b-2409",
})
_TEXT_ONLY_MODELS = frozenset({
    "groq:openai/gpt-oss-20b", "groq:openai/gpt-oss-120b",
    "groq:allam-2-7b", "cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf:@cf/meta/llama-3.1-8b-instruct",
    "cf:@cf/qwen/qwen2.5-coder-32b-instruct", "mistral:codestral-latest",
    "mistral:open-mistral-nemo",
})
_GEMINI_TEXT_OR_AUDIO_ONLY = frozenset({
    "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts",
    "gemini-3.5-live-translate-preview",
})


def detect_flm_base() -> str:
    """Read the local router's configured port without raising."""
    for path in _FLM_CONFIG_PATHS:
        try:
            port = int(json.loads(path.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if 1 <= port <= 65535:
            return f"http://localhost:{port}/v1"
    return FLM_DEFAULT_BASE


def _custom_parts(model_or_id: str) -> tuple[str, str]:
    raw = (model_or_id or "").strip()
    if raw.startswith(CUSTOM_PROVIDER_PREFIX):
        raw = raw[len(CUSTOM_PROVIDER_PREFIX):]
    provider_id, separator, model = raw.partition(_CUSTOM_MODEL_SEPARATOR)
    return provider_id.strip(), model.strip() if separator else ""


def custom_model_id(provider_id: str, model: str) -> str:
    return f"{CUSTOM_PROVIDER_PREFIX}{provider_id}{_CUSTOM_MODEL_SEPARATOR}{model}"


def _find_custom_provider(settings: dict, model_or_id: str) -> dict | None:
    provider_id, _ = _custom_parts(model_or_id)
    if not provider_id:
        return None
    providers = settings.get("custom_providers") if isinstance(settings, dict) else None
    if not isinstance(providers, list):
        return None
    for entry in providers:
        if isinstance(entry, dict) and str(entry.get("id", "")).strip() == provider_id:
            return entry
    return None


def get_custom_provider(settings: dict, provider_id: str) -> dict | None:
    return _find_custom_provider(settings, provider_id)


def custom_provider_base_url(entry: dict) -> str:
    """Resolve Cloudflare Account IDs entered as a URL host to its API URL."""
    base_url = str(entry.get("base_url", "")).strip().rstrip("/")
    provider_name = str(entry.get("name", "")).strip().casefold()
    if "cloudflare" not in provider_name:
        return base_url
    if "<account_id>" in base_url.casefold():
        return ""

    account_id = ""
    if _CLOUDFLARE_ACCOUNT_ID.fullmatch(base_url):
        account_id = base_url
    else:
        candidate = (
            base_url if "://" in base_url else f"https://{base_url}"
        )
        parsed = urlsplit(candidate)
        host = parsed.hostname or ""
        if (
            _CLOUDFLARE_ACCOUNT_ID.fullmatch(host)
            and parsed.path in ("", "/", "/v1")
            and not parsed.query
            and not parsed.fragment
        ):
            account_id = host
    if account_id:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/v1"
        )
    return base_url


def custom_provider_config(model: str, settings: dict) -> tuple[str, str, str] | None:
    """Return endpoint, key, and exact selected custom-provider model."""
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return None
    _, selected_model = _custom_parts(model)
    provider_model = selected_model or str(entry.get("model", "")).strip()
    return (
        custom_provider_base_url(entry),
        str(entry.get("api_key", "")).strip(),
        provider_model,
    )


def custom_provider_label(model: str, settings: dict) -> str:
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return model
    _, selected_model = _custom_parts(model)
    name = str(entry.get("name", "")).strip() or str(entry.get("id", ""))
    provider_model = selected_model or str(entry.get("model", "")).strip()
    return f"{name} ({provider_model})" if provider_model else name


def custom_provider_models(entry: dict) -> list[str]:
    """Return the one model currently selected for this provider."""
    selected_model = str(entry.get("model", "")).strip()
    return [selected_model] if selected_model else []


def provider_of(model: str) -> str:
    m = (model or "").strip()
    if m.casefold() == GOOGLE_TRANSLATE_MODEL:
        return "google_translate"
    if m.startswith(CUSTOM_PROVIDER_PREFIX):
        return "custom_provider"
    if m.startswith(FLM_PREFIX):
        return "freellm"
    if m.startswith(GROQ_PREFIX):
        return "groq"
    if m.startswith(CF_PREFIX):
        return "cf"
    if m.startswith(MISTRAL_PREFIX):
        return "mistral"
    if m.lower().startswith("gemini"):
        return "gemini"
    return CUSTOM


def provider_creds(provider: str, settings: dict) -> tuple[str, str]:
    """Return endpoint and key for a built-in OpenAI-compatible service."""
    shared_base = (settings.get("openai_base") or "").strip()
    shared_key = (settings.get("openai_key") or "").strip()
    if provider == "freellm":
        base = (settings.get("flm_base") or "").strip()
        key = (settings.get("flm_key") or "").strip()
        if base and key:
            return base, key
        if "localhost" in shared_base and shared_key:
            return shared_base, shared_key
        return base or detect_flm_base(), key
    if provider == "groq":
        key = (settings.get("groq_key") or "").strip()
        if key:
            return GROQ_BASE, key
        return (shared_base, shared_key) if "groq" in shared_base else (GROQ_BASE, "")
    if provider == "cf":
        account = (settings.get("cloudflare_account_id") or "").strip()
        token = (settings.get("cloudflare_token") or "").strip()
        if account and token:
            return f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1", token
        return (shared_base, shared_key) if "cloudflare" in shared_base else ("", "")
    if provider == "mistral":
        key = (settings.get("mistral_key") or "").strip()
        if key:
            return MISTRAL_BASE, key
        return (shared_base, shared_key) if "mistral" in shared_base else (MISTRAL_BASE, "")
    return shared_base, shared_key


def _stored_models(settings: dict, provider: str) -> list[str]:
    catalogues = settings.get("provider_models")
    values = catalogues.get(provider) if isinstance(catalogues, dict) else None
    if isinstance(values, list):
        return list(dict.fromkeys(
            value.strip() for value in values if isinstance(value, str) and value.strip()))
    return []


def _configured(provider: str, settings: dict) -> bool:
    if provider == "gemini":
        return bool((settings.get("gemini_key") or "").strip())
    if provider == CUSTOM:
        return bool((settings.get("openai_base") or "").strip() and (settings.get("openai_key") or "").strip() and (settings.get("openai_model") or "").strip())
    base, key = provider_creds(provider, settings)
    return bool(base and key)


def availability(model: str, settings: dict) -> tuple[bool, str]:
    provider = provider_of(model)
    if provider == "google_translate":
        return True, ""
    if provider == "custom_provider":
        entry = _find_custom_provider(settings, model)
        if entry is None:
            return False, "provider no longer exists"
        if not entry.get("enabled", True):
            return False, "provider is disabled"
        base, key, selected_model = custom_provider_config(model, settings) or ("", "", "")
        if not base:
            return False, "needs a Base URL"
        if not key:
            return False, "needs an API key"
        if not selected_model:
            return False, "needs a selected model"
        return True, _recorded_failure(model, settings)
    if provider == "gemini":
        return (True, "") if _configured("gemini", settings) else (False, "needs a Gemini API key")
    if provider == "freellm":
        base, key = provider_creds(provider, settings)
        if base and key:
            return True, ""
        return False, f"needs a FreeLLMAPI key at {base or detect_flm_base()}"
    if provider in ("groq", "cf", "mistral"):
        return (True, "") if _configured(provider, settings) else (False, "provider is not connected")
    return (True, "") if _configured(CUSTOM, settings) else (False, "needs an endpoint, API key, and model")


def _recorded_failure(model: str, settings: dict) -> str:
    status = settings.get("model_status")
    if not isinstance(status, dict):
        return ""
    value = status.get(model)
    return value.strip() if isinstance(value, str) else ""


def model_groups(settings: dict) -> list[tuple[str, bool]]:
    """Return only models whose provider has complete credentials."""
    rows: list[tuple[str, bool]] = []

    def add_group(header: str, models: list[str]) -> None:
        models = list(dict.fromkeys(model for model in models if model))
        if models:
            rows.append((header, False))
            rows.extend((model, availability(model, settings)[0]) for model in models)

    if _configured("gemini", settings):
        add_group(GEMINI_HDR, _stored_models(settings, "gemini"))
    if _configured("groq", settings):
        add_group(GROQ_HDR, [GROQ_PREFIX + model for model in _stored_models(settings, "groq")])
    if _configured("cf", settings):
        add_group(CF_HDR, [CF_PREFIX + model for model in _stored_models(settings, "cf")])
    if _configured("mistral", settings):
        add_group(MISTRAL_HDR, [MISTRAL_PREFIX + model for model in _stored_models(settings, "mistral")])
    if _configured("freellm", settings):
        add_group(FLM_HDR, [FLM_PREFIX + model for model in _stored_models(settings, "freellm")])

    custom_models: list[str] = []
    for entry in settings.get("custom_providers", []) or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        provider_id = str(entry.get("id", "")).strip()
        if (
            not provider_id
            or not custom_provider_base_url(entry)
            or not str(entry.get("api_key", "")).strip()
        ):
            continue
        custom_models.extend(custom_model_id(provider_id, model) for model in custom_provider_models(entry))
    add_group(CUSTOM_HDR, custom_models)

    if _configured(CUSTOM, settings):
        add_group("── 🔌 Legacy OpenAI-compatible connection ──", [str(settings.get("openai_model", "")).strip()])
    return rows


def all_models(settings: dict | None = None) -> list[str]:
    if not isinstance(settings, dict):
        return []
    return [label for label, enabled in model_groups(settings) if enabled]


def image_capability(model: str) -> str:
    if provider_of(model) == "custom_provider":
        return "unknown"
    normalized = (model or "").strip().lower()
    if normalized.startswith(FLM_PREFIX):
        bare = normalized[len(FLM_PREFIX):]
        if bare in FLM_MEASURED_TEXT_ONLY:
            return "unsupported"
        return "supported" if bare in FLM_MEASURED_VISION else "unknown"
    if normalized in _IMAGE_SUPPORTED_MODELS:
        return "supported"
    if normalized in _TEXT_ONLY_MODELS:
        return "unsupported"
    if normalized.startswith("gemini-"):
        return "unsupported" if normalized in _GEMINI_TEXT_OR_AUDIO_ONLY else "supported"
    return "unknown"


def measured_vision(model: str, settings: dict) -> str:
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return "unknown"
    probes = settings.get("vision_probes")
    row = probes.get(str(entry.get("id", "")).strip()) if isinstance(probes, dict) else None
    if not isinstance(row, dict):
        return "unknown"
    _, selected_model = _custom_parts(model)
    selected_model = selected_model or str(entry.get("model", "")).strip()
    verdict = row.get(selected_model)
    return verdict if verdict in ("supported", "unsupported") else "unknown"


def custom_image_capability(model: str, settings: dict) -> str:
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return "unknown"
    measured = measured_vision(model, settings)
    if measured != "unknown":
        return measured
    return "supported" if entry.get("vision_enabled") else "unsupported"
