"""Conversational requests routed through the currently selected model."""
from __future__ import annotations

from collections.abc import Iterator

from . import gemini, opencode_bridge, opencode_cli, opencode_compat, opencode_zen
from . import registry
from .prompt import LANGUAGE_NAMES


_GEMINI_SEARCH_INSTRUCTIONS = (
    "\n\nGoogle Search is available. Use it whenever it can improve the "
    "accuracy or completeness of your answer, including factual details "
    "beyond the supplied passage. Clearly distinguish sourced facts from "
    "inference and cite the sources returned by the tool."
)
_GEMINI_NO_SEARCH_INSTRUCTIONS = (
    "\n\nGoogle Search is unavailable for this response. Do not claim to have "
    "searched the web or invent citations. If answering a factual question, "
    "make clear that the answer is based on model knowledge and is unverified."
)
def _is_search_quota_error(error: RuntimeError) -> bool:
    return str(error).startswith(
        "Google Search request was rejected with HTTP 429.")


def _model_messages(
    messages: list[dict[str, str]],
    source_text: str,
    translated_text: str,
    source_language: str,
    target_language: str,
    extra_instructions: str = "",
) -> tuple[str, list[dict[str, str]]]:
    target_name = LANGUAGE_NAMES.get(target_language, "Arabic")
    source_name = LANGUAGE_NAMES.get(
        source_language, "the automatically detected source language")
    system = (
        "You are the helpful reading assistant in Capio, a book and document "
        "reader. The supplied passage is from the book or document the student "
        "is reading. Answer about its content directly; do not ask which book "
        "they are reading or whether it is about music. Do not assume the "
        "passage is about music unless its content makes that clear. "
        f"Reply in {target_name}. Your response MUST use the selected target "
        "language; do not switch to English or another language unless the "
        "student explicitly requests it. Answer the student's questions conversationally "
        "and accurately. Explain only as much as needed. Keep musical notation, "
        "chord symbols, and quoted source-language terms unchanged when relevant. "
        f"The source language is {source_name}. The passage and its translation "
        "being discussed follow.\n\nOriginal passage:\n{source_text}\n\n"
        f"Translation:\n{translated_text}"
    )
    extra_instructions = extra_instructions.strip()
    if extra_instructions:
        system += (
            "\n\nAdditional instructions from the translation window. "
            "Follow them when explaining the passage and answering the student's "
            f"questions:\n{extra_instructions}"
        )
    conversation = [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    ]
    return system, conversation


def _reply_language(settings: dict) -> str:
    if settings.get("chat_reply_language_only", False):
        return str(settings.get("chat_reply_language", "ar"))
    return str(settings.get("target_language", "ar"))


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
        _reply_language(settings),
        str(settings.get("explain_instructions", "") or ""),
    )
    if not conversation or conversation[-1]["role"] != "user":
        raise ValueError("A user message is required to ask the model.")
    use_google_search = provider == "gemini"

    if provider == "gemini":
        api_key = (settings.get("gemini_key") or "").strip()
        if not api_key:
            raise RuntimeError("Chat with this Gemini model requires a Gemini API key.")
        if not use_google_search:
            return gemini.chat(conversation, api_key, model, system)
        try:
            return gemini.chat(
                conversation, api_key, model,
                system + _GEMINI_SEARCH_INSTRUCTIONS,
                use_google_search=True,
            )
        except RuntimeError as error:
            if not _is_search_quota_error(error):
                raise
            answer = gemini.chat(
                conversation,
                api_key,
                model,
                system + _GEMINI_NO_SEARCH_INSTRUCTIONS,
                use_google_search=False,
            )
            return answer
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
) -> Iterator[str]:
    """Yield reply text deltas from streaming-capable selected providers."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    provider = registry.provider_of(model)
    system, conversation = _model_messages(
        messages,
        source_text,
        translated_text,
        settings.get("source_language", "auto"),
        _reply_language(settings),
        str(settings.get("explain_instructions", "") or ""),
    )
    if not conversation or conversation[-1]["role"] != "user":
        raise ValueError("A user message is required to ask the model.")
    use_google_search = provider == "gemini"

    if provider == "gemini":
        api_key = (settings.get("gemini_key") or "").strip()
        if not api_key:
            raise RuntimeError(
                "Chat with this Gemini model requires a Gemini API key.")
        if not use_google_search:
            yield from gemini.stream_chat(
                conversation, api_key, model, system)
            return
        try:
            yield from gemini.stream_chat(
                conversation,
                api_key,
                model,
                system + _GEMINI_SEARCH_INSTRUCTIONS,
                use_google_search=True,
            )
        except RuntimeError as error:
            if not _is_search_quota_error(error):
                raise
            yield from gemini.stream_chat(
                conversation,
                api_key,
                model,
                system + _GEMINI_NO_SEARCH_INSTRUCTIONS,
                use_google_search=False,
            )
        return
    if provider == "bridge":
        yield from opencode_bridge.stream_chat(
            conversation, model, system, settings)
        return
    if provider == "opencode":
        yield from opencode_zen.stream_chat(conversation, model, system)
        return
    if provider == "opencode_cli":
        yield reply(messages, settings, source_text, translated_text)
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
            raise RuntimeError(
                "Custom provider not found — re-add it in Settings → Custom Providers.")
        base, key, provider_model = cfg
        if not base or not key or not provider_model:
            raise RuntimeError(
                "Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
        entry = registry.get_custom_provider(settings, model)
        reasoning = bool(entry.get("reasoning_enabled")) if entry else False
        yield from opencode_compat.stream_chat(
            conversation, base, key, provider_model, system,
            reasoning_enabled=reasoning)
        return

    base = (settings.get("openai_base") or "").strip()
    key = (settings.get("openai_key") or "").strip()
    if not base or not key:
        raise RuntimeError(
            "Chat with this model requires an OpenAI-compatible base URL "
            "and API key in Settings.")
    yield from opencode_compat.stream_chat(
        conversation, base, key, model, system)
