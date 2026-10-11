"""
Text cleaner for Xteink X4 EPUB Optimizer.
Handles: whitespace normalization, OCR artifact repair, encoding fixes,
Unicode normalization, punctuation cleanup.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from lxml import etree


@dataclass
class TextCleanReport:
    double_spaces_fixed: int = 0
    ocr_ligatures_fixed: int = 0
    smart_quotes_normalized: int = 0
    encoding_issues_fixed: int = 0
    unicode_normalized: int = 0
    punctuation_fixed: int = 0
    total_fixes: int = 0

    def summary(self) -> str:
        parts = []
        if self.double_spaces_fixed:
            parts.append(f"{self.double_spaces_fixed} extra spaces")
        if self.ocr_ligatures_fixed:
            parts.append(f"{self.ocr_ligatures_fixed} OCR artifacts")
        if self.smart_quotes_normalized:
            parts.append(f"{self.smart_quotes_normalized} quotes normalized")
        if self.encoding_issues_fixed:
            parts.append(f"{self.encoding_issues_fixed} encoding fixes")
        if self.punctuation_fixed:
            parts.append(f"{self.punctuation_fixed} punctuation fixes")
        if self.unicode_normalized:
            parts.append(f"{self.unicode_normalized} unicode fixes")
        return ", ".join(parts) if parts else "no text issues found"

    def merge(self, other: 'TextCleanReport'):
        """Merge counts from another report into this one."""
        self.double_spaces_fixed += other.double_spaces_fixed
        self.ocr_ligatures_fixed += other.ocr_ligatures_fixed
        self.smart_quotes_normalized += other.smart_quotes_normalized
        self.encoding_issues_fixed += other.encoding_issues_fixed
        self.unicode_normalized += other.unicode_normalized
        self.punctuation_fixed += other.punctuation_fixed
        self.total_fixes += other.total_fixes


@dataclass
class TextCleanOptions:
    fix_whitespace: bool = True
    fix_ocr: bool = True
    normalize_quotes: bool = True
    fix_encoding: bool = True
    fix_punctuation: bool = True
    normalize_unicode: bool = True
    # CJK handling for quote normalization:
    #   None  - decide per document from its CJK character density
    #   True  - always treat as CJK (set from a zh/ja/ko dc:language)
    #   False - never treat as CJK
    cjk_mode: Optional[bool] = None


# Common OCR ligature/artifact mappings
OCR_LIGATURES = {
    '\ufb01': 'fi',
    '\ufb02': 'fl',
    '\ufb03': 'ffi',
    '\ufb04': 'ffl',
    '\ufb00': 'ff',
}

SMART_QUOTE_MAP = {
    '\u2018': "'",   # left single quote
    '\u2019': "'",   # right single quote
    '\u201c': '"',   # left double quote
    '\u201d': '"',   # right double quote
    '\u2014': '--',  # em dash
    '\u2013': '-',   # en dash
    '\u2026': '...', # ellipsis character
    '\u00a0': ' ',   # non-breaking space
    '\u201a': ',',   # low-9 quote (often misread comma)
}

# The subset of SMART_QUOTE_MAP that is still safe once the text is CJK.
# Everything else in that table rewrites standard Chinese/Japanese typography:
# \u201c \u201d are the normal Simplified Chinese quotation marks (not stylistic curls),
# \u2026\u2026 is the Chinese ellipsis, and \u2014\u2014 is the Chinese dash. Latinizing them is
# the same class of damage as replacing English quotes with random glyphs.
CJK_SAFE_QUOTE_MAP = {
    '\u00a0': ' ',   # non-breaking space
}

# Codepoint ranges that mark text as Chinese/Japanese/Korean.
CJK_RANGES = (
    (0x3040, 0x30ff),  # Hiragana + Katakana
    (0x3400, 0x4dbf),  # CJK Unified Ideographs Extension A
    (0x4e00, 0x9fff),  # CJK Unified Ideographs
    (0xac00, 0xd7af),  # Hangul Syllables
    (0xf900, 0xfaff),  # CJK Compatibility Ideographs
)

# BCP-47 primary subtags whose books should always be treated as CJK.
CJK_LANGUAGE_TAGS = ('zh', 'ja', 'ko')


def is_cjk_language(language: str) -> bool:
    """Check a dc:language value (e.g. 'zh', 'zh-CN', 'ja-JP') for a CJK tag."""
    if not language:
        return False
    primary = language.strip().lower().replace('_', '-').split('-')[0]
    return primary in CJK_LANGUAGE_TAGS


def has_cjk_content(text: str, threshold: float = 0.05) -> bool:
    """
    Report whether enough of `text` is CJK to treat it as CJK typography.

    The threshold is deliberately low. Latin quote normalization is cosmetic,
    but Latinizing CJK punctuation destroys meaning, so a book with only
    occasional Chinese passages should still keep its punctuation intact.
    """
    if not text:
        return False

    cjk = 0
    considered = 0
    for ch in text:
        if ch.isspace():
            continue
        considered += 1
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in CJK_RANGES):
            cjk += 1

    if not considered:
        return False

    return (cjk / considered) >= threshold

# Common mojibake patterns (UTF-8 bytes misinterpreted as Latin-1)
MOJIBAKE_PATTERNS = {
    '\u00c3\u00a9': '\u00e9',  # e-acute
    '\u00c3\u00a8': '\u00e8',  # e-grave
    '\u00c3\u00ab': '\u00eb',  # e-umlaut
    '\u00c3\u00a0': '\u00e0',  # a-grave
    '\u00c3\u00bc': '\u00fc',  # u-umlaut
    '\u00c3\u00b1': '\u00f1',  # n-tilde
    '\u00c3\u00a7': '\u00e7',  # c-cedilla
    '\u00c3\u00b6': '\u00f6',  # o-umlaut
    '\u00c3\u00a4': '\u00e4',  # a-umlaut
    '\u00c3\u00bc': '\u00fc',  # u-umlaut
    '\u00c2\u00a3': '\u00a3',  # pound sign
    '\u00c2\u00bb': '\u00bb',  # right guillemet
    '\u00c2\u00ab': '\u00ab',  # left guillemet
    '\u00c2\u00b0': '\u00b0',  # degree sign
}

# Tags whose text content should not be modified
SKIP_TAGS = frozenset({'script', 'style', 'pre', 'code', 'kbd', 'samp'})
HORIZONTAL_WHITESPACE = frozenset({' ', '\t', '\u00a0'})


def _is_horizontal_whitespace_only(text: str) -> bool:
    """Return True for text nodes that are intentionally just horizontal space."""
    return bool(text) and all(ch in HORIZONTAL_WHITESPACE for ch in text)


def _fix_whitespace(text: str) -> tuple[str, int]:
    """Fix multiple consecutive spaces, tabs within text."""
    count = 0
    # Multiple spaces/tabs to single space
    result, n = re.subn(r'[ \t]{2,}', ' ', text)
    count += n
    # Space before punctuation (but not ellipsis)
    result, n = re.subn(r'\s+([.,;:!?])', r'\1', result)
    count += n
    return result, count


def _fix_ocr_artifacts(text: str, normalize_quotes: bool = True,
                       is_cjk: bool = False) -> tuple[str, int, int]:
    """Fix OCR ligature artifacts and optionally normalize smart quotes."""
    lig_count = 0
    quote_count = 0

    # Fix ligatures
    for old, new in OCR_LIGATURES.items():
        if old in text:
            n = text.count(old)
            text = text.replace(old, new)
            lig_count += n

    # Normalize smart quotes/dashes. CJK text keeps its own punctuation.
    if normalize_quotes:
        quote_map = CJK_SAFE_QUOTE_MAP if is_cjk else SMART_QUOTE_MAP
        for old, new in quote_map.items():
            if old in text:
                n = text.count(old)
                text = text.replace(old, new)
                quote_count += n

    return text, lig_count, quote_count


def _fix_mojibake(text: str) -> tuple[str, int]:
    """Detect and repair common mojibake patterns."""
    count = 0
    for broken, fixed in MOJIBAKE_PATTERNS.items():
        if broken in text:
            n = text.count(broken)
            text = text.replace(broken, fixed)
            count += n
    return text, count


def _fix_punctuation(text: str) -> tuple[str, int]:
    """Fix common punctuation issues."""
    count = 0
    # 4+ dots → ellipsis
    result, n = re.subn(r'\.{4,}', '...', text)
    count += n
    # Missing space after sentence-ending punctuation before uppercase letter
    result, n = re.subn(r'([.!?])([A-Z])', r'\1 \2', result)
    count += n
    # Multiple consecutive commas
    result, n = re.subn(r',{2,}', ',', result)
    count += n
    # Multiple consecutive exclamation/question marks (more than 3)
    result, n = re.subn(r'([!?]){4,}', r'\1\1\1', result)
    count += n
    return result, count


def _get_local_tag(tag: str) -> str:
    """Get local tag name, stripping namespace."""
    if '}' in tag:
        return tag.split('}')[-1]
    return tag


def clean_text_content(xhtml_bytes: bytes, options: TextCleanOptions = None) -> tuple[bytes, TextCleanReport]:
    """
    Clean text content within XHTML, preserving markup structure.
    Processes el.text and el.tail for all elements except script/style/pre/code.

    Returns (cleaned_bytes, report).
    """
    if options is None:
        options = TextCleanOptions()

    report = TextCleanReport()

    try:
        tree = etree.fromstring(xhtml_bytes)
    except etree.XMLSyntaxError:
        parser = etree.HTMLParser(recover=True)
        tree = etree.fromstring(xhtml_bytes, parser)
        if tree is None:
            return xhtml_bytes, report

    # Decide CJK handling once for the whole document rather than per text node,
    # so a stray Latin-only paragraph in a Chinese book is treated consistently.
    is_cjk = options.cjk_mode
    if is_cjk is None:
        is_cjk = has_cjk_content(' '.join(tree.itertext()))

    def _process_text(text: str) -> str:
        """Apply all enabled text fixes to a string."""
        nonlocal report

        # Preserve intentionally spaced-out text nodes, such as redaction spans.
        if _is_horizontal_whitespace_only(text):
            return text

        if options.fix_whitespace:
            text, n = _fix_whitespace(text)
            report.double_spaces_fixed += n

        if options.fix_ocr:
            text, lig_n, quote_n = _fix_ocr_artifacts(text, options.normalize_quotes, is_cjk)
            report.ocr_ligatures_fixed += lig_n
            report.smart_quotes_normalized += quote_n

        if options.fix_encoding:
            text, n = _fix_mojibake(text)
            report.encoding_issues_fixed += n

        if options.fix_punctuation:
            text, n = _fix_punctuation(text)
            report.punctuation_fixed += n

        if options.normalize_unicode:
            normalized = unicodedata.normalize('NFC', text)
            if normalized != text:
                report.unicode_normalized += 1
            text = normalized

        return text

    # Walk all elements
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue

        local_tag = _get_local_tag(el.tag)
        if local_tag in SKIP_TAGS:
            continue

        # Process el.text
        if el.text:
            el.text = _process_text(el.text)

        # Process el.tail
        if el.tail:
            el.tail = _process_text(el.tail)

    report.total_fixes = (
        report.double_spaces_fixed +
        report.ocr_ligatures_fixed +
        report.smart_quotes_normalized +
        report.encoding_issues_fixed +
        report.punctuation_fixed +
        report.unicode_normalized
    )

    result = etree.tostring(tree, encoding='unicode', pretty_print=True)
    return result.encode('utf-8'), report
