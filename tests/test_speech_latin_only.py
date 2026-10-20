"""Latin-only speech: pronounce the foreign counterpart, skip the Arabic one.

The reader translates English books into Arabic and keeps the original name in
parentheses -- ``دوروثي فرينش (Dorothy French)``. In this mode the voice says
``Dorothy French`` at the position of the Arabic name, never reads the
parenthetical, keeps the highlight on the Arabic words, and remembers the pair
for the rest of the book so later Arabic-only occurrences are read in Latin.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SENTENCE = "شَعَرَتْ دوروثي فرينش (Dorothy French) بالأسى تجاه الشابة."


class LatinOnlyTextTest(unittest.TestCase):
    def test_the_arabic_transliteration_is_replaced_by_its_latin_counterpart(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(SENTENCE, {})

        self.assertEqual(
            " ".join(plan.text.split()),
            "شَعَرَتْ Dorothy French بالأسى تجاه الشابة.")

    def test_the_highlight_stays_on_the_arabic_words(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(SENTENCE, {})
        name_start = SENTENCE.index("دوروثي")
        name_span = (name_start, name_start + len("دوروثي فرينش"))
        mapped = {
            plan.text[start:end]: plan.display_spans[index]
            for index, (start, end) in enumerate(plan.words)
        }

        self.assertEqual(mapped["Dorothy"], name_span)
        self.assertEqual(mapped["French"], name_span)
        open_paren = SENTENCE.index("(Dorothy")
        close_paren = SENTENCE.index(")") + 1
        for start, end in plan.display_spans:
            self.assertFalse(
                start < close_paren and end > open_paren,
                "the parenthetical Latin text must never be highlighted")

    def test_the_pair_is_learned_for_later_occurrences(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(SENTENCE, {})

        self.assertEqual(plan.learned["دوروثي فرينش"], "Dorothy French")
        self.assertEqual(plan.learned["دوروثي"], "Dorothy")
        self.assertEqual(plan.learned["فرينش"], "French")

    def test_an_arabic_descriptive_word_stays_arabic_inside_a_foreign_phrase(self):
        from popup.speech_worker import build_latin_only_speech

        text = ("أَحْضَرَهَا إِلَى مَحَطَّةِ حَافِلَاتِ غْرَيْهَاوُنْد "
                "(Greyhound Bus Depot)")
        plan = build_latin_only_speech(text, {})

        self.assertIn("مَحَطَّةِ", plan.text)
        self.assertIn("حَافِلَاتِ", plan.text)
        self.assertNotIn("غْرَيْهَاوُنْد", plan.text)
        self.assertIn("Greyhound", plan.text)
        self.assertNotIn("Bus", plan.text)
        self.assertNotIn("Depot", plan.text)
        self.assertEqual(plan.learned["غريهاوند"], "Greyhound")
        self.assertNotIn("محطة حافلات غريهاوند", plan.learned)

    def test_a_fully_translated_phrase_keeps_every_arabic_word(self):
        from popup.speech_worker import build_latin_only_speech

        text = ("دَخَلَ جُون سُومْتَر مَكْتَبَ مَكْتِبِ التَّحْقِقَاتِ "
                "الفِدْرَالِيِّ (Federal Bureau of Investigation) "
                "فِي سَانْ فْرَانْسِسْكُو.")
        plan = build_latin_only_speech(text, {})

        self.assertIn("مَكْتَبَ", plan.text)
        self.assertIn("التَّحْقِقَاتِ", plan.text)
        self.assertIn("الفِدْرَالِيِّ", plan.text)
        self.assertIn("فِي سَانْ فْرَانْسِسْكُو", plan.text)
        self.assertNotIn("Federal", plan.text)
        self.assertNotIn("Investigation", plan.text)
        self.assertEqual(plan.learned, {})

    def test_a_quoted_transliteration_is_read_only_in_latin(self):
        from popup.speech_worker import build_latin_only_speech

        text = 'قَالَ "بُوب" (Bob) إِنَّهُ جَاهِزٌ.'
        plan = build_latin_only_speech(text, {})

        self.assertIn("Bob", plan.text)
        self.assertNotIn("بُوب", plan.text)
        self.assertEqual(plan.learned["بوب"], "Bob")

    def test_a_quoted_transliteration_is_latin_when_it_returns(self):
        from popup.speech_worker import build_latin_only_speech

        text = "قَالَ «بُوب» (Bob) ثُمَّ عَادَ بُوب إِلَى الْبَيْتِ."
        plan = build_latin_only_speech(text, {})

        self.assertEqual(plan.text.count("Bob"), 2)
        self.assertNotIn("بُوب", plan.text)

    def test_a_name_with_a_voiced_sibilant_still_matches(self):
        from popup.speech_worker import build_latin_only_speech

        text = "ذَهَبَ غُورْدُون بَاوُورْز (Gordon Bowers) إِلَى الْبَيْتِ."
        plan = build_latin_only_speech(text, {})

        self.assertIn("Gordon Bowers", plan.text)
        self.assertNotIn("باوورز", plan.text)

    def test_the_foreign_word_is_english_when_it_returns_alone(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(
            "ثم ركبت غريهاوند إلى البيت.",
            {"غريهاوند": "Greyhound"})

        self.assertIn("Greyhound", plan.text)
        self.assertNotIn("غريهاوند", plan.text)

    def test_a_name_learned_earlier_is_english_inside_a_new_sentence(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(
            "كانت سماء واشنطن صافية.",
            {"واشنطن": "Washington"})

        self.assertIn("سماء", plan.text)
        self.assertIn("Washington", plan.text)
        self.assertNotIn("واشنطن", plan.text)

    def test_the_name_is_english_in_every_sentence_after_its_pair(self):
        from popup.speech_worker import build_latin_only_speech

        text = ("زارت واشنطن (Washington) في الربيع. "
                "كانت سماء واشنطن صافية.")
        plan = build_latin_only_speech(text, {})

        self.assertEqual(plan.text.count("Washington"), 2)
        self.assertIn("سماء", plan.text)
        self.assertNotIn("واشنطن", plan.text)

    def test_only_as_many_arabic_words_as_the_latin_phrase_are_skipped(self):
        from popup.speech_worker import build_latin_only_speech

        text = "زارت مدينة هنتنغتون بيتش (Huntington Beach) في الصيف."
        plan = build_latin_only_speech(text, {})

        self.assertIn("زارت", plan.text)
        self.assertIn("مدينة", plan.text)
        self.assertNotIn("هنتنغتون", plan.text)
        self.assertIn("Huntington Beach", plan.text)
        self.assertIn("في الصيف", plan.text)

    def test_one_arabic_word_can_stand_for_a_multiword_latin_name(self):
        from popup.speech_worker import build_latin_only_speech

        text = "وصلنا إلى نيويورك (New York) في الصباح."
        plan = build_latin_only_speech(text, {})

        self.assertIn("إلى", plan.text)
        self.assertNotIn("نيويورك", plan.text)
        self.assertIn("New York", plan.text)

    def test_a_descriptive_arabic_word_before_the_name_stays_arabic(self):
        from popup.speech_worker import build_latin_only_speech

        text = "شمال كارولاينا (North Carolina) ولاية أمريكية."
        plan = build_latin_only_speech(text, {})

        self.assertIn("شمال", plan.text)
        self.assertNotIn("كارولاينا", plan.text)
        self.assertIn("North Carolina", plan.text)

    def test_a_name_is_read_in_latin_every_time_it_reappears(self):
        from popup.speech_worker import build_latin_only_speech

        text = ("دوروثي فرينش (Dorothy French) كانت هادئة. "
                "ثم عادت دوروثي فرينش إلى البيت.")
        plan = build_latin_only_speech(text, {})

        self.assertEqual(plan.text.count("Dorothy"), 2)
        self.assertNotIn("دوروثي", plan.text)
        second = text.index("دوروثي", text.index(")"))
        spans = [
            plan.display_spans[index]
            for index, (start, end) in enumerate(plan.words)
            if plan.text[start:end] == "Dorothy"
        ]
        self.assertIn((second, second + len("دوروثي فرينش")), spans)

    def test_a_pair_learned_on_an_earlier_page_is_reused(self):
        from popup.speech_worker import build_latin_only_speech

        text = "ثم عادت دوروثي فرينش إلى البيت."
        plan = build_latin_only_speech(
            text, {"دوروثي فرينش": "Dorothy French"})

        self.assertEqual(
            " ".join(plan.text.split()),
            "ثم عادت Dorothy French إلى البيت.")
        self.assertEqual(plan.learned, {})

    def test_learned_pairs_ignore_arabic_diacritics(self):
        from popup.speech_worker import build_latin_only_speech, learn_latin_pairs

        pairs = learn_latin_pairs("دُورُوثِي فْرِينْش (Dorothy French) عادت.")

        self.assertEqual(pairs["دوروثي فرينش"], "Dorothy French")
        self.assertEqual(pairs["دوروثي"], "Dorothy")
        self.assertEqual(pairs["فرينش"], "French")
        plan = build_latin_only_speech(
            "ثم عادت دوروثي فرينش إلى البيت.", pairs)
        self.assertNotIn("دوروثي", plan.text)

    def test_a_parenthetical_without_an_arabic_counterpart_is_left_alone(self):
        from popup.speech_worker import build_latin_only_speech

        text = "(Dorothy French) عادت إلى البيت."
        plan = build_latin_only_speech(text, {})

        self.assertEqual(
            " ".join(plan.text.split()), "(Dorothy French) عادت إلى البيت.")

    def test_clicking_the_parenthetical_starts_at_its_arabic_name(self):
        from popup.speech_worker import latin_only_click_offset

        self.assertEqual(
            latin_only_click_offset(SENTENCE, SENTENCE.index("Dorothy")),
            SENTENCE.index("دوروثي"))
        outside = SENTENCE.index("بالأسى")
        self.assertEqual(latin_only_click_offset(SENTENCE, outside), outside)

    def test_english_names_with_v_and_p_keep_their_latin_pronunciation(self):
        from popup.speech_worker import build_latin_only_speech

        text = ("مَعَ وَالِدَتِهَا أَلْفِيرَا (Elvera) فِي كَامِينُو بَادِيرَا "
                "(Camino Padera) فِي بَاسِفِيك بِيتش (Pacific Beach).")
        plan = build_latin_only_speech(text, {})

        for name in ("Elvera", "Camino Padera", "Pacific Beach"):
            self.assertIn(name, plan.text)
        for arabic in ("أَلْفِيرَا", "بَادِيرَا", "بَاسِفِيك"):
            self.assertNotIn(arabic, plan.text)


class LatinOnlySegmentationTest(unittest.TestCase):
    def test_one_continuous_segment_keeps_every_span_on_the_display_text(self):
        from popup.speech_worker import _speech_segments_with_voices

        segments, voices, spans = _speech_segments_with_voices(
            SENTENCE, "voice", target_language="ar",
            latin_only=True, continuous=True)

        self.assertEqual(len(segments), 1)
        self.assertIn("Dorothy French", segments[0])
        self.assertNotIn("دوروثي", segments[0])
        self.assertEqual(voices, ["voice"])
        open_paren = SENTENCE.index("(Dorothy")
        close_paren = SENTENCE.index(")") + 1
        for segment_spans in spans:
            for start, end in segment_spans:
                self.assertLessEqual(end, len(SENTENCE))
                self.assertFalse(
                    start < close_paren and end > open_paren,
                    "the parenthetical must never be highlighted")

    def test_the_mode_is_ignored_for_non_arabic_targets(self):
        from popup.speech_worker import _speech_segments_with_voices

        segments, _, _ = _speech_segments_with_voices(
            SENTENCE, "voice", target_language="fr",
            latin_only=True, continuous=True)

        self.assertIn("Dorothy", segments[0])
        self.assertIn("دوروثي", segments[0])

    def test_the_existing_modes_are_untouched(self):
        from popup.speech_worker import _speech_segments_with_voices

        skipped, _, _ = _speech_segments_with_voices(
            SENTENCE, "voice", target_language="ar", continuous=True)
        self.assertNotIn("Dorothy", skipped[0])
        self.assertIn("دوروثي", skipped[0])

        both, _, _ = _speech_segments_with_voices(
            SENTENCE, "voice", target_language="ar",
            continuous=True, read_latin_words=True)
        self.assertIn("Dorothy", both[0])
        self.assertIn("دوروثي", both[0])


class LatinOnlyWorkerTest(unittest.TestCase):
    def test_the_worker_synthesizes_the_latin_only_plan(self):
        from popup.speech_worker import SpeechWorker

        worker = SpeechWorker(
            SENTENCE, None, target_language="ar", latin_only=True)
        self.addCleanup(worker.deleteLater)

        self.assertTrue(worker._latin_only)
        self.assertIn("Dorothy", worker._segments[0])
        self.assertNotIn("دوروثي", worker._segments[0])


class LatinOnlySettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_the_two_latin_word_options_are_mutually_exclusive_and_saved(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(
            dialog.speech_latin_only.text(),
            "Read only Latin words and skip arabic")
        self.assertEqual(
            dialog.speech_read_latin_words.text(),
            "Read both, Latin-script and Arabic")
        self.assertFalse(dialog.speech_latin_only.isChecked())

        dialog.speech_latin_only.setChecked(True)
        self.assertFalse(dialog.speech_read_latin_words.isChecked())
        values = dialog.values()
        self.assertTrue(values["speech_latin_only"])
        self.assertFalse(values["speech_read_latin_words"])

        dialog.speech_read_latin_words.setChecked(True)
        self.assertFalse(dialog.speech_latin_only.isChecked())
        values = dialog.values()
        self.assertFalse(values["speech_latin_only"])
        self.assertTrue(values["speech_read_latin_words"])


class LatinOnlyPopupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _popup(self, **overrides):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        popup = self._popup_without_translation(
            dict(
                DEFAULTS, source_text=SENTENCE, target_language="ar",
                **overrides))
        self.addCleanup(popup.deleteLater)
        return popup

    @staticmethod
    def _popup_without_translation(settings, **kwargs):
        from popup.translate_popup import TranslatePopup

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            return TranslatePopup(settings, **kwargs)

    def test_the_popup_passes_the_mode_and_its_book_glossary_to_the_worker(self):
        popup = self._popup(speech_latin_only=True)
        glossary = {}
        popup.set_speech_glossary(glossary)

        popup._learn_speech_glossary(SENTENCE)

        self.assertEqual(glossary["دوروثي فرينش"], "Dorothy French")
        kwargs = popup._speech_worker_kwargs(SENTENCE, 0)
        self.assertTrue(kwargs["latin_only"])
        self.assertEqual(
            kwargs["glossary"]["دوروثي فرينش"], "Dorothy French")
        self.assertIsNot(kwargs["glossary"], glossary)

    def test_the_popup_learns_nothing_while_the_mode_is_off(self):
        popup = self._popup(speech_latin_only=False)
        popup.set_speech_glossary({})

        popup._learn_speech_glossary(SENTENCE)

        self.assertEqual(popup._speech_glossary, {})

    def test_a_finished_translation_teaches_the_book_before_any_speech(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        shared = {}
        popup = self._popup_without_translation(
            dict(
                DEFAULTS, source_text=SENTENCE, target_language="ar",
                speech_latin_only=True),
            speech_glossary=shared)
        self.addCleanup(popup.deleteLater)

        popup.set_pretranslated_content(SENTENCE, "")

        self.assertEqual(shared["دوروثي فرينش"], "Dorothy French")
        self.assertEqual(shared["دوروثي"], "Dorothy")

    def test_two_popups_of_one_book_share_the_learned_names(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        shared = {}
        first = self._popup_without_translation(
            dict(
                DEFAULTS, source_text=SENTENCE, target_language="ar",
                speech_latin_only=True),
            speech_glossary=shared)
        self.addCleanup(first.deleteLater)
        first._learn_speech_glossary(SENTENCE)

        second = self._popup_without_translation(
            dict(
                DEFAULTS, source_text="ثم عادت دوروثي فرينش إلى البيت.",
                target_language="ar", speech_latin_only=True),
            speech_glossary=shared)
        self.addCleanup(second.deleteLater)
        plan = second._latin_only_speech("ثم عادت دوروثي فرينش إلى البيت.")

        self.assertEqual(shared["دوروثي فرينش"], "Dorothy French")
        self.assertNotIn("دوروثي", plan.text)


class LatinOnlyWiringTest(unittest.TestCase):
    def test_the_main_window_refreshes_open_popups_for_the_new_setting(self):
        from main import MainWindow

        self.assertIn("speech_latin_only", MainWindow._POPUP_DISPLAY_KEYS)

    def test_one_book_keeps_one_glossary(self):
        from main import MainWindow

        class Book:
            pass

        book = Book()
        first = MainWindow._speech_glossary_for(book)
        second = MainWindow._speech_glossary_for(book)

        self.assertIs(first, second)
        self.assertIsNone(MainWindow._speech_glossary_for(None))


class ForeignPauseTest(unittest.TestCase):
    """A Latin name gets a comma after it, before Arabic resumes."""

    def test_a_latin_name_between_arabic_words_gets_a_comma_after_it(self):
        from popup.speech_worker import _with_foreign_pauses

        spoken = _with_foreign_pauses(
            "الملازم Jess Haskins ملاحظات عن الجثة.")

        self.assertEqual(
            spoken, "الملازم Jess Haskins، ملاحظات عن الجثة.")

    def test_the_display_text_and_its_word_map_are_untouched(self):
        from popup.speech_worker import (
            _speech_source_word_spans, _with_foreign_pauses,
            build_latin_only_speech,
        )

        plan = build_latin_only_speech(
            "كَتَبَ الْمُلَازِمُ جيس هاسكينز (Jess Haskins) مُلَاحَظَاتٍ.",
            {},
        )
        spoken = _with_foreign_pauses(plan.text)

        self.assertEqual(
            len(_speech_source_word_spans(plan.text)),
            len(_speech_source_word_spans(spoken)),
        )
        self.assertEqual(
            [plan.text[start:end]
             for start, end in _speech_source_word_spans(plan.text)],
            [spoken[start:end]
             for start, end in _speech_source_word_spans(spoken)],
        )
        self.assertNotIn("،", plan.text)
        self.assertIn("Jess Haskins،", spoken)
        self.assertNotIn("الملازم،", spoken)

    def test_arabic_only_and_latin_only_text_are_untouched(self):
        from popup.speech_worker import _with_foreign_pauses

        with self.subTest("arabic"):
            text = "الملازم كتب ملاحظات عن الجثة."
            self.assertEqual(_with_foreign_pauses(text), text)
        with self.subTest("latin"):
            text = "Lieutenant Jess Haskins wrote notes."
            self.assertEqual(_with_foreign_pauses(text), text)

    def test_existing_commas_are_not_doubled(self):
        from popup.speech_worker import _with_foreign_pauses

        self.assertEqual(
            _with_foreign_pauses("الملازم، Jess Haskins، ملاحظات."),
            "الملازم، Jess Haskins، ملاحظات.",
        )
        self.assertEqual(
            _with_foreign_pauses("الملازم Jess Haskins، ملاحظات."),
            "الملازم Jess Haskins، ملاحظات.",
        )

    def test_paragraph_edges_do_not_get_a_leading_or_trailing_comma(self):
        from popup.speech_worker import _with_foreign_pauses

        self.assertEqual(
            _with_foreign_pauses("Jess Haskins قال شيئا."),
            "Jess Haskins، قال شيئا.",
        )
        self.assertEqual(
            _with_foreign_pauses("قال شيئا Jess Haskins"),
            "قال شيئا Jess Haskins",
        )
        self.assertEqual(_with_foreign_pauses("Jess Haskins"), "Jess Haskins")

    def test_emphasis_and_quote_markers_do_not_hide_the_seam(self):
        from popup.speech_worker import _with_foreign_pauses

        self.assertEqual(
            _with_foreign_pauses("الملازم *Jess Haskins* ملاحظات."),
            "الملازم Jess Haskins، ملاحظات.",
        )
        self.assertEqual(
            _with_foreign_pauses('الملازم "Jess Haskins" ملاحظات.'),
            "الملازم Jess Haskins، ملاحظات.",
        )

    def test_accented_latin_edges_are_still_a_seam(self):
        from popup.speech_worker import _with_foreign_pauses

        self.assertEqual(
            _with_foreign_pauses("قال Café ثم."),
            "قال Café، ثم.",
        )

    def test_bidirectional_marks_do_not_break_the_name_pair(self):
        from popup.speech_worker import (
            _with_foreign_pauses, build_latin_only_speech,
        )

        source = "هاسكينز \u200f(Jess Haskins)\u200f مُلَاحَظَاتٍ"
        plan = build_latin_only_speech(source, {})
        spoken = _with_foreign_pauses(plan.text)

        self.assertNotIn("هاسكينز", plan.text)
        self.assertNotIn("\u200f", spoken)
        self.assertEqual(spoken, "Jess Haskins، مُلَاحَظَاتٍ")

    def test_a_glued_parenthetical_does_not_glue_the_next_word(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(
            "هاسكينز(Jess Haskins)مُلَاحَظَاتٍ", {})

        self.assertEqual(plan.text, "Jess Haskins مُلَاحَظَاتٍ")

    def test_the_worker_adds_a_comma_after_the_latin_words_in_arabic_modes(self):
        from popup.speech_worker import SpeechWorker

        text = "الملازم Jess Haskins ملاحظات."
        latin_only = SpeechWorker(
            text, None, target_language="ar", latin_only=True)
        read_both = SpeechWorker(
            text, None, target_language="ar", read_latin_words=True)
        normal = SpeechWorker(text, None, target_language="ar")
        english = SpeechWorker(
            text, None, target_language="en", latin_only=True)
        for worker in (latin_only, read_both, normal, english):
            self.addCleanup(worker.deleteLater)

        self.assertTrue(latin_only._foreign_pauses)
        self.assertTrue(read_both._foreign_pauses)
        self.assertFalse(normal._foreign_pauses)
        self.assertFalse(english._foreign_pauses)
        self.assertIn(
            "Jess Haskins،", latin_only._synthesis_text(
                latin_only._segments[0]))
        self.assertIn(
            "Jess Haskins،", read_both._synthesis_text(
                read_both._segments[0]))
        self.assertNotIn(
            "،", normal._synthesis_text(normal._segments[0]))

    def test_the_worker_keeps_word_indices_aligned_with_the_seam(self):
        from popup.speech_worker import (
            SpeechWorker, _match_synthesis_boundary_span,
            _speech_source_word_spans,
        )

        worker = SpeechWorker(
            "الملازم Jess Haskins ملاحظات.", None,
            target_language="ar", read_latin_words=True)
        self.addCleanup(worker.deleteLater)
        segment = worker._segments[0]
        spoken = worker._synthesis_text(segment)
        spans = worker._segment_word_spans[0]

        self.assertEqual(
            len(_speech_source_word_spans(spoken)), len(spans))
        matched = _match_synthesis_boundary_span(
            "Haskins", spoken, segment, spans, 0)
        self.assertIsNotNone(matched)
        self.assertEqual(matched[2], 3)


class ForeignNameConjunctionTest(unittest.TestCase):
    """Initials and the waw that joins two Latin names must not be swallowed."""

    def test_initials_in_a_latin_name_lose_their_periods(self):
        from popup.speech_worker import build_latin_only_speech

        text = (
            "رَصَدَ الضَّابِطَانِ ر. ت. هُوبْكِنْسُون "
            "(R. T. Hopkinson) مَقْصَفَ هُولِوُود (Hollywood)."
        )
        plan = build_latin_only_speech(text, {})

        self.assertIn("R T Hopkinson", plan.text)
        self.assertNotIn("R. T.", plan.text)

    def test_an_attached_waw_before_a_latin_name_becomes_and(self):
        from popup.speech_worker import build_latin_only_speech

        text = (
            "رَصَدَ الضَّابِطَانِ ر. ت. هُوبْكِنْسُون "
            "(R. T. Hopkinson) وَجُو دِنِيس (Joe Denis) "
            "مَقْصَفَ هُولِوُود (Hollywood)."
        )
        plan = build_latin_only_speech(text, {})

        self.assertIn("R T Hopkinson and Joe Denis", plan.text)
        self.assertNotIn("وَجُو", plan.text)
        self.assertNotIn("Hopkinson Joe Denis", plan.text)
        self.assertEqual(plan.learned["جو دنيس"], "Joe Denis")
        self.assertEqual(plan.learned["وجو دنيس"], "Joe Denis")

    def test_a_latin_name_starting_with_w_keeps_its_w(self):
        from popup.speech_worker import build_latin_only_speech

        text = "دُفِنَتْ فِي مَقْبَرَةِ وُودْلُون (Woodlawn)."
        plan = build_latin_only_speech(text, {})

        self.assertIn("Woodlawn", plan.text)
        self.assertNotIn("and Woodlawn", plan.text)
        self.assertEqual(plan.learned["وودلون"], "Woodlawn")

    def test_and_is_highlighted_with_the_arabic_name_it_replaces(self):
        from popup.speech_worker import build_latin_only_speech

        text = (
            "رَصَدَ الضَّابِطَانِ ر. ت. هُوبْكِنْسُون "
            "(R. T. Hopkinson) وَجُو دِنِيس (Joe Denis) "
            "مَقْصَفَ هُولِوُود (Hollywood)."
        )
        plan = build_latin_only_speech(text, {})
        name_start = text.index("وَجُو")
        name_span = (name_start, name_start + len("وَجُو دِنِيس"))
        mapped = {
            plan.text[start:end]: plan.display_spans[index]
            for index, (start, end) in enumerate(plan.words)
        }

        self.assertEqual(mapped["and"], name_span)
        self.assertEqual(mapped["Joe"], name_span)
        self.assertEqual(mapped["Denis"], name_span)

    def test_a_later_occurrence_without_waw_is_not_given_and(self):
        from popup.speech_worker import build_latin_only_speech

        text = (
            "ذَهَبَ وَجُو دِنِيس (Joe Denis) إِلَى الْبَيْتِ. "
            "ثُمَّ عَادَ جُو دِنِيس إِلَى الْبَيْتِ."
        )
        plan = build_latin_only_speech(text, {})

        self.assertEqual(plan.text.count("Joe Denis"), 2)
        self.assertEqual(plan.text.count("and Joe Denis"), 1)

    def test_the_spoken_plan_does_not_pause_before_and(self):
        from popup.speech_worker import (
            _with_foreign_pauses, build_latin_only_speech,
        )

        text = (
            "رَصَدَ الضَّابِطَانِ ر. ت. هُوبْكِنْسُون "
            "(R. T. Hopkinson) وَجُو دِنِيس (Joe Denis) "
            "مَقْصَفَ هُولِوُود (Hollywood)."
        )
        spoken = _with_foreign_pauses(build_latin_only_speech(text, {}).text)

        self.assertIn("R T Hopkinson and Joe Denis،", spoken)


class CalendarWordsTest(unittest.TestCase):
    """Arabic month names stay Arabic even when a glossary learned them."""

    def test_a_parenthetical_month_is_not_learned(self):
        from popup.speech_worker import learn_latin_pairs

        self.assertEqual(learn_latin_pairs("في يناير (January) 1947."), {})
        self.assertEqual(learn_latin_pairs("في ديسمبر (December) 1946."), {})

    def test_a_parenthetical_month_keeps_the_arabic_and_drops_the_parentheses(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech("في يناير (January) 1947.", {})
        self.assertEqual(plan.text, "في يناير 1947.")

        plan = build_latin_only_speech("في 8 ديسمبر (December) 1946.", {})
        self.assertEqual(plan.text, "في 8 ديسمبر 1946.")

    def test_a_month_already_in_the_glossary_is_not_replaced(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(
            "في 8 يَنَايِرَ 1947 و 9 دِيسِمْبَرَ 1946.",
            {"يناير": "January", "ديسمبر": "December"},
        )

        self.assertEqual(
            plan.text, "في 8 يَنَايِرَ 1947 و 9 دِيسِمْبَرَ 1946.")

    def test_a_month_inside_a_longer_pair_is_not_learned(self):
        from popup.speech_worker import (
            build_latin_only_speech, learn_latin_pairs,
        )

        self.assertEqual(
            learn_latin_pairs("في شهر يناير (January) 1947."), {})
        plan = build_latin_only_speech(
            "في شهر يناير (January) 1947.", {})
        self.assertEqual(plan.text, "في شهر يناير 1947.")

    def test_a_weekday_is_not_learned(self):
        from popup.speech_worker import learn_latin_pairs

        self.assertEqual(
            learn_latin_pairs("في الاثنين (Monday) 1947."), {})

    def test_a_calendar_unit_is_not_replaced(self):
        from popup.speech_worker import build_latin_only_speech

        plan = build_latin_only_speech(
            "في شهر يناير من عام 1947.",
            {"شهر": "month", "عام": "year"},
        )

        self.assertEqual(plan.text, "في شهر يناير من عام 1947.")


if __name__ == "__main__":
    unittest.main()
