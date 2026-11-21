"""Unit tests for syncmily/text.py, syncmily/safety.py, the limits and the safety
template filters (hardening spec section 8.3)."""

import copy
import pickle
import re
import threading
import time as clock
import unicodedata
import uuid
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import BadRequest, ImproperlyConfigured, ValidationError
from django.db import connection, models, transaction
from django.http import JsonResponse
from django.template import Context, Template
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import path, reverse
from hypothesis import given, settings
from hypothesis import strategies as st

from accounts.models import Membership, Workspace
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event, TripStart
from syncmily import limits
from syncmily.safety import (
    POLICIES,
    CleanCharField,
    CleanTextField,
    Params,
    QuotaExceeded,
    SafeCharField,
    SafeFormMixin,
    SafeTextField,
    enforce_quota,
    get_policy,
    parse_json_body,
    safe_hex_color,
    safe_next_url,
    validate_event_window,
    validate_text,
)
from syncmily.text import (
    DEFAULT_IGNORABLE_CHARS,
    EMOJI_MODIFIER_CHARS,
    EXTENDED_PICTOGRAPHIC_CHARS,
    GENERIC_MARK_CHARS,
    MARKUP_PATTERNS,
    ascii_skeleton,
    clean_visible_text,
    has_mark_flood,
    has_visible,
    is_malformed,
    longest_mark_run,
    normalize_text,
    truncate_graphemes,
)

ZWJ, ZWNJ, VS16 = "\u200d", "\u200c", "\ufe0f"
RLO = "\u202e"
BLACK_FLAG, CANCEL_TAG = "\U0001f3f4", "\U000e007f"


def flag(code):
    return BLACK_FLAG + "".join(chr(0xE0000 + ord(c)) for c in code) + CANCEL_TAG


FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
PROPERTY = settings(max_examples=300, derandomize=True, database=None, deadline=None)

# An alphabet that makes the rules meet: joiners, selectors, tags, flags, emoji, skin
# tones, generic and script marks, letters of ZWNJ scripts, controls and blanks.
TRICKY = [
    ZWJ,
    ZWNJ,
    VS16,
    "\ufe0e",
    "\u200b",
    RLO,
    "\u2066",
    "\u2060",
    "\ufeff",
    "\u00ad",
    "\u034f",
    "\u3164",
    "\u115f",
    "\u2800",
    "\u00a0",
    "\u2028",
    "\u0085",
    "\n",
    "\r",
    "\t",
    "\x1b",
    "\x00",
    " ",
    BLACK_FLAG,
    CANCEL_TAG,
    "\U000e0067",
    "\U000e0062",
    "\U000e0073",
    "\U000e0031",
    "\U000e0041",
    "\U0001f468",
    "\U0001f469",
    "\u2764",
    "\U0001f3fd",
    "\U0001f1fa",
    "#",
    "1",
    "\u20e3",
    "\u0301",
    "\u0300",
    "\u0316",
    "\u0489",
    "\u05bc",
    "\u0e49",
    "\u094d",
    "\u093f",
    "\u0915",
    "\u0937",
    "\u06cc",
    "\u062e",
    "a",
    "e",
    "Z",
    "<",
    "\U00011038",
    "\U00011013",
]
tricky_text = st.text(alphabet=st.sampled_from(TRICKY), max_size=60)
any_text = st.text(alphabet=st.characters(), max_size=120)
texts = st.one_of(tricky_text, any_text)


def kept_marks_per_base(value):
    """(longest generic run, most marks on one base) in the NFD form."""
    worst_generic = worst_total = generic = total = 0
    for ch in unicodedata.normalize("NFD", value):
        if unicodedata.category(ch) in ("Mn", "Me"):
            total += 1
            generic += ch in GENERIC_MARK_CHARS
            worst_generic = max(worst_generic, generic)
            worst_total = max(worst_total, total)
        else:
            generic = total = 0
    return worst_generic, worst_total


def allowed_formats(value):
    """True when every Cf character sits where emoji, flags or ZWNJ spelling need it."""
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == BLACK_FLAG:
            end = index + 1
            while end < len(value) and "\U000e0020" <= value[end] <= "\U000e007e":
                end += 1
            if end > index + 1:
                if end - index - 1 > 6 or value[end : end + 1] != CANCEL_TAG:
                    return False
                index = end + 1
                continue
        elif "\U000e0000" <= ch <= "\U000e0fff":
            return False  # a tag character outside a flag
        elif ch == ZWJ:
            before = value[:index].rstrip(
                "\ufe0e\ufe0f" + "".join(EMOJI_MODIFIER_CHARS)
            )
            after = value[index + 1 : index + 2]
            if not (
                before[-1:] in EXTENDED_PICTOGRAPHIC_CHARS
                and after in EXTENDED_PICTOGRAPHIC_CHARS
            ):
                return False
        elif ch == ZWNJ:
            if not (
                unicodedata.category(value[index - 1 : index] or "\x00")[0] in "LM"
                and unicodedata.category(value[index + 1 : index + 2] or "\x00") == "Lo"
            ):
                return False
        elif unicodedata.category(ch) == "Cf" or (
            ch in DEFAULT_IGNORABLE_CHARS and ch not in "\ufe0e\ufe0f"
        ):
            return False
        index += 1
    return True


