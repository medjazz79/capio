"""Conversational requests routed through the currently selected model."""
from __future__ import annotations

from . import gemini, opencode_bridge, opencode_cli, opencode_compat, opencode_zen
from . import registry
from .prompt import LANGUAGE_NAMES


def _model_messages(
    messages: list[dict[str, str]],
    source_text: str,
    translated_text: str,
    source_language: str,
    target_language: str,
    extra_instructions: str = "",
    reply_language: str | None = None,
) -> tuple[str, list[dict[str, str]]]:
    target_name = LANGUAGE_NAMES.get(target_language, "Arabic")
    source_name = LANGUAGE_NAMES.get(
        source_language, "the automatically detected source language")
    if source_text.strip() or translated_text.strip():
        system = (
            "You are a helpful assistant discussing the supplied passage. "
            f"Reply in {target_name}. Answer conversationally and accurately. "
            "Explain only as much as needed. The user may be reading about any "
            "subject; do not assume the passage or question is about music or "
            "harmony. Discuss music only when it is relevant to the passage or "
            "the user explicitly asks about it. Keep notation and quoted "
            "source-language terms unchanged when relevant. "
            f"The source language is {source_name}. The passage and its "
            "translation being discussed follow.\n\n"
            f"Original passage:\n{source_text}\n\n"
            f"Translation:\n{translated_text}"
        )
    else:
        system = (
            "You are a helpful general-purpose conversational AI assistant. "
            "The user may ask about any subject; do not assume a music-related "
            "context. Answer the user's "
            f"questions clearly and accurately. Reply in {target_name}. Follow "
            "the user's lead and explain concepts at the level they ask for. Do not "
            "assume access to a book, document, image, or other context that "
            "the user has not included in the conversation."
        )
    extra_instructions = extra_instructions.strip()
    if extra_instructions and (source_text.strip() or translated_text.strip()):
        system += (
            "\n\nAdditional instructions from the translation window. "
            "Follow them when explaining the passage and answering the user's "
            f"questions:\n{extra_instructions}"
        )
    if reply_language in LANGUAGE_NAMES:
        reply_language_name = LANGUAGE_NAMES[reply_language]
        system += (
            f"\n\nStrict response-language requirement: respond only in "
            f"{reply_language_name}. Write the entire answer, including "
            "explanations and follow-up replies, in that language. Do not switch "
            "to or add another language."
        )
    conversation = [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    ]
    return system, conversation


