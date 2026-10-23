"""User settings stored as JSON in the home directory.

Design rules:
  * ONE source of truth for the model: the `model` key. `gemini_model` and
    `opencode_model` are *derived* (see `resolve_model`), never stored, so the
    two competing "model" fields can never drift apart again.
  * The file is written WITHOUT a BOM. A BOM makes `json.loads` raise, which
    used to make `load()` silently fall back to defaults.
"""
from __future__ import annotations

import json
import copy
import threading
from contextlib import contextmanager
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger("harmony_translator.config.settings")

# The engine the app uses unless the user picks another one.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_LINE_SPACING = 0
MIN_LINE_SPACING = -50
MAX_LINE_SPACING = 50
DEFAULT_TRANSLATION_SIDE_MARGIN_PX = 7
MIN_TRANSLATION_SIDE_MARGIN_PX = 0
MAX_TRANSLATION_SIDE_MARGIN_PX = 100
DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX = 7
MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX = 0
MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX = 100
MIN_TRANSLATION_FONT_SIZE = 5.0
MAX_TRANSLATION_FONT_SIZE = 144.0
DEFAULT_CHAT_FONT_SIZE = 12.0
MIN_CHAT_FONT_SIZE = 8.0
MAX_CHAT_FONT_SIZE = 30.0

# Max settings file size to prevent DoS (1 MB is far beyond normal ~5 KB)
_MAX_SETTINGS_FILE_BYTES = 1_048_576
_SETTINGS_READ_RETRIES = 3


class SettingsLoadError(ValueError):
    """Settings could not be read safely, and no recovery copy was usable."""


class Settings(dict):
    """A normal settings mapping with a private, non-serialized edit baseline."""

    def __init__(self, values=(), *, baseline=None):
        super().__init__(copy.deepcopy(dict(values)))
        self.baseline = copy.deepcopy(dict(self) if baseline is None else baseline)

    def copy(self):
        return Settings(self, baseline=self.baseline)


def copy_settings(values: dict) -> Settings:
    return Settings(values, baseline=getattr(values, "baseline", values))


def _merge_edits(current, incoming, baseline):
    if isinstance(incoming, dict) and isinstance(baseline, dict):
        merged = copy.deepcopy(current) if isinstance(current, dict) else {}
        for key in baseline.keys() - incoming.keys():
            merged.pop(key, None)
        for key, value in incoming.items():
            if key not in baseline or value != baseline[key]:
                merged[key] = _merge_edits(merged.get(key), value, baseline.get(key))
        return merged
    return copy.deepcopy(incoming)


def _merge_provider_edits(current, incoming, baseline):
    def by_id(entries):
        return {entry["id"]: entry for entry in _sanitize_custom_providers(entries)}
    live, edited, original = map(by_id, (current, incoming, baseline))
    # A stale editor must not resurrect a provider removed by another window.
    for provider_id in original.keys() - live.keys():
        edited.pop(provider_id, None)
    return list(_merge_edits(live, edited, original).values())


_SETTINGS_LOCK = threading.RLock()


@contextmanager
def _settings_transaction():
    """Serialize the reader and floating process, including read/merge/write."""
    with _SETTINGS_LOCK:
        with SETTINGS_PATH.with_suffix(".lock").open("a+b") as lock_file:
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 5
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.025)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read_text_with_retry(path: Path) -> str:
    for attempt in range(_SETTINGS_READ_RETRIES):
        try:
            return path.read_text(encoding="utf-8-sig")
        except PermissionError:
            if attempt + 1 == _SETTINGS_READ_RETRIES:
                raise
            time.sleep(0.05 * (2 ** attempt))
    raise AssertionError("unreachable")


def _read_settings_dict(path: Path) -> dict:
    data = json.loads(_read_text_with_retry(path))
    if not isinstance(data, dict):
        raise ValueError("Settings file is not a JSON object")
    return data