class NormalizerPropertyTests(SimpleTestCase):
    def check_clean(self, value, multiline):
        out = normalize_text(value, multiline=multiline)
        self.assertEqual(normalize_text(out, multiline=multiline), out, ascii(value))
        self.assertEqual(unicodedata.normalize("NFC", out), out)
        allowed_controls = "\n\t" if multiline else ""
        self.assertFalse(
            [
                ch
                for ch in out
                if unicodedata.category(ch) == "Cc" and ch not in allowed_controls
            ],
            ascii(out),
        )
        self.assertFalse(set(out) & set("\u2028\u2029\u2800\u00a0\ud800"), ascii(out))
        self.assertTrue(allowed_formats(out), ascii(out))
        generic, total = kept_marks_per_base(out)
        self.assertLessEqual(generic, limits.GENERIC_MARKS_PER_BASE, ascii(out))
        self.assertLessEqual(total, limits.MARKS_PER_BASE, ascii(out))
        self.assertLessEqual(
            longest_mark_run(out, generic=True), limits.GENERIC_MARKS_PER_BASE
        )
        self.assertEqual(out, out.strip(" \n\t"))
        if not multiline:
            self.assertNotIn("  ", out)
        else:
            self.assertNotIn("\n\n\n", out)
        self.assertTrue(out == "" or has_visible(out), ascii(out))
        return out

    @PROPERTY
    @given(value=texts)
    def test_single_line_output_is_clean_and_stable(self, value):
        self.check_clean(value, multiline=False)

    @PROPERTY
    @given(value=texts)
    def test_multiline_output_is_clean_and_stable(self, value):
        self.check_clean(value, multiline=True)

    @PROPERTY
    @given(value=texts)
    def test_nfkc_mode_is_stable_too(self, value):
        out = normalize_text(value, nfkc=True)
        self.assertEqual(normalize_text(out, nfkc=True), out)
        self.assertEqual(unicodedata.normalize("NFKC", out), out)

    @PROPERTY
    @given(value=texts)
    def test_validators_raise_only_validation_errors(self, value):
        for name in POLICIES:
            try:
                clean = validate_text(value, name)
            except ValidationError:
                continue
            self.assertEqual(
                normalize_text(clean, multiline=POLICIES[name].multiline), clean
            )

    @PROPERTY
    @given(value=texts, length=st.integers(1, 40))
    def test_truncation_keeps_clean_text_clean(self, value, length):
        clean = normalize_text(value)
        cut = truncate_graphemes(clean, length)
        self.assertLessEqual(len(cut), max(length, 1))
        self.assertEqual(normalize_text(cut), cut, ascii(clean))

    def test_flood_check_matches_what_the_normalizer_drops(self):
        self.assertTrue(has_mark_flood("a" + "\u0301" * 4))
        self.assertFalse(has_mark_flood("a" + "\u0301" * 3))
        # Marks split by an invisible character still count for the same letter.
        self.assertTrue(has_mark_flood("a\u0301\u0301\u034f\u0301\u0301"))
        # Other scripts keep up to 8 marks on one letter.
        self.assertFalse(has_mark_flood("\u0e14" + "\u0e49" * 8))
        self.assertTrue(has_mark_flood("\u0e14" + "\u0e49" * 9))
        self.assertFalse(has_mark_flood("plain ascii"))


