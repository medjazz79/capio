"""Word highlighting for Japanese, Chinese, Korean, and Hindi.

Edge TTS reports (captured from real synthesis):

* Japanese: 音楽 / は / 言葉 / の / 限界 ...
* Chinese:  音乐 / 是 / 一种 / 超越 / 语言 ...

Both scripts are written without spaces. The old word splitter treated the
whole sentence as one word, so none of those boundaries matched and the
highlight never moved. Korean and Hindi use spaces and already matched; they
are kept here so a future change cannot quietly break them.
"""
import unittest

from popup import speech_worker


JAPANESE = "音楽は言葉の限界を超える普遍的な言語である。"
JAPANESE_BOUNDARIES = [
    "音楽", "は", "言葉", "の", "限界", "を", "超える", "普遍的", "な",
    "言語", "で", "ある",
]
CHINESE = "音乐是一种超越语言界限的普遍语言。"
CHINESE_BOUNDARIES = [
    "音乐", "是", "一种", "超越", "语言", "界限", "的", "普遍", "语言",
]
KOREAN = "음악은 말의 한계를 넘어서는 보편적인 언어이다."
KOREAN_BOUNDARIES = [
    "음악은", "말의", "한계를", "넘어서는", "보편적인", "언어이다",
]
HINDI = "संगीत एक सार्वभौमिक भाषा है जो शब्दों की सीमाओं से परे है।"
HINDI_BOUNDARIES = [
    "संगीत", "एक", "सार्वभौमिक", "भाषा", "है", "जो", "शब्दों", "की",
    "सीमाओं", "से", "परे", "है",
]


def _matched_words(text, boundaries):
    segments, _voices, spans = speech_worker._speech_segments_with_voices(
        text, "test-voice", target_language="ja", continuous=True)
    index = 0
    matched = []
    for boundary in boundaries:
        result = speech_worker._match_synthesis_boundary_span(
            boundary, segments[0], text, spans[0], index)
        if result is None:
            matched.append(None)
            continue
        start, end, index = result
        matched.append(text[start:end])
    return matched


class AsianHighlightTest(unittest.TestCase):
    def test_japanese_and_chinese_split_into_character_spans(self):
        for text in (JAPANESE, CHINESE):
            with self.subTest(text=text):
                words = [
                    text[start:end]
                    for start, end in speech_worker._word_spans(text)
                ]
                self.assertEqual(words[0], text[0])
                self.assertGreater(len(words), 5)
                self.assertTrue(all(len(word) == 1 for word in words[:-1]))

    def test_latin_words_inside_japanese_stay_whole(self):
        text = "この曲は C major と呼ばれる。"
        words = [
            text[start:end]
            for start, end in speech_worker._word_spans(text)
        ]
        self.assertIn("C", words)
        self.assertIn("major", words)

    def test_korean_words_stay_whole(self):
        words = [
            KOREAN[start:end]
            for start, end in speech_worker._word_spans(KOREAN)
        ]
        self.assertEqual(words[:3], ["음악은", "말의", "한계를"])

    def test_japanese_boundaries_advance_the_highlight(self):
        self.assertEqual(
            _matched_words(JAPANESE, JAPANESE_BOUNDARIES),
            JAPANESE_BOUNDARIES,
        )

    def test_chinese_boundaries_advance_the_highlight(self):
        self.assertEqual(
            _matched_words(CHINESE, CHINESE_BOUNDARIES),
            CHINESE_BOUNDARIES,
        )

    def test_korean_and_hindi_boundaries_still_advance(self):
        self.assertEqual(
            _matched_words(KOREAN, KOREAN_BOUNDARIES), KOREAN_BOUNDARIES)
        self.assertEqual(
            _matched_words(HINDI, HINDI_BOUNDARIES), HINDI_BOUNDARIES)

    def test_hindi_matras_are_kept_when_matching(self):
        self.assertEqual(
            speech_worker._normalize_boundary_token("संगीत"),
            "संगीत",
        )

    def test_clicking_a_character_starts_at_that_character(self):
        position = CHINESE.index("超")
        self.assertEqual(
            speech_worker.speech_start_offset(CHINESE, position), position)


if __name__ == "__main__":
    unittest.main()
