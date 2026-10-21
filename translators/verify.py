"""Probe custom provider endpoints and cache short, actionable failures."""
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


def _readable(error: Exception) -> str:
    """Return one concise line from a provider error."""
    text = str(error).strip()
    return (text.splitlines()[0] if text else error.__class__.__name__)[:120]


def verify_custom_providers(
    settings: dict,
    timeout: int = PROBE_TIMEOUT,
) -> dict[str, str]:
    """Probe each enabled custom provider with a tiny request -> verdicts for model_status."""
    from concurrent.futures import ThreadPoolExecutor
    from . import openai_compat

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
            text = openai_compat.translate(
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


def _status(settings: dict) -> dict[str, str]:
    current = settings.get("model_status")
    if not isinstance(current, dict):
        return {}
    return {k: v for k, v in current.items() if isinstance(k, str)}