def _settings_recovery_candidates(path: Path) -> list[Path]:
    candidates = []
    try:
        recovery_copy = path.with_name(
            f"{path.stem}.corrupt-{os.getpid()}-{secrets.token_hex(4)}"
            f"{path.suffix}"
        )
        shutil.copy2(path, recovery_copy)
        candidates.append(recovery_copy)
    except OSError:
        log.exception("Could not make a recovery copy of %s", path)

    legacy_backup = path.with_suffix(".corrupt.json")
    if legacy_backup not in candidates and legacy_backup.is_file():
        candidates.append(legacy_backup)
    return candidates

DEFAULTS = {
    # --- AI engines -------------------------------------------------------
    "gemini_key": "",
    # Measured per-model verdicts: "" = answered, anything else = that error.
    # Filled by `translators.verify`; see registry.py for why names are not
    # trusted on their own.
    "model_status": {},
    "openai_base": "",
    "openai_key": "",
    "openai_model": "",
    # One token slot per provider. Empty means "use the shared
    # OpenAI-compatible slot above" so existing setups keep working.
    "groq_key": "",
    "cloudflare_account_id": "",
    "cloudflare_token": "",
    "mistral_key": "",
    # The user's own FreeLLMAPI router: one local gateway that stacks many free
    # tiers and fails over between them. `flm_base` is auto-detected from the
    # router's config.json on first run, so the port is never guessed.
    "flm_base": "",
    "flm_key": "",
    # Ids the router reported as `ready` on the last refresh, so a saved model
    # can be checked for retirement without another network round-trip.
    "flm_models_ready": [],
    # Model catalogues fetched after a provider is connected.  Keeping these
    # separate from credentials lets the picker be dynamic without exposing
    # models for services the user has not configured.
    "provider_models": {},
    "api_key_providers": [],
    "model": DEFAULT_MODEL,
    "model_favorites": [],
    "custom_provider_model_favorites": [],
    "custom_provider_collapsed": {},
    "settings_last_page": 0,
    "settings_last_page_version": 5,
    "model_instructions": "",
    "explain_mode": False,
    "explain_instructions": "",
    # Master switch for live streaming display, TTS disk cache and
    # parallel speech synthesis.
    "fast_mode": True,
    "allow_translation_fallback": True,
    # One editable source=preferred-term glossary for each target language.
    "translation_glossaries": {},
    # Additional logical pixels between wrapped translation lines.
    "line_spacing_px": DEFAULT_LINE_SPACING,
    # Equal inner margin on the left and right sides of translated text.
    "translation_side_margin_px": DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
    # Equal inner margin above and below translated text.
    "translation_top_bottom_margin_px": DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
    # Remembered Settings-window size and position.
    "settings_size": [],
    "settings_pos": [],
    # --- Custom providers --------------------------------------------------
    # Each entry: {id, name, base_url, api_key, model, models, enabled}
    # `id` is unique (generated), `name` shown in picker, `base_url` + `api_key`
    # + `model` fully describe the endpoint. Mirrors Kilo Code's provider config.
    "custom_providers": [],
    # User-defined display order for provider cards on the API Keys page.
    "api_key_provider_order": [],
    # API Keys page custom credentials are managed independently from
    # translation providers configured on the AI Providers page.
    "api_key_custom_providers": [],
    # Measured image support, per provider then per model:
    #   {"<provider id>": {"<model id>": "supported" | "unsupported"}}
    # Absent means never tested. The Vision checkbox above is the switch that
    # *allows* images; this records what the endpoint was actually seen to do,
    # so the model list stops claiming vision for every model a provider owns.
    "vision_probes": {},
    # --- Application ------------------------------------------------------
    "email": "",
    "selection_mode": "ocr",
    "ocr_langs": "auto",
    "source_language": "auto",
    "target_language": "ar",
    "chat_reply_language": "ar",
    "chat_reply_language_only": False,
    "float_source_language": "auto",
    "float_target_language": "ar",
    "font_size": 17,
    "chat_font_size": DEFAULT_CHAT_FONT_SIZE,
    "translation_font": "",
    "translation_font_arabic": "",
    "translation_font_latin": "",
    "arabic_tashkeel": False,
    "theme": "Dark Slate",
    "color_scheme": "system",
    "pdf_canvas_theme_background": False,
    "speech_engine": "edge",
    "gemini_tts_voice": "Kore",
    "speech_rate": 1.0,
    "speech_rate_arabic": 1.0,
    "speech_rate_other": 1.0,
    "chat_speech_rate": 1.0,
    "speech_voice": "fr-FR-VivienneMultilingualNeural",
    "automatic_speech_voice": False,
    "speech_read_latin_words": False,
    "speech_latin_only": False,
    "prefetch_next_page_translation": False,
    "automatic_page_reading_time_enabled": False,
    "automatic_page_reading_time_minutes": 15,
    # --- session memory ---------------------------------------------------
    "window_geometry": "",
    "splitter_sizes": [],
    "thumbs_visible": False,
    "chat_dock_open": False,
    "chat_dock_width": 360,
    "chat_dock_area": "right",
    "chat_composer_splitter_sizes": [],
    "last_dir": "",
    "last_open_dir": "",
    "last_pdf": "",
    "last_page": 0,
    "book_positions": {},
    "last_zoom": None,
    "recent_files": [],
    "open_tabs": [],
    "active_tab": 0,
    "float_pos": [],
    "popup_size": [],
    "popup_pos": [],
}

