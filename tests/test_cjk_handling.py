import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = PROJECT_ROOT / 'cli' / 'epubkit_pipeline'
sys.path.insert(0, str(PIPELINE_DIR))

from metadata_handler import (  # noqa: E402
    MAX_FILENAME_BYTES, format_filename, truncate_filename_bytes
)
from text_cleaner import (  # noqa: E402
    TextCleanOptions, clean_text_content, has_cjk_content, is_cjk_language
)


def _doc(body: str) -> bytes:
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        + body +
        '</body></html>'
    ).encode('utf-8')


class CjkPunctuationTests(unittest.TestCase):
    """
    CJK books use “ ” as ordinary quotation marks, …… as the ellipsis and ——
    as the dash. Latinizing them corrupts the text rather than tidying it.
    """

    def test_chinese_punctuation_is_preserved(self):
        cleaned, _ = clean_text_content(
            _doc('<p>他说：“我们明天就走。”她点了点头……夜晚——没有人说话。</p>')
        )
        text = cleaned.decode('utf-8')

        self.assertIn('“我们明天就走。”', text)
        self.assertIn('……', text)
        self.assertIn('——', text)
        self.assertNotIn('"', text.split('<body>')[1])

    def test_latin_quotes_are_still_normalized(self):
        cleaned, _ = clean_text_content(
            _doc('<p>He said, “we leave tomorrow.” She nodded… it was long—very.</p>')
        )
        text = cleaned.decode('utf-8')

        self.assertIn('"we leave tomorrow."', text)
        self.assertIn('...', text)
        self.assertIn('--', text)

    def test_cjk_passage_in_a_latin_document_is_protected(self):
        cleaned, _ = clean_text_content(
            _doc('<p>他说：“走。”</p><p>He said, “go.”</p>')
        )

        self.assertIn('“走。”', cleaned.decode('utf-8'))

    def test_cjk_mode_can_be_forced_off(self):
        cleaned, _ = clean_text_content(
            _doc('<p>他说：“走。”</p>'), TextCleanOptions(cjk_mode=False)
        )

        self.assertIn('"走。"', cleaned.decode('utf-8'))

    def test_language_tag_detection(self):
        for tag in ('zh', 'zh-CN', 'zh_TW', 'ja', 'ko-KR', 'JA'):
            self.assertTrue(is_cjk_language(tag), tag)
        for tag in ('en', 'en-GB', 'fr', '', None):
            self.assertFalse(is_cjk_language(tag), tag)

    def test_content_detection_ignores_whitespace(self):
        self.assertTrue(has_cjk_content('　　他说'))
        self.assertFalse(has_cjk_content('   '))
        self.assertFalse(has_cjk_content('Hello, world.'))


class FilenameByteLimitTests(unittest.TestCase):
    """
    Filesystems cap names at 255 bytes, not characters. CJK codepoints take
    3 bytes each in UTF-8, so character-based truncation overflows the limit.
    """

    def test_long_cjk_title_fits_the_byte_budget(self):
        name = format_filename('金' * 120, '姜立涵')

        self.assertLessEqual(len(name.encode('utf-8')), MAX_FILENAME_BYTES)
        self.assertTrue(name.endswith('.epub'))

    def test_truncation_lands_on_a_character_boundary(self):
        name = truncate_filename_bytes('英' * 100 + '.epub')

        # Would raise UnicodeDecodeError if a multi-byte char had been split.
        self.assertEqual(name.encode('utf-8').decode('utf-8'), name)
        self.assertTrue(name.endswith('.epub'))

    def test_short_names_are_untouched(self):
        self.assertEqual(truncate_filename_bytes('book.epub'), 'book.epub')

    def test_long_ascii_title_is_also_capped(self):
        name = truncate_filename_bytes('a' * 300 + '.epub')

        self.assertLessEqual(len(name.encode('utf-8')), MAX_FILENAME_BYTES)
        self.assertTrue(name.endswith('.epub'))

    def test_degenerate_names_do_not_crash(self):
        for value in ('', '.epub', 'x'):
            self.assertIsInstance(truncate_filename_bytes(value), str)


if __name__ == '__main__':
    unittest.main()