def reply(
    messages: list[dict[str, str]],
    settings: dict,
    source_text: str,
    translated_text: str,
) -> str:
    """Reply using the selected model; failures never silently change models."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    provider = registry.provider_of(model)
    system, conversation = _model_messages(
        messages,
        source_text,
        translated_text,
        settings.get("source_language", "auto"),
        settings.get("target_language", "ar"),
        str(settings.get("explain_instructions", "") or ""),
        (
            str(settings.get("chat_reply_language") or "")
            if settings.get("chat_reply_language_only", False) else None
        ),
    )
    if not conversation or conversation[-1]["role"] != "user":
        raise ValueError("A user message is required to ask the model.")

    if provider == "gemini":
        api_key = (settings.get("gemini_key") or "").strip()
        if not api_key:
            raise RuntimeError("Chat with this Gemini model requires a Gemini API key.")
        return gemini.chat(conversation, api_key, model, system)
    if provider == "bridge":
        return opencode_bridge.chat(conversation, model, system, settings)
    if provider == "opencode":
        return opencode_zen.chat(conversation, model, system)
    if provider == "opencode_cli":
        return opencode_cli.chat(
            conversation, model[len(registry.OPENCODE_CLI_PREFIX):], system)
    if provider in {"groq", "cf", "mistral"}:
        base, key = registry.provider_creds(provider, settings)
        if not base or not key:
            raise RuntimeError(
                f"Chat requires a {provider.upper()} API key in Settings.")
        prefix = {
            "groq": registry.GROQ_PREFIX,
            "cf": registry.CF_PREFIX,
            "mistral": registry.MISTRAL_PREFIX,
        }[provider]
        return opencode_compat.chat(
            conversation, base, key, model[len(prefix):], system)
    if provider == "custom_provider":
        cfg = registry.custom_provider_config(model, settings)
        if cfg is None:
            raise RuntimeError("Custom provider not found — re-add it in Settings → Custom Providers.")
        base, key, provider_model = cfg
        if not base or not key or not provider_model:
            raise RuntimeError("Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
        entry = registry.get_custom_provider(settings, model)
        reasoning = bool(entry.get("reasoning_enabled")) if entry else False
        return opencode_compat.chat(conversation, base, key, provider_model, system, reasoning_enabled=reasoning)

    base = (settings.get("openai_base") or "").strip()
    key = (settings.get("openai_key") or "").strip()
    if not base or not key:
        raise RuntimeError(
            "Chat with this model requires an OpenAI-compatible base URL "
            "and API key in Settings.")
    return opencode_compat.chat(conversation, base, key, model, system)


def reply_stream(
    messages: list[dict[str, str]],
    settings: dict,
    source_text: str,
    translated_text: str,
):
    """Yield reply deltas from the selected model without changing providers."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    provider = registry.provider_of(model)
    system, conversation = _model_messages(
        messages,
        source_text,
        translated_text,
        settings.get("source_language", "auto"),
        settings.get("target_language", "ar"),
        str(settings.get("explain_instructions", "") or ""),
        (
            str(settings.get("chat_reply_language") or "")
            if settings.get("chat_reply_language_only", False) else None
        ),
    )
    if not conversation or conversation[-1]["role"] != "user":
        raise ValueError("A user message is required to ask the model.")

    if provider == "gemini":
        api_key = (settings.get("gemini_key") or "").strip()
        if not api_key:
            raise RuntimeError("Chat with this Gemini model requires a Gemini API key.")
        yield from gemini.chat_stream(conversation, api_key, model, system)
        return
    if provider == "bridge":
        yield from opencode_bridge.chat_stream(
            conversation, model, system, settings)
        return
    if provider == "opencode":
        yield from opencode_zen.chat_stream(conversation, model, system)
        return
    if provider == "opencode_cli":
        yield from opencode_cli.chat_stream(
            conversation, model[len(registry.OPENCODE_CLI_PREFIX):], system)
        return
    if provider in {"groq", "cf", "mistral"}:
        base, key = registry.provider_creds(provider, settings)
        if not base or not key:
            raise RuntimeError(
                f"Chat requires a {provider.upper()} API key in Settings.")
        prefix = {
            "groq": registry.GROQ_PREFIX,
            "cf": registry.CF_PREFIX,
            "mistral": registry.MISTRAL_PREFIX,
        }[provider]
        yield from opencode_compat.stream_chat(
            conversation, base, key, model[len(prefix):], system)
        return
    if provider == "custom_provider":
        cfg = registry.custom_provider_config(model, settings)
        if cfg is None:
            raise RuntimeError("Custom provider not found — re-add it in Settings → Custom Providers.")
        base, key, provider_model = cfg
        if not base or not key or not provider_model:
            raise RuntimeError("Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
        entry = registry.get_custom_provider(settings, model)
        reasoning = bool(entry.get("reasoning_enabled")) if entry else False
        yield from opencode_compat.stream_chat(
            conversation,
            base,
            key,
            provider_model,
            system,
            reasoning_enabled=reasoning,
        )
        return

    base = (settings.get("openai_base") or "").strip()
    key = (settings.get("openai_key") or "").strip()
    if not base or not key:
        raise RuntimeError(
            "Chat with this model requires an OpenAI-compatible base URL "
            "and API key in Settings.")
    yield from opencode_compat.stream_chat(
        conversation, base, key, model, system)
