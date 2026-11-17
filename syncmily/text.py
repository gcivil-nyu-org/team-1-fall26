import unicodedata

# Format characters that emoji need: the zero-width joiner (family and
# profession emoji) and the tag characters used by subdivision flags.
_EMOJI_FORMAT = {"‍"} | {chr(c) for c in range(0xE0020, 0xE0080)}


def clean_visible_text(value):
    """
    Remove invisible format characters (zero-width spaces, RTL overrides,
    BOMs) but keep the ones emoji need, then trim. Text with no visible
    character left comes back empty, so required fields reject it.
    """
    kept = "".join(
        ch
        for ch in value or ""
        if unicodedata.category(ch) != "Cf" or ch in _EMOJI_FORMAT
    )
    if not any(not ch.isspace() and unicodedata.category(ch) != "Cf" for ch in kept):
        return ""
    return kept.strip()
