"""Unicode rules for user text: the one normalizer and its helpers.

Pure functions with no database access, so model fields, forms, template filters, the
chat consumer, importers and tests can call them anywhere. Hardening spec section 2.2,
with the changes in section 10.1, describes each rule. Every function runs in linear
time, and normalize_text never raises.
"""

import re
import unicodedata
from functools import lru_cache, partial

from syncmily import limits
from syncmily._emoji_data import (
    DEFAULT_IGNORABLE,
    EMOJI_MODIFIER,
    EXTENDED_PICTOGRAPHIC,
)

ZWJ = "\u200d"
ZWNJ = "\u200c"
VARIATION_SELECTORS = frozenset("\ufe0e\ufe0f")  # text and emoji presentation
BLACK_FLAG = "\U0001f3f4"
CANCEL_TAG = "\U000e007f"
KEYCAP_BASES = frozenset("#*0123456789")
ELLIPSIS = "\u2026"
WHITESPACE = frozenset(" \n\t")

# The generic combining-diacritic blocks: the ranges the database CHECK counts.
GENERIC_MARK_RANGES = (
    (0x0300, 0x036F),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x20D0, 0x20FF),
    (0xFE20, 0xFE2F),
)

# Scripts that spell words with a zero-width non-joiner (Persian, Urdu and the Indic
# scripts). ZWNJ survives only between two letters of one of these scripts, and both
# letters must belong to the same script.
ZWNJ_SCRIPTS = (
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0x0870, 0x08FF, "arabic"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFC, "arabic"),
    (0x0900, 0x097F, "devanagari"),
    (0xA8E0, 0xA8FF, "devanagari"),
    (0x0980, 0x09FF, "bengali"),
    (0x0A00, 0x0A7F, "gurmukhi"),
    (0x0A80, 0x0AFF, "gujarati"),
    (0x0B00, 0x0B7F, "oriya"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"),
    (0x0D00, 0x0D7F, "malayalam"),
    (0x0D80, 0x0DFF, "sinhala"),
)

# Text that looks like markup or an embedded image. A bare "<3" and "a < b" stay legal,
# and so do plain URLs and "javascript:" text, which render as inert text. Each pattern
# runs in linear time: no quantified group can rescan text another attempt has read.
MARKUP_PATTERNS = (
    re.compile(r"<[A-Za-z/!?]"),  # a tag start
    re.compile(r"(?i)\bdata:[\w/+.-]*;base64,"),  # a data URI
    re.compile(r"!\[[^\[\]]*\]\([^()]*\)"),  # a markdown image
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{120,}"),  # an unbroken base64 run
)

_MALFORMED = re.compile(r"[\x00\ud800-\udfff]")  # NUL and lone surrogates
_RUN_OF_SPACES = re.compile(" {2,}")
_BLANK_LINES = re.compile(r"\n{3,}")
_FLAG_TAGS = frozenset(
    chr(cp) for cp in [*range(0xE0030, 0xE003A), *range(0xE0061, 0xE007B)]
)
_TAGS = frozenset(chr(cp) for cp in range(0xE0020, 0xE0080))
_ASCII_PRINTABLE = re.compile(r"[!-~]")


def _expand(ranges):
    return frozenset(chr(cp) for first, last in ranges for cp in range(first, last + 1))


def _char_class(ranges):
    return "".join(f"\\U{first:08X}-\\U{last:08X}" for first, last in ranges)


EXTENDED_PICTOGRAPHIC_CHARS = _expand(EXTENDED_PICTOGRAPHIC)
EMOJI_MODIFIER_CHARS = _expand(EMOJI_MODIFIER)
DEFAULT_IGNORABLE_CHARS = _expand(DEFAULT_IGNORABLE)
GENERIC_MARK_CHARS = _expand(GENERIC_MARK_RANGES)

# Line breaks and other controls (step 3). Single-line text turns each into one space;
# multiline text keeps LF and TAB and turns the other line breaks into LF.
_C0_C1 = [*range(0x00, 0x20), *range(0x7F, 0xA0)]
_LINE_BREAKS = (0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029)
_ASCII_SINGLE_LINE = {cp: " " for cp in _C0_C1 if cp < 0x80}
_ASCII_MULTILINE = {
    **{cp: " " for cp in _C0_C1 if cp < 0x80 and cp not in (0x09, 0x0A)},
    **{cp: "\n" for cp in (0x0B, 0x0C, 0x0D)},
}


