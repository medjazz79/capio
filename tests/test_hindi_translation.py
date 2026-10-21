"""Hindi output contract, correction, and reused popup tests with fake HTTP."""
import json
import unittest
from unittest import mock

from config.settings import DEFAULTS
from tests.test_app import _GuiCase
from translators import chain


HINDI = "यह हिन्दी में किया गया नया अनुवाद है। इसमें मूल पाठ का अर्थ और सभी महत्वपूर्ण विवरण सुरक्षित रखे गए हैं।"
KOREAN = "이것은 한국어로 작성된 이전 번역입니다. 원문의 의미와 중요한 내용을 모두 보존하면서 자연스럽게 번역한 문장입니다."
CHINESE = "这是此前显示的中文翻译。译文保留了原文的意思和全部重要细节，并使用自然流畅的中文表达。"


def settings(**changes):
    values = dict(DEFAULTS,
        model="gemini-3.5-flash-lite", gemini_key="offline-test-key",
        source_language="en", target_language="hi", fast_mode=True,
        allow_translation_fallback=False)
    values.update(changes)
    return values


def response(text):
    data = {"candidates": [{
        "content": {"parts": [{"text": text}]}, "finishReason": "STOP",
    }]}
    result = mock.Mock(status_code=200)
    result.json.return_value = data
    result.iter_lines.return_value = [
        ("data: " + json.dumps(data, ensure_ascii=False)).encode("utf-8")]
    return result


