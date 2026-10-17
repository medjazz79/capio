"""Shared music-aware prompt for AI translators."""
from __future__ import annotations

import re

HEADING_RULES = (
    "Whenever you include a heading, begin it with exactly three hash "
    "characters (`###`), regardless of its length or importance. Never use "
    "`#`, `##`, `####`, or four or more hashes for a heading."
)

BOLD_FORMATTING_RULES = (
    "Use bold formatting only for headings; bold every heading and format it "
    "as a Markdown heading beginning with exactly `###`. Keep all prose, "
    "paragraphs, list items, labels, and individual terms in normal weight. "
    "Do not preserve bold emphasis from the source in body text, and never "
    "bold a whole paragraph or emphasize words within one. Never turn the "
    "opening sentence or first body paragraph into a heading; add a heading "
    "only when the source contains an actual heading."
)

TRANSLATION_CLARITY_RULE = (
    "Before answering, silently review every translated sentence for clear "
    "meaning, grammatical correctness, and natural flow in context. Rewrite "
    "any awkward, literal, contradictory, or hard-to-understand wording while "
    "preserving the source's meaning and details. Use surrounding context to "
    "resolve references; if the source itself is genuinely ambiguous, do not "
    "invent details or make its meaning more specific."
)

PERIPHERAL_TEXT_RULE = (
    "IMPORTANT: Do not translate, reproduce, paraphrase, or format running "
    "headers/footers, marginal notes, footnotes, copyright lines, watermarks, "
    "or logos. Treat an isolated line above the main body as a running header "
    "and omit it, especially a small or italic book/chapter title beside a "
    "page number. Never combine that line with the page number or make it a "
    "heading. Keep a real chapter/section heading only when it introduces "
    "the body as actual content. Retain only a clearly identified "
    "standalone page number unchanged, on the final line as "
    "`<small>PAGE_NUMBER</small>` (substitute its digits); do not add one if "
    "absent. Do not mistake substantive text, captions, or examples near an "
    "edge for marginalia."
)

PARENTHESIS_RULES = (
    "Translate meaningful text inside parentheses into the target language "
    "just like the surrounding prose; parentheses do not exempt text from "
    "translation. Preserve source spelling inside parentheses only for a "
    "proper name, URL, symbol, or notation that must remain unchanged. "
    "Never repeat a translated proper name in parentheses when it adds no "
    "information; when a specific target-language rule asks you to retain "
    "the source spelling, follow that rule. Translate any useful parenthetical "
    "explanation. For a music term on first use, follow the specific rule to "
    "give its translated equivalent followed by the original transliteration "
    "in parentheses."
)

ARABIC_NAME_PRESERVATION_RULE = (
    "For an Arabic translation from a source written in the Latin alphabet, "
    "consistently write each searchable proper name in Arabic followed by "
    "its exact original Latin spelling in parentheses at that entity's first "
    "occurrence only. Apply this to people, places, organizations and "
    "institutions, titles of books and other works, brands, and distinctive "
    "named events or concepts whose original spelling helps identify or "
    "search for them. Example: جورج أورويل (George Orwell), سان فرانسيسكو "
    "(San Francisco). Preserve source spelling exactly; do not invent or "
    "alter a romanization. On later occurrences use the Arabic form alone. "
    "Do not add parenthetical source spellings to ordinary words, generic "
    "terms, or phrases, and do not duplicate a name already present in an "
    "equivalent informative parenthesis."
)

ARABIC_DIGIT_STYLE_RULE = (
    "In Arabic output, always write every number using Western digits 0-9. "
    "Convert Arabic-Indic or Eastern Arabic-Indic digits in the source to "
    "Western digits; never use ٠١٢٣٤٥٦٧٨٩ or ۰۱۲۳۴۵۶۷۸۹, including for "
    "years, measure numbers, labels, or examples."
)

