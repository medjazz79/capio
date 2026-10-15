"""Regression tests for translation prompts and direct Markdown output."""
import unittest
from unittest import mock

from PIL import Image

from config.settings import DEFAULTS
from translators import chain


class TestTranslationOutput(unittest.TestCase):
    def test_translation_prompts_preserve_table_rows_and_columns(self):
        from translators.prompt import (
            TABLE_FORMATTING_RULE,
            system_instructions,
        )
        from translators.vision import image_system_instructions

        prompts = (
            system_instructions(),
            image_system_instructions(),
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(TABLE_FORMATTING_RULE, prompt)
                self.assertIn("Do not flatten table cells", prompt)
                self.assertIn("do not guess or", prompt)

    def test_image_prompts_are_concise_and_shared_across_vision_providers(self):
        from translators.vision import (
            image_system_instructions, image_translation_prompt,
            openai_image_messages, responses_image_input,
        )

        prompt = image_translation_prompt("en", "it")
        system = image_system_instructions(
            "Keep names precise.", "en", "it")
        self.assertIn(
            "Translate the complete image from English into Italian", prompt)
        self.assertNotIn("genuine source footnotes", prompt)
        self.assertNotIn("Final reminder", prompt)
        self.assertNotIn("UKULELE_TAB_RULES", prompt)
        self.assertIn("Translate from English into Italian", system)
        self.assertIn("Translate all body text, actual content headings", system)
        self.assertIn("Keep a real chapter/section heading", system)
        self.assertIn(
            "Treat an isolated line above the main body as a running header",
            system,
        )
        self.assertIn("### headings", system)
        self.assertIn("Use Markdown with ### headings", system)
        self.assertIn("Preserve order and paragraph breaks", system)
        self.assertIn(
            "On clearly musical pages",
            system,
        )
        self.assertIn(
            "bold headings only and keep prose normal",
            system,
        )
        self.assertIn("never mistake them for music", system)
        self.assertIn(
            "read-aloud skips them",
            system,
        )
        self.assertIn("Preserve equations as LaTeX", system)
        self.assertIn(
            "literal pitch labels, not HTML",
            system,
        )
        self.assertIn(
            "On clearly musical pages",
            system,
        )
        self.assertIn(
            "Never omit notes, give only selected pitches",
            system,
        )
        self.assertIn("Transcribe every legible note exactly and in order",
                      system)
        self.assertNotIn("ukulele", system)
        self.assertIn("Preserve chord labels and adjacent notes.", system)
        self.assertIn(
            "Treat an isolated line above the main body as a running header",
            system,
        )
        self.assertIn("standalone page number", system)
        self.assertIn("on the final line", system)
        self.assertIn("<small>PAGE_NUMBER</small>", system)
        self.assertIn("standalone page number", system)
        self.assertIn("on the final line", system)
        self.assertIn("<small>PAGE_NUMBER</small>", system)
        self.assertIn("output only the translation", system)
        self.assertIn("Keep names precise.", system)
        self.assertLess(len(prompt) + len(system), 3200)

        messages = openai_image_messages(
            "data:image/jpeg;base64,image", source_language="en",
            target_language="it", instructions="Keep names precise.",
        )
        self.assertIn(prompt, messages[1]["content"][0]["text"])
        self.assertEqual(messages[0]["content"], system)
        _system_prompt, response_input = responses_image_input(
            "data:image/jpeg;base64,image", source_language="en",
            target_language="it", instructions="Keep names precise.",
        )
        self.assertIn(prompt, response_input[0]["content"][0]["text"])
        self.assertEqual(_system_prompt, system)

    def test_translation_prompts_require_a_clarity_review(self):
        from translators.prompt import (
            TRANSLATION_CLARITY_RULE,
            system_instructions,
            user_prompt,
        )
        from translators.vision import (
            image_system_instructions,
        )

        for prompt in (
            system_instructions(),
            user_prompt("Example source."),
        ):
            with self.subTest(prompt=prompt[:50]):
                self.assertIn(TRANSLATION_CLARITY_RULE, prompt)
                self.assertIn(
                    "silently review every translated sentence", prompt)
                self.assertIn("do not invent details", prompt)
        image_prompt = image_system_instructions()
        self.assertIn(
            "Write clear, grammatical, natural prose without changing meaning",
            image_prompt,
        )
        self.assertIn("or resolving genuine ambiguity.", image_prompt)
        self.assertIn("invent no facts", image_prompt)

    def test_translation_prompts_translate_useful_parenthetical_text(self):
        from translators.prompt import system_instructions, user_prompt
        text_prompt = user_prompt("Kepler (the German astronomer)")
        prompts = (
            system_instructions(""),
            text_prompt,
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(
                    "Translate meaningful text inside parentheses into the "
                    "target language",
                    prompt,
                )
                self.assertIn(
                    "Never repeat a translated proper name in parentheses",
                    prompt,
                )
                self.assertIn(
                    "Translate any useful parenthetical explanation",
                    prompt,
                )
                self.assertIn("A blank line is layout only", prompt)
        self.assertIn("OCR CONTENT RULE:", text_prompt)
        self.assertLess(
            text_prompt.index("OCR CONTENT RULE:"),
            text_prompt.index("Kepler (the German astronomer)"),
        )

    def test_arabic_prompts_preserve_searchable_names_on_first_use(self):
        from translators.prompt import (
            ARABIC_NAME_PRESERVATION_RULE,
            system_instructions,
            user_prompt,
        )

        for prompt in (
            system_instructions(target_language="ar"),
            user_prompt("George Orwell visited San Francisco."),
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(ARABIC_NAME_PRESERVATION_RULE, prompt)
                self.assertIn("first occurrence only", prompt)
                self.assertIn("جورج أورويل (George Orwell)", prompt)
                self.assertIn("سان فرانسيسكو (San Francisco)", prompt)
                self.assertIn("ordinary words, generic terms", prompt)

        self.assertNotIn(
            ARABIC_NAME_PRESERVATION_RULE,
            system_instructions(target_language="fr"),
        )
        self.assertNotIn(
            ARABIC_NAME_PRESERVATION_RULE,
            user_prompt("George Orwell visited San Francisco.", target_language="fr"),
        )

    def test_arabic_image_prompts_preserve_searchable_names_on_first_use(self):
        from translators.vision import (
            image_system_instructions,
            image_translation_prompt,
        )

        image_system = image_system_instructions(target_language="ar")
        self.assertIn("exact source spelling in parentheses", image_system)
        self.assertIn("at first mention only", image_system)
        self.assertNotIn(
            "George Orwell",
            image_translation_prompt(target_language="ar"),
        )
        self.assertNotIn(
            "exact source spelling in parentheses",
            image_system_instructions(target_language="fr"),
        )

    def test_arabic_prompts_preserve_western_digit_shapes(self):
        from translators.prompt import (
            ARABIC_DIGIT_STYLE_RULE,
            system_instructions,
            user_prompt,
        )
        from translators.vision import (
            image_system_instructions,
            image_translation_prompt,
        )

        for prompt in (
            system_instructions(target_language="ar"),
            user_prompt("Measure 1944", target_language="ar"),
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(ARABIC_DIGIT_STYLE_RULE, prompt)
                self.assertIn("0-9", prompt)
                self.assertIn("Convert Arabic-Indic", prompt)

        image_system = image_system_instructions(target_language="ar")
        self.assertIn("Use Western digits 0-9 throughout", image_system)
        self.assertNotIn(
            "Western digits",
            image_translation_prompt(target_language="ar"),
        )
        self.assertNotIn(
            ARABIC_DIGIT_STYLE_RULE,
            system_instructions(target_language="fr"),
        )

    def test_translation_output_normalizes_arabic_digits_to_western(self):
        from translators.chain import _prepare_translation

        self.assertEqual(
            _prepare_translation("القياس ١٩٤٤ و ۱۲۳"),
            "القياس 1944 و 123",
        )

    def test_translation_prompts_reserve_bold_formatting_for_headings(self):
        from translators.prompt import system_instructions, user_prompt

        for prompt in (
            system_instructions(),
            user_prompt("A heading\n\nAn emphasized sentence."),
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(
                    "Use bold formatting only for headings", prompt)
                self.assertIn(
                    "Keep all prose, paragraphs, list items, labels, and "
                    "individual terms in normal weight",
                    prompt,
                )

    def test_text_prompts_omit_peripheral_text_and_keep_small_page_number(self):
        from translators.prompt import system_instructions, user_prompt

        for prompt in (
            system_instructions("", "en", "ar"),
            user_prompt("Body sentence.", "en", "ar"),
        ):
            with self.subTest(prompt=prompt[:60]):
                self.assertIn(
                    "Do not translate, reproduce, paraphrase, or format "
                    "running",
                    prompt,
                )
                self.assertIn("running headers", prompt)
                self.assertIn("footers", prompt)
                self.assertIn("marginal notes, footnotes", prompt)
                self.assertIn("copyright lines", prompt)
                self.assertIn("standalone page number", prompt)
                self.assertIn("<small>PAGE_NUMBER</small>", prompt)
                self.assertIn("do not add one if absent", prompt)
                self.assertIn("Never add translator's notes", prompt)

    def test_music_notation_rules_are_conditional_on_source_domain(self):
        from translators.prompt import system_instructions, user_prompt
        from translators.vision import (
            image_system_instructions, image_translation_prompt,
        )

        system = system_instructions("", "auto", "ar")
        code_prompt = user_prompt(
            "Python example: scale = [1, 2, 3]\n"
            "matrix = [[1, 0], [0, 1]]\nprint(matrix)", "auto", "ar")
        math_prompt = user_prompt(
            "The equation is \\(x^2 + y^2 = z^2\\), with "
            "\\(A = \\begin{bmatrix}a&b\\\\c&d\\end{bmatrix}\\).",
            "auto", "ar")
        note_sequence = "C D E F# G A B C"
        music_prompt = user_prompt(
            f"C major scale: {note_sequence}", "auto", "ar")
        image_system = image_system_instructions("")
        image_prompt = image_translation_prompt()

        self.assertIn("do not assume it is a music book", system)
        self.assertNotIn("Convert every standalone scale", system)
        self.assertNotIn("four-string ukulele tab", system)
        for prompt in (code_prompt, math_prompt):
            with self.subTest(prompt=prompt[-120:]):
                self.assertIn("Keep source code, commands, identifiers", prompt)
                self.assertNotIn("Convert every standalone scale", prompt)
                self.assertNotIn("fenced `music` code block", prompt)
        self.assertIn("inline `$...$` or display `$$...$$` LaTeX", math_prompt)
        self.assertIn("Never put math or science in a `music` block", math_prompt)
        self.assertIn("preserve every source note", music_prompt)
        self.assertIn(
            "Never omit notes, reduce the sequence to selected symbols",
            music_prompt,
        )
        self.assertIn("complete, faithful note sequence", music_prompt)
        self.assertNotIn("ukulele", music_prompt)
        self.assertIn(note_sequence, music_prompt)
        self.assertIn(
            "On clearly musical pages",
            image_system,
        )
        self.assertIn(
            "never mistake them for music",
            image_system,
        )
        self.assertIn("Preserve equations as LaTeX", image_system)
        self.assertIn("Transcribe every legible note exactly and in order",
                      image_system)
        self.assertIn(
            "Never omit notes, give only selected pitches",
            image_system,
        )
        self.assertNotIn("ukulele", image_prompt)

    def test_translation_prompts_adapt_style_to_the_source_domain(self):
        from translators.prompt import DOMAIN_STYLE_RULES, user_prompt
        from translators.vision import image_system_instructions

        story = user_prompt(
            "The narrator remembered the winter evening.", "auto", "ar")
        programming = user_prompt(
            "A Python function returns a value from an API.", "auto", "ar")
        music = user_prompt(
            "The melody resolves to the tonic.", "auto", "ar")
        image_system = image_system_instructions(target_language="ar")

        for prompt in (story, programming, music, image_system):
            with self.subTest(prompt=prompt[:80]):
                self.assertIn("Translate as an expert", prompt)
                self.assertIn("infer it from context", prompt)
                self.assertIn("Preserve meaning and tone", prompt)
        self.assertIn("preserve narrative voice, register, rhythm", story)
        self.assertIn("without embellishment", story)
        self.assertIn(
            "standard Arabic terms when Arabic is the target", programming)
        self.assertIn("preserve code, commands, identifiers", programming)
        self.assertIn("Music: use accurate terminology", music)
        self.assertEqual(
            image_system.count(DOMAIN_STYLE_RULES), 1)

    def test_translation_prompts_use_three_hashes_for_every_heading(self):
        from translators.prompt import system_instructions, user_prompt

        heading_rule = (
            "Whenever you include a heading, begin it with exactly three "
            "hash characters (`###`)"
        )
        for prompt in (
            system_instructions("", "auto", "ar"),
            user_prompt("a short title and a longer section title", "auto", "ar"),
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(heading_rule, prompt)
                self.assertIn(
                    "Never use `#`, `##`, `####`, or four or more hashes",
                    prompt,
                )

    def test_image_markdown_is_returned_without_language_check_or_retry(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
        )
        markdown = (
            "ترجمة عربية.\n\n"
            "### مثال\n"
            "The effects of EU membership.\n"
            "`inline source notation`\n"
            "```python\nx = True\n```"
        )
        with mock.patch(
            "translators.gemini.translate_image",
            return_value=markdown,
        ) as translate:
            translated, badge = chain.translate_image(
                Image.new("RGB", (20, 20), "white"), settings)

        self.assertEqual(translated, markdown)
        self.assertEqual(badge, "✦ chosen image: gemini-3.5-flash-lite")
        translate.assert_called_once()
        self.assertNotIn(
            "CRITICAL CORRECTION",
            translate.call_args.kwargs["instructions"],
        )

    def test_streaming_translation_retries_when_output_language_is_wrong(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        with (
            mock.patch(
                "translators.chain._stream_single_gemini",
                return_value=iter([("done", "English output", "Gemini")]),
            ),
            mock.patch(
                "translators.chain._detect_translation_language",
                side_effect=["en", "ar"],
            ),
            mock.patch(
                "translators.chain._translate_chunk",
                return_value=("النص العربي المصحح", "Gemini"),
            ) as retry,
        ):
            events = list(chain.translate_stream("Source passage", settings))

        retry.assert_called_once()
        self.assertEqual(
            events[-1],
            ("done", "النص العربي المصحح", "Gemini"),
        )