class TestHindiTranslationContract(unittest.TestCase):
    def test_text_and_image_prompts_require_hindi_in_devanagari(self):
        from translators.prompt import request_user_prompt, system_instructions
        from translators.vision import image_system_instructions, image_translation_prompt

        prompts = (
            system_instructions(target_language="hi"),
            request_user_prompt("Original passage", target_language="hi"),
            image_system_instructions(target_language="hi"),
            image_translation_prompt(target_language="hi"),
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt[:60]):
                self.assertIn("Hindi", prompt)
                self.assertIn("Devanagari", prompt)

    def test_stream_correction_changes_the_actual_http_request(self):
        payloads = []

        def post(url, **kwargs):
            payload = kwargs["json"]
            payloads.append(payload)
            system = payload["system_instruction"]["parts"][0]["text"]
            corrected = ("CRITICAL OUTPUT-LANGUAGE CORRECTION" in system
                         and "Devanagari" in system)
            return response(HINDI if corrected else KOREAN)

        with mock.patch("translators.gemini.requests.post", side_effect=post):
            events = list(chain.translate_stream("Original English passage", settings()))
        self.assertEqual(events[-1][1], HINDI)
        self.assertEqual(len(payloads), 2)
        self.assertNotEqual(payloads[0], payloads[1])
        correction = payloads[1]["system_instruction"]["parts"][0]["text"]
        self.assertIn("Korean", correction)
        self.assertIn("Hindi", correction)

    def test_failed_correction_sends_no_third_request(self):
        with mock.patch("translators.gemini.requests.post",
                        side_effect=lambda *args, **kwargs: response(KOREAN)) as post:
            with self.assertRaisesRegex(RuntimeError, "instead of Hindi"):
                list(chain.translate_stream("Original English passage", settings()))
        self.assertEqual(post.call_count, 2)

    def test_correct_hindi_stream_needs_only_one_http_request(self):
        with mock.patch("translators.gemini.requests.post",
                        side_effect=lambda *args, **kwargs: response(HINDI)) as post:
            events = list(chain.translate_stream("Original English passage", settings()))
        self.assertEqual(events[-1][1], HINDI)
        post.assert_called_once()

    def test_compatible_provider_correction_is_explicit_and_has_no_old_history(self):
        payloads = []

        def post(url, **kwargs):
            payload = kwargs["json"]
            payloads.append(payload)
            instructions = payload["messages"][0]["content"]
            text = (HINDI if "CRITICAL OUTPUT-LANGUAGE CORRECTION" in instructions
                    else KOREAN)
            data = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
            result = mock.Mock(status_code=200, headers={})
            result.json.return_value = data
            event = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
            result.iter_lines.return_value = [
                ("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8"),
                b"data: [DONE]",
            ]
            return result

        opts = settings(model="offline-compatible-model",
                        openai_base="https://offline.invalid/v1",
                        openai_key="offline-test-key")
        with mock.patch("translators.openai_compat.requests.post", side_effect=post):
            events = list(chain.translate_stream("Original English passage", opts))
        self.assertEqual(events[-1][1], HINDI)
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertEqual([message["role"] for message in payload["messages"]],
                             ["system", "user"])
            for message in payload["messages"]:
                self.assertIn("Hindi", message["content"])
                self.assertIn("Devanagari", message["content"])

    def test_short_korean_prose_is_rejected_for_hindi(self):
        self.assertTrue(chain._needs_language_retry(settings(), "이것은 한국어 번역입니다."))

    def test_hindi_is_identified_by_script_even_if_statistical_guess_is_wrong(self):
        with mock.patch("langdetect.detect", return_value="ko"):
            self.assertEqual(chain._detect_translation_language(HINDI), "hi")

    def test_workers_do_not_display_korean_preview_for_hindi_text_or_image(self):
        from popup.worker import TranslateWorker

        for image_mode in (False, True):
            with self.subTest(image=image_mode):
                opts = ({"pil_image": object()} if image_mode
                        else {"text": "Original English passage"})
                worker = TranslateWorker(settings(selection_mode="image"), **opts)
                previews, completed = [], []
                worker.partial.connect(previews.append)
                worker.done.connect(lambda text, badge: completed.append(text))
                events = [("partial", KOREAN, ""), ("partial", HINDI, ""),
                          ("done", HINDI, "mock")]
                target = ("translators.chain.translate_image_stream" if image_mode
                          else "translators.chain.translate_stream")
                with mock.patch(target, return_value=iter(events)):
                    worker.run()
                self.assertEqual(previews, [HINDI])
                self.assertEqual(completed, [HINDI])

    def test_wrong_image_language_is_not_accepted_or_retried(self):
        from popup.worker import TranslateWorker

        worker = TranslateWorker(settings(selection_mode="image"), pil_image=object())
        completed, failures, previews = [], [], []
        worker.partial.connect(previews.append)
        worker.done.connect(lambda text, badge: completed.append(text))
        worker.failed.connect(failures.append)
        with mock.patch("translators.chain.translate_image_stream", return_value=iter([
            ("partial", KOREAN, ""), ("done", KOREAN, "mock"),
        ])) as stream:
            worker.run()
        self.assertEqual(completed, [])
        self.assertEqual(previews, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("instead of Hindi", failures[0])
        stream.assert_called_once()

    def test_hindi_preview_keeps_latin_music_names_and_notation(self):
        text = "बाख (Bach) ने C major के बारे में विस्तार से समझाया है।"
        self.assertTrue(chain.translation_preview_matches_target(text, settings()))
        self.assertFalse(chain.translation_preview_matches_target(KOREAN + " Bach C major", settings()))


class TestHindiPopupHttp(_GuiCase):
    def test_chinese_to_hindi_in_both_apps_with_real_chain_and_fake_http(self):
        from popup.translate_popup import TranslatePopup

        for floating in (False, True):
            with self.subTest(floating=floating):
                payloads = []

                def post(url, **kwargs):
                    payload = kwargs["json"]
                    payloads.append(payload)
                    system = payload["system_instruction"]["parts"][0]["text"]
                    if "CRITICAL OUTPUT-LANGUAGE CORRECTION" in system:
                        return response(HINDI)
                    return response(CHINESE if len(payloads) == 1 else KOREAN)

                with mock.patch("translators.gemini.requests.post", side_effect=post):
                    popup = TranslatePopup(
                        settings(target_language="zh"), source_text="Original English passage",
                        floating_language_controls=True, float_language_preferences=floating)
                    try:
                        self.assertTrue(popup._worker.wait(3000))
                        self.app.processEvents()
                        self.assertEqual(popup.result.toPlainText(), CHINESE)
                        displayed = []
                        popup.result.textChanged.connect(
                            lambda: displayed.append(popup.result.toPlainText()))
                        popup._language_actions["target"]["hi"].trigger()
                        self.assertTrue(popup._worker.wait(3000))
                        self.app.processEvents()
                        self.assertEqual(popup.result.toPlainText(), HINDI)
                        self.assertNotIn(CHINESE, displayed)
                        self.assertNotIn(KOREAN, displayed)
                        self.assertEqual(len(payloads), 3)
                    finally:
                        for worker in popup._translation_workers:
                            worker.requestInterruption()
                            worker.wait(3000)
                        self.app.processEvents()
                        popup.close()