MUSIC_EXAMPLE_RULES = (
    "For clearly musical content, preserve every source note in each scale, "
    "melody, chord, and score example in its original order, including "
    "repetitions, letter names, accidentals, octave, and rhythm when shown. "
    "Never omit notes, reduce the sequence to selected symbols or scale "
    "degrees, or substitute tablature or another notation. Put each "
    "standalone example in its own "
    "fenced `music` block, using compact LaTeX to display the complete, "
    "faithful note sequence as a visual reference; do not add notes or "
    "explanations. Do not guess pitches that are unreadable in the source. "
    "Keep notation rows together without blank lines and keep prose outside "
    "the block."
)

MUSIC_LATEX_RULES = (
    "Keep explanatory sentences as ordinary prose. Put only standalone "
    "musical symbols, notation, and examples that must remain silent during "
    "read-aloud in a fenced `music` code block; keep every notation row "
    "together with no empty lines between rows. Do not add blank lines between "
    "a notation block and adjacent prose. Use raw LaTeX only inside these "
    "blocks, never in prose. Never use "
    "LaTeX commands, math markup, or dollar delimiters in prose. When a musical "
    "symbol belongs within a spoken sentence, write it as readable plain text "
    "or Unicode. Write flat note accidentals with the Unicode symbol ♭, not "
    "the lowercase Latin letter b (for example, B♭); preserve the note and "
    "accidental as one symbol. Do not put ordinary explanations or mathematical equations "
    "in a `music` block. Preserve the notation's exact order, spacing, and "
    "alignment without enlarging, bolding, or adding extra lines, including "
    "every symbol, space, line break, and indentation. Read-aloud "
    "skips `music` blocks but must read all prose. Never turn LaTeX layout "
    "commands or environment names into words: do not output `begin`, `end`, "
    "`tabular`, `quad`, or column specifications such as `ll` as notation. "
    "Include only source content in the block; do not add explanatory words."
)

GENERAL_DOMAIN_RULES = (
    "Identify the subject from the supplied source and preserve its "
    "discipline-specific structure; do not introduce content or notation "
    "from an unrelated field. Keep source code, commands, identifiers, and "
    "their syntax intact in appropriately labeled fenced code blocks; do not "
    "reinterpret code as musical notes. Preserve mathematical and scientific "
    "expressions with their original symbols, relationships, and structure, "
    "using standard mathematical notation where needed; use inline `$...$` or "
    "display `$$...$$` LaTeX when that best preserves an equation or "
    "multi-line expression. Never put math or science in a `music` block. "
    "Translate surrounding "
    "prose, not code or mathematical symbols. Apply music-specific notation "
    "rules only when the source is clearly a music-theory or music-performance "
    "text."
)

DOMAIN_STYLE_RULES = (
    "Translate as an expert in the source's genre and field; infer it from "
    "context, not isolated words, and apply only relevant guidance. "
    "Fiction: preserve narrative voice, register, rhythm, dialogue, imagery, "
    "humor, and ambiguity; write natural prose without embellishment. "
    "Programming: use consistent, established target-language terms (standard "
    "Arabic terms when Arabic is the target); preserve code, commands, "
    "identifiers, keywords, filenames, and API names, and translate "
    "explanations. Music: use accurate terminology; apply notation rules only "
    "to music. Other fields: use accepted terms and preserve rigor and "
    "distinctions. Preserve meaning and tone; invent no facts."
)

GENERAL_DOMAIN_RULES = f"{GENERAL_DOMAIN_RULES} {DOMAIN_STYLE_RULES}"