SETTINGS_PATH = Path.home() / ".harmony_translator.json"


def configure_playback_volume(audio_output) -> None:
    """Leave volume and mute untouched; the user controls them in Windows.

    Shared by the reader, floating popup and chat player. In particular, do
    not restore a saved value or force full volume when creating a player.
    """


def _custom_providers_path() -> Path:
    return SETTINGS_PATH.with_name(
        f"{SETTINGS_PATH.stem}_custom_providers.json"
    )


def resolve_model(settings: dict) -> str:
    """The one model the app should use. Never returns an empty string."""
    return (settings.get("model") or "").strip() or DEFAULT_MODEL


def _sanitize_custom_providers(raw) -> list[dict]:
    """Validate and clean custom_providers list."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "")).strip()
        if not cid:
            cid = f"custom-{secrets.token_hex(4)}"
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        name = str(item.get("name", "")).strip() or cid
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        api_key = str(item.get("api_key", "")).strip()
        model = str(item.get("model", "")).strip()
        raw_models = item.get("models", ())
        models = []
        if isinstance(raw_models, (list, tuple)):
            for candidate in raw_models:
                candidate = str(candidate).strip()
                if candidate and candidate not in models:
                    models.append(candidate[:256])
                if len(models) >= 400:
                    break
        if model and model not in models:
            models.insert(0, model)
        enabled = bool(item.get("enabled", True))
        # Kilo Code style per-provider capabilities
        reasoning_enabled = bool(item.get("reasoning_enabled", item.get("reasoning", False)))
        vision_enabled = bool(item.get("vision_enabled", item.get("vision", item.get("supports_images", False))))
        # Basic SSRF guard: only http(s) endpoints, block obvious internal bypasses
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            base_url = "https://" + base_url
        # Keep entry even if incomplete so user can finish editing;
        # availability() will report what's missing.
        out.append({
            "id": cid,
            "name": name[:64],
            "base_url": base_url[:512],
            "api_key": api_key[:512],
            "model": model[:256],
            "models": models,
            "enabled": enabled,
            "reasoning_enabled": reasoning_enabled,
            "vision_enabled": vision_enabled,
        })
    return out


def _sanitize_provider_models(raw) -> dict[str, list[str]]:
    """Keep only fetched model identifiers for built-in providers."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for provider in ("gemini", "groq", "cf", "mistral", "freellm"):
        values = raw.get(provider)
        if not isinstance(values, (list, tuple)):
            continue
        models: list[str] = []
        for value in values:
            value = str(value).strip()
            if value and value not in models:
                models.append(value[:256])
            if len(models) >= 400:
                break
        if models:
            out[provider] = models
    return out


