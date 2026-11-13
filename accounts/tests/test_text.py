from django.test import SimpleTestCase

from syncmily.text import clean_visible_text


class CleanVisibleTextTests(SimpleTestCase):
    def test_invisible_only_is_empty(self):
        for value in ("​​", "‮", " ﻿ ", "‍", ""):
            self.assertEqual(clean_visible_text(value), "")

    def test_bidi_and_zero_width_are_removed(self):
        self.assertEqual(clean_visible_text("‮evil​"), "evil")

    def test_emoji_sequences_survive(self):
        family = "\U0001f468‍\U0001f469‍\U0001f467 trip"
        scotland = (
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
        )
        self.assertEqual(clean_visible_text(family), family)
        self.assertEqual(clean_visible_text(scotland), scotland)


class FamilyNameEmojiTests(SimpleTestCase):
    def test_family_name_keeps_emoji_and_drops_header_breaks(self):
        from accounts.forms import clean_display_text

        self.assertEqual(clean_display_text("Evil\nBcc: x@y.z‮"), "Evil Bcc: x@y.z")
        family = "The \U0001f468‍\U0001f469‍\U0001f467 crew"
        self.assertEqual(clean_display_text(family), family)
        self.assertEqual(clean_display_text("​\r\n‍"), "")