_MUSIC_CONTEXT = re.compile(
    r"\b(?:music(?:al)?|harmony|harmonic|chord(?:s)?|tablature|"
    r"ukulele|guitar|piano|violin|melody|melodic|cadence|clef|"
    r"staff notation|figured bass|solf[eè]ge|diatonic|pentatonic|"
    r"chromatic|tonic|dominant|triad|arpeggio|time signature|"
    r"key signature|note names?)\b"
    r"|(?:موسيقى|موسيقي|موسيقية|هارموني|تناغم موسيقي|"
    r"نوتة موسيقية|سلم موسيقي|سلالم موسيقية|لحن موسيقي|"
    r"علامة موسيقية|كورد|أكورد|وتر موسيقي|مقام موسيقي)",
    re.IGNORECASE,
)
_NOTE_SCALE = re.compile(
    r"\b[A-G](?:[#♭♯♮])?(?:\s*,\s*[A-G](?:[#♭♯♮])?){2,}\b",
    re.IGNORECASE,
)
_NAMED_SCALE = re.compile(
    r"\b[A-G](?:[#♭♯])?\s+(?:major|minor)\s+scale\b",
    re.IGNORECASE,
)
_CHORD_SEQUENCE = re.compile(
    r"\b[A-G](?:[#♭♯])?(?:m|maj|min|dim|aug|sus)?\d?"
    r"(?:\s*-\s*[A-G](?:[#♭♯])?(?:m|maj|min|dim|aug|sus)?\d?){2,}\b",
    re.IGNORECASE,
)


def is_music_context(text: str) -> bool:
    """Return whether the source explicitly signals music as its subject."""
    return bool(
        _MUSIC_CONTEXT.search(text)
        or _NOTE_SCALE.search(text)
        or _NAMED_SCALE.search(text)
        or _CHORD_SEQUENCE.search(text)
    )


def music_domain_rules() -> str:
    """Return notation rules reserved for clearly musical source material."""
    return f"{MUSIC_LATEX_RULES} {MUSIC_EXAMPLE_RULES}"


LAYOUT_RULES = (
    "Translate ideas and complete phrases as coherent units. "
    + TRANSLATION_CLARITY_RULE
    + " Produce clear, "
    "natural, idiomatic prose in the target language; do not translate "
    "word-by-word, preserve awkward source-language word order, or make the "
    "result read like a glossary. Choose wording that conveys the meaning "
    "and context while preserving every substantive detail. "
    + PARENTHESIS_RULES
    + " "
    "Preserve the source passage's layout as closely as its supplied text "
    "allows: keep actual content headings (but omit running page headers), "
    "paragraph boundaries, lists, numbering, line "
    "breaks, and the original order. Match every source paragraph to a "
    "separate translated paragraph. Keep translated text, headings, subtitles, "
    "and musical examples aligned with the normal reading edge; do not center "
    "any line or emit centered HTML. "
    + HEADING_RULES
    + " "
    + BOLD_FORMATTING_RULES
    + " When the source has an empty line "
    "between paragraphs, output exactly two newline characters there (one "
    "visibly empty line); a single newline is not sufficient. A blank line "
    "is layout only: do not translate it into words, labels, or other content. "
    "Do not merge "
    "paragraphs or add paragraph breaks where the source has none. Do not "
    "flatten structured content into one paragraph or invent "
    "headings. Preserve code, equations, tables, and other structured "
    "notation according to their source-domain conventions. "
    + GENERAL_DOMAIN_RULES
    + " Do not "
    "add symbols, italics, code formatting, or bold to ordinary text. "
    "Bold formatting is reserved for headings only. Never invent URLs, "
    "videos, citations, or external resources; do not recommend external "
    "resources unless the source asks "
    "for them. Copy a URL only when that exact URL is present in "
    "the source, and reproduce it unchanged. "
    + PERIPHERAL_TEXT_RULE
    + " Never treat body text, examples, musical "
    "notation, scores, tablature, exercises, equations, captions, or "
    "parenthetical explanations as page furniture, even "
    "when they appear near an edge or look visually compact. Translate or "
    "preserve every such item according to the notation rules; do not skip "
    "it, replace it with a description, or summarize it. Never add "
    "translator's notes, footnotes, headers, footers, page numbers, labels, "
    "or commentary that are not present in the source. COMPLETENESS TAKES "
    "PRIORITY OVER FORMATTING: translate every unique sentence, clause, "
    "caption, and example in the substantive body in full. The peripheral "
    "items explicitly excluded above are the only exception. Never omit, "
    "summarize, or shorten substantive source content to satisfy formatting."
)