class _Tables:
    """Character classes read from this Python's own Unicode database, built once."""

    def __init__(self):
        category = unicodedata.category
        marks, formats, spaces = [], [], []
        # Every Mn, Me, Cf and Zs character sits in planes 0, 1 and 14.
        for first, last in ((0, 0x1FFFF), (0xE0000, 0xE0FFF)):
            for cp in range(first, last + 1):
                kind = category(chr(cp))
                if kind in ("Mn", "Me"):
                    marks.append(cp)
                elif kind == "Cf":
                    formats.append(cp)
                elif kind == "Zs":
                    spaces.append(cp)
        # Unassigned code points inside the generic diacritic blocks: this Python
        # cannot tell whether they are marks, but the database CHECK counts every code
        # point in those blocks, so they are dropped like invisible characters.
        formats += [
            cp
            for first, last in GENERIC_MARK_RANGES
            for cp in range(first, last + 1)
            if category(chr(cp)) == "Cn"
        ]
        self.marks = frozenset(map(chr, marks))
        self.formats = frozenset(map(chr, formats))
        invisible = sorted(set(formats) | {ord(ch) for ch in DEFAULT_IGNORABLE_CHARS})
        special = sorted(set(marks) | set(invisible))
        self.special = re.compile("[" + _char_class(_to_ranges(special)) + "]+")
        self.mark_runs = re.compile("[" + _char_class(_to_ranges(marks)) + "]+")
        # Quick checks that let most text skip the slow per-character work. A
        # character class with ranges above U+FFFF makes Python's regex engine test
        # every range one by one, so these classes stay in the BMP, and one coarse
        # range stands in for the rare combining marks of plane 1.
        bmp_marks = "[" + _char_class(_to_ranges(cp for cp in marks if cp < 0x10000))
        plane_1_marks = [cp for cp in marks if 0x10000 <= cp <= 0x1FFFF]
        controls = [*_C0_C1, 0x2028, 0x2029, 0x2800, *(cp for cp in spaces if cp != 32)]
        self.controls = re.compile(
            "[" + _char_class(_to_ranges(sorted(controls))) + "]"
        )
        self.pass_checks = (
            re.compile("[" + _char_class(_to_ranges(invisible)) + "]"),
            re.compile(bmp_marks + f"]{{{limits.GENERIC_MARKS_PER_BASE + 1},}}"),
            re.compile("[ \\n\\t]" + bmp_marks + "]"),
            re.compile(
                "[" + _char_class([(plane_1_marks[0], plane_1_marks[-1])]) + "]"
            ),
        )
        self.generic_runs = re.compile("[" + _char_class(GENERIC_MARK_RANGES) + "]+")
        emoji = (
            *EXTENDED_PICTOGRAPHIC,
            *EMOJI_MODIFIER,
            (0xFE0E, 0xFE0F),
            (0x200D, 0x200D),
        )
        self.emoji_clusters = re.compile("[" + _char_class(emoji) + "]+")
        single = {cp: " " for cp in [*_C0_C1, 0x2028, 0x2029, 0x2800, *spaces]}
        multi = {cp: " " for cp in [*_C0_C1, 0x2800, *spaces]}
        multi.update({cp: "\n" for cp in _LINE_BREAKS})
        del multi[0x09]
        del multi[0x0A]
        self.single_line = single
        self.multiline = multi


def _to_ranges(code_points):
    ranges = []
    for cp in code_points:
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    return ranges


@lru_cache(maxsize=None)
def _tables():
    return _Tables()


def _zwnj_script(ch):
    if not ch:
        return None
    cp = ord(ch)
    for first, last, script in ZWNJ_SCRIPTS:
        if first <= cp <= last:
            return script
    return None


def _keeps_zwnj(before, after):
    """True when ZWNJ sits between two letters of one script that spells with it."""
    script = _zwnj_script(before)
    return (
        script is not None
        and unicodedata.category(before)[0] in "LM"
        and _zwnj_script(after) == script
        and unicodedata.category(after)[0] == "L"
    )


def _ends_in_emoji(segment, emoji_tail):
    """Whether text ending with `segment` ends in a pictograph (plus skin tones)."""
    end = len(segment)
    while end and segment[end - 1] in EMOJI_MODIFIER_CHARS:
        end -= 1
    if not end:
        return emoji_tail
    return segment[end - 1] in EXTENDED_PICTOGRAPHIC_CHARS


