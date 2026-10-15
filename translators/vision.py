"""Shared image preparation and prompts for multimodal translation providers."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from .prompt import (
    DOMAIN_STYLE_RULES,
    LANGUAGE_NAMES,
    PERIPHERAL_TEXT_RULE,
    TABLE_FORMATTING_RULE,
)


def encode_image(image: Image.Image) -> str:
    """Return a size-limited JPEG as an OpenAI-compatible data URL."""
    if not isinstance(image, Image.Image):
        raise TypeError("Image translation requires a PIL image")
    image = image.convert("RGB")
    image.thumbnail((3072, 3072), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def image_translation_prompt(
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    target = LANGUAGE_NAMES.get(target_language, "Arabic")
    source_description = (
        f" from {LANGUAGE_NAMES.get(source_language, source_language)}"
        if source_language != "auto"
        else ""
    )
    prompt = (
        f"Translate the complete image{source_description} into {target}. "
        "Preserve all body text, order, and examples; if all text is already "
        f"in {target}, reproduce it unchanged. Output only the translation."
    )
    return prompt


def image_system_instructions(
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    source = LANGUAGE_NAMES.get(
        source_language, "the automatically detected language")
    target = LANGUAGE_NAMES.get(target_language, "Arabic")
    source_rule = (
        f"Identify the source language in the image and translate it into "
        f"{target}."
        if source_language == "auto"
        else f"Translate from {source} into {target}."
    )
    prompt = (
        f"{source_rule} Translate all body text, actual content headings, "
        "captions, parentheticals, and examples in the image. "
        f"{PERIPHERAL_TEXT_RULE} "
        "Preserve order and paragraph breaks. Use Markdown with ### headings; "
        "bold headings only and keep prose normal. Preserve equations as LaTeX "
        "($...$ inline, $$...$$ display). Do not summarize or omit unique "
        "source text. Keep `<C>`, `<F>`, and `<G>` as "
        "literal pitch labels, not HTML. Keep code, equations, and science in "
        "their own notation; never mistake them for music. On clearly "
        "musical pages, put scores and scales/melodies "
        "in fenced `music` blocks as compact LaTeX references. "
        "Transcribe every legible note exactly and in order, retaining "
        "repeats, letter names, accidentals, octave, and rhythm. Never omit "
        "notes, give only selected pitches, replace them with scale "
        "degrees/tablature, or guess unreadable pitches. Keep prose outside "
        "blocks; read-aloud skips them. Preserve chord labels and adjacent "
        "notes. "
        f"Write all prose in {target} and output only the translation. "
        f"{DOMAIN_STYLE_RULES} "
        "Write clear, grammatical, natural prose without changing meaning "
        "or resolving genuine ambiguity."
    )
    prompt += f" {TABLE_FORMATTING_RULE}"
    if target_language == "ar":
        prompt += (
            " For Arabic translations from Latin-script sources, write each "
            "searchable proper name in Arabic followed by its exact source "
            "spelling in parentheses at first mention only; use Arabic alone "
            "later. Do not add spellings to ordinary terms or duplicate an "
            "informative parenthesis. Use Western digits 0-9 throughout."
        )
    if instructions.strip():
        prompt += f"\n\nAdditional user instructions:\n{instructions.strip()}"
    return prompt


def openai_image_messages(
    image_url: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> list[dict]:
    return [
        {
            "role": "system",
            "content": image_system_instructions(
                instructions, source_language, target_language),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": image_translation_prompt(
                        source_language, target_language),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def responses_image_input(
    image_url: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> tuple[str, list[dict]]:
    return (
        image_system_instructions(
            instructions, source_language, target_language),
        [{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": image_translation_prompt(
                        source_language, target_language),
                },
                {"type": "input_image", "image_url": image_url},
            ],
        }],
    )