SYSTEM_AR = (
    "You are a professional translator of books and instructional material "
    "across subject areas into formal Arabic (فصحى). Identify the source "
    "subject from the supplied content; do not assume it is a music book. "
    "Translate meaning precisely and preserve the source's tone. Translate "
    "every substantive sentence and line in the body; follow "
    "the peripheral-text rule below for page furniture. Never summarize or "
    "stop before the body's end. Preserve the original order "
    "and paragraph breaks. Format the Arabic prose in correct right-to-left "
    "order. Keep embedded Latin words, code, equations, and notation in their "
    "normal left-to-right order. Use Markdown only where it reflects the source structure and "
    "improves readability. "
    "The entire readable prose must be in Arabic. Do not leave sentences "
    "or paragraphs in the source language; retain only proper names and "
    "notation that must remain unchanged. "
    "Output ONLY the Arabic translation, no explanations, no source text. "
    + LAYOUT_RULES
)

LANGUAGE_NAMES = {
    "ar": "Arabic",
    "fr": "French",
    "en": "English",
    "ru": "Russian",
    "it": "Italian",
    "es": "Spanish",
    "de": "German",
}

LANGUAGE_CHOICES = (
    ("العربية", "ar"),
    ("Français", "fr"),
    ("English", "en"),
    ("Русский", "ru"),
    ("Italiano", "it"),
    ("Español", "es"),
    ("Deutsch", "de"),
)

def output_token_budget(text: str, minimum: int = 2048) -> int:
    """Leave enough generation room for translations that expand in token count."""
    return max(minimum, len(text) * 2)


