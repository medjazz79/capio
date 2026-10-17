"""One source of truth for every translation model the app offers.

Before this module the project carried four competing model lists in four
files, so dead ids and wrong availability rules leaked into the picker.
Everything now flows from here:

    provider_of(model)  ->  "gemini" | "opencode" | "opencode_cli" | "custom"
    availability(model)  ->  (ok: bool, reason: str)
    model_groups(...)    ->  the grouped list the settings combo renders

**The free-tier lists are measured, not believed.** On 2026-10-07 every id
below was called against the live Zen gateway and against the authenticated
OpenCode CLI:

  * `space-bunny-free` -> 200 with no key at all, from plain HTTP and from
    inside the CLI. It is the only one.
  * Every other `-free` id -> 403 "free tier can only be used from within
    OpenCode", *including* from inside the authenticated CLI. The block is an
    account entitlement, not a transport, so a proxy cannot lift it.
  * The "credits" ids -> provider.quota: the account has no balance.

OpenCode's own log shows that same 403 on every day from 2026-09-24 onward,
so these ids never worked on this machine. They are therefore not offered:
listing a model that cannot answer is worse than not offering it, because the
user pays with a failed translation and a confusing badge.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import gemini as gemini_api
from .opencode_bridge import BRIDGE_FREE, ordered_models

# --- Gemini -----------------------------------------------------------------

# Gemini ids are owned by gemini.STARTER_MODELS / gemini.known_models(); do not
# keep a second copy here. Gemini model availability varies by API key; the
# authenticated catalog is refreshed explicitly from Settings.

# --- OpenCode Zen -----------------------------------------------------------

# Measured working with no API key, 2026-10-07. One entry, not a list kept
# broad on purpose: see the module docstring.
#
# The *owner* of `space-bunny-free` is the streaming bridge (`BRIDGE_FREE`
# above), which is the only route measured to answer it: the plain-HTTP Zen
# request in `opencode_zen` gets 403 without the OpenCode client fingerprint.
# ZEN_FREE is therefore a view of that catalog, never a second claim on the
# id. Two catalogs listing it made `provider_of` pick a winner by source
# order, so the Zen branch was unreachable and `opencode_zen` stayed reachable
# only for ids `availability` had already reported unavailable.
_ZEN_KEYLESS_IDS = frozenset({"space-bunny-free"})


def zen_free_ids() -> list[str]:
    """The Zen-shaped ids that are free with no key, in bridge-catalog order."""
    return [m for m in BRIDGE_FREE if m in _ZEN_KEYLESS_IDS]


ZEN_FREE = zen_free_ids()

# Paid Zen ids are deliberately absent. They answered provider.quota on this
# account, and re-adding them means re-adding the OpenCode API key field that
# the user asked to remove. Put them back only with a measured working id and a
# way to pay for it.
ZEN_PAID: list[str] = []

CUSTOM = "custom"
CUSTOM_PROVIDER_PREFIX = "custom:"
OPENCODE_CLI_PREFIX = "opencode-cli:"

_GEMINI_PREFIX = "gemini"
GOOGLE_TRANSLATE_MODEL = "google-translate"


def _find_custom_provider(settings: dict, model_or_id: str) -> dict | None:
    """Return the custom_provider dict for `custom:<id>` or for a bare id."""
    raw_id = (model_or_id or "").strip()
    if raw_id.startswith(CUSTOM_PROVIDER_PREFIX):
        raw_id = raw_id[len(CUSTOM_PROVIDER_PREFIX):]
    if not raw_id:
        return None
    providers = settings.get("custom_providers") if isinstance(settings, dict) else None
    if not isinstance(providers, list):
        return None
    for entry in providers:
        if isinstance(entry, dict) and str(entry.get("id", "")).strip() == raw_id:
            return entry
    return None


def custom_provider_label(model: str, settings: dict) -> str:
    """Human label for a custom provider model id, e.g. 'My API (gpt-4o)'."""
    entry = _find_custom_provider(settings, model)
    if entry:
        name = str(entry.get("name", "")).strip() or entry.get("id", "")
        provider_model = str(entry.get("model", "")).strip()
        return f"{name} ({provider_model})" if provider_model else name
    return model


def custom_provider_config(model: str, settings: dict) -> tuple[str, str, str] | None:
    """Return (base_url, api_key, provider_model) for a custom provider id, or None."""
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return None
    return (
        str(entry.get("base_url", "")).strip(),
        str(entry.get("api_key", "")).strip(),
        str(entry.get("model", "")).strip(),
    )


def get_custom_provider(settings: dict, provider_id: str) -> dict | None:
    """Public lookup for a custom provider by id."""
    return _find_custom_provider(settings, provider_id)


def provider_of(model: str) -> str:
    """Which engine serves this model id."""
    m = (model or "").strip()
    if not m:
        return CUSTOM
    if m.casefold() == GOOGLE_TRANSLATE_MODEL:
        return "google_translate"
    if m.startswith(CUSTOM_PROVIDER_PREFIX):
        return "custom_provider"
    if m.startswith(OPENCODE_CLI_PREFIX):
        return "opencode_cli"
    if m.startswith(FLM_PREFIX):
        return "freellm"
    if m.startswith(GROQ_PREFIX):
        return "groq"
    if m.startswith(CF_PREFIX):
        return "cf"
    if m.startswith(MISTRAL_PREFIX):
        return "mistral"
    if m in BRIDGE_FREE:
        return "bridge"
    if m.lower().startswith(_GEMINI_PREFIX):
        return "gemini"
    # No Zen branch for the ids the bridge serves: `BRIDGE_FREE` is tested
    # above and owns them (see `zen_free_ids`). An unoffered Zen-shaped id still
    # lands on "opencode" below, which is what earns the honest free-tier
    # reason instead of a base URL the user never configured.
    if "/" in m or ":" in m:  # "meta-llama/llama-3.1-8b:free" -> OpenAI-compatible
        return CUSTOM
    if m.endswith("-free"):
        # A Zen-shaped id we no longer offer. It still belongs to Zen, so it
        # gets the honest "not free any more" reason instead of being sent to
        # a custom base URL the user never configured.
        return "opencode"
    return CUSTOM


def availability(model: str, settings: dict) -> tuple[bool, str]:
    """Can this model actually serve a request right now? -> (ok, reason).

    `settings["model_status"]` holds verification verdicts from
    `translators.verify`: "" means the model answered, anything else is the
    error it produced. A model with a recorded failure is reported as
    unavailable with that error, so the picker stops claiming it works.
    """
    provider = provider_of(model)
    if provider == "google_translate":
        return True, ""
    if provider == "custom_provider":
        entry = _find_custom_provider(settings, model)
        if entry is None:
            return False, "custom provider not found — remove or re-add it in Settings"
        if not entry.get("enabled", True):
            return False, f"custom provider '{entry.get('name','')}' is disabled"
        if not str(entry.get("base_url", "")).strip():
            return False, "needs a Base URL in Settings → Custom Providers"
        if not str(entry.get("api_key", "")).strip():
            return False, "needs an API Key in Settings → Custom Providers"
        if not str(entry.get("model", "")).strip():
            return False, "needs a Model ID in Settings → Custom Providers"
        recorded = _recorded_failure(model, settings)
        if recorded:
            return False, recorded
        return True, ""
    if provider == "gemini":
        if not (settings.get("gemini_key") or "").strip():
            return False, "needs a Gemini key"
        # Judged against *this* key's catalogue, from cache: Gemini model
        # availability is per key, and a shared list meant switching keys kept
        # the old key's verdict (or a four-model starter fallback) on screen.
        if model not in gemini_api.catalogue_for(
                (settings.get("gemini_key") or "").strip()):
            return False, "not available on your Google key"
        return True, ""
    if provider == "opencode_cli":
        from . import opencode_cli
        if not opencode_cli.find_executable():
            return False, "needs the OpenCode CLI installed and available on PATH"
        recorded = _recorded_failure(model, settings)
        if recorded:
            return False, recorded
        return True, ""
    if provider == "opencode":
        recorded = _recorded_failure(model, settings)
        if recorded:
            return False, recorded
        # Every id the bridge serves is routed to the bridge by `provider_of`,
        # so anything reaching here is a Zen-shaped id this build no longer
        # offers. Say so instead of implying a missing key would unlock it.
        only_free = (ZEN_FREE[0] if ZEN_FREE else "none")
        return False, (
            f"not on the free tier any more — only {only_free} is free "
            f"with no key"
        )
    if provider == "bridge":
        recorded = _recorded_failure(model, settings)
        if recorded:
            return False, recorded
        return True, ""
    if provider == "freellm":
        base, key = provider_creds("freellm", settings)
        # No "needs the router URL" case: `provider_creds` always yields a base
        # (stored, shared slot, or the detected port), so a branch for an empty
        # one could never fire, and the user with an unconfigured router was
        # told to copy a key instead.
        if not key:
            return False, (
                "needs your FreeLLMAPI unified key (Keys page of "
                f"{_keys_page_url(base)})")
        return True, ""
    if provider == "groq":
        base, key = provider_creds("groq", settings)
        if not key:
            return False, "needs a Groq key in Settings (free at console.groq.com)"
        return True, ""
    if provider == "cf":
        base, key = provider_creds("cf", settings)
        if not base:
            return False, ("needs your Cloudflare Account ID in Settings "
                            "(dash.cloudflare.com)")
        if not key:
            return False, "needs a Cloudflare API token"
        return True, ""
    if provider == "mistral":
        base, key = provider_creds("mistral", settings)
        if not key:
            return False, "needs a Mistral key in Settings (free Experiment tier)"
        return True, ""
    # OpenAI-compatible
    if not (settings.get("openai_base") or "").strip():
        return False, "needs an OpenAI-compatible base URL"
    if not (settings.get("openai_key") or "").strip():
        return False, "needs an API key"
    if not (settings.get("openai_model") or "").strip():
        return False, "no model configured"
    return True, ""


def _recorded_failure(model: str, settings: dict) -> str:
    """The verification error for this model, or "" when none/verified."""
    status = settings.get("model_status")
    if not isinstance(status, dict):
        return ""
    error = status.get(model)
    return error.strip() if isinstance(error, str) else ""


def _keys_page_url(base: str) -> str:
    """The router dashboard the unified key is copied from.

    Derived from the base the app actually talks to, minus the `/v1` API path:
    the dashboard is a page, not an endpoint. Hardcoding a port here sent the
    user to a URL this install never uses while `detect_flm_base()` knew the
    real one.
    """
    origin = (base or "").strip().rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[:-len("/v1")].rstrip("/")
    return origin or (detect_flm_base().rstrip("/"))


def all_models() -> list[str]:
    """Gemini starters + the free Zen ids, de-duplicated."""
    seen, out = set(), []
    for m in [*gemini_api.known_models(), *ZEN_FREE, *BRIDGE_FREE]:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# --- grouped model list for the settings combo ------------------------------

GEMINI_HDR = "── ✦ Gemini — needs a Gemini key ──"
ZEN_FREE_HDR = "── ⚡ OpenCode free — works with NO key ──"
BRIDGE_HDR = "── 🌉 OpenCode bridge free — NO key (streaming) ──"
GOOGLE_TRANSLATE_HDR = "── 🌐 Google Translate — unofficial, no key ──"
GROQ_HDR = "── ⚡ Groq — needs a Groq key (free, no card) ──"
CF_HDR = "── ☁ Cloudflare — needs Account ID + token (free) ──"
MISTRAL_HDR = "── 🌀 Mistral — needs a Mistral key (free tier) ──"
FLM_HDR = "── 🔀 FreeLLMAPI — your local router (many free tiers, auto-failover) ──"
CUSTOM_HDR = "── 🔌 OpenAI-compatible — needs base URL + key ──"
CUSTOM_PROVIDER_HDR = "── 🔧 Custom Providers — your own API endpoints ──"
CLI_HDR = "── ⌘ OpenCode CLI — verified models ──"

# Curated Groq ids, verified live 2026-10-08 (Groq retires ids silently —
# llama-3.3-70b-versatile and llama-3.1-8b-instant were gone that day).
# The ⟳ button replaces this with the live /models list when a key is set.
GROQ_MODELS = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "allam-2-7b",
]
GROQ_PREFIX = "groq:"
CF_PREFIX = "cf:"
MISTRAL_PREFIX = "mistral:"
FLM_PREFIX = "flm:"

GROQ_BASE = "https://api.groq.com/openai/v1"
MISTRAL_BASE = "https://api.mistral.ai/v1"

# --- FreeLLMAPI (the user's own local router) -------------------------------
#
# FreeLLMAPI is a self-hosted gateway that stacks many free tiers behind one
# OpenAI-compatible `/v1` endpoint and fails over between them on its own. The
# value for this app is not "one more provider": it is that `auto` never returns
# "Rate limit exceeded" from the user's perspective, because the router rotates
# keys and models underneath a single name.
#
# Every reply carries `X-Routed-Via: <platform>/<model>`, which the chain turns
# into the badge. That is what keeps the app's no-silent-substitution rule: the
# user sees which engine actually answered, even though they picked "auto".
FLM_DEFAULT_BASE = "http://localhost:3001/v1"

# Where the desktop/tray app stores its own port. The docs say 3001, but a real
# install writes the port it actually bound to here, so read it instead of
# guessing — otherwise the field defaults to a dead URL.
_FLM_CONFIG_PATHS = [
    Path.home() / "AppData" / "Roaming" / "freellmapi" / "config.json",
    Path.home() / ".freellmapi" / "config.json",
]


def detect_flm_base() -> str:
    """The router's real base URL, read from its own config; else the default.

    Never raises: a missing or unreadable config just means the documented
    default, which the user can still correct by hand in Settings.
    """
    for path in _FLM_CONFIG_PATHS:
        try:
            port = int(json.loads(path.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if 1 <= port <= 65535:
            return f"http://localhost:{port}/v1"
    return FLM_DEFAULT_BASE


# Router pseudo-models, not real model ids: each one is a routing strategy the
# gateway applies. They are listed before the live catalog because "auto" is
# the reason to run a router at all.
FLM_ROUTER_MODELS = [
    "auto",
    "auto:fast",
    "auto:smart",
    "auto:reliable",
    "auto:balanced",
    "fusion",
]

# --- which router models actually read an image -----------------------------
#
# Measured live 2026-10-09 on this machine with Google, Cloudflare and
# HuggingFace keys configured. The gateway's own `ready` flag was useless here:
# it means "a key is configured", not "vision works", so both lists below come
# from sending a real PNG and recording the outcome.
#
# The router pseudo-models are deliberately absent. `auto` failed on images
# twice: once with 429 `routing_exhausted` ("6 model lacks vision") before any
# vision key existed, and once with 502 after one did (it routed to
# gemini-3.7-flash, which Google answered 503). Whether `auto` can serve an
# image depends on the keys configured, so it is reported as unknown and the
# picker says so, rather than promising something it may not deliver.
#
# Order matters: the first entries are the ones to offer when an image request
# fails, which is what the image error message does.
FLM_MEASURED_VISION = [
    "gemma-4-26b-a4b-it",      # 8.8s via google/gemma-4-26b-a4b-it
    "gemma-4-26b-it",          # 8.0s
    "gemini-3.1-flash-lite",   # 3.6s
    "gemini-3.5-flash",        # 4.1s
    "gemini-3-flash-preview",  # 4.2s
    "gemini-3.8-flash",        # 5.4s
    "gemini-3.5-flash-lite",   # 12.0s
    "gemini-3.6-flash",        # 8.3s
]

# These look like vision models by name and still rejected every image, several
# in ~2s — a refusal, not an attempt. `llava-1.5-7b` is the trap that made
# guessing from the model name wrong.
FLM_MEASURED_TEXT_ONLY = frozenset({
    "llava-1.5-7b",
    "llama-4-scout",
    "llama-4-maverick",
    "moondream-3.1-9b-a2b",
    "gemma-2b-it-lora",
    "gemma-7b-it-lora",
    "gemma-sea-lion-v4-27b",
    "gemma-4-31b-it",
    "llama-3.2-11b-vision",
    "llama-3.2-90b-vision",
    "aya-vision-32b",
    "command-a-vision",
    "glm-4.6v-flash",
    "qwen2.5-vl-72b",
    "qwen3-vl-235b-a22b-instruct",
})


def provider_creds(provider: str, settings: dict) -> tuple[str, str]:
    """(base_url, api_key) for a token-slot provider.

    A provider-specific token wins; otherwise the shared OpenAI-compatible
    slot is used when it points at that provider, so setups saved before
    the per-provider slots existed keep working untouched.
    """
    shared_base = (settings.get("openai_base") or "").strip()
    shared_key = (settings.get("openai_key") or "").strip()
    if provider == "freellm":
        base = (settings.get("flm_base") or "").strip()
        key = (settings.get("flm_key") or "").strip()
        if base and key:
            return base, key
        # A setup saved before the dedicated slot existed kept the router in the
        # shared fields; recognise it by host rather than by a name so the
        # migration happens without the user retyping anything.
        if "localhost" in shared_base and shared_key:
            return shared_base, shared_key
        # A key with no stored URL still works: the router's own config knows
        # its port, and guessing it wrong only costs a failed connection.
        return (base or detect_flm_base()), key
    if provider == "groq":
        if (settings.get("groq_key") or "").strip():
            return GROQ_BASE, settings["groq_key"].strip()
        if "groq" in shared_base and shared_key:
            return shared_base, shared_key
        return GROQ_BASE, ""
    if provider == "cf":
        account = (settings.get("cloudflare_account_id") or "").strip()
        token = (settings.get("cloudflare_token") or "").strip()
        if account and token:
            return (f"https://api.cloudflare.com/client/v4/accounts/"
                    f"{account}/ai/v1", token)
        if "cloudflare" in shared_base and shared_key:
            return shared_base, shared_key
        return "", ""
    if provider == "mistral":
        if (settings.get("mistral_key") or "").strip():
            return MISTRAL_BASE, settings["mistral_key"].strip()
        if "mistral" in shared_base and shared_key:
            return shared_base, shared_key
        return MISTRAL_BASE, ""
    return shared_base, shared_key

# Curated Cloudflare Workers AI ids, verified 2026-10-08 (freellmapi hub +
# official docs). Needs the account ID inside the base URL:
# https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1
CF_MODELS = [
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/meta/llama-3.1-8b-instruct",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/qwen/qwen2.5-coder-32b-instruct",
    # Vision-capable (catalog-sourced; needs a Cloudflare key to verify live).
    "@cf/meta/llama-3.2-11b-vision-instruct",
]
# Mistral La Plateforme aliases (stable documented names; free Experiment
# tier is ~2 RPM — fine to try, slow for whole books).
# pixtral adds vision.
MISTRAL_MODELS = [
    "mistral-small-latest",
    "codestral-latest",
    "open-mistral-nemo",
    "mistral-large-latest",
    "pixtral-12b-2409",
]

# Per-model input modalities from the providers' model catalogs. Unknown live
# ids stay explicitly unverified rather than inheriting a provider-wide guess.
_IMAGE_SUPPORTED_MODELS = frozenset({
    "groq:qwen/qwen3.8-27b",
    "cf:@cf/meta/llama-3.2-11b-vision-instruct",
    "cf:@cf/mistralai/mistral-small-3.1-24b-instruct",
    "mistral:mistral-small-latest",
    "mistral:mistral-large-latest",
    "mistral:pixtral-12b-2409",
    "space-bunny-free",
    "muse-spark-1.3-contributor-free",
    "mimo-v2.6-flash-free",
    "fledge-alpha-free",
    "muse-spark-1.2-contributor-free",
    "longcat-2.5-preview-free",
})
_TEXT_ONLY_MODELS = frozenset({
    "groq:openai/gpt-oss-20b",
    "groq:openai/gpt-oss-120b",
    "groq:allam-2-7b",
    "cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf:@cf/meta/llama-3.1-8b-instruct",
    "cf:@cf/qwen/qwen2.5-coder-32b-instruct",
    "mistral:codestral-latest",
    "mistral:open-mistral-nemo",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
})
_GEMINI_TEXT_OR_AUDIO_ONLY = frozenset({
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
    "gemini-3.5-live-translate-preview",
})


def image_capability(model: str) -> str:
    """Return ``supported``, ``unsupported``, or ``unknown`` for image input."""
    if (model or "").strip().startswith(CUSTOM_PROVIDER_PREFIX):
        return "unknown"
    normalized = (model or "").strip().lower()
    if provider_of(normalized) == "google_translate":
        return "unsupported"
    if normalized.startswith(FLM_PREFIX):
        bare = normalized[len(FLM_PREFIX):]
        if bare in FLM_MEASURED_TEXT_ONLY:
            return "unsupported"
        if bare in FLM_MEASURED_VISION:
            return "supported"
        # A strategy (`auto`, `auto:fast`, `fusion`) or an unmeasured id: the
        # gateway decides, and we have not proven which way.
        return "unknown"
    if normalized in _IMAGE_SUPPORTED_MODELS:
        return "supported"
    if normalized in _TEXT_ONLY_MODELS:
        return "unsupported"
    if normalized.startswith("gemini-"):
        return (
            "unsupported"
            if normalized in _GEMINI_TEXT_OR_AUDIO_ONLY
            else "supported"
        )
    if provider_of(normalized) == "opencode_cli":
        return "unsupported"
    return "unknown"


def measured_vision(model: str, settings: dict) -> str:
    """What the provider's current model was seen to do with an image.

    A gateway serves text and vision models side by side, so the provider's
    checkbox cannot answer for any one of them. Returns "supported",
    "unsupported", or "unknown" when the model was never tested.

    `model` identifies the *provider* (`custom:<id>`, or a bare id); the
    verdict is filed under the model name the provider currently has selected,
    because that is what a Detect run recorded.
    """
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return "unknown"
    probes = settings.get("vision_probes")
    if not isinstance(probes, dict):
        return "unknown"
    row = probes.get(str(entry.get("id", "")).strip())
    if not isinstance(row, dict):
        return "unknown"
    name = str(entry.get("model", "")).strip()
    verdict = row.get(name)
    return verdict if verdict in ("supported", "unsupported") else "unknown"


def custom_image_capability(model: str, settings: dict) -> str:
    """Image capability for a custom provider, preferring what was measured.

    The Vision / Images checkbox is the user's switch to *allow* images. It is
    not evidence that the endpoint can take them, so a model that was tested
    and refused an image stays "unsupported" even with the box ticked — that
    mismatch is exactly what used to send a page to a text-only model and
    fail. An untested model falls back to the checkbox, which is the only
    thing the user has to go on.
    """
    entry = _find_custom_provider(settings, model)
    if entry is None:
        return "unknown"
    measured = measured_vision(model, settings)
    if measured != "unknown":
        return measured
    return "supported" if entry.get("vision_enabled") else "unsupported"


def model_groups(
    settings: dict,
    live_opencode: list[str] | None = None,
    live_cli: list[str] | None = None,
    live_groq: list[str] | None = None,
    live_cf: list[str] | None = None,
    live_flm: list[str] | None = None,
) -> list[tuple[str, bool]]:
    """[(label, enabled)] for the combo. Disabled rows still show their reason.

    The picker offers three groups: Gemini, the streaming bridge, and Groq.
    `live_opencode` / `live_cli` are accepted for call compatibility but
    deliberately ignored: the Zen catalog still lists ids that answer 403,
    and CLI/custom engines keep working when already selected but are no
    longer offered. Working-ness comes from `model_status`, which records
    real replies.
    """
    del live_opencode
    del live_cli

    def flag(m: str) -> bool:
        return availability(m, settings)[0]

    rows: list[tuple[str, bool]] = [(GEMINI_HDR, False)]
    rows += [(m, flag(m)) for m in gemini_api.known_models()]

    rows.append((BRIDGE_HDR, False))
    rows += [(m, flag(m)) for m in ordered_models(
        bool(settings.get("fast_mode", True)))]

    rows.append((GOOGLE_TRANSLATE_HDR, False))
    rows.append((GOOGLE_TRANSLATE_MODEL, flag(GOOGLE_TRANSLATE_MODEL)))

    groq_models = list(dict.fromkeys((live_groq or []) + GROQ_MODELS))
    rows.append((GROQ_HDR, False))
    rows += [(f"{GROQ_PREFIX}{m}", flag(f"{GROQ_PREFIX}{m}"))
             for m in groq_models]

    cf_models = list(dict.fromkeys((live_cf or []) + CF_MODELS))
    rows.append((CF_HDR, False))
    rows += [(f"{CF_PREFIX}{m}", flag(f"{CF_PREFIX}{m}"))
             for m in cf_models]

    rows.append((MISTRAL_HDR, False))
    rows += [(f"{MISTRAL_PREFIX}{m}", flag(f"{MISTRAL_PREFIX}{m}"))
             for m in MISTRAL_MODELS]

    # Router pseudo-models first (that is the point of running a gateway),
    # then whatever `?execution_status=ready` reported as callable right now.
    flm_models = list(dict.fromkeys(
        [*FLM_ROUTER_MODELS, *(live_flm or [])]))
    rows.append((FLM_HDR, False))
    rows += [(f"{FLM_PREFIX}{m}", flag(f"{FLM_PREFIX}{m}"))
             for m in flm_models]
    # Custom providers — each one is a user-defined OpenAI-compatible endpoint
    # like Kilo Code's custom provider config.
    custom_providers = settings.get("custom_providers") if isinstance(settings.get("custom_providers"), list) else []
    if custom_providers:
        rows.append((CUSTOM_PROVIDER_HDR, False))
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("id", "")).strip()
            if not cid:
                continue
            mid = f"{CUSTOM_PROVIDER_PREFIX}{cid}"
            rows.append((mid, flag(mid)))
    return rows