class NormalizerExampleTests(SimpleTestCase):
    def test_emoji_and_scripts_survive(self):
        survivors = {
            "family": f"Soccer {FAMILY} night",
            "scotland": flag("gbsct"),
            "england": flag("gbeng"),
            "wales": flag("gbwls"),
            "keycaps": "#\ufe0f\u20e3 1\ufe0f\u20e3",
            "skin tone": "\U0001f44b\U0001f3fd hi",
            "technologist": "\U0001f9d1\U0001f3fd\u200d\U0001f4bb",
            "heart on fire": "\u2764\ufe0f\u200d\U0001f525",
            "rainbow flag": "\U0001f3f3\ufe0f\u200d\U0001f308",
            "country flag": "\U0001f1fa\U0001f1f8",
            "cafe": "Caf\u00e9",
            "vietnamese": "Nguy\u1ec5n",
            "devanagari conjunct": "\u0915\u094d\u0937\u093f",
            "thai": "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35",
            "arabic": "\u0645\u0631\u062d\u0628\u0627",
            "hebrew points": "\u05e9\u05c1\u05b8\u05dc\u05d5\u05b9\u05dd",
            "cjk": "\u6771\u4eac",
            "korean": "\ud55c\uad6d\uc5b4",
            "persian zwnj": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
            "devanagari zwnj": "\u0915\u094d\u200c\u0937",
            "we heart soccer": "We <3 soccer, 3 < 5",
        }
        for name, value in survivors.items():
            with self.subTest(name):
                expected = unicodedata.normalize("NFC", value)
                self.assertEqual(normalize_text(value), expected)
                self.assertFalse(has_mark_flood(value))

    def test_hostile_characters_go(self):
        cases = {
            "bidi": ("a" + RLO + "b\u2066c\u2069", "abc"),
            "zero width": ("a\u200bb\u2060c\ufeffd", "abcd"),
            "zwnj between latin letters": ("a\u200cb", "ab"),
            "zwj inside a word": ("pa\u200dss", "pass"),
            "zwj padding": ("A" + ZWJ * 40 + "B", "AB"),
            "tags without a flag": ("Hidden\U000e004f\U000e004b", "Hidden"),
            "tags after a flag but too many": (
                BLACK_FLAG + "\U000e0067" * 8,
                BLACK_FLAG,
            ),
            "nul and surrogate": ("a\x00b\ud800c", "abc"),
            "controls": ("A\x07B\x1bC\x7fD\x85E\u2028F\u2029G", "A B C D E F G"),
            "line breaks": ("Line one\nLine two\r\nx", "Line one Line two x"),
            "spaces": ("  a \u00a0\u3000 b  ", "a b"),
            "decomposed": ("Cafe\u0301", "Caf\u00e9"),
            "mark flood": ("a" + "\u0301" * 140, "\u00e1\u0301\u0301"),
            "mark with no base": ("\u0301abc d \u0301e", "abc d e"),
            "selector after a letter": ("a\ufe0fb", "ab"),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(normalize_text(value), expected)

    def test_text_that_draws_nothing_is_empty(self):
        for value in (
            "",
            None,
            "   ",
            "\u200b\u200b",
            RLO,
            "\u3164\u3164",
            "\u115f\u1160",
            "\uffa0",
            "\u2800",
            "\u3164\u2800",
            "\u034f" * 3,
            "\u17b4" * 3,
            "\ufe0f" * 3,
            "\u180b" * 3,
            "\u00ad",
            ZWJ,
            "\t\n",
        ):
            with self.subTest(ascii(value)):
                self.assertEqual(normalize_text(value), "")
                self.assertEqual(normalize_text(value, multiline=True), "")

    def test_multiline_keeps_lines_and_tabs(self):
        value = "Pickup:\r\n\tLeo\u2028Mia  \n\n\n\nThanks \x07"
        self.assertEqual(
            normalize_text(value, multiline=True), "Pickup:\n\tLeo\nMia\n\nThanks"
        )

    def test_never_raises_on_odd_input(self):
        for value in (12, 3.5, b"bytes", ["a"], object()):
            self.assertIsInstance(normalize_text(value), str)

    def test_aliases_point_at_the_one_normalizer(self):
        from accounts.forms import clean_display_text
        from checklists.models import clean_text

        self.assertIs(clean_display_text, clean_visible_text)
        self.assertIs(clean_text, clean_visible_text)
        self.assertEqual(clean_visible_text("a\nb"), "a b")

    def test_helpers(self):
        self.assertTrue(is_malformed("a\x00"))
        self.assertTrue(is_malformed("a\ud800"))
        self.assertFalse(is_malformed("plain"))
        self.assertFalse(is_malformed(None))
        self.assertEqual(longest_mark_run("a" + "\u0301" * 5), 5)
        self.assertEqual(longest_mark_run("\u00e1\u00e9"), 1)
        self.assertEqual(ascii_skeleton("Ian"), ascii_skeleton("lan"))
        self.assertEqual(ascii_skeleton("M0m"), ascii_skeleton("Mom"))
        self.assertEqual(ascii_skeleton("rnary"), ascii_skeleton("mary"))
        self.assertEqual(ascii_skeleton("vvill"), "wlll")
        self.assertFalse(has_visible("\u3164\u2800 \u200b"))
        self.assertTrue(has_visible("\U0001f600"))

    def test_truncation_respects_graphemes(self):
        self.assertEqual(truncate_graphemes("Hello world", 20), "Hello world")
        self.assertEqual(truncate_graphemes("Hello world", 8), "Hello w\u2026")
        self.assertEqual(truncate_graphemes("Cafe\u0301 au lait", 5), "Caf\u2026")
        self.assertEqual(truncate_graphemes(f"ab {FAMILY} cd", 6), "ab\u2026")
        self.assertEqual(
            truncate_graphemes("\U0001f1fa\U0001f1f8\U0001f1eb\U0001f1f7", 3),
            "\U0001f1fa\U0001f1f8\u2026",
        )
        self.assertEqual(truncate_graphemes("abc", 0), "")
        self.assertEqual(truncate_graphemes(None, 5), "")

    def test_a_100000_character_input_is_fast(self):
        hostile = [
            "![" * 50_000,
            "![a](" * 20_000,
            "data:" * 20_000,
            ("A" * 119 + " ") * 833,
            "<" * 100_000,
            "a" + "\u0301" * 99_999,
            "\u200b" * 100_000,
        ]
        for value in hostile:
            for name in POLICIES:
                started = clock.perf_counter()
                try:
                    validate_text(value, name)
                except ValidationError:
                    pass
                self.assertLess(clock.perf_counter() - started, 0.05, (value[:8], name))
            started = clock.perf_counter()
            any(pattern.search(value) for pattern in MARKUP_PATTERNS)
            self.assertLess(clock.perf_counter() - started, 0.05, value[:8])


class ValidateTextTests(SimpleTestCase):
    def message(self, raw, policy="TITLE"):
        with self.assertRaises(ValidationError) as caught:
            validate_text(raw, policy)
        return caught.exception.messages[0]

    def test_checks_and_messages_in_order(self):
        self.assertEqual(self.message(5), "This value is not text.")
        self.assertEqual(self.message("x" * 865), "Keep this under 200 characters.")
        self.assertEqual(
            self.message("a\x00" + "<b>"),
            "This text has characters that are not allowed.",
        )
        self.assertEqual(
            self.message("<b>a" + "\u0301" * 9), "Too many accent marks on one letter."
        )
        self.assertEqual(
            self.message("<b>bold</b>"), "Remove the HTML or image code from this text."
        )
        self.assertEqual(self.message("\u200b\u3164"), "Enter some visible text.")
        self.assertEqual(
            self.message("a " * 100 + "a"), "Keep this under 200 characters."
        )
        self.assertEqual(self.message("a", "SEARCH"), "Enter at least 2 characters.")

    def test_raw_bound_comes_before_unicode_work(self):
        # 864 raw characters normalize to 2 and pass; 865 are refused unread.
        self.assertEqual(validate_text("a" + " " * 862 + "b", "TITLE"), "a b")
        self.assertEqual(
            self.message("a" + " " * 863 + "b"), "Keep this under 200 characters."
        )

    def test_markup_is_refused(self):
        for raw in (
            "<img src=x onerror=alert(1)>",
            "<svg onload=alert(2)>",
            "</title>",
            "<!-- hidden -->",
            "<?xml",
            "![x](https://evil.example/a.png)",
            "data:image/svg+xml;base64,PHN2Zz4=",
            "DATA:text/html;base64,AAAA",
            "A" * 120,
            "x " + "Zm9v" * 40,
        ):
            with self.subTest(raw[:20]):
                self.assertEqual(
                    self.message(raw), "Remove the HTML or image code from this text."
                )

    def test_ordinary_text_with_angle_brackets_and_urls_is_fine(self):
        for raw in (
            "We <3 soccer, 3 < 5",
            "a < b",
            "a<3",
            "https://example.org/field?x=1",
            "javascript:alert(1)",
            "Pick up at 5 > 4",
            "[x](https://example.org)",
            "A" * 119,
        ):
            with self.subTest(raw):
                self.assertEqual(validate_text(raw, "TITLE"), raw)

    def test_policies_follow_the_spec_table(self):
        self.assertEqual(POLICIES["NAME"].max_length, 100)
        self.assertFalse(POLICIES["PLACE"].required)
        self.assertTrue(POLICIES["CHAT"].multiline)
        self.assertFalse(POLICIES["CHAT"].reject_mark_flood)
        self.assertFalse(POLICIES["SEARCH"].reject_markup)
        self.assertTrue(POLICIES["USERNAME"].nfkc)
        self.assertEqual(get_policy("TITLE", max_length=150).max_length, 150)
        self.assertEqual(get_policy("NAME", max_length=500).max_length, 100)
        # Chat trims a flood instead of refusing it.
        self.assertEqual(
            validate_text("a" + "\u0301" * 50, "CHAT"), "\u00e1\u0301\u0301"
        )
        self.assertEqual(validate_text("one\ntwo", "CHAT"), "one\ntwo")
        self.assertEqual(validate_text("", "PLACE"), "")


# ---------------------------------------------------------------------------
# Form fields, the form mixin and the model fields
# ---------------------------------------------------------------------------


class DemoForm(SafeFormMixin, forms.Form):
    title = forms.CharField(max_length=200)
    note = forms.CharField(required=False)
    story = forms.CharField(required=False, widget=forms.Textarea)
    secret = forms.CharField(widget=forms.PasswordInput, required=False)
    token = forms.CharField(widget=forms.HiddenInput, required=False)
    email = forms.EmailField(required=False)

    class Meta:
        text_policies = {"title": "TITLE", "story": "DESCRIPTION"}


class FamilyNameForm(SafeFormMixin, forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]


class SafeFormTests(SimpleTestCase):
    def test_plain_text_fields_get_their_policy(self):
        form = DemoForm()
        self.assertIsInstance(form.fields["title"], SafeCharField)
        self.assertEqual(form.fields["title"].policy.name, "TITLE")
        self.assertEqual(form.fields["note"].policy.name, "SHORT_TEXT")
        self.assertIsInstance(form.fields["story"], SafeTextField)
        self.assertTrue(form.fields["story"].policy.multiline)
        for name in ("secret", "token", "email"):
            self.assertNotIsInstance(form.fields[name], SafeCharField, name)
        self.assertIn('maxlength="200"', str(form["title"]))

    def test_validation_runs_through_the_policy(self):
        form = DemoForm({"title": "<b>Soccer</b>"})
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["title"], ["Remove the HTML or image code from this text."]
        )
        form = DemoForm(
            {"title": RLO + "Soccer\npractice", "story": "Line one\nLine two"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Soccer practice")
        self.assertEqual(form.cleaned_data["story"], "Line one\nLine two")
        self.assertFalse(DemoForm({"title": "   "}).is_valid())

    def test_a_refused_form_never_echoes_a_huge_value(self):
        form = DemoForm({"title": "x" * 60_000})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["title"], ["Keep this under 200 characters."])
        self.assertEqual(len(form.data["title"]), POLICIES["TITLE"].raw_limit + 1)
        self.assertLess(len(str(form["title"])), 2_000)

    def test_padding_is_not_mistaken_for_length(self):
        form = DemoForm({"title": "Soccer practice" + " " * 300 + "at the park"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Soccer practice at the park")

    def test_model_forms_take_the_column_policy(self):
        form = FamilyNameForm({"name": "The Reyes family"})
        self.assertIsInstance(form.fields["name"], SafeCharField)
        self.assertEqual(form.fields["name"].policy.name, "NAME")
        self.assertTrue(form.is_valid())
        self.assertFalse(FamilyNameForm({"name": "<img src=x>"}).is_valid())
        # Links stay legal in names: the invite email no longer carries the name.
        self.assertTrue(
            FamilyNameForm({"name": "Visit https://example.org"}).is_valid()
        )


class LengtheningTextTests(TestCase):
    """NFC turns some characters into two or three code points (U+0958 becomes ka
    plus a nukta). Text that fitted must never reach the database too long."""

    LONGER = "\u0958"

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user("grow_parent", password="x" * 14)
        cls.ws = Workspace.objects.create(name="Grow family", primary_admin=cls.parent)

    def setUp(self):
        self.client.force_login(self.parent)
        self.client.raise_request_exception = False

    def test_forms_refuse_or_fit_text_that_grows(self):
        token = signing.dumps(
            {
                "id": str(uuid.uuid4()),
                "user": self.parent.pk,
                "workspace": str(self.ws.pk),
            },
            salt="events.create",
        )
        event_post = {
            "action": "save",
            "submission_token": token,
            "title": "Trip",
            "date": "2027-01-05",
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": "America/New_York",
            "transport_mode": "transit",
        }
        posts = [
            (
                "events:workspace_create",
                [self.ws.pk],
                {**event_post, "title": self.LONGER * 150},
            ),
            (
                "events:workspace_create",
                [self.ws.pk],
                {**event_post, "location": self.LONGER * 200},
            ),
            ("checklists:create", [self.ws.pk], {"title": self.LONGER * 100}),
            (
                "accounts:family_home",
                [self.ws.pk],
                {
                    "location": self.LONGER * 200,
                    "latitude": "40.7",
                    "longitude": "-74.0",
                },
            ),
        ]
        for name, args, data in posts:
            with self.subTest(name):
                response = self.client.post(reverse(name, args=args), data)
                self.assertLess(response.status_code, 500)
        for event in Event.objects.all():
            self.assertLessEqual(len(event.title), 200)
            self.assertLessEqual(len(event.location), 255)
        self.ws.refresh_from_db()
        self.assertLessEqual(len(self.ws.home_location), 255)
        self.assertFalse(Checklist.objects.filter(workspace=self.ws).exists())

    def test_model_writes_fit_the_column(self):
        start = datetime(2027, 5, 4, 10, tzinfo=dt_timezone.utc)
        event = Event(
            created_by=self.parent,
            workspace=self.ws,
            title="Trip",
            location=self.LONGER * 200,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        event.full_clean(exclude=["created_by", "workspace"])
        event.save()
        self.assertLessEqual(len(event.location), 255)
        self.assertTrue(event.location.endswith("\u2026"))


class CleanModelFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user("safety_parent", password="x" * 14)
        cls.ws = Workspace.objects.create(
            name="Safety family", primary_admin=cls.parent
        )

    def test_every_user_text_column_normalizes(self):
        start = datetime(2027, 5, 4, 10, tzinfo=dt_timezone.utc)
        event = Event.objects.create(
            created_by=self.parent,
            workspace=self.ws,
            title=RLO + "Soccer\npractice",
            location="Pier\u200b 40",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        checklist = Checklist.objects.create(
            workspace=self.ws, created_by=self.parent, title="Bag\U000e004f"
        )
        item = ChecklistItem.objects.create(checklist=checklist, description="a\u200db")
        message = ChatMessage.objects.create(
            workspace=self.ws,
            sender=self.parent,
            content="one\r\ntwo" + RLO,
            event_label="Lbl\u2066",
        )
        trip = TripStart.objects.create(
            user=self.parent, event=event, mode="here", label="Home\x00\n"
        )
        Workspace.objects.filter(pk=self.ws.pk).update(home_location="")
        self.ws.name, self.ws.home_location = "Fam\u3164ily", "1 Main" + RLO
        self.ws.save()
        expected = {
            event: {"title": "Soccer practice", "location": "Pier 40"},
            checklist: {"title": "Bag"},
            item: {"description": "ab"},
            message: {"content": "one\ntwo", "event_label": "Lbl"},
            trip: {"label": "Home"},
            self.ws: {"name": "Family", "home_location": "1 Main"},
        }
        for instance, values in expected.items():
            for name, value in values.items():
                # The instance holds the clean text, and so does the database.
                self.assertEqual(getattr(instance, name), value, name)
                instance.refresh_from_db()
                self.assertEqual(getattr(instance, name), value, name)

    def test_bulk_create_normalizes_too(self):
        Checklist.objects.bulk_create(
            [Checklist(workspace=self.ws, created_by=self.parent, title=RLO + "List")]
        )
        self.assertTrue(Checklist.objects.filter(title="List").exists())

    def test_a_value_that_fitted_still_fits_after_normalizing(self):
        # Each U+FB2C becomes three code points under NFC.
        checklist = Checklist.objects.create(
            workspace=self.ws, created_by=self.parent, title="\ufb2c" * 150
        )
        self.assertLessEqual(len(checklist.title), 150)
        checklist.refresh_from_db()
        self.assertLessEqual(len(checklist.title), 150)

    def test_person_names_normalize_through_the_signal(self):
        user = get_user_model().objects.create_user(
            "safety_named", first_name=RLO + "Ann  Lee", last_name="Smith\u200b\n"
        )
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Ann Lee", "Smith"))
        user.first_name = RLO + "Bo"
        user.save(update_fields=["last_name"])
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Ann Lee")

    def test_fields_keep_migrations_and_forms_in_step(self):
        clean_fields = {
            (model._meta.label, f.name): f
            for model in (
                Event,
                TripStart,
                Workspace,
                Checklist,
                ChecklistItem,
                ChatMessage,
            )
            for f in model._meta.get_fields()
            if isinstance(f, (CleanCharField, CleanTextField))
        }
        self.assertEqual(
            set(clean_fields),
            {
                ("events.Event", "title"),
                ("events.Event", "location"),
                ("events.TripStart", "label"),
                ("accounts.Workspace", "name"),
                ("accounts.Workspace", "home_location"),
                ("checklists.Checklist", "title"),
                ("checklists.ChecklistItem", "description"),
                ("chat.ChatMessage", "content"),
                ("chat.ChatMessage", "event_label"),
            },
        )
        for key, model_field in clean_fields.items():
            self.assertIn(model_field.text_policy, POLICIES, key)
            name, path, _, kwargs = model_field.deconstruct()
            self.assertIn(
                path, ("django.db.models.CharField", "django.db.models.TextField")
            )
            self.assertNotIn("policy", kwargs)
            form_field = model_field.formfield()
            self.assertIsInstance(form_field, SafeCharField, key)
            self.assertEqual(form_field.max_length, model_field.max_length, key)
        self.assertIsInstance(
            ChatMessage._meta.get_field("content").formfield(), SafeTextField
        )
        used = {f.text_policy for f in clean_fields.values()}
        # The rest are for forms only (search box, AI description, usernames, the
        # mixin's default); every policy is in use somewhere.
        self.assertLessEqual(
            set(POLICIES) - used, {"SEARCH", "DESCRIPTION", "USERNAME", "SHORT_TEXT"}
        )
        self.assertIsInstance(Event._meta.get_field("title"), models.CharField)


# ---------------------------------------------------------------------------
# Params: every malformed value is a 400, never a 500
# ---------------------------------------------------------------------------


def params_view(request):
    params = Params(request.GET, request)
    kind = request.GET.get("kind")
    readers = {
        "choice": lambda: params.choice("value", {"day", "week"}, default="day"),
        "int": lambda: params.int("value", 1, 10_000, default=1),
        "date": lambda: str(params.date("value")),
        "time": lambda: str(params.time("value")),
        "float": lambda: params.float("value", -90, 90),
        "timezone": lambda: params.timezone("value", "UTC"),
        "text": lambda: params.text("value", "LABEL"),
        "token": lambda: params.token("value", r"[A-Za-z0-9-]{1,64}", 64),
        "next": lambda: params.next_url("value", default="/home/"),
    }
    return JsonResponse({"value": readers[kind]()})


urlpatterns = [path("params/", params_view)]


@override_settings(ROOT_URLCONF=__name__)
class ParamsTests(SimpleTestCase):
    def get(self, kind, value):
        return self.client.get("/params/", {"kind": kind, "value": value})

    def test_malformed_values_are_400(self):
        bad = {
            "choice": ["month", "DAY", "day "],
            "int": ["abc", "1.5", "-1", "0", "9" * 20, "1e3", "\u0663", " 1", "1\x00"],
            "date": [
                "2027-02-30",
                "0001-01-01",
                "2101-01-01",
                "20270101",
                "2027-W01-1",
            ],
            "time": ["24:00", "9:00", "09:60", "09:00:00", "noon"],
            "float": ["nan", "inf", "-inf", "1e999", "0x10", "91", "1,5"],
            "timezone": ["Mars/Olympus", "x" * 100, "UTC\x00", "../etc/passwd"],
            "text": ["a\x00b", "<img src=x>", "a" + "\u0301" * 9],
            "token": ["abc\n", "a" * 65, "a b", "\u0430bc"],
        }
        for kind, values in bad.items():
            for value in values:
                with self.subTest(kind=kind, value=ascii(value)):
                    self.assertEqual(self.get(kind, value).status_code, 400)

    def test_good_and_missing_values(self):
        good = {
            ("choice", "week"): "week",
            ("choice", ""): "day",
            ("int", "42"): 42,
            ("int", ""): 1,
            ("date", "2027-03-01"): "2027-03-01",
            ("time", "09:30"): "09:30:00",
            ("float", "-74.0"): -74.0,
            ("timezone", "America/New_York"): "America/New_York",
            ("timezone", ""): "UTC",
            ("text", "Pier \u200b40"): "Pier 40",
            ("token", "abc-123"): "abc-123",
        }
        for (kind, value), expected in good.items():
            with self.subTest(kind=kind, value=value):
                response = self.get(kind, value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["value"], expected)

    def test_open_redirect_matrix(self):
        unsafe = [
            "https://evil.example/steal",
            "//evil.example/",
            "///evil.example/",
            "/\\evil.example/",
            "\\\\evil.example",
            "/\t/evil.example",
            " //evil.example",
            "http:evil.example",
            "javascript:alert(1)",
            "%2F%2Fevil.example\r\n",
            "https://testserver@evil.example/",
            "/caf\u00e9/",
            "",
        ]
        for value in unsafe:
            with self.subTest(ascii(value)):
                self.assertEqual(self.get("next", value).json()["value"], "/home/")
        for value in ("/events/?view=day", "/accounts/profile/", "/%2F%2Fevil.example"):
            with self.subTest(value):
                self.assertEqual(self.get("next", value).json()["value"], value)

    @settings(max_examples=200, derandomize=True, database=None, deadline=None)
    @given(value=st.text(max_size=40))
    def test_accessors_raise_only_bad_request(self, value):
        params = Params({"v": value}, RequestFactory().get("/"))
        for read in (
            lambda: params.choice("v", {"a"}),
            lambda: params.int("v", -5, 5),
            lambda: params.date("v"),
            lambda: params.time("v"),
            lambda: params.float("v", -90, 90),
            lambda: params.timezone("v"),
            lambda: params.text("v", "TITLE"),
            lambda: params.token("v", r"[a-z]+", 10),
            lambda: params.next_url("v"),
        ):
            try:
                read()
            except BadRequest:
                pass

    def test_safe_next_url_needs_a_request(self):
        request = RequestFactory().get("/", secure=True)
        self.assertEqual(safe_next_url(request, "http://testserver/x"), "")
        self.assertEqual(
            safe_next_url(request, "https://testserver/x"), "https://testserver/x"
        )

    def test_safe_next_url_refuses_a_bare_word(self):
        """Live round 4 (ZAP): a bare word such as "foo" passed the host check as a
        relative URL, and redirect() then read it as a view name and raised
        NoReverseMatch, a 500. Only a path or an http(s) URL may pass."""
        request = RequestFactory().get("/")
        for value in ("foo", "accounts:dashboard", "dashboard/", "?x=1", "#top"):
            self.assertEqual(safe_next_url(request, value, "/home/"), "/home/")
        self.assertEqual(safe_next_url(request, "/day/?x=1", "/home/"), "/day/?x=1")


# ---------------------------------------------------------------------------
# Dates, colours and JSON bodies
# ---------------------------------------------------------------------------


class SmallHelperTests(SimpleTestCase):
    def test_event_window(self):
        ny = ZoneInfo("America/New_York")
        start = datetime(2027, 3, 1, 10, tzinfo=ny)
        validate_event_window(start, start + timedelta(days=90))
        validate_event_window(
            datetime(1900, 1, 1, tzinfo=ny), datetime(1900, 1, 1, 1, tzinfo=ny)
        )
        validate_event_window(
            datetime(2100, 12, 31, 22, tzinfo=ny), datetime(2100, 12, 31, 23, tzinfo=ny)
        )
        refused = [
            (start, start, "End time must be after start time."),
            (start, start - timedelta(hours=1), "End time must be after start time."),
            (
                start,
                start + timedelta(days=90, minutes=1),
                "A plan can last at most 90 days.",
            ),
            (
                datetime(1899, 12, 31, 23, tzinfo=ny),
                datetime(1900, 1, 1, 1, tzinfo=ny),
                "Choose a date between 1900 and 2100.",
            ),
            (
                datetime(2100, 12, 31, 23, tzinfo=ny),
                datetime(2101, 1, 1, 0, tzinfo=ny),
                "Choose a date between 1900 and 2100.",
            ),
            (
                datetime(1, 1, 1, tzinfo=dt_timezone.utc),
                datetime(1, 1, 2, tzinfo=dt_timezone.utc),
                "Choose a date between 1900 and 2100.",
            ),
        ]
        for first, last, message in refused:
            with self.subTest(first=first, last=last):
                with self.assertRaises(ValidationError) as caught:
                    validate_event_window(first, last, tz=ny)
                self.assertEqual(caught.exception.messages, [message])

    def test_hex_colours(self):
        self.assertEqual(safe_hex_color("#c62828", "#000000"), "#c62828")
        self.assertEqual(safe_hex_color("#fff", "#000000"), "#ffffff")
        self.assertEqual(safe_hex_color("0039A6", "#000000"), "#0039A6")
        for bad in (
            "><zz",
            "red",
            "#12",
            "#gggggg",
            "url(x)",
            "#c628;x",
            None,
            5,
            "#1234567",
        ):
            with self.subTest(bad):
                self.assertEqual(safe_hex_color(bad, "#0039A6"), "#0039A6")

    def test_json_bodies(self):
        self.assertEqual(parse_json_body(b'{"a": [1, {"b": 2}]}'), {"a": [1, {"b": 2}]})
        self.assertEqual(parse_json_body(b"[[[[[1]]]]]"), [[[[[1]]]]])
        for body in (
            b"[[[[[[1]]]]]]",
            b"{not json",
            b"[" * 100_000,
            b"1" * 70_000,
            b"\xff",
        ):
            with self.subTest(body[:10]):
                with self.assertRaises(BadRequest):
                    parse_json_body(body)


class TemplateFilterTests(SimpleTestCase):
    def render(self, source, **values):
        return Template("{% load safety %}" + source).render(Context(values))

    def test_filters_escape_and_isolate(self):
        self.assertEqual(
            self.render("{{ v|bdi }}", v="<b>x</b>"), "<bdi>&lt;b&gt;x&lt;/b&gt;</bdi>"
        )
        self.assertEqual(self.render("{{ v|bdi }}", v=None), "<bdi></bdi>")
        self.assertEqual(self.render("{{ v|short_name }}", v="Ann"), "<bdi>Ann</bdi>")
        long_name = "Alexandra-Maria " * 3 + '"<x>'
        html = self.render("{{ v|short_name }}", v=long_name)
        self.assertTrue(html.startswith('<bdi title="Alexandra-Maria Alexandra-Maria'))
        self.assertIn("&quot;&lt;x&gt;", html)
        self.assertNotIn("<x>", html)
        self.assertIn("\u2026</bdi>", html)
        self.assertEqual(
            self.render("{{ v|short_name:5 }}", v="Benjamin"),
            '<bdi title="Benjamin">Benj\u2026</bdi>',
        )
        self.assertEqual(self.render("{{ v|clamp:6 }}", v="<script>"), "&lt;scri\u2026")


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


class QuotaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user("quota_parent", password="x" * 14)
        cls.ws = Workspace.objects.create(name="Quota family", primary_admin=cls.parent)

    def test_each_kind_counts_its_rows(self):
        Checklist.objects.bulk_create(
            Checklist(workspace=self.ws, created_by=self.parent, title=f"L{n}")
            for n in range(limits.QUOTAS.checklists_per_family)
        )
        with transaction.atomic():
            with self.assertRaises(QuotaExceeded) as caught:
                enforce_quota(self.ws, "checklists")
        self.assertIn("200 checklists", caught.exception.messages[0])
        checklist = Checklist.objects.first()
        ChecklistItem.objects.bulk_create(
            ChecklistItem(checklist=checklist, description=f"i{n}")
            for n in range(limits.QUOTAS.items_per_checklist)
        )
        with transaction.atomic():
            with self.assertRaisesMessage(QuotaExceeded, "200 items"):
                enforce_quota(self.ws, "items_per_checklist", checklist=checklist)

    def test_events_per_day_and_rolling_cap(self):
        ny = ZoneInfo("America/New_York")
        day = date(2027, 6, 1)
        start = datetime.combine(day, time(8), ny)
        Event.objects.bulk_create(
            Event(
                created_by=self.parent,
                workspace=self.ws,
                title=f"E{n}",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
            )
            for n in range(limits.QUOTAS.events_per_day)
        )
        with transaction.atomic():
            with self.assertRaises(QuotaExceeded):
                enforce_quota(self.ws, "events_per_day", day=day, tz=ny)
            self.assertEqual(
                enforce_quota(
                    self.ws, "events_per_day", day=day + timedelta(days=1), tz=ny
                ),
                0,
            )
            self.assertEqual(enforce_quota(self.ws, "events_rolling_30d"), 40)

    def test_members_count_pending_invites(self):
        User = get_user_model()
        for n in range(limits.QUOTAS.members_per_family):
            Membership.objects.create(
                workspace=self.ws,
                user=User.objects.create(username=f"pending{n}", is_active=False),
                role="kid",
                invited_by=self.parent,
            )
        with transaction.atomic():
            with self.assertRaises(QuotaExceeded):
                enforce_quota(self.ws, "members")


class QuotaGuardTests(SimpleTestCase):
    def test_needs_a_transaction(self):
        # SimpleTestCase opens no transaction, and the guard fires before any query.
        with self.assertRaisesMessage(ImproperlyConfigured, "transaction.atomic"):
            enforce_quota("any-family", "checklists")

    def test_the_error_survives_copy_and_pickle(self):
        error = QuotaExceeded("Full.", kind="checklists", limit=200)
        for clone in (copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
            self.assertEqual(clone.messages, ["Full."])
            self.assertEqual(clone.code, "quota")
        self.assertEqual((error.kind, error.limit), ("checklists", 200))


class QuotaRaceTests(TransactionTestCase):
    """Two saves at once for one family: the row lock lets exactly one in."""

    def test_two_threads_cannot_both_slip_under_the_cap(self):
        User = get_user_model()
        parent = User.objects.create_user("race_parent", password="x" * 14)
        workspace = Workspace.objects.create(name="Race family", primary_admin=parent)
        cap = limits.QUOTAS.checklists_per_family
        Checklist.objects.bulk_create(
            Checklist(workspace=workspace, created_by=parent, title=f"L{n}")
            for n in range(cap - 1)
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def add_one(name):
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    enforce_quota(workspace, "checklists")
                    clock.sleep(0.2)  # widen the window for the other thread
                    Checklist.objects.create(
                        workspace=workspace, created_by=parent, title=name
                    )
                outcomes.append("saved")
            except QuotaExceeded:
                outcomes.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=add_one, args=(f"T{n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(outcomes), ["refused", "saved"])
        self.assertEqual(Checklist.objects.filter(workspace=workspace).count(), cap)


class LimitsTests(SimpleTestCase):
    def test_numbers_after_the_second_opinion(self):
        quotas = limits.QUOTAS
        self.assertEqual(quotas.events_per_day, 40)
        self.assertEqual(quotas.events_per_30_days, 1500)
        self.assertEqual(quotas.chat_messages_per_family, 50_000)
        self.assertEqual(quotas.chat_trim_to, 45_000)
        self.assertEqual(quotas.chat_retention_days, 365)
        self.assertEqual(quotas.chat_messages_per_user_per_day, 600)
        self.assertEqual(quotas.google_events_per_connection, 2000)
        self.assertEqual(
            (quotas.google_pages_per_sync, quotas.google_page_size), (8, 250)
        )
        self.assertEqual(
            (quotas.chat_sockets_per_user, quotas.chat_sockets_per_family), (5, 30)
        )
        self.assertEqual(quotas.members_per_family, 12)
        self.assertEqual(quotas.checklists_per_family, 200)
        self.assertEqual(quotas.items_per_checklist, 200)
        self.assertEqual(quotas.pending_invite_days, 14)
        self.assertEqual(limits.RATES["write_anonymous"].limit, 120)
        self.assertEqual(
            limits.RATES["event_write_minute"], limits.Rate(30, 60, "user")
        )
        self.assertFalse(hasattr(quotas, "events_total"))

    def test_every_rate_key_is_known(self):
        for scope, rate in limits.RATES.items():
            self.assertIn(rate.key, ("user", "ip", "user_and_ip", "recipient"), scope)
            self.assertGreater(rate.limit, 0, scope)
            self.assertGreater(rate.window, 0, scope)
        self.assertTrue(re.fullmatch(r"\d+k", limits.NGINX_BODY_CAP_DEFAULT))