def _drop_invisible_and_cap_marks(text, tables):
    """Steps 4 and 5 in one left-to-right pass. Returns (text, flooded).

    Every decision looks at what was kept so far and at the next raw character, so a
    second pass over the result keeps exactly the same characters. flooded is True
    when a mark was dropped because its base already had the most marks allowed.
    """
    # Nothing changes unless the text has an invisible character, a run of more marks
    # than the generic cap or a mark with no base. Most text has none.
    if text[:1] not in tables.marks and not any(
        check.search(text) for check in tables.pass_checks
    ):
        return text, False
    pieces = []
    flooded = False
    last = ""  # the last kept character
    emoji_tail = False  # the kept text ends in a pictograph, skin tones or a VS
    base = False  # combining marks may attach to the last kept character
    generic = total = 0  # marks kept on the current base
    position = 0
    for match in tables.special.finditer(text):
        start, end = match.span()
        if start > position:
            segment = text[position:start]
            pieces.append(segment)
            emoji_tail = _ends_in_emoji(segment, emoji_tail)
            last = segment[-1]
            base = last not in WHITESPACE
            generic = total = 0
        index = start
        while index < end:
            ch = text[index]
            following = text[index + 1] if index + 1 < len(text) else ""
            if ch == ZWJ:
                if emoji_tail and following in EXTENDED_PICTOGRAPHIC_CHARS:
                    pieces.append(ch)
                    last, emoji_tail, base = ch, False, False
            elif ch in VARIATION_SELECTORS:
                if last in EXTENDED_PICTOGRAPHIC_CHARS or last in KEYCAP_BASES:
                    pieces.append(ch)
                    last = ch
                    total += 1
            elif ch == ZWNJ:
                if _keeps_zwnj(last, following):
                    pieces.append(ch)
                    last, emoji_tail, base = ch, False, False
            elif ch in _TAGS:
                flag_end = index
                while flag_end < end and text[flag_end] in _FLAG_TAGS:
                    flag_end += 1
                if (
                    last == BLACK_FLAG
                    and 2 <= flag_end - index <= 6
                    and flag_end < end
                    and text[flag_end] == CANCEL_TAG
                ):
                    pieces.append(text[index : flag_end + 1])
                    last, emoji_tail, base = CANCEL_TAG, False, False
                    index = flag_end
                elif flag_end > index:
                    index = flag_end - 1  # drop the whole run of tag letters
            elif ch in DEFAULT_IGNORABLE_CHARS or ch in tables.formats:
                pass  # invisible: dropped
            elif base:  # a combining mark (Mn or Me) on a base character
                is_generic = ch in GENERIC_MARK_CHARS
                if total >= limits.MARKS_PER_BASE or (
                    is_generic and generic >= limits.GENERIC_MARKS_PER_BASE
                ):
                    flooded = True
                else:
                    pieces.append(ch)
                    last, emoji_tail = ch, False
                    total += 1
                    generic += is_generic
            index += 1
        position = end
    if position == 0:
        return text, False
    pieces.append(text[position:])
    return "".join(pieces), flooded


def _cap_joiners(text, tables):
    """At most limits.ZWJ_PER_EMOJI joiners in one run of emoji."""

    def cap(match):
        cluster = match.group()
        if cluster.count(ZWJ) <= limits.ZWJ_PER_EMOJI:
            return cluster
        kept, seen = [], 0
        for ch in cluster:
            if ch == ZWJ:
                seen += 1
                if seen > limits.ZWJ_PER_EMOJI:
                    continue
            kept.append(ch)
        return "".join(kept)

    if text.count(ZWJ) <= limits.ZWJ_PER_EMOJI:
        return text
    return tables.emoji_clusters.sub(cap, text)


def _controls(text, multiline, tables=None):
    """Step 3: controls and separators become spaces (or LF in multiline text)."""
    if multiline:
        text = text.replace("\r\n", "\n")
    if tables is None:
        if text.isprintable():
            return text
        return text.translate(_ASCII_MULTILINE if multiline else _ASCII_SINGLE_LINE)
    if tables.controls.search(text) is None:
        return text
    return text.translate(tables.multiline if multiline else tables.single_line)


def _unicode_steps(text, multiline, nfkc):
    """Steps 2 to 6 for text that is not plain ASCII. Returns (text, flooded)."""
    tables = _tables()
    decompose, compose = ("NFKD", "NFKC") if nfkc else ("NFD", "NFC")
    text = _controls(unicodedata.normalize(decompose, text), multiline, tables)
    flooded = False
    # Composing can only merge marks into letters, so one more pass settles the text.
    # The loop stops as soon as a pass would change nothing.
    for _ in range(3):
        cleaned, dropped = _drop_invisible_and_cap_marks(text, tables)
        cleaned = _cap_joiners(cleaned, tables)
        flooded = flooded or dropped
        composed = unicodedata.normalize(compose, cleaned)
        text = unicodedata.normalize(decompose, composed)
        if text == cleaned:
            break
    return composed, flooded


def _tidy_whitespace(text, multiline):
    """Step 7: collapse and trim spaces (and blank lines in multiline text)."""
    if not multiline:
        return _RUN_OF_SPACES.sub(" ", text).strip(" ")
    if " " in text or "\t" in text:
        text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip(" \t\n")