def _sanitize_vision_probes(raw, custom_providers) -> dict:
    """Clean the measured-vision map, dropping entries for models that left.

    The map is keyed by provider id then model id, both of which the user can
    rename or remove in the same screen that writes it, so it is pruned here
    rather than trusted to stay in step.
    """
    if not isinstance(raw, dict):
        return {}
    live = {
        str(item.get("id", "")).strip()
        for item in _sanitize_custom_providers(custom_providers)
    }
    out: dict[str, dict[str, str]] = {}
    for provider_id, models in raw.items():
        provider_id = str(provider_id).strip()
        if provider_id not in live or not isinstance(models, dict):
            continue
        kept = {
            str(model).strip()[:256]: verdict
            for model, verdict in models.items()
            if str(model).strip()
            and verdict in ("supported", "unsupported")
        }
        if kept:
            out[provider_id] = kept
    return out


def load() -> dict:
    with _settings_transaction():
        return Settings(_load_unlocked())


def _load_unlocked() -> dict:
    """Read settings, filling in defaults without replacing unreadable data."""
    data = {}
    settings_file_loaded = False
    settings_file_exists = False
    try:
        size = SETTINGS_PATH.stat().st_size
        settings_file_exists = True
    except FileNotFoundError:
        size = 0
    except OSError:
        # Do not treat an access-denied stat as a missing settings file.
        settings_file_exists = True
        size = 0

    if size > _MAX_SETTINGS_FILE_BYTES:
        try:
            _backup_oversized_file(SETTINGS_PATH)
        except OSError:
            log.exception("Could not back up oversized settings file")
        raise SettingsLoadError(
            "The settings file is larger than the supported limit. "
            "It was preserved; move or inspect the oversized backup before "
            "starting Capio again."
        )

    if settings_file_exists:
        try:
            data = _read_settings_dict(SETTINGS_PATH)
            settings_file_loaded = True
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.warning("Could not load settings file: %s", exc)
            recovered = None
            recovered_path = None
            for candidate in _settings_recovery_candidates(SETTINGS_PATH):
                try:
                    if candidate.stat().st_size > _MAX_SETTINGS_FILE_BYTES:
                        continue
                    recovered = _read_settings_dict(candidate)
                    recovered_path = candidate
                    break
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
            if recovered is None:
                raise SettingsLoadError(
                    "Capio could not read your settings file, and no valid "
                    "recovery copy was found. The existing file was preserved "
                    "to avoid overwriting your keys and preferences."
                ) from exc
            data = recovered
            settings_file_loaded = True
            log.warning("Recovered settings from %s", recovered_path)
    merged = copy.deepcopy(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    for key in ("speech_rate_arabic", "speech_rate_other"):
        if key not in data:
            merged[key] = merged["speech_rate"]
    if "color_scheme" not in data:
        merged["color_scheme"] = (
            "light" if merged.get("theme") == "Pure Light"
            else "dark" if settings_file_loaded
            else "system"
        )
    if merged.get("color_scheme") not in ("system", "dark", "light"):
        merged["color_scheme"] = "system"

    providers_path = _custom_providers_path()
    providers_file_exists = False
    try:
        providers_stat = providers_path.stat()
        providers_file_exists = True
        if providers_stat.st_size > _MAX_SETTINGS_FILE_BYTES:
            log.warning(
                "Custom providers file exceeds the supported size"
            )
            try:
                _backup_oversized_file(providers_path)
            except OSError:
                log.exception(
                    "Could not back up oversized custom providers file"
                )
            raise SettingsLoadError(
                "The custom providers file is larger than the supported "
                "limit. It was preserved; inspect the oversized backup "
                "before starting Capio again."
            )
        else:
            providers_data = json.loads(_read_text_with_retry(providers_path))
            if isinstance(providers_data, list):
                merged["custom_providers"] = _sanitize_custom_providers(
                    providers_data
                )
            else:
                raise SettingsLoadError(
                    "Capio's custom providers file is not a JSON list. The "
                    "existing file was preserved to avoid overwriting "
                    "provider settings."
                )
    except FileNotFoundError:
        pass
    except SettingsLoadError:
        raise
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise SettingsLoadError(
            "Capio could not read its custom providers file. The existing file "
            "was preserved to avoid overwriting provider settings."
        ) from exc
    if not providers_file_exists and settings_file_loaded:
        legacy_providers = _sanitize_custom_providers(
            merged.get("custom_providers")
        )
        merged["custom_providers"] = legacy_providers
        if legacy_providers:
            try:
                _write_json_atomically(providers_path, legacy_providers)
            except (OSError, TypeError, ValueError):
                log.exception(
                    "Could not migrate custom providers to %s", providers_path
                )
    # NOTE: no prefix-based model reset here. `groq:` is a live provider
    # (registry.provider_of), so resetting it bounced the user back to
    # Gemini on every load.
    if not isinstance(merged.get("model_status"), dict):
        merged["model_status"] = {}
    merged["model_status"] = {
        k: v for k, v in merged["model_status"].items()
        if isinstance(k, str) and isinstance(v, str)
    }
    ready = merged.get("flm_models_ready")
    merged["flm_models_ready"] = [
        m for m in ready if isinstance(m, str) and m.strip()
    ] if isinstance(ready, list) else []
    merged["provider_models"] = _sanitize_provider_models(
        merged.get("provider_models"))
    # Sanitize custom providers
    merged["custom_providers"] = _sanitize_custom_providers(merged.get("custom_providers"))
    merged["api_key_custom_providers"] = _sanitize_custom_providers(
        merged.get("api_key_custom_providers"))
    merged["vision_probes"] = _sanitize_vision_probes(
        merged.get("vision_probes"), merged["custom_providers"])
    # NOTE: `flm_base` is deliberately NOT auto-filled here. `load()` must
    # return the file's own values (a test asserts a round-trip), and a
    # machine-dependent guess does not belong in persisted state. The detected
    # URL is used as the Settings dialog's placeholder default instead.
    try:
        line_spacing = int(merged.get(
            "line_spacing_px", DEFAULT_LINE_SPACING))
    except (TypeError, ValueError):
        line_spacing = DEFAULT_LINE_SPACING
    merged["line_spacing_px"] = max(
        MIN_LINE_SPACING, min(MAX_LINE_SPACING, line_spacing)
    )
    try:
        side_margin = int(merged.get(
            "translation_side_margin_px",
            DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
        ))
    except (TypeError, ValueError):
        side_margin = DEFAULT_TRANSLATION_SIDE_MARGIN_PX
    merged["translation_side_margin_px"] = max(
        MIN_TRANSLATION_SIDE_MARGIN_PX,
        min(MAX_TRANSLATION_SIDE_MARGIN_PX, side_margin),
    )
    try:
        top_bottom_margin = int(merged.get(
            "translation_top_bottom_margin_px",
            DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
        ))
    except (TypeError, ValueError):
        top_bottom_margin = DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX
    merged["translation_top_bottom_margin_px"] = max(
        MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
        min(MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX, top_bottom_margin),
    )
    try:
        font_size = float(merged.get("font_size", DEFAULTS["font_size"]))
    except (TypeError, ValueError):
        font_size = float(DEFAULTS["font_size"])
    # Earlier versions stored Ctrl+wheel zoom as a multiplier. Fold that
    # multiplier into font_size so Settings and the displayed text stay linked.
    if "translation_zoom_factor" in data:
        try:
            legacy_zoom = float(data["translation_zoom_factor"])
        except (TypeError, ValueError):
            legacy_zoom = 1.0
        if 0.0 < legacy_zoom < float("inf"):
            font_size *= legacy_zoom
    if not 0.0 < font_size < float("inf"):
        font_size = float(DEFAULTS["font_size"])
    merged["font_size"] = max(
        MIN_TRANSLATION_FONT_SIZE,
        min(MAX_TRANSLATION_FONT_SIZE, font_size),
    )
    try:
        chat_font_size = float(merged.get(
            "chat_font_size", DEFAULT_CHAT_FONT_SIZE))
    except (TypeError, ValueError):
        chat_font_size = DEFAULT_CHAT_FONT_SIZE
    if not 0.0 < chat_font_size < float("inf"):
        chat_font_size = DEFAULT_CHAT_FONT_SIZE
    merged["chat_font_size"] = max(
        MIN_CHAT_FONT_SIZE,
        min(MAX_CHAT_FONT_SIZE, chat_font_size),
    )
    # A saved model that cannot answer any more must not stay selected: it
    # would fail on every selection with a badge the user cannot act on.
    #
    # Retired provider prefixes are migrated to the default. Credentials are
    # never silently changed; the picker will only offer a provider after it
    # has a complete configuration.
    from translators import registry
    saved = str(merged.get("model", ""))
    retired_opencode_models = {
        "space-bunny-free",
        "muse-spark-1.3-contributor-free",
        "mimo-v2.6-flash-free",
        "fledge-alpha-free",
        "muse-spark-1.2-contributor-free",
        "longcat-2.5-preview-free",
        "nemotron-3.5-lightning-free",
        "nemotron-3-ultra-free",
    }
    if (
        saved.startswith(("opencode-cli:", "opencode:"))
        or saved in retired_opencode_models
    ):
        merged["model"] = DEFAULT_MODEL
    elif _is_retired_router_model(merged, saved):
        merged["model"] = DEFAULT_MODEL
    elif _is_dead_prefixed_model(merged, saved):
        merged["model"] = DEFAULT_MODEL
    elif saved.startswith(registry.CUSTOM_PROVIDER_PREFIX):
        # Saved custom provider no longer exists (deleted in Settings).
        # Reset to default so availability() doesn't fail silently.
        if registry.get_custom_provider(merged, saved[len(registry.CUSTOM_PROVIDER_PREFIX):]) is None:
            merged["model"] = DEFAULT_MODEL
    for key in ("opencode_cli_models", "effort_default", "effort_levels"):
        merged.pop(key, None)
    return merged


def _is_retired_router_model(settings: dict, saved: str) -> bool:
    """True for a FreeLLMAPI id this router no longer lists.

    A FreeLLMAPI model the router dropped. Sticky sessions and a stale settings
    file can both name one; leaving it selected would fail every selection with
    a badge the user cannot act on. Router pseudo-models (`auto`, `auto:fast`,
    ...) are always valid because the gateway resolves them itself. And with no
    key stored there is nothing to judge against, so the user's choice is left
    alone -- they may well paste a key and fix it, and silently swapping their
    choice would be the old "silent substitution" bug all over again.
    """
    from translators import registry
    if registry.provider_of(saved) != "freellm":
        return False
    bare = saved[len(registry.FLM_PREFIX):]
    if bare in registry.FLM_ROUTER_MODELS:
        return False
    if bare in (settings.get("flm_models_ready") or []):
        return False
    if not (settings.get("flm_key") or "").strip():
        return False
    return True


def _is_dead_prefixed_model(settings: dict, saved: str) -> bool:
    """True for a prefixed model whose provider is gone or id retired.

    Measured 2026-10-08/09: Groq removed `llama-3.3-70b-versatile` and
    `llama-3.1-8b-instant` without notice, and Cerebras was dropped as a
    provider. An id from either cannot answer any more, so it must not stay
    selected — it would fail on every selection with a badge the user cannot
    act on.

    A *live* provider id (`groq:qwen3.8-27b`) is never treated as dead: the
    curated list is curated, not exhaustive, and the user's own key may serve
    models the app does not ship. Only the ids this build knows are retired
    get moved.
    """
    from translators import registry
    if not saved:
        return False
    # Providers this app no longer routes to at all.
    dead_prefixes = ("cerebras:",)
    if saved.startswith(dead_prefixes):
        return True
    # Individual ids a live provider retired without notice.
    known_dead = {
        "groq:llama-3.3-70b-versatile",
        "groq:llama-3.1-8b-instant",
    }
    return saved in known_dead


def save(settings: dict) -> None:
    with _settings_transaction():
        baseline = getattr(settings, "baseline", None)
        if baseline is not None:
            current = _load_unlocked()
            changes = {
                key: value for key, value in settings.items()
                if key in DEFAULTS and value != baseline.get(key)
            }
            for key, value in changes.items():
                merger = _merge_provider_edits if key == "custom_providers" else _merge_edits
                current[key] = merger(current.get(key), value, baseline.get(key))
        else:
            current = settings
        _save_unlocked(current)
        if isinstance(settings, Settings):
            settings.clear()
            settings.update(copy.deepcopy(current))
            settings.baseline = copy.deepcopy(current)


def _save_unlocked(settings: dict) -> None:
    """Write settings atomically, without a BOM (a BOM breaks `json.loads`)."""
    clean = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
    # Sanitize custom_providers before saving
    clean["custom_providers"] = _sanitize_custom_providers(clean.get("custom_providers"))
    clean["api_key_custom_providers"] = _sanitize_custom_providers(
        clean.get("api_key_custom_providers"))
    clean["provider_models"] = _sanitize_provider_models(
        clean.get("provider_models"))
    clean["vision_probes"] = _sanitize_vision_probes(
        clean.get("vision_probes"), clean["custom_providers"])
    providers_path = _custom_providers_path()
    try:
        _backup_oversized_file(providers_path)
        _backup_oversized_file(SETTINGS_PATH)
        _write_json_atomically(providers_path, clean["custom_providers"])
        _write_json_atomically(SETTINGS_PATH, clean)
    except (OSError, TypeError, ValueError) as exc:
        log.exception("Could not save settings: %s", exc)
        raise


def _backup_oversized_file(path: Path) -> Path | None:
    """Preserve oversized user data before a later save can replace it."""
    try:
        if path.stat().st_size <= _MAX_SETTINGS_FILE_BYTES:
            return None
    except FileNotFoundError:
        return None
    backup = path.with_name(
        f"{path.stem}.oversized-{os.getpid()}-{secrets.token_hex(4)}"
        f"{path.suffix}"
    )
    shutil.copy2(path, backup)
    log.warning("Backed up oversized user data to %s", backup)
    return backup


def _write_json_atomically(path: Path, value) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # Use unique temp file to avoid races between processes
    tmp_fd = None
    tmp_path = None
    try:
        tmp_dir = str(path.parent)
        # Create temp file with unique name
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=f".{os.getpid()}.{secrets.token_hex(4)}.tmp",
            dir=tmp_dir,
        )
        # Write with fsync for durability
        data = text.encode("utf-8")
        os.write(tmp_fd, data)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None
        # Windows can briefly deny a replace while another process or
        # scanner has the destination open. Retry before surfacing the error.
        for attempt in range(_SETTINGS_READ_RETRIES):
            try:
                Path(tmp_path).replace(path)
                break
            except PermissionError:
                if attempt + 1 == _SETTINGS_READ_RETRIES:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        # fsync directory on POSIX for durability (no-op on Windows but safe)
        try:
            dir_fd = os.open(tmp_dir, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def add_custom_provider(settings: dict, name: str, base_url: str, api_key: str, model: str) -> dict:
    """Add a new custom provider and return the entry. Like Kilo Code's provider add."""
    providers = _sanitize_custom_providers(settings.get("custom_providers"))
    cid = f"custom-{secrets.token_hex(4)}"
    entry = {
        "id": cid,
        "name": name.strip() or cid,
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": model.strip(),
        "models": [model.strip()] if model.strip() else [],
        "enabled": True,
        "reasoning_enabled": False,
        "vision_enabled": False,
    }
    providers.append(entry)
    settings["custom_providers"] = providers
    return entry


def remove_custom_provider(settings: dict, provider_id: str) -> bool:
    """Remove a custom provider by id. Returns True if removed."""
    providers = _sanitize_custom_providers(settings.get("custom_providers"))
    original_len = len(providers)
    providers = [p for p in providers if p["id"] != provider_id]
    settings["custom_providers"] = providers
    return len(providers) < original_len


def update_custom_provider(settings: dict, provider_id: str, **kwargs) -> bool:
    """Update a custom provider. Returns True if found."""
    providers = _sanitize_custom_providers(settings.get("custom_providers"))
    for p in providers:
        if p["id"] == provider_id:
            for k in ("name", "base_url", "api_key", "model", "models", "enabled", "reasoning_enabled", "vision_enabled"):
                if k in kwargs:
                    if k in ("enabled", "reasoning_enabled", "vision_enabled"):
                        p[k] = bool(kwargs[k])
                    elif k == "models":
                        p[k] = list(kwargs[k]) if isinstance(kwargs[k], (list, tuple)) else []
                    else:
                        p[k] = str(kwargs[k]).strip()[:512]
            settings["custom_providers"] = providers
            return True
    return False
