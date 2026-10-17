"""Chained translation: chosen model -> other keyed providers -> free -> glossary.

Two rules this module exists to enforce:

1. No silent truncation. `chunk_text` splits on paragraph *and* sentence
   boundaries, so a 10k-character single paragraph no longer becomes one
   oversized chunk that the model silently cuts off.
2. No silent substitution. The returned badge always says which engine
   actually produced the text, and says so explicitly when it was not the
   model the user picked.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from . import (
    fallback,
    gemini,
    mymemory,
    opencode_bridge,
    opencode_cli,
    opencode_compat,
    opencode_zen,
    registry,
)

# Larger batches reduce serial model round-trips for long page selections while
# staying comfortably below common request/context limits.
MAX_CHUNK = 4000
FULL_PAGE_CHUNK = MAX_CHUNK
_GEMMA3_4B_MODEL = re.compile(r"^gemma3:4b(?:$|[-_.])", re.IGNORECASE)
_QWEN3_VL_2B_MODEL = re.compile(
    r"^qwen3-vl:2b(?:-instruct)?(?:$|[-_.])", re.IGNORECASE)
_GEMMA3_4B_INSTRUCTIONS = (
    "Model-specific rules for Gemma 3:4B: Translate the entire supplied "
    "passage into the selected target language; do not summarize, skip, or "
    "shorten it. Include every body paragraph, sentence, parenthetical "
    "explanation, caption, and musical example. Never omit "
    "a score, chord example, tablature, exercise, or equation because it is "
    "compact, looks unusual, or appears near a page edge; follow the existing "
    "notation rules for how to render it. Omit running headers and footers, "
    "marginal notes, footnotes, copyright lines, decorative watermarks, and "
    "logos. Preserve a clearly identified standalone page number unchanged "
    "as `<small>PAGE_NUMBER</small>` on the final line. If unsure whether "
    "an isolated line at the top is a running title or header, omit it; "
    "preserve a real section heading that introduces body text. If unsure "
    "whether other text is body content or marginalia, translate it as body "
    "content. Do not "
    "add commentary or notes absent from "
    "the source. Before answering, silently check that every unique source "
    "paragraph and example is represented. Output only what the current "
    "translation mode requests; do not add unrelated commentary."
)
_QWEN3_VL_2B_INSTRUCTIONS = (
    "Model-specific rules for Qwen3-VL:2B: When the input includes an image, "
    "inspect the complete image and translate its visible source content; "
    "do not answer with an image description or translate only the most "
    "prominent text. Preserve every unique body paragraph, caption, "
    "parenthetical explanation, and musical example. Never omit "
    "notation, scores, chord examples, tablature, exercises, or equations "
    "because they are small, visually unusual, or near a page edge. Apply "
    "the existing notation instructions to those examples without inventing "
    "notes or replacing them with descriptions. Omit running headers/footers, "
    "marginal notes, footnotes, copyright lines, decorative watermarks, and "
    "logos. Preserve a clearly identified standalone page number unchanged "
    "as `<small>PAGE_NUMBER</small>` on the final line. If unsure whether "
    "an isolated line at the top is a running title or header, omit it; "
    "preserve a real section heading that introduces body text. If unsure "
    "whether other text is body content or marginalia, translate it as body "
    "content. "
    "For text-only input, translate every unique part of the supplied text "
    "under the same completeness rules. Follow the selected target language "
    "and the current translation mode."
)

# Sentence end followed by a space and a capital/quote/digit. Covers Latin
# (harmony books) without trying to be a full Unicode sentence tokenizer.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])[ \t]+(?=[\"'“‘(\[]?[A-ZÀ-Þ0-9])")
_LATEX_ARTIFACT = re.compile(
    r"(?<![A-Za-z])(?:"
    r"\\(?:begin|end)\s*\{\s*(?:array|tabular|matrix|pmatrix|bmatrix|cases)\s*\}"
    r"|(?:begin|end)\s*\{\s*(?:array|tabular|matrix|pmatrix|bmatrix|cases)\s*\}"
    r"|\\[A-Za-z]{2,}\b"
    r"|\\(?:\(|\)|\[|\])"
    r"|\\[{}%&#_$]"
    r"|\\\\(?:\s|$)"
    r")",
    re.IGNORECASE,
)
_MALFORMED_MUSIC_LATEX = re.compile(
    r"(?<![A-Za-z])(?:"
    r"\\?(?:begin|end)[ \t]*\{?[ \t]*"
    r"(?:tabular|array|matrix|pmatrix|bmatrix|cases|aligned|align|"
    r"equation|gather)[ \t]*\}?[ \t]*(?:\{[^{}\n]*\}|[lcr|]+)?"
    r"|\\?(?:qquad|quad)\b"
    r"|\\(?:hline|toprule|midrule|bottomrule)\b"
    r"|\\cline[ \t]*\{[^{}\n]*\}"
    r")",
    re.IGNORECASE,
)
_MUSIC_FENCE = re.compile(
    r"(```music[^\n]*\n)(.*?)(```)",
    re.IGNORECASE | re.DOTALL,
)
_CODE_FENCE = re.compile(r"```([^\n`]*)\n.*?```", re.DOTALL)
_FRAGMENTED_MUSIC_MIN_ROWS = 12
_MATH_DELIMITED = re.compile(r"(?<!\\)\$+[^$\n]*[=<>^_][^$\n]*\$+")
_MUSICAL_DOLLAR = re.compile(
    r"\s*(?:[A-G](?:[#♭♯♮])?(?:[_^]?\d+)?|[0-9]+[fdb])\s*",
    re.IGNORECASE,
)
_MUSICAL_NOTE = r"[A-Ga-g](?:[ \t]*(?:b|♭|#|♯|♮))*(?:[ \t]*\d+)?"
_MUSICAL_NOTE_SEQUENCE = re.compile(
    rf"(?<![A-Za-z])({_MUSICAL_NOTE}(?:[ \t]*,[ \t]*"
    rf"{_MUSICAL_NOTE}){{2,}})(?![A-Za-z])"
)
_DUPLICATE_FLAT_NOTE = re.compile(
    r"(?<![A-Za-z])([A-Ga-g])(?:[ \t]*(?:b|♭)){2,}"
)
_ASCII_FLAT_NOTE = re.compile(
    r"(?<![A-Za-z])([A-Ga-g])[ \t]*b(?=[ \t]*\d*(?:[ \t]*,|$))"
)
_LATEX_MUSIC_COMMAND = re.compile(r"\\(?:flat|sharp|natural)\b", re.IGNORECASE)
_LATEX_MUSIC_LABEL = re.compile(
    r"\b(?:[A-G]\s+)?(?:treble|bass|alto|tenor|G|F|C)\s+clef\b",
    re.IGNORECASE,
)
_LATEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ",
    "pi": "π", "sigma": "σ", "phi": "φ", "omega": "ω",
    "times": "×", "cdot": "·", "pm": "±", "le": "≤", "leq": "≤",
    "ge": "≥", "geq": "≥", "neq": "≠", "approx": "≈", "infty": "∞",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂",
    "to": "→", "rightarrow": "→", "leftarrow": "←",
    "leftrightarrow": "↔", "degree": "°", "circ": "°",
    "flat": "♭", "sharp": "♯", "natural": "♮",
}
_LATEX_SCRIPT_DIGITS = str.maketrans(
    "0123456789+-=()",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾",
)
_LATEX_SUBSCRIPT_DIGITS = str.maketrans(
    "0123456789+-=()",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎",
)
_WESTERN_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def _normalize_flat_note_sequences(text: str) -> str:
    """Render each flat note with one Unicode accidental."""
    return _MUSICAL_NOTE_SEQUENCE.sub(
        lambda sequence: _ASCII_FLAT_NOTE.sub(
            r"\1♭",
            _DUPLICATE_FLAT_NOTE.sub(r"\1♭", sequence.group(1)),
        ),
        text,
    )


def _latex_group(text: str, start: int) -> tuple[str, int] | None:
    """Read one balanced {...} group starting at `start`."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return None
    depth = 1
    for index in range(start + 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
    return None


def _replace_latex_command(
    text: str, command: str, render: Callable[..., str], arity: int = 1,
) -> str:
    pattern = re.compile(r"\\" + re.escape(command) + r"\b")
    position = 0
    while match := pattern.search(text, position):
        groups = []
        end = match.end()
        for _ in range(arity):
            group = _latex_group(text, end)
            if group is None:
                break
            value, end = group
            groups.append(value)
        if len(groups) != arity:
            position = match.end()
            continue
        replacement = render(*groups)
        text = text[:match.start()] + replacement + text[end:]
        position = match.start() + len(replacement)
    return text


def _normalize_latex_inline(text: str) -> str:
    """Convert common prose/math TeX into readable Unicode or plain text."""
    text = re.sub(r"\^\s*\\circ\b", "°", text)
    for command in (
        "text", "textrm", "textit", "textbf", "mathrm", "mathbf",
        "mathit", "operatorname", "emph",
    ):
        text = _replace_latex_command(
            text, command, _normalize_latex_inline)
    text = _replace_latex_command(
        text, "frac", lambda numerator, denominator: (
            f"({_normalize_latex_inline(numerator)})/"
            f"({_normalize_latex_inline(denominator)})"
        ), arity=2)
    text = _replace_latex_command(
        text, "sqrt", lambda content:
        f"√({_normalize_latex_inline(content)})")
    text = re.sub(
        r"\^\{([^{}]+)\}",
        lambda match: match.group(1).translate(_LATEX_SCRIPT_DIGITS),
        text,
    )
    text = re.sub(
        r"_\{([^{}]+)\}",
        lambda match: match.group(1).translate(_LATEX_SUBSCRIPT_DIGITS),
        text,
    )
    text = re.sub(r"\^([0-9+-])", lambda m: m.group(1).translate(
        _LATEX_SCRIPT_DIGITS), text)
    text = re.sub(r"_([0-9+-])", lambda m: m.group(1).translate(
        _LATEX_SUBSCRIPT_DIGITS), text)
    text = re.sub(
        r"\\([A-Za-z]+)",
        lambda match: _LATEX_SYMBOLS.get(match.group(1).lower(),
                                          match.group(1)),
        text,
    )
    text = re.sub(r"\\[()[\]]", "", text)
    text = re.sub(r"\\([{}%&#_$])", r"\1", text)
    text = re.sub(r"\\\\", " ", text)
    text = re.sub(r"[{}]", "", text)
    return text


def _normalize_latex_dollars(text: str) -> str:
    pattern = re.compile(r"(?<!\\)(\$+)(.+?)(?<!\\)\1", re.DOTALL)

    def render(match: re.Match[str]) -> str:
        content = match.group(2)
        if _MUSICAL_DOLLAR.fullmatch(content) and not _LATEX_MUSIC_COMMAND.search(
            content
        ):
            return _normalize_latex_inline(content)
        if _LATEX_MUSIC_COMMAND.search(content):
            return _normalize_latex_inline(content)
        if not _LATEX_ARTIFACT.search(content) and not re.search(
            r"[=<>^_]", content
        ):
            return match.group(0)
        return _normalize_latex_inline(content)

    return pattern.sub(render, text)


def _normalize_latex_outside_dollars(text: str) -> str:
    pattern = re.compile(r"(?<!\\)(\$+)(.+?)(?<!\\)\1", re.DOTALL)
    parts = []
    position = 0
    for match in pattern.finditer(text):
        parts.append(_normalize_latex_inline(text[position:match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_normalize_latex_inline(text[position:]))
    return "".join(parts)


def _is_fragmented_music_block(body: str) -> bool:
    rows = [line.strip() for line in body.splitlines() if line.strip()]
    return (
        len(rows) >= _FRAGMENTED_MUSIC_MIN_ROWS
        and sum(len(row) <= 4 for row in rows) / len(rows) >= 0.8
    )


def _has_latex_tablature_rows(body: str) -> bool:
    labels = re.findall(
        r"(?<![A-Za-z])([eBGDAE])\s*\|[-0-9xX.]+\|",
        body,
    )
    return len(set(labels)) >= 2


def normalize_latex_output(text: str) -> str:
    """Remove raw TeX from model output without silencing explanatory prose.

    Musical examples with TeX music markers are converted to readable fenced
    text so the speech reader skips just the example. Inline notation, prose,
    fractions, and equations stay normal text and remain available to TTS.
    """
    protected_fences: list[tuple[str, str]] = []

    def protect_code_fence(match: re.Match[str]) -> str:
        info = match.group(1).strip().split(maxsplit=1)
        language = info[0].lower() if info else ""
        if language == "music":
            return match.group(0)
        if language in {"tex", "latex"}:
            body = match.group(0)[match.group(0).find("\n") + 1:-3]
            is_music_notation = bool(
                _LATEX_MUSIC_COMMAND.search(body)
                or _LATEX_MUSIC_LABEL.search(body)
                or re.search(r"(?m)^\s*[eBGDAE]\s*\|[-0-9]", body)
                or _has_latex_tablature_rows(body)
            )
            if is_music_notation:
                return match.group(0)
        token = chr(0xE000 + len(protected_fences))
        protected_fences.append((token, match.group(0)))
        return token

    text = _CODE_FENCE.sub(protect_code_fence, text)

    has_malformed_music_latex = any(
        _MALFORMED_MUSIC_LATEX.search(match.group(2))
        for match in _MUSIC_FENCE.finditer(text)
    )
    has_fragmented_music = any(
        _is_fragmented_music_block(match.group(2))
        for match in _MUSIC_FENCE.finditer(text)
    )
    if (
        not _LATEX_ARTIFACT.search(text)
        and not _MATH_DELIMITED.search(text)
        and not has_malformed_music_latex
        and not has_fragmented_music
    ):
        normalized = _normalize_flat_note_sequences(text)
        for token, fence in protected_fences:
            normalized = normalized.replace(token, fence)
        return normalized

    def normalize_music_fence(match: re.Match[str]) -> str:
        body = match.group(2)
        if _is_fragmented_music_block(body):
            body = " ".join(
                line.strip() for line in body.splitlines() if line.strip())
        body = _MALFORMED_MUSIC_LATEX.sub("", body)
        body = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n", body)
        body = re.sub(r"[ \t]*&[ \t]*", " | ", body)
        body = _normalize_latex_inline(body)
        body = re.sub(r"[ \t]{2,}", " ", body)
        body = re.sub(r"\n[ \t]*\n+", "\n", body).strip()
        return match.group(1) + body + "\n" + match.group(3)

    text = _MUSIC_FENCE.sub(normalize_music_fence, text)

    environment = re.compile(
        r"\\begin\{\s*(array|tabular|matrix|pmatrix|bmatrix|cases|aligned|align|"
        r"equation|gather)\s*\}(?:\s*\{[^{}]*\})?(.*?)"
        r"\\end\{\s*\1\s*\}",
        re.IGNORECASE | re.DOTALL,
    )

    def render_environment(match: re.Match[str]) -> str:
        body = match.group(2)
        musical = bool(
            _LATEX_MUSIC_COMMAND.search(body) or
            _LATEX_MUSIC_LABEL.search(body) or
            _has_latex_tablature_rows(body) or
            re.search(
                r"(?m)^\s*[eBGDAE]\s*(?:&|\\mid|\\vert)|"
                r"\b[eBGDAE]\|[-0-9]",
                body,
            ))
        body = re.sub(r"\\(?:hline|toprule|midrule|bottomrule)\b", "", body)
        body = re.sub(r"\\cline\s*\{[^{}]*\}", "", body)
        rows = re.split(r"\\\\(?:\[[^\]]*\])?", body)
        rendered_rows = []
        for row in rows:
            cells = [_normalize_latex_inline(cell.strip())
                     for cell in row.split("&")]
            row_text = " | ".join(cell for cell in cells if cell)
            if row_text:
                rendered_rows.append(row_text)
        result = "\n".join(rendered_rows)
        return f"\n```music\n{result}\n```\n" if musical else result

    text = environment.sub(render_environment, text)
    text = re.sub(
        r"```(?:tex|latex)\s*```music\s*\n(.*?)\n```\s*```",
        lambda match: f"```music\n{match.group(1).strip()}\n```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"```(?:tex|latex)(?=[ \t]*\n)",
        "```music",
        text,
        flags=re.IGNORECASE,
    )
    text = _normalize_latex_dollars(text)
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "", text)
    text = re.sub(
        r"(?<!\\)\b(?:begin|end)\s*\{\s*(?:array|matrix|pmatrix|"
        r"bmatrix|cases|aligned|align|equation|gather)\s*\}", "", text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\|[clr|]+\|", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\\(?:left|right|displaystyle|textstyle|quad|qquad|hline|"
        r"toprule|midrule|bottomrule)\b", "", text)
    text = _normalize_latex_outside_dollars(text)
    parts = re.split(r"(```music\b.*?```)", text, flags=re.IGNORECASE | re.DOTALL)
    normalized = "".join(
        part if index % 2 else re.sub(r"[ \t]{2,}", " ", part)
        for index, part in enumerate(parts)
    )
    normalized = _normalize_flat_note_sequences(normalized)
    for token, fence in protected_fences:
        normalized = normalized.replace(token, fence)
    return normalized


def _normalize_latex_partial(text: str) -> str:
    # A stream may stop between the backslash and the rest of a TeX command.
    stable_text = re.sub(r"\\[A-Za-z]*$", "", text)
    return normalize_latex_output(stable_text).translate(_WESTERN_DIGITS)


def dehyphenate(text: str) -> str:
    """Join words broken by line-end hyphens (common in books/OCR): sen-\nsible -> sensible."""
    def replace_break(match: re.Match[str]) -> str:
        prefix, marker, suffix = match.groups()
        if marker == "-" and len(prefix) == 1 and prefix.isupper():
            return f"{prefix}{marker}{suffix}"
        return f"{prefix}{suffix}"

    return re.sub(
        r"(\S+)[ \t]*([_-])[ \t]*\n[ \t]*(\S)",
        replace_break,
        text,
    )


def _prepare_translation(text: str) -> str:
    """Normalize notation and digit shapes for rendering."""
    text = normalize_latex_output(text).translate(_WESTERN_DIGITS)
    parts = re.split(
        r"(```.*?```)",
        text,
        flags=re.DOTALL,
    )
    return "".join(
        part if index % 2 else _remove_nonheading_bold(part)
        for index, part in enumerate(parts)
    )


def _remove_nonheading_bold(text: str) -> str:
    """Keep bold styling on headings, but remove it from translated prose."""
    parts = re.split(r"(`+.*?`+)", text, flags=re.DOTALL)
    for part_index in range(0, len(parts), 2):
        lines = parts[part_index].splitlines(keepends=True)
        for line_index, line in enumerate(lines):
            heading = re.match(r"^(\s{0,3}#{1,6})(?:[ \t]+|$)(.*)", line)
            if heading and not _is_prose_mislabelled_as_heading(
                heading.group(2).strip()
            ):
                continue
            if re.match(r"^\s*<h[1-6]\b", line, re.IGNORECASE):
                continue
            if heading:
                line = re.sub(r"^(\s{0,3}#{1,6})[ \t]*", "", line, count=1)
            line = re.sub(
                r"\\\*\\\*(?=\S)(.+?)(?<=\S)\\\*\\\*",
                r"\1",
                line,
            )
            line = re.sub(
                r"\\_\\_(?=\S)(.+?)(?<=\S)\\_\\_",
                r"\1",
                line,
            )
            line = re.sub(
                r"\\?(\*\*|__)(?=\S)(.+?)(?<=\S)\\?\1",
                r"\2",
                line,
            )
            line = re.sub(
                r"</?(?:strong|b)\b[^>]*>",
                "",
                line,
                flags=re.IGNORECASE,
            )
            lines[line_index] = line
        prose = "".join(lines)

        def remove_bold_css(match: re.Match[str]) -> str:
            style = re.sub(
                r"(?:^|;)\s*font-weight\s*:\s*(?:bold|[6-9]00)"
                r"(?:\s*!important)?\s*(?=;|$)",
                "",
                match.group(2),
                flags=re.IGNORECASE,
            ).strip(" ;")
            if not style:
                return ""
            quote = match.group(1)
            return f" style={quote}{style}{quote}"

        parts[part_index] = re.sub(
            r"\sstyle=(['\"])(.*?)\1",
            remove_bold_css,
            prose,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return "".join(parts)


def _is_prose_mislabelled_as_heading(text: str) -> bool:
    """Reject heading markers placed before a sentence or paragraph."""
    if len(text) > 180:
        return True
    text = re.sub(r"^(?:\*\*|__)?\d+(?:\.\d+)*\.\s+", "", text, count=1)
    return bool(re.search(r"(?<=[.!?؟。])\s+\S", text))


def _split_words(para: str, limit: int) -> list[str]:
    """Last-resort split for text with no sentence punctuation at all."""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for w in para.split():
        if cur and size + len(w) + 1 > limit:
            out.append(" ".join(cur))
            cur, size = [], 0
        cur.append(w)
        size += len(w) + 1
    if cur:
        out.append(" ".join(cur))
    return out


def _split_long(para: str, limit: int) -> list[str]:
    sentences = [s for s in _SENTENCE_SPLIT.split(para) if s]
    if len(sentences) <= 1:
        return _split_words(para, limit) if para.strip() else [para]
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for s in sentences:
        if cur and size + len(s) + 1 > limit:
            out.append(" ".join(cur))
            cur, size = [], 0
        cur.append(s)
        size += len(s) + 1
    if cur:
        out.append(" ".join(cur))
    return out


def _translation_chunks(text: str, settings: dict) -> list[str]:
    limit = (
        FULL_PAGE_CHUNK
        if settings.get("_full_page_translation")
        else MAX_CHUNK
    )
    return chunk_text(text, limit=limit)


def chunk_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split into model-sized pieces. Whole paragraphs stay whole, so joining
    the result with newlines reproduces the input; only a paragraph that is
    itself over the limit gets broken up further."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n"):
        if len(para) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.extend(_split_long(para, limit))
            continue
        if size + len(para) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 1
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def translate(text: str, settings: dict) -> tuple[str, str]:
    """Returns (translated_text, engine_badge).

    A long selection is split into chunks, so several engines can answer the
    same request. The badge reports that whole picture: which engine handled
    most of the text, and whether the user's own model was used throughout.
    """
    parts: list[str] = []
    badges: list[str] = []
    if not text.strip():
        return "", "—"
    source_text = dehyphenate(text.strip())
    chunks = _translation_chunks(source_text, settings)
    for index, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        chunk_settings = settings
        if settings.get("explain_mode", False) and index < len(chunks) - 1:
            chunk_settings = {**settings, "explain_mode": False}
        arabic, badge = _translate_chunk(chunk, chunk_settings)
        parts.append(arabic)
        badges.append(badge)
    if not parts:
        return "", "—"
    from config.settings import resolve_model
    translated = "\n".join(parts).translate(_WESTERN_DIGITS)
    return translated, _merge_badges(badges, resolve_model(settings))


def translate_image(image, settings: dict) -> tuple[str, str]:
    """Send an image to the selected model's multimodal provider adapter."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    instructions = _translation_instructions(settings, model)
    translated, badge = _translate_image_once(image, settings, instructions)
    translated = _prepare_translation(translated)
    if not translated.strip():
        raise RuntimeError(f"Selected model {model} returned an empty translation.")
    return translated, badge


def _image_source_language(selected_source_language: str) -> str:
    return selected_source_language


def _translate_image_once(
    image, settings: dict, instructions: str,
) -> tuple[str, str]:
    """Make one image request, returning its translation and honest badge."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    provider = registry.provider_of(model)
    image_capability = (
        registry.custom_image_capability(model, settings)
        if provider == "custom_provider"
        else registry.image_capability(model)
    )
    if image_capability == "unsupported":
        if provider == "custom_provider":
            raise RuntimeError(
                "Image translation is disabled for this custom provider. "
                "Enable Vision / Images in Settings → Custom Providers."
            )
        if provider == "freellm":
            known = registry.FLM_MEASURED_VISION[0]
            raise RuntimeError(
                f"{model} is text-only on this FreeLLMAPI router — it rejected "
                f"an image in testing. Pick {registry.FLM_PREFIX}{known} in "
                f"Settings → Translation; it is measured to read images here."
            )
        raise RuntimeError(
            f"{model} is text-only and does not support image input. "
            "Choose a model marked 'Supports images' in Settings."
        )
    source_language = _image_source_language(
        settings.get("source_language", "auto"))
    target_language = settings.get("target_language", "ar")
    routed_image_model = ""

    if provider == "gemini":
        if not (settings.get("gemini_key") or "").strip():
            raise RuntimeError("Image translation requires a Gemini API key.")
        translated = gemini.translate_image(
            image, settings["gemini_key"], model,
            instructions=instructions,
            source_language=source_language,
            target_language=target_language,
        )
    elif provider == "bridge":
        translated = opencode_bridge.translate_image(
            image, model, instructions=instructions,
            source_language=source_language, target_language=target_language,
            settings=settings,
        )
    elif provider == "opencode":
        translated = opencode_zen.translate_image(
            image, model, instructions=instructions,
            source_language=source_language, target_language=target_language,
        )
    elif provider == "custom_provider":
        cfg = registry.custom_provider_config(model, settings)
        if cfg is None:
            raise RuntimeError("Custom provider not found — re-add it in Settings → Custom Providers.")
        base, key, provider_model = cfg
        if not base or not key or not provider_model:
            raise RuntimeError("Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
        entry = registry.get_custom_provider(settings, model)
        reasoning = bool(entry.get("reasoning_enabled")) if entry else False
        translated = opencode_compat.translate_image(
            image, base, key, provider_model,
            instructions=instructions,
            source_language=source_language,
            target_language=target_language,
            reasoning_enabled=reasoning,
        )
    elif provider == "custom":
        base = (settings.get("openai_base") or "").strip()
        key = (settings.get("openai_key") or "").strip()
        if not base or not key:
            raise RuntimeError(
                "Image translation for this model requires an OpenAI-compatible "
                "base URL and API key in Settings."
            )
        translated = opencode_compat.translate_image(
            image, base, key, model,
            instructions=instructions,
            source_language=source_language,
            target_language=target_language,
        )
    elif provider == "freellm":
        base, key = registry.provider_creds("freellm", settings)
        if not key:
            raise RuntimeError(
                "Image translation needs your FreeLLMAPI unified key in "
                "Settings (API Keys tab)."
            )
        bare = model[len(registry.FLM_PREFIX):]
        try:
            report: dict = {}
            translated = opencode_compat.translate_image(
                image, base, key, bare,
                instructions=instructions,
                source_language=source_language,
                target_language=target_language,
                report=report,
            )
            routed_image_model = str(report.get("routed_via") or "")
        except RuntimeError as error:
            raise RuntimeError(
                _explain_router_image_failure(error, model, base)
            ) from error
    elif provider in ("groq", "cf", "mistral"):
        base, key = registry.provider_creds(provider, settings)
        if not (base and key):
            raise RuntimeError(
                "Image translation needs this provider's token in Settings "
                "(API Keys tab)."
            )
        prefixes = {
            "groq": registry.GROQ_PREFIX,
            "cf": registry.CF_PREFIX,
            "mistral": registry.MISTRAL_PREFIX,
        }
        bare = model[len(prefixes[provider]):]
        translated = opencode_compat.translate_image(
            image, base, key, bare,
            instructions=instructions,
            source_language=source_language,
            target_language=target_language,
        )
    else:
        raise RuntimeError(
            "Direct image translation is not supported for OpenCode CLI models. "
            "Choose Gemini, an OpenCode bridge model, or an OpenAI-compatible "
            "vision model."
        )

    if provider == "freellm":
        return translated, (
            f"🔀 {routed_image_model or model[len(registry.FLM_PREFIX):]}")
    if provider == "custom_provider":
        label = registry.custom_provider_label(model, settings)
        return translated, f"🔧 custom: {label}"
    return translated, f"✦ chosen image: {model}"


def translate_image_stream(image, settings: dict):
    """Yield ("partial", accumulated_text, "") then ("done", text, badge)."""
    from config.settings import resolve_model

    model = resolve_model(settings)
    provider = registry.provider_of(model)
    if not settings.get("fast_mode", True) or provider not in (
        "gemini", "bridge", "freellm", "opencode", "custom", "custom_provider", "groq", "cf",
        "mistral",
    ):
        translated, badge = translate_image(image, settings)
        yield ("done", translated, badge)
        return

    instructions = _translation_instructions(settings, model)
    source_language = _image_source_language(
        settings.get("source_language", "auto"))
    target_language = settings.get("target_language", "ar")
    partial = ""
    report: dict = {}

    def stream_deltas(instructions_for_call: str):
        if provider == "gemini":
            api_key = (settings.get("gemini_key") or "").strip()
            if not api_key:
                raise RuntimeError(
                    "Image translation requires a Gemini API key.")
            return gemini.stream_translate_image(
                image, api_key, model, instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language,
            )
        if provider == "bridge":
            return opencode_bridge.stream_image(
                image, model, instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language, settings=settings,
            )
        if provider == "opencode":
            return opencode_zen.stream_translate_image(
                image, model, instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language,
            )
        if provider == "freellm":
            base, key = registry.provider_creds("freellm", settings)
            if not key:
                raise RuntimeError(
                    "Image translation needs your FreeLLMAPI unified key in "
                    "Settings (API Keys tab).")
            return opencode_compat.stream_translate_image(
                image, base, key, model[len(registry.FLM_PREFIX):],
                instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language, report=report,
            )
        if provider == "custom_provider":
            cfg = registry.custom_provider_config(model, settings)
            if cfg is None:
                raise RuntimeError("Custom provider not found — re-add it in Settings → Custom Providers.")
            base, key, provider_model = cfg
            if not base or not key or not provider_model:
                raise RuntimeError("Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
            entry = registry.get_custom_provider(settings, model)
            reasoning = bool(entry.get("reasoning_enabled")) if entry else False
            return opencode_compat.stream_translate_image(
                image, base, key, provider_model, instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language,
                reasoning_enabled=reasoning,
                report=report,
            )
        if provider == "custom":
            base = (settings.get("openai_base") or "").strip()
            key = (settings.get("openai_key") or "").strip()
            if not base or not key:
                raise RuntimeError(
                    "Image translation for this model requires an "
                    "OpenAI-compatible base URL and API key in Settings.")
            return opencode_compat.stream_translate_image(
                image, base, key, model, instructions=instructions_for_call,
                source_language=source_language,
                target_language=target_language,
                report=report,
            )
        base, key = registry.provider_creds(provider, settings)
        if not (base and key):
            raise RuntimeError(
                "Image translation needs this provider's token in Settings "
                "(API Keys tab).")
        prefix = {
            "groq": registry.GROQ_PREFIX,
            "cf": registry.CF_PREFIX,
            "mistral": registry.MISTRAL_PREFIX,
        }[provider]
        return opencode_compat.stream_translate_image(
            image, base, key, model[len(prefix):],
            instructions=instructions_for_call,
            source_language=source_language,
            target_language=target_language,
            report=report,
        )

    try:
        for delta in stream_deltas(instructions):
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        if not partial.strip():
            raise RuntimeError(f"Selected model {model} streamed no image text.")

        translated = _prepare_translation(partial.strip())
        if not translated.strip():
            raise RuntimeError(
                f"Selected model {model} returned an empty translation.")
    except Exception as error:
        raise RuntimeError(
            f"Image translation stream failed for {model}; no second "
            f"request was made: {error}"
        ) from error

    if provider == "freellm":
        badge = (
            f"🔀 {report.get('routed_via') or model[len(registry.FLM_PREFIX):]}")
    elif provider == "custom_provider":
        badge = f"🔧 custom: {registry.custom_provider_label(model, settings)}"
    else:
        badge = f"✦ chosen image: {model}"
    # An image costs more than the text it produces, so saying what it cost
    # matters here at least as much as it does in the OCR path.
    provider_name = {
        "gemini": "gemini", "bridge": "bridge", "opencode": "zen",
        "freellm": "freellm", "custom_provider": "custom",
        "custom": "custom", "groq": "groq", "cf": "cf", "mistral": "mistral",
    }.get(provider, provider)
    _flush_usage()
    yield ("done", translated,
           badge + _token_suffix(report, model, provider_name))


def _explain_router_image_failure(error: Exception, model: str,
                                 base: str) -> str:
    """Turn a gateway image failure into something the user can act on.

    The raw upstream sentences are kept because they carry the two facts a
    generic message throws away: *why* it failed and *when* it clears. A 429
    from FreeLLMAPI names the cause in the same breath as the reset time
    ("6 model lacks vision ... Soonest reset ~23m"), and "check your provider
    quota" is actively misleading when the real problem is that no vision model
    is enabled at all.
    """
    raw = str(error)
    known = registry.FLM_MEASURED_VISION[0]
    suggestion = f"{registry.FLM_PREFIX}{known}"
    hint = (
        f" This FreeLLMAPI setup has no vision model it can serve right now. "
        f"Pick {suggestion} in Settings → Translation; it is the fastest one "
        f"measured to read an image here."
    )
    if "lacks vision" in raw or "routing_exhausted" in raw:
        return raw + hint
    if "502" in raw or "upstream_error" in raw:
        return (
            f"{raw}\n\nThe router could not serve this image: it is a routing "
            f"strategy, so which model answers depends on what is available. "
            f"Pick {suggestion} for images — measured working."
        )
    if "429" in raw:
        return raw + hint
    return raw


def translate_stream(text: str, settings: dict):
    """Yield ("partial", accumulated_text, "") then ("done", full, badge).

    Supported model adapters stream each chunk as it is generated. Providers
    without a streaming API still emit completed chunks through this contract.
    """
    from config.settings import resolve_model

    if not text.strip():
        yield ("done", "", "—")
        return
    text = dehyphenate(text.strip())
    if not settings.get("fast_mode", True):
        # Classic behavior: spinner until the whole reply is ready.
        arabic, badge = translate(text, settings)
        yield ("done", arabic, badge)
        return
    chunks = _translation_chunks(text, settings)
    chosen = resolve_model(settings)
    provider = registry.provider_of(chosen)
    streaming_providers = {
        "gemini", "bridge", "freellm", "opencode", "opencode_cli", "custom_provider",
        "custom", "groq", "cf", "mistral",
    }
    if provider in streaming_providers:
        parts: list[str] = []
        badges: list[str] = []
        for index, chunk in enumerate(chunks):
            chunk_settings = settings
            if settings.get("explain_mode", False) and index < len(chunks) - 1:
                chunk_settings = {**settings, "explain_mode": False}
            streamer = {
                "gemini": _stream_single_gemini,
                "bridge": _stream_single_bridge,
                "freellm": _stream_single_router,
                "custom_provider": _stream_single_custom_provider,
            }.get(provider, _stream_single_compatible)
            current = ""
            for kind, value, badge in streamer(chunk, chunk_settings):
                if kind == "partial":
                    current = value
                    yield (
                        "partial",
                        "\n".join([*parts, current]).translate(_WESTERN_DIGITS),
                        "",
                    )
                elif kind == "done":
                    current = value
                    from .prompt import LANGUAGE_NAMES

                    target_language = chunk_settings.get(
                        "target_language", "ar")
                    source_language = chunk_settings.get(
                        "source_language", "auto")
                    detected_language = _detect_translation_language(value)
                    if (
                        target_language in LANGUAGE_NAMES
                        and source_language != target_language
                        and detected_language
                        and detected_language != target_language
                    ):
                        current, badge = _translate_chunk(
                            chunk, chunk_settings)
                        detected_language = _detect_translation_language(
                            current)
                        if (
                            detected_language
                            and detected_language != target_language
                        ):
                            target_name = LANGUAGE_NAMES[target_language]
                            raise RuntimeError(
                                "Streaming translation returned "
                                f"{detected_language} instead of "
                                f"{target_name}."
                            )
                        yield (
                            "partial",
                            "\n".join([*parts, current]).translate(
                                _WESTERN_DIGITS),
                            "",
                        )
                    parts.append(current)
                    badges.append(badge)
        yield (
            "done",
            "\n".join(parts).translate(_WESTERN_DIGITS),
            _merge_badges(badges, chosen),
        )
        return
    parts: list[str] = []
    badges: list[str] = []
    for index, chunk in enumerate(chunks):
        chunk_settings = settings
        if settings.get("explain_mode", False) and index < len(chunks) - 1:
            chunk_settings = {**settings, "explain_mode": False}
        arabic, badge = _translate_chunk(chunk, chunk_settings)
        parts.append(arabic)
        badges.append(badge)
        yield ("partial", "\n".join(parts).translate(_WESTERN_DIGITS), "")
    yield (
        "done",
        "\n".join(parts).translate(_WESTERN_DIGITS),
        _merge_badges(badges, chosen),
    )


def _stream_single_compatible(chunk: str, s: dict):
    """Stream OpenAI-compatible, Zen, and OpenCode CLI text requests."""
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    provider = registry.provider_of(chosen)
    instructions = _translation_instructions(s, chosen)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"

    if provider == "opencode":
        deltas = opencode_zen.stream_translate(
            chunk, chosen, instructions=instructions,
            source_language=source_language, target_language=target_language)
        badge = f"◈ {chosen.replace('-free', '')}"
    elif provider == "opencode_cli":
        cli_model = chosen[len(registry.OPENCODE_CLI_PREFIX):]
        deltas = opencode_cli.stream_translate(
            chunk, cli_model, instructions=instructions,
            source_language=source_language, target_language=target_language)
        badge = f"◈ chosen: OpenCode CLI ({cli_model})"
    else:
        if provider == "custom":
            base = (s.get("openai_base") or "").strip()
            key = (s.get("openai_key") or "").strip()
            if not base or not key:
                deltas = iter(())
                badge = f"◈ chosen: {chosen}"
            else:
                bare = chosen
                deltas = opencode_compat.stream_translate(
                    chunk, base, key, bare, instructions=instructions,
                    source_language=source_language,
                    target_language=target_language)
                badge = f"◈ chosen: {chosen}"
        else:
            base, key = registry.provider_creds(provider, s)
            prefixes = {
                "groq": registry.GROQ_PREFIX,
                "cf": registry.CF_PREFIX,
                "mistral": registry.MISTRAL_PREFIX,
            }
            bare = chosen[len(prefixes[provider]):]
            marks = {"groq": "⚡ groq", "cf": "☁ cf",
                     "mistral": "🌀 mistral"}
            deltas = opencode_compat.stream_translate(
                chunk, base, key, bare, instructions=instructions,
                source_language=source_language,
                target_language=target_language)
            badge = f"{marks[provider]}: {bare}"

    partial = ""
    try:
        for delta in deltas:
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        if not partial.strip():
            raise RuntimeError(f"{chosen} streamed no translation text.")
        final = _prepare_translation(partial.strip())
    except Exception as error:
        translated, fallback_badge = _translate_free_fallback(
            chunk, s, chosen, str(error)[:80])
        yield ("partial", translated, "")
        yield ("done", translated, fallback_badge)
        return
    yield ("done", final, badge)


def _stream_single_gemini(chunk: str, s: dict):
    """Stream the chosen Gemini model and normalize its output for display."""
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    instructions = _translation_instructions(s, chosen)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"
    partial = ""
    try:
        for delta in gemini.stream_translate(
            chunk, s["gemini_key"], chosen,
            instructions=instructions,
            source_language=source_language,
            target_language=target_language,
        ):
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        if not partial.strip():
            raise RuntimeError(f"Gemini {chosen} streamed no text")

        final = _prepare_translation(partial.strip())
    except Exception:
        translated, badge = _translate_chunk(chunk, s)
        yield ("partial", translated, "")
        yield ("done", translated, badge)
        return
    yield ("done", final, f"✦ chosen: {chosen}{_token_suffix({}, chosen, 'gemini')}")
    _flush_usage()


def _translation_instructions(settings: dict, model: str = "") -> str:
    instructions = str(settings.get("model_instructions", "") or "").strip()
    if settings.get("target_language", "ar") == "ar":
        if settings.get("arabic_tashkeel", False):
            tashkeel_instruction = (
                "Arabic tashkeel is enabled: write the complete Arabic "
                "translation with accurate diacritical marks on all Arabic "
                "words, including vowel marks, shadda, sukun, and tanween "
                "wherever grammatically appropriate. Preserve the intended "
                "meaning and do not add marks to Latin text, names, URLs, or "
                "notation. This setting takes precedence over conflicting "
                "formatting requests."
            )
        else:
            tashkeel_instruction = (
                "Arabic tashkeel is disabled: write Arabic text without "
                "diacritical marks (including short vowels, shadda, sukun, "
                "and tanween). Preserve normal Arabic spelling, hamza letters, "
                "and punctuation. This setting takes precedence over "
                "conflicting formatting requests."
            )
        instructions = "\n\n".join(
            part for part in (instructions, tashkeel_instruction) if part
        )
    if not settings.get("explain_mode", False):
        return _add_model_specific_instructions(settings, model, instructions)

    from .prompt import LANGUAGE_NAMES

    target_language = settings.get("target_language", "ar")
    target_name = LANGUAGE_NAMES.get(target_language, "Arabic")
    heading = {
        "ar": "الشرح",
        "en": "Explanation",
        "fr": "Explication",
        "ru": "Пояснение",
        "it": "Spiegazione",
        "es": "Explicación",
        "de": "Erläuterung",
    }.get(target_language, "Explanation")
    explanation = (
        "Explain mode is enabled and overrides any instruction to output only "
        "the translation for this request. First give the complete translation. "
        f"Immediately after it, add a separate Markdown heading exactly "
        f"`### {heading}`, followed by one concise paragraph explaining the "
        f"meaning of the translated passage in {target_name}. The explanation "
        "must clarify the ideas; do not repeat the translation, add unrelated "
        "information, or use another language."
    )
    explain_instructions = str(
        settings.get("explain_instructions", "") or ""
    ).strip()
    if explain_instructions:
        explanation += (
            "\n\nAdditional instructions for the explanation only:\n"
            f"{explain_instructions}"
        )
    instructions = f"{instructions}\n\n{explanation}".strip()
    return _add_model_specific_instructions(settings, model, instructions)


def _add_model_specific_instructions(
    settings: dict, model: str, instructions: str,
) -> str:
    model_name = (model or "").strip()
    if registry.provider_of(model_name) == "custom_provider":
        config = registry.custom_provider_config(model_name, settings)
        model_name = config[2] if config else ""
    model_instructions = []
    if _GEMMA3_4B_MODEL.match(model_name):
        model_instructions.append(_GEMMA3_4B_INSTRUCTIONS)
    if _QWEN3_VL_2B_MODEL.match(model_name):
        model_instructions.append(_QWEN3_VL_2B_INSTRUCTIONS)
    return "\n\n".join(
        part for part in (instructions, *model_instructions) if part
    )


def _stream_single_bridge(chunk: str, s: dict):
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    chosen_short = chosen.replace("-free", "")
    instructions = _translation_instructions(s, chosen)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"
    partial = ""
    try:
        for delta in opencode_bridge.stream(
            chunk, chosen, instructions=instructions,
            source_language=source_language, target_language=target_language,
            settings=s,
        ):
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        final = partial.strip()
        if not final:
            raise RuntimeError(f"Bridge {chosen} streamed no text")
        final = _prepare_translation(final)
    except Exception as error:
        arabic, badge = _translate_free_fallback(
            chunk, s, chosen, str(error)[:80])
        yield ("partial", arabic, "")
        yield ("done", arabic, badge)
        return
    yield ("done", final, f"◈ chosen bridge: {chosen_short}")


def _stream_single_router(chunk: str, s: dict):
    """Stream a FreeLLMAPI request so the first words show up in ~1s.

    A transport or provider failure falls back to the regular translation
    path, which can use the configured free-service alternatives.
    """
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    bare = chosen[len(registry.FLM_PREFIX):]
    base, key = registry.provider_creds("freellm", s)
    instructions = _translation_instructions(s, chosen)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"

    def _fallback():
        arabic, badge = _translate_chunk(chunk, s)
        yield ("partial", arabic, "")
        yield ("done", arabic, badge)

    if not key:
        yield from _fallback()
        return
    partial = ""
    report: dict = {}
    try:
        for delta in opencode_compat.stream_translate(
            chunk, base, key, bare, instructions=instructions,
            source_language=source_language, target_language=target_language,
            report=report,
        ):
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        final = partial.strip()
        if not final:
            raise RuntimeError("Router streamed no text")
        final = _prepare_translation(final)
    except Exception:
        # Includes the router being unreachable: a connection error mid-stream
        # must not leave the popup spinning forever.
        yield from _fallback()
        return
    # Name the engine the gateway actually used, never the pseudo-model alone.
    via = str(report.get("routed_via") or "")
    fell_over = str(report.get("fallback") or "")
    badge = f"🔀 {via}" if via else f"🔀 flm: {bare}"
    if fell_over:
        badge += f"  ({fell_over})"
    yield ("done", final, badge)


_TOKEN_SUFFIX_MARK = "  ·  "
_TOKEN_SUFFIX_TAIL = " tok ("


def _split_token_suffix(badge: str) -> tuple[str, dict]:
    """Peel the `  ·  N tok (...)` tail off a badge, returning core + totals.

    A page is split into chunks and each is its own request, so the badge
    arriving here carries only one chunk's share. They are added up before
    anything is displayed.
    """
    marker = badge.find(_TOKEN_SUFFIX_TAIL)
    if marker < 0:
        return badge, {}
    tail = badge[marker:]
    close = tail.rfind(")")
    if close < 0:
        return badge, {}
    head, numbers = badge[:marker], tail[marker + len(_TOKEN_SUFFIX_MARK):close]
    totals: dict = {}
    for field in ("in", "out", "reasoning", "cached"):
        found = re.search(rf"([\d,]+) {field}\b", numbers)
        if found:
            totals[field] = int(found.group(1).replace(",", ""))
    return head, totals


def _flush_usage() -> None:
    """Persist the token ledger after a translation.

    Throttled inside `usage.save`, so a page split into many chunks writes
    once rather than once per chunk.
    """
    from . import usage

    usage.save()


def _token_suffix(report: dict, model: str = "", provider: str = "") -> str:
    """`  ·  1,234 tok (820 in / 414 out)` appended to a badge, when known.

    Endpoints that never send `usage` simply leave the report empty and the
    badge reads exactly as it did before. `model` is the fallback lookup: the
    OpenAI-compatible streams hand their counts over in `report`, while
    Gemini's badge is built after a generator that only yields deltas, so
    its numbers are read back from the ledger instead.
    """
    totals = report.get("tokens") if isinstance(report, dict) else None
    if not isinstance(totals, dict) or not totals:
        if not model:
            return ""
        from . import usage

        totals = usage.last(model, provider)
    if not totals:
        return ""
    # Two spellings reach here: the OpenAI-compatible streams report through
    # `report` as prompt/completion, while `usage.last()` uses in/out. Read
    # both rather than making every future caller remember which is which.
    return _format_token_suffix({
        "in": int(totals.get("prompt") or totals.get("in") or 0),
        "out": int(totals.get("completion") or totals.get("out") or 0),
        "reasoning": int(totals.get("reasoning") or 0),
        "cached": int(totals.get("cached") or 0),
    })


def _format_token_suffix(totals: dict) -> str:
    if not totals:
        return ""
    prompt = int(totals.get("in") or 0)
    completion = int(totals.get("out") or 0)
    total = prompt + completion
    if total <= 0:
        return ""
    parts = [f"{prompt:,} in", f"{completion:,} out"]
    if totals.get("reasoning"):
        parts.append(f"{int(totals['reasoning']):,} reasoning")
    if totals.get("cached"):
        parts.append(f"{int(totals['cached']):,} cached")
    return f"{_TOKEN_SUFFIX_MARK}{total:,} tok ({' / '.join(parts)})"


def _stream_single_custom_provider(chunk: str, s: dict):
    """Stream a Custom Provider request (Kilo Code style)."""
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    cfg = registry.custom_provider_config(chosen, s)
    if cfg is None:
        arabic, badge = _translate_chunk(chunk, s)
        yield ("partial", arabic, "")
        yield ("done", arabic, badge)
        return
    base, key, provider_model = cfg
    if not base or not key or not provider_model:
        arabic, badge = _translate_chunk(chunk, s)
        yield ("partial", arabic, "")
        yield ("done", arabic, badge)
        return
    instructions = _translation_instructions(s, provider_model)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"
    partial = ""
    entry = registry.get_custom_provider(s, chosen)
    reasoning = bool(entry.get("reasoning_enabled")) if entry else False
    usage_report: dict = {}
    try:
        for delta in opencode_compat.stream_translate(
            chunk, base, key, provider_model, instructions=instructions,
            source_language=source_language, target_language=target_language,
            reasoning_enabled=reasoning,
            report=usage_report,
        ):
            partial += delta
            yield ("partial", _normalize_latex_partial(partial), "")
        final = partial.strip()
        if not final:
            raise RuntimeError("Custom provider streamed no text")
        final = _prepare_translation(final)
    except Exception:
        arabic, badge = _translate_chunk(chunk, s)
        yield ("partial", arabic, "")
        yield ("done", arabic, badge)
        return
    label = registry.custom_provider_label(chosen, s)
    yield ("done", final, f"🔧 custom: {label}{_token_suffix(usage_report)}")


def _merge_badges(badges: list[str], chosen: str = "") -> str:
    """One honest line for a whole selection, with its token total added on.

    A page is split into chunks and each is its own request, so each badge
    carries only one chunk's share of the tokens. Those are summed and the
    total is appended once, to whichever engine line the merge below picks.
    The ledger is flushed here so the running totals survive a restart.
    """
    cores: list[str] = []
    totals: dict = {}
    for badge in badges:
        core, chunk_totals = _split_token_suffix(badge)
        cores.append(core)
        for field, value in chunk_totals.items():
            totals[field] = totals.get(field, 0) + value
    _flush_usage()
    return _merge_engine_badges(cores, chosen) + _format_token_suffix(totals)


def _split_token_suffix(badge: str) -> tuple[str, dict]:
    """Peel the `  ·  1,234 tok (820 in / 414 out)` tail off a badge.

    Returns the badge without the tail plus the counts it carried. A badge
    with no tail comes back unchanged with empty counts.
    """
    marker = badge.rfind(_TOKEN_SUFFIX_MARK)
    if marker < 0:
        return badge, {}
    tail = badge[marker + len(_TOKEN_SUFFIX_MARK):]
    if not tail.startswith(tail.split(" ")[0]) or " tok (" not in tail:
        return badge, {}
    head, numbers = badge[:marker], tail[marker + len(_TOKEN_SUFFIX_MARK):]
    open_paren = numbers.find("(")
    if open_paren < 0:
        return badge, {}
    numbers = numbers[open_paren + 1:].rstrip().rstrip(")")
    totals: dict = {}
    for field in ("in", "out", "reasoning", "cached"):
        found = re.search(rf"([\d,]+)\s+{field}\b", numbers)
        if found:
            totals[field] = int(found.group(1).replace(",", ""))
    if not totals:
        return badge, {}
    return head, totals


def _merge_engine_badges(badges: list[str], chosen: str = "") -> str:
    """One honest line for a whole selection, not just its last chunk.

    A page split into 8 chunks can be answered by your model for 6 and by a
    substitute for 2. Reporting only the last chunk either hid that or cried
    wolf, so the badge now states which engine did the most work and whether
    your own model was used throughout.

    `chosen` is the model `resolve_model` settled on. It is compared against the
    *engine each badge names*, structurally: a page answered by the bridge or by
    the user's own router is their model answering, even though neither badge
    contains the word "chosen" — the two shapes that made the old
    `"chosen:" in badge` test report a false "(not your model)".
    """
    badges = [b for b in badges if b and b != "—"]
    if not badges:
        return "—"
    if len(set(badges)) == 1:
        # Every chunk behaved the same (the usual case, and a single chunk):
        # keep the original badge so its reason survives.
        return badges[0]

    chosen_provider = registry.provider_of(chosen) if chosen else ""
    counts: dict[str, int] = {}
    used_yours = 0
    for badge in badges:
        engine, marked, via_router = _badge_engine(badge)
        if _is_chosen_engine(engine, marked, via_router, chosen,
                             chosen_provider):
            used_yours += 1
        counts[engine] = counts.get(engine, 0) + 1
    top = max(counts, key=lambda k: counts[k])
    total = len(badges)

    if used_yours == total:
        return f"✦ {top}"
    if used_yours:
        return f"⚠ {top}  ({used_yours}/{total} chunks by your model)"
    return f"⚠ {top}  (not your model)"


# A badge is a small grammar, not prose, and `_merge_badges` reads that shape
# rather than searching the text for a word:
#
#   "<mark> chosen: <model>"           the model the user picked answered
#   "<mark> chosen bridge: <model>"    ditto, through the streaming bridge
#   "<mark> <engine>"                  a directly-keyed provider, or the router
#   "<substitute>  ⚠ <reason>"         something else answered, and why
#
# The reason after the second ⚠ is prose and is never read as an engine, so
# "⚠ chosen model X failed: …" cannot be mistaken for proof that X answered.
_BADGE_MARKS = "✦◈🔀⚡☁🌀🔧🌐"
_CHOSEN_HEADS = ("chosen:", "chosen bridge:")


def _badge_engine(badge: str) -> tuple[str, bool, bool]:
    """-> (engine named, carries the "chosen" marker, named by the router)."""
    head = badge.partition("⚠")[0].strip()
    bare = head.lstrip(_BADGE_MARKS + " ").strip()
    marked = bare.startswith(_CHOSEN_HEADS)
    via_router = head.startswith("🔀")
    via_custom = head.startswith("🔧")
    if marked:
        bare = re.sub(r"^chosen(?:\s+bridge)?:\s*", "", bare).strip()
    # For custom: "🔧 custom: My API (gpt-4o)" -> mark as chosen
    if via_custom:
        return bare, True, via_router
    return bare, marked, via_router


def _same_model(engine: str, model: str) -> bool:
    """The same id, ignoring the space a provider badge puts after its mark."""
    return bool(model) and engine.replace(" ", "") == model.replace(" ", "")


def _is_chosen_engine(engine: str, marked: bool, via_router: bool,
                      chosen: str, chosen_provider: str) -> bool:
    """Did the engine the user picked answer this chunk?"""
    if not chosen:
        # No resolved model to compare against (a direct `_merge_badges` call).
        # Fall back to the badge's own marker; every production caller passes
        # the model, so this is the legacy path only.
        return marked
    if via_router:
        # The router is the user's own engine and `X-Routed-Via` names who
        # served it, so the pseudo-model the user typed is not in the badge.
        return chosen_provider == "freellm"
    if chosen_provider == "custom_provider":
        return marked
    if chosen_provider == "google_translate":
        return marked and engine.casefold() == "google translate"
    if not engine:
        return False
    if _same_model(engine, chosen) or _same_model(
            engine, chosen.replace("-free", "")):
        return True
    if chosen_provider == "opencode_cli":
        return chosen[len(registry.OPENCODE_CLI_PREFIX):] in engine
    return False


def _translate_free_fallback(
    chunk: str, s: dict, chosen: str, failure: str,
) -> tuple[str, str]:
    from .prompt import LANGUAGE_NAMES

    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"
    note = f"⚠ chosen model {chosen} failed: {failure}" if failure else \
           f"⚠ {chosen} could not run"
    failures: list[tuple[str, str]] = (
        [("selected translation engine", failure)] if failure else []
    )
    if registry.provider_of(chosen) != "google_translate":
        try:
            result = fallback.gtx_translate(
                chunk, source_language, target_language)
            return result, f"Free service  {note}"
        except Exception as error:
            failures.append(("free translation service", _one_line(error)))
    try:
        result = mymemory.translate(
            chunk,
            email=s.get("email", ""),
            source_language=source_language,
            target_language=target_language,
        )
        return result, f"Free service (alt)  {note}"
    except Exception as error:
        failures.append(("alternate free translation", _one_line(error)))
    if target_language == "ar":
        try:
            return fallback.glossary_partial(chunk), f"Offline glossary  {note}"
        except Exception as error:
            failures.append(("offline glossary", _one_line(error)))
    detail = "".join(
        f" The {name} said: {reason}." for name, reason in failures)
    raise RuntimeError(
        f"Could not translate into {LANGUAGE_NAMES[target_language]}. "
        "Check your internet connection or configure a translation model."
        f"{detail}"
    )


def _translate_chunk(chunk: str, s: dict) -> tuple[str, str]:
    from config.settings import resolve_model
    from .prompt import LANGUAGE_NAMES

    chosen = resolve_model(s)
    provider = registry.provider_of(chosen)
    chosen_short = chosen.replace("-free", "")
    instructions = _translation_instructions(s)
    source_language = s.get("source_language", "auto")
    target_language = s.get("target_language", "ar")
    if source_language != "auto" and source_language not in LANGUAGE_NAMES:
        source_language = "auto"
    if target_language not in LANGUAGE_NAMES:
        target_language = "ar"

    def _call(model: str, extra_instructions: str = "") -> tuple[str, str]:
        instructions = _translation_instructions(s, model)
        if extra_instructions:
            instructions = "\n\n".join(
                part for part in (instructions, extra_instructions) if part)
        if registry.provider_of(model) == "google_translate":
            return fallback.gtx_translate(
                chunk, source_language, target_language,
            ), "🌐 chosen: Google Translate"
        if registry.provider_of(model) == "gemini":
            return gemini.translate(
                chunk, s["gemini_key"], model, instructions=instructions,
                source_language=source_language, target_language=target_language,
            ), f"✦ {model}"
        if registry.provider_of(model) == "bridge":
            return opencode_bridge.translate(
                chunk, model, instructions=instructions,
                source_language=source_language,
                target_language=target_language,
                settings=s,
            ), f"◈ bridge: {model}"
        if registry.provider_of(model) == "freellm":
            base, key = registry.provider_creds("freellm", s)
            bare = model[len(registry.FLM_PREFIX):]
            out, via = opencode_compat.translate_with_engine(
                chunk, base, key, bare, instructions=instructions,
                source_language=source_language,
                target_language=target_language,
            )
            return out, f"🔀 {via}" if via else f"🔀 flm: {bare}"
        if registry.provider_of(model) == "custom_provider":
            cfg = registry.custom_provider_config(model, s)
            if cfg is None:
                raise RuntimeError("Custom provider not found — re-add it in Settings → Custom Providers.")
            base, key, provider_model = cfg
            if not base or not key or not provider_model:
                raise RuntimeError("Custom provider is incomplete — fill Base URL, API Key and Model in Settings.")
            entry = registry.get_custom_provider(s, model)
            reasoning = bool(entry.get("reasoning_enabled")) if entry else False
            return opencode_compat.translate(
                chunk, base, key, provider_model, instructions=instructions,
                source_language=source_language, target_language=target_language,
                reasoning_enabled=reasoning,
            ), f"🔧 custom: {registry.custom_provider_label(model, s)}"
        if registry.provider_of(model) in ("groq", "cf", "mistral"):
            base, key = registry.provider_creds(
                registry.provider_of(model), s)
            prefix = {"groq": registry.GROQ_PREFIX,
                      "cf": registry.CF_PREFIX,
                      "mistral": registry.MISTRAL_PREFIX}[
                          registry.provider_of(model)]
            bare = model[len(prefix):]
            badge_mark = {"groq": "⚡ groq", "cf": "☁ cf",
                          "mistral": "🌀 mistral"}[
                              registry.provider_of(model)]
            return opencode_compat.translate(
                chunk, base, key, bare, instructions=instructions,
                source_language=source_language, target_language=target_language,
            ), f"{badge_mark}: {bare}"
        if registry.provider_of(model) == "custom":
            base = (s.get("openai_base") or "").strip()
            key = (s.get("openai_key") or "").strip()
            if not base or not key:
                raise RuntimeError(
                    "OpenAI-compatible base URL and API key are not configured.")
            return opencode_compat.translate(
                chunk, base, key, model, instructions=instructions,
                source_language=source_language,
                target_language=target_language,
            ), f"◈ chosen: {model}"
        if registry.provider_of(model) == "opencode_cli":
            cli_model = model[len(registry.OPENCODE_CLI_PREFIX):]
            return opencode_cli.translate(
                chunk, cli_model, instructions=instructions,
                source_language=source_language, target_language=target_language,
            ), f"◈ chosen: OpenCode CLI ({cli_model})"
        return opencode_zen.translate(
            chunk, model, instructions=instructions,
            source_language=source_language, target_language=target_language,
        ), f"◈ {chosen_short}"

    def _translated_call(model: str) -> tuple[str, str]:
        output, badge = _call(model)
        output = _prepare_translation(output)
        detected_language = _detect_translation_language(output)
        if (
            detected_language
            and detected_language != target_language
            and source_language != target_language
        ):
            from .prompt import LANGUAGE_NAMES

            target_name = LANGUAGE_NAMES[target_language]
            detected_name = LANGUAGE_NAMES.get(
                detected_language, detected_language)
            correction = (
                f"Your previous response was in {detected_name}, not "
                f"{target_name}. Translate the complete source passage into "
                f"{target_name}. Do not leave sentences in {detected_name}; "
                "return only the requested translation."
            )
            output, badge = _call(model, correction)
            output = _prepare_translation(output)
            detected_language = _detect_translation_language(output)
            if detected_language and detected_language != target_language:
                raise RuntimeError(
                    f"Selected model returned {detected_language} instead "
                    f"of the requested {target_language} translation.")
        return output, badge or _call_badge(model)

    def _call_badge(model: str) -> str:
        call_provider = registry.provider_of(model)
        if call_provider == "gemini":
            return f"✦ {model}"
        if call_provider == "google_translate":
            return "🌐 chosen: Google Translate"
        if call_provider == "bridge":
            return f"◈ bridge: {model}"
        if call_provider == "freellm":
            return f"🔀 flm: {model[len(registry.FLM_PREFIX):]}"
        if call_provider == "custom_provider":
            return f"🔧 custom: {registry.custom_provider_label(model, s)}"
        if call_provider in ("groq", "cf", "mistral"):
            prefix = {"groq": registry.GROQ_PREFIX,
                      "cf": registry.CF_PREFIX,
                      "mistral": registry.MISTRAL_PREFIX}[call_provider]
            bare = model[len(prefix):]
            mark = {"groq": "⚡ groq", "cf": "☁ cf",
                    "mistral": "🌀 mistral"}[call_provider]
            return f"{mark}: {bare}"
        if call_provider == "custom":
            return f"◈ chosen: {model}"
        if call_provider == "opencode_cli":
            cli_model = model[len(registry.OPENCODE_CLI_PREFIX):]
            return f"◈ chosen: OpenCode CLI ({cli_model})"
        return f"◈ {model.replace('-free', '')}"

    # 1. The engine the user actually chose. If it cannot run, say why instead
    #    of pretending: the old code silently swapped in Gemini here.
    failure = ""
    if provider == "gemini":
        if not (s.get("gemini_key") or "").strip():
            failure = "no Gemini key"
        else:
            try:
                arabic, badge = _translated_call(chosen)
                return arabic, f"✦ chosen: {chosen}"
            except Exception as e:
                failure = str(e)[:80]
    elif provider == "opencode":
        try:
            arabic, badge = _translated_call(chosen)
            return arabic, f"◈ chosen: {chosen_short}"
        except Exception as e:
            failure = str(e)[:80]
    elif provider == "bridge":
        try:
            arabic, badge = _translated_call(chosen)
            return arabic, f"◈ chosen bridge: {chosen_short}"
        except Exception as e:
            failure = str(e)[:80]
    elif provider == "freellm":
        base, key = registry.provider_creds("freellm", s)
        if not key:
            failure = (
                "no FreeLLMAPI unified key — copy it from the Keys page of "
                f"{base}")
        else:
            try:
                return _translated_call(chosen)
            except Exception as e:
                failure = str(e)[:80]
    elif provider in ("groq", "cf", "mistral"):
        base, key = registry.provider_creds(provider, s)
        if not (base and key):
            failure = "provider token is not configured"
        else:
            try:
                return _translated_call(chosen)
            except Exception as e:
                failure = str(e)[:80]
    elif provider == "custom_provider":
        cfg = registry.custom_provider_config(chosen, s)
        if cfg is None or not all(cfg):
            failure = "custom provider is not configured or incomplete"
        else:
            try:
                return _translated_call(chosen)
            except Exception as e:
                failure = str(e)[:80]
    elif provider == "opencode_cli":
        try:
            return _translated_call(chosen)
        except Exception as e:
            failure = str(e)[:80]
    elif provider == "google_translate":
        try:
            return _translated_call(chosen)
        except Exception as e:
            failure = str(e)[:80]
    else:
        base = (s.get("openai_base") or "").strip()
        key = (s.get("openai_key") or "").strip()
        if not (base and key):
            failure = "OpenAI-compatible is not configured"
        else:
            try:
                out = _prepare_translation(opencode_compat.translate(
                    chunk, base, key, chosen,
                    instructions=instructions,
                    source_language=source_language,
                    target_language=target_language,
                ))
                return out, f"◈ chosen: {chosen}"
            except Exception as e:
                failure = str(e)[:80]

    # No second OpenAI-compatible attempt here: the same call already ran (and
    # failed) in the "chosen engine" step above, so it could only add ~20s of
    # latency before the free layers.
    return _translate_free_fallback(chunk, s, chosen, failure)


def _one_line(error: Exception) -> str:
    """One short line of an exception, for a user-facing message."""
    text = " ".join(str(error).split())
    return (text or error.__class__.__name__)[:160]


def _detect_translation_language(text: str) -> str | None:
    """Detect the prose language when there is enough text to judge reliably."""
    from langdetect import LangDetectException, detect

    prose = re.sub(r"```.*?```|`[^`]*`", " ", text, flags=re.DOTALL)
    prose = re.sub(r"https?://\S+", " ", prose)
    if sum(character.isalpha() for character in prose) < 40:
        return None
    try:
        return detect(prose)
    except LangDetectException:
        return None