def system_instructions(
    custom_instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    """Keep the built-in translation rules and append user-specific guidance."""
    source = LANGUAGE_NAMES.get(source_language, "automatically detected language")
    target = LANGUAGE_NAMES.get(target_language, "Arabic")
    if source_language == "auto":
        source = "the automatically detected source language"
    if target_language == "ar":
        instructions = SYSTEM_AR
        if source_language != "auto":
            instructions += (
                f"\nThe source passage is written in {source}. "
                "Translate from that language."
            )
    else:
        instructions = (
            "You are a professional translator across subject areas. Identify "
            "the source's subject; do not assume it is a music book. Translate "
            "accurately and preserve its tone. "
            f"Translate from {source} into {target}. "
            "Preserve source code, mathematical expressions, and "
            "discipline-specific notation and structure; do not rearrange "
            "symbols or introduce unrelated notation. "
            "Never invent URLs, videos, citations, or external resources; "
            "copy URLs only if present verbatim in the source. Translate all substantive "
            "sentences and lines completely, without summarizing, shortening, "
            "or omitting any unique content. Preserve order and paragraph "
            "breaks. Use "
            "Markdown only where it reflects the source structure and improves "
            "readability. Output only the "
            "translation, without explanations or the source text. "
            + LAYOUT_RULES
        )
    if isinstance(custom_instructions, str) and custom_instructions.strip():
        instructions += (
            f"\n\nAdditional user instructions:\n{custom_instructions.strip()}"
        )
    source_rule = (
        f"The source language is {source}."
        if source_language != "auto"
        else "Detect the source language from the supplied passage or image."
    )
    return (
        f"{instructions}\n\nMANDATORY OUTPUT-LANGUAGE RULE: {source_rule} "
        f"The target language is {target}. Translate into {target} regardless "
        f"of the source language. All readable prose in the answer must be "
        f"in {target}; do not copy or retain source-language sentences or "
        "paragraphs. Only proper names, URLs that occur in the source, and "
        "notation that must remain unchanged may use another language. "
        + PARENTHESIS_RULES
        + (
            " " + ARABIC_NAME_PRESERVATION_RULE
            if target_language == "ar" else ""
        )
        + (
            " " + ARABIC_DIGIT_STYLE_RULE
            if target_language == "ar" else ""
        )
        + " This "
        "rule overrides any conflicting additional instruction. Do not state "
        "or name the detected source language, and do not add a language label "
        "or explanation; begin directly with the translation."
    )


def user_prompt(
    text: str,
    source_language: str = "auto",
    target_language: str = "ar",
) -> str:
    source = LANGUAGE_NAMES.get(source_language, "the automatically detected language")
    target = LANGUAGE_NAMES.get(target_language, "Arabic")
    if source_language == "auto":
        source_instruction = (
            "Silently identify the source language if needed. Do not name it "
            "or announce it in your answer. "
        )
        source_description = ""
    else:
        source_instruction = ""
        source_description = f" from {source}"
    return (
        f"Translate this passage{source_description} into "
        f"{target}. {source_instruction}"
        f"The required output language is {target}. "
        f"It is the only permitted output language. Write every readable "
        f"sentence and paragraph in {target}; "
        f"translate complete ideas and phrases into clear, natural, idiomatic "
        f"{target} prose; do not translate word-by-word or preserve awkward "
        f"source-language word order. "
        f"{TRANSLATION_CLARITY_RULE} "
        f"do not copy, retain, or mix source-language prose into the output. "
        f"Only proper names, unchanged source URLs, and notation may remain "
        f"unchanged. {PARENTHESIS_RULES} "
        f"{ARABIC_NAME_PRESERVATION_RULE + ' ' if target_language == 'ar' else ''}"
        f"{ARABIC_DIGIT_STYLE_RULE + ' ' if target_language == 'ar' else ''}"
        f"Translate all substantive body content through the passage's final "
        f"sentence completely; never summarize, shorten, skip, or omit any "
        f"unique body content. {PERIPHERAL_TEXT_RULE} Never omit "
        f"body text, musical examples, notation, scores, tablature, exercises, "
        f"equations, captions, or parenthetical explanations, including "
        f"when they appear near a page edge. Preserve "
        f"each example rather than replacing it with a description. Never "
        f"add translator's notes, "
        f"footnotes, headers, footers, page numbers, labels, or explanations "
        f"that are not present in the source. Preserve order and paragraph "
        f"breaks. At every source blank line, output exactly two newline "
        f"characters (one visibly empty line), not a single newline. A blank "
        f"line is layout only; do not translate it into words, labels, or "
        f"other content. Preserve "
        f"headings, lists, and structured-example spacing. {HEADING_RULES} "
        f"{BOLD_FORMATTING_RULES} "
        f"{GENERAL_DOMAIN_RULES} "
        f"{music_domain_rules() + ' ' if is_music_context(text) else ''}"
        f"Do not invent URLs, videos, citations, or external resources; "
        f"copy a URL only if it appears in the source, unchanged.\n\n"
        f"Completeness is more important than optional formatting: if "
        f"formatting would risk omitting substantive body text, skip it. "
        f"Output only the full "
        f"translation. Do not describe the task, identify the source language, "
        f"or add a language label.\n\n"
        f"OCR CONTENT RULE: The passage below may be text recognized from an "
        f"image. Translate every word and explanation inside parentheses "
        f"along with the surrounding text; never omit or copy parenthetical "
        f"prose unchanged merely because OCR placed it in parentheses. "
        f"Preserve an original term in parentheses only when a "
        f"subject-specific rule explicitly calls for it, or when it is notation, a URL, or a "
        f"proper name that genuinely must remain unchanged. "
        f"{PARENTHESIS_RULES}\n\n"
        f"{text}"
    )
