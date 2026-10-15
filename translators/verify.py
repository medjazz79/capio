"""Check which models actually answer, and remember the answer.

The picker used to trust model *names*: anything ending in `-free` was shown
as ready, and every other `-free` id failed at translation time with 403 or
500. Names cannot tell you whether an account is entitled to a free tier.

So each model is asked directly with a tiny request, and the verdict is
stored in `settings["model_status"]`:

    ""      -> the model answered; keep it enabled
    "403 …" -> it failed with that error; show it disabled with the reason

Verdicts are cached so opening Settings stays fast, and `refresh()` re-checks
on demand. `verify_cli_models` is the only consumer today; the Zen side is
already reduced to the single measured id in `registry.ZEN_FREE`.
"""
from __future__ import annotations

# One word in, one word out. A translation-shaped probe would need a real
# sentence and would cost far more tokens to prove the same thing.
PROMPT = "Say OK"

# Long enough for a small model to answer, short enough that a dead endpoint
# fails fast instead of stalling the Settings dialog. This is the single
# definition: `translators.probe` imports it, so the Settings screen and the
# picker verdict can no longer disagree about the same model. It is the
# stricter of the two values the two callers used (25s there, 20s here).
PROBE_TIMEOUT = 25
WORKERS = 6


def _probe_one(model: str, timeout: int = PROBE_TIMEOUT) -> str:
    """-> "" when the model answered, else the error that stopped it."""
    from . import opencode_cli, opencode_compat, registry
    from .opencode_bridge import BRIDGE_FREE
    bare = model[len("opencode-cli:"):] if model.startswith("opencode-cli:") else model
    if bare in BRIDGE_FREE or registry.provider_of(model) == "bridge":
        from . import opencode_bridge
        try:
            text = opencode_bridge.translate(
                PROMPT, bare, source_language="en", target_language="en",
                timeout=timeout,
            )
        except Exception as error:
            return _readable(error)
        return "" if text.strip() else "returned an empty reply"
    if registry.provider_of(model) == "custom_provider":
        # Probe the custom OpenAI-compatible endpoint directly.
        # Note: caller must pass settings that contain the provider entry.
        # verify_cli_models is not used for custom providers; probe.py handles them.
        return "custom provider probe handled by probe.py"
    try:
        text = opencode_cli.translate(
            PROMPT, model, source_language="en", target_language="en",
            timeout=timeout,
        )
    except Exception as error:
        return _readable(error)
    return "" if text.strip() else "returned an empty reply"


def _readable(error: Exception) -> str:
    """One short line an end user can act on.

    `opencode run` exits non-zero and dumps its NDJSON event stream into the
    error, so match against the extracted message rather than the whole blob,
    and never hand the user raw JSON.
    """
    text = str(error).strip()
    reason = _message_from_event_stream(text)
    haystack = f"{reason}\n{text}".lower()

    if "freetiererror" in haystack or "free tier can only be used" in haystack:
        return "403 — free tier blocked for this account"
    if "provider.quota" in haystack or "insufficient" in haystack \
            or "balance" in haystack:
        return "no credits on this account"
    if "unregistered caller" in haystack or "unauthenticated" in haystack:
        return "needs a Google connection in OpenCode"
    if "provider.auth" in haystack or "api key" in haystack:
        return "not authorised for this account"

    fallback = reason or (text.splitlines()[0] if text else "")
    return (fallback or error.__class__.__name__)[:120]


def _message_from_event_stream(text: str) -> str:
    """Pull the real error message out of OpenCode's NDJSON failure output.

    The CLI exits 1 and prints its event stream, so a failed run can start
    with `{"type":"step_start",...}` and only later carry
    `{"type":"error",...,"error":{"message":"..."}}`. Searching for the first
    `"message":` therefore finds a progress event, not the failure. Anchor on
    the error object and take the *last* message inside it.
    """
    anchor = text.rfind('"error":{')
    if anchor >= 0:
        marker = '"message":'
        start = text.find(marker, anchor)
        if start >= 0:
            message = _quoted(text, start + len(marker))
            if message:
                return message
    # No error object: fall back to the last message in the stream.
    start = text.rfind('"message":')
    return _quoted(text, start + len('"message":')) if start >= 0 else ""


def _quoted(text: str, start: int) -> str:
    """The JSON string literal beginning at `start`, unescaped. "" if absent."""
    if start >= len(text) or text[start] != '"':
        return ""
    out: list[str] = []
    i = start + 1
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if char == '"':
            return "".join(out)
        out.append(char)
        i += 1
    return ""


def verify_custom_providers(
    settings: dict,
    timeout: int = PROBE_TIMEOUT,
) -> dict[str, str]:
    """Probe each enabled custom provider with a tiny request -> verdicts for model_status."""
    from concurrent.futures import ThreadPoolExecutor
    from . import opencode_compat

    providers = settings.get("custom_providers") if isinstance(settings.get("custom_providers"), list) else []
    targets: list[str] = []
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        cid = str(entry.get("id", "")).strip()
        if not cid:
            continue
        base = str(entry.get("base_url", "")).strip()
        key = str(entry.get("api_key", "")).strip()
        prov_model = str(entry.get("model", "")).strip()
        if not (base and key and prov_model):
            continue
        targets.append(cid)

    if not targets:
        return _status(settings)

    def _probe_custom(cid: str) -> str:
        from translators import registry as _reg
        cfg = _reg.custom_provider_config(f"custom:{cid}", settings)
        if cfg is None:
            return "provider not found"
        base, key, prov_model = cfg
        try:
            text = opencode_compat.translate(
                PROMPT, base, key, prov_model,
                source_language="en", target_language="en",
            )
        except Exception as error:
            return _readable(error)
        return "" if text.strip() else "returned an empty reply"

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(targets))) as pool:
        verdicts = list(pool.map(_probe_custom, targets))

    status = _status(settings)
    for cid, verdict in zip(targets, verdicts):
        status[f"custom:{cid}"] = verdict
    return status


def verify_cli_models(
    models: list[str],
    settings: dict,
    timeout: int = PROBE_TIMEOUT,
) -> dict[str, str]:
    """Probe each model, concurrently -> the new `model_status` mapping.

    Existing verdicts for models not in `models` are preserved, so a partial
    refresh does not forget what is already known.
    """
    from concurrent.futures import ThreadPoolExecutor

    models = [m for m in dict.fromkeys(models) if isinstance(m, str) and m]
    if not models:
        return _status(settings)

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(models))) as pool:
        verdicts = list(pool.map(lambda m: _probe_one(m, timeout), models))

    status = _status(settings)
    for model, verdict in zip(models, verdicts):
        status[f"opencode-cli:{model}"] = verdict
    return status


def _status(settings: dict) -> dict[str, str]:
    current = settings.get("model_status")
    if not isinstance(current, dict):
        return {}
    return {k: v for k, v in current.items() if isinstance(k, str)}


def refresh(settings: dict, cli_models: list[str] | None = None) -> dict[str, str]:
    """Re-check the CLI models and merge the result into `settings`."""
    models = cli_models
    if models is None:
        models = settings.get("opencode_cli_models") or []
    status = verify_cli_models(list(models), settings)
    settings["model_status"] = status
    return status