def normalize_text(value, *, multiline=False, nfkc=False):
    """Return the clean, storable form of user text. Never raises.

    NUL and lone surrogates go; controls and line separators become spaces (LF stays
    in multiline text); invisible format characters go, except the joiners, variation
    selectors and tags that emoji, flags and Persian or Indic spelling need; combining
    marks are capped per letter; the result is NFC (NFKC when nfkc is set), with spaces
    collapsed and trimmed. Text with nothing visible left becomes "".
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = _MALFORMED.sub("", value)
    if value.isascii():
        text = _controls(value, multiline)
    else:
        text = _unicode_steps(value, multiline, nfkc)[0]
    text = _tidy_whitespace(text, multiline)
    return text if has_visible(text) else ""


# Kept for the callers and tests that already use these names.
clean_visible_text = partial(normalize_text, multiline=False)


def has_visible(value):
    """True when the text holds a letter, digit, punctuation, symbol or emoji.

    Hangul fillers, other default-ignorable letters and the Braille blank (U+2800)
    draw nothing, so they do not count.
    """
    if not value:
        return False
    if value.isascii():
        return _ASCII_PRINTABLE.search(value) is not None
    for ch in value:
        if ch < "\x80":
            if "!" <= ch <= "~":
                return True
        elif ch == "\u2800" or ch in DEFAULT_IGNORABLE_CHARS:
            continue
        elif unicodedata.category(ch)[0] in "LNPS" or ch in EXTENDED_PICTOGRAPHIC_CHARS:
            return True
    return False


def is_malformed(value):
    """True when the text holds NUL or a lone surrogate, which no column can store."""
    return isinstance(value, str) and _MALFORMED.search(value) is not None


def has_mark_flood(value, *, multiline=False, nfkc=False):
    """True when normalize_text would drop marks because one letter carries too many:
    more than limits.GENERIC_MARKS_PER_BASE generic diacritics or more than
    limits.MARKS_PER_BASE marks in all. Forms reject such text; other paths trim it.
    """
    if not isinstance(value, str) or value.isascii():
        return False
    return _unicode_steps(_MALFORMED.sub("", value), multiline, nfkc)[1]


def longest_mark_run(value, *, generic=False):
    """Longest run of consecutive combining marks (Mn and Me) after NFD.

    With generic=True only marks from the generic diacritic blocks count, as in the
    database CHECK.
    """
    if not isinstance(value, str) or value.isascii():
        return 0
    tables = _tables()
    pattern = tables.generic_runs if generic else tables.mark_runs
    text = unicodedata.normalize("NFD", value)
    return max((len(m.group()) for m in pattern.finditer(text)), default=0)


def has_markup(value):
    """True when the text matches one of MARKUP_PATTERNS."""
    return any(pattern.search(value) for pattern in MARKUP_PATTERNS)


def _is_regional_indicator(ch):
    return "\U0001f1e6" <= ch <= "\U0001f1ff"


def _is_boundary(value, index):
    """True when a grapheme break may fall between value[index - 1] and value[index]."""
    before, after = value[index - 1], value[index]
    if before == ZWJ or (before == "\r" and after == "\n"):
        return False
    if (
        after == ZWJ
        or after in _TAGS
        or after in EMOJI_MODIFIER_CHARS
        or after in DEFAULT_IGNORABLE_CHARS
        or "\u1160" <= after <= "\u11ff"  # Hangul vowel and final jamo
        or unicodedata.category(after)[0] == "M"
    ):
        return False
    if _is_regional_indicator(before) and _is_regional_indicator(after):
        run = 0
        while index - run - 1 >= 0 and _is_regional_indicator(value[index - run - 1]):
            run += 1
        return run % 2 == 0  # never split a two-letter flag
    return True


def truncate_graphemes(value, n):
    """Cut text to at most n code points, ending in an ellipsis when it cut.

    The cut backs off so it never separates a letter from its marks, a joiner, a
    variation selector, a skin tone or a flag's tag characters.
    """
    if value is None:
        return ""
    value = str(value)
    if len(value) <= n:
        return value
    if n <= 0:
        return ""
    cut = n - 1
    while cut > 0 and not _is_boundary(value, cut):
        cut -= 1
    return value[:cut].rstrip() + ELLIPSIS


_SKELETON_PAIRS = (("rn", "m"), ("vv", "w"))
_SKELETON_MAP = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s"})


def ascii_skeleton(value):
    """A look-alike key for usernames: "Ian", "lan" and "1an" share one skeleton."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    for pair, single in _SKELETON_PAIRS:
        text = text.replace(pair, single)
    return text.translate(_SKELETON_MAP)
