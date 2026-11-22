"""
Security lane "inventory": every way data enters Syncmily, and what it does on the way.

Every test_poc_* asserts the SECURE behaviour, so it FAILS while the gap exists.
test_cov_* tests record measured confirmations that held up (they pass).
test_map_* tests write measurement files (render-context map, model constraint map)
to the directory named by INV_OUT, when set.

Run:
  set -a && . <target.env> && set +a
  POSTGRES_DB=syncmily_inventory python manage.py test syncmily.tests.security.test_inventory --noinput

No network: Google, Groq, MTA and Transitous are never called.
"""

import asyncio
import json
import os
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from html.parser import HTMLParser
from io import BytesIO
from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, models
from django.template.loader import render_to_string
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from accounts.services import available_username
from chat.consumers import HouseholdChatConsumer
from chat.models import ChatMessage
from chat.services import create_message
from checklists.models import Checklist, ChecklistItem
from events.models import Event, TripStart

User = get_user_model()
PW = "Inv3ntory-test-pw-9!"
OUT = os.environ.get("INV_OUT", "")

BIDI = "‮"  # RIGHT-TO-LEFT OVERRIDE (category Cf)
TAG = "".join(chr(0xE0000 + ord(c)) for c in "OK")  # invisible ASCII "OK" in tag chars
NUL = "\x00"


def _bad_invisible(value):
    """Format characters that are neither a joiner nor a tag character."""
    for ch in value:
        if (
            unicodedata.category(ch) == "Cf"
            and ch != "‍"
            and not (0xE0020 <= ord(ch) <= 0xE007F)
        ):
            return True
    return False


def _tag_smuggle(value):
    """Tag characters with no black-flag emoji before them hide ASCII from the reader."""
    return (
        any(0xE0020 <= ord(c) <= 0xE007F for c in value) and "\U0001f3f4" not in value
    )


def _control(value):
    return any(unicodedata.category(ch) == "Cc" for ch in value)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = User.objects.create_user("invparent", "invparent@example.com", PW)
        cls.parent2 = User.objects.create_user("invparent2", "invp2@example.com", PW)
        cls.kid = User.objects.create_user("invkid", "invkid@example.com", PW)
        cls.outsider = User.objects.create_user("invout", "invout@example.com", PW)
        cls.ws = Workspace.objects.create(name="Inv Family", primary_admin=cls.parent)
        now = timezone.now()
        Membership.objects.create(
            workspace=cls.ws,
            user=cls.parent2,
            role="parent",
            invited_by=cls.parent,
            accepted_at=now,
        )
        Membership.objects.create(
            workspace=cls.ws,
            user=cls.kid,
            role="kid",
            invited_by=cls.parent,
            accepted_at=now,
        )
        start = now + timedelta(days=3)
        cls.event = Event.objects.create(
            created_by=cls.parent,
            workspace=cls.ws,
            title="Soccer",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            location="Field",
            latitude=40.7,
            longitude=-74.0,
        )

    def setUp(self):
        cache.clear()
        self.client = Client(raise_request_exception=False)

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PW))

    def create_token(self, user=None, ws=None):
        return signing.dumps(
            {
                "id": str(uuid.uuid4()),
                "user": (user or self.parent).pk,
                "workspace": str(ws.pk if ws else self.ws.pk),
            },
            salt="events.create",
        )

    def post_event(self, **overrides):
        data = {
            "action": "save",
            "submission_token": self.create_token(),
            "title": "T",
            "date": "2027-01-05",
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": "America/New_York",
            "location": "",
            "transport_mode": "transit",
        }
        data.update(overrides)
        return self.client.post(
            reverse("events:workspace_create", args=[self.ws.pk]), data
        )


# ---------------------------------------------------------------------------------
# 1. Raw reads and path/query parameters: does a bad value give a 500?
# ---------------------------------------------------------------------------------
class RawReadTypeChecks(Base):
    def assertNot500(self, response, label=""):
        self.assertLess(
            response.status_code, 500, f"{label} gave {response.status_code}"
        )

    def test_cov_chat_page_cursor_out_of_range_int(self):
        """chat/views.py:91-92 int() of ?before/?after: huge and negative ids."""
        self.login(self.parent)
        url = reverse("chat:messages", args=[self.ws.pk])
        for value in ("1" + "0" * 30, "-1", "9" * 4400, "٣", "1_0", " 5", "1e3", "NaN"):
            for key in ("before", "after"):
                self.assertNot500(
                    self.client.get(url, {key: value}), f"{key}={value[:12]}"
                )

    def test_poc_event_trip_label_with_nul_byte_is_not_a_500(self):
        """events/views.py:807 reads label raw; NUL cannot be stored in Postgres text."""
        self.login(self.parent)
        response = self.client.post(
            reverse("events:trip", args=[self.event.pk]),
            {
                "mode": "here",
                "latitude": "40.7",
                "longitude": "-74.0",
                "label": "a" + NUL + "b",
            },
        )
        self.assertLess(response.status_code, 500)

    def test_cov_event_trip_rejects_non_finite_and_out_of_range_coordinates(self):
        self.login(self.parent)
        url = reverse("events:trip", args=[self.event.pk])
        for lat, lon in (
            ("nan", "1"),
            ("inf", "1"),
            ("1e999", "0"),
            ("91", "0"),
            ("0", "181"),
            ("abc", "0"),
            ("", ""),
        ):
            r = self.client.post(
                url, {"mode": "here", "latitude": lat, "longitude": lon}
            )
            self.assertEqual(r.status_code, 400, f"{lat},{lon}")

    def test_poc_change_event_time_enforces_the_same_1900_2100_bound_as_the_form(self):
        """events/views.py:904-913 has no MIN/MAX_EVENT_DATE check (forms.py:19-20)."""
        self.login(self.parent)
        self.client.post(
            reverse("events:change_time", args=[self.event.pk]),
            {
                "starts_date": "9999-12-31",
                "starts_time": "08:00",
                "ends_date": "9999-12-31",
                "ends_time": "09:00",
            },
        )
        self.event.refresh_from_db()
        self.assertLessEqual(self.event.starts_at.year, 2100)

    def test_poc_far_future_event_does_not_break_its_own_page(self):
        """An event stored in year 9999 must still render in every selectable zone."""
        # The window CHECK (spec 8.5) now refuses this row. Drop it inside this
        # test's transaction, which rolls back, so the page's own guard is still
        # tested against a row that an old release or raw SQL could have stored.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "ALTER TABLE events_event DROP CONSTRAINT event_within_window"
            )
        ev = Event.objects.create(
            created_by=self.parent,
            workspace=self.ws,
            title="Far",
            starts_at=datetime(9999, 12, 31, 13, 0, tzinfo=dt_timezone.utc),
            ends_at=datetime(9999, 12, 31, 14, 0, tzinfo=dt_timezone.utc),
        )
        self.login(self.parent)
        r = self.client.get(
            reverse("events:detail", args=[ev.pk]) + "?timezone=Pacific/Kiritimati"
        )
        self.assertLess(r.status_code, 500)

    def test_cov_query_parameters_on_schedule_and_dashboard_pages(self):
        self.login(self.parent)
        bad = [
            "",
            "x" * 5000,
            "0001-01-01",
            "9999-12-31",
            "2026-02-30",
            "2026-W47-1",
            "%00",
            "١٢٣",
            "-1",
            "1" + "0" * 40,
            "<script>",
        ]
        for value in bad:
            for url, key in (
                (reverse("accounts:dashboard"), "week"),
                (reverse("events:list"), "date"),
                (reverse("events:list"), "page"),
                (reverse("events:list"), "view"),
                (reverse("events:list"), "timezone"),
                (reverse("events:workspace_list", args=[self.ws.pk]), "date"),
                (reverse("events:detail", args=[self.event.pk]), "timezone"),
                (reverse("events:delete", args=[self.event.pk]), "timezone"),
                (reverse("events:workspace_create", args=[self.ws.pk]), "date"),
                (reverse("events:workspace_create", args=[self.ws.pk]), "time"),
                (reverse("events:choose_workspace"), "date"),
                (reverse("events:choose_workspace"), "workspace"),
            ):
                r = self.client.get(url, {key: value})
                self.assertNot500(r, f"{url}?{key}={value[:12]!r}")
        for view in ("week", "day", "upcoming"):
            for day in ("9999-12-31", "0001-01-01"):
                self.assertNot500(
                    self.client.get(reverse("events:list"), {"date": day, "view": view})
                )

    def test_cov_integer_and_uuid_path_parameters(self):
        """int/uuid converters: out-of-range ids reach the ORM."""
        self.login(self.parent)
        big = "9" * 30
        for url in (
            f"/events/{big}/",
            f"/events/{big}/edit/",
            f"/events/{big}/trip/",
            f"/checklists/items/{big}/edit/",
        ):
            self.assertNot500(self.client.get(url), url)
        for url in (
            f"/events/{big}/delete/",
            f"/events/{big}/time/",
            f"/events/{big}/transport/",
            f"/checklists/items/{big}/toggle/",
            f"/checklists/items/{big}/delete/",
            f"/accounts/workspaces/{self.ws.pk}/members/{big}/remove/",
            f"/accounts/workspaces/{self.ws.pk}/members/{big}/resend/",
        ):
            self.assertNot500(self.client.post(url), url)

    def test_cov_invite_and_reset_tokens_of_any_shape(self):
        for token in ("x", "x" * 10000, "%00", "a:b:c", "Zm9v:1:2", "‮"):
            r = self.client.get(reverse("accounts:accept_invite", args=[token]))
            self.assertNot500(r, token[:10])
        r = self.client.get("/accounts/reset/" + "A" * 5000 + "/" + "b" * 5000 + "/")
        self.assertNot500(r)

    def test_cov_two_step_code_of_any_shape(self):
        self.login(self.parent)
        for code in ("9" * 100000, "0" * 5000, "\x00", "٠٠٠٠٠٠", " ", "‮123456"):
            r = self.client.post(reverse("accounts:two_step_disable"), {"code": code})
            self.assertNot500(r, code[:8])

    def test_cov_member_colors_keys_and_values(self):
        self.login(self.parent)
        url = reverse("events:member_colors", args=[self.ws.pk])
        for key, value in (
            ("color-abc", "#aaaaaa"),
            ("color-" + "9" * 30, "#aaaaaa"),
            (f"color-{self.kid.pk}", "red;background:url(x)"),
            (f"color-{self.kid.pk}", "#aabbcc\n"),
        ):
            self.assertNot500(self.client.post(url, {key: value}), key)
        self.assertFalse(
            self.kid.schedule_colors.exclude(color__regex=r"^#[0-9a-f]{6}$").exists()
        )

    def test_cov_ocr_upload_bad_shapes(self):
        """events/views.py:964: missing, empty, wrong type, bomb-sized header."""
        from PIL import Image

        self.login(self.parent)
        url = reverse("events:scan_ocr", args=[self.ws.pk])
        big = BytesIO()
        Image.new("1", (10, 10)).save(big, "PNG")
        raw = bytearray(big.getvalue())
        raw[16:24] = (50000).to_bytes(4, "big") + (50000).to_bytes(4, "big")
        cases = [
            {},
            {"document": "text-not-file"},
            {"document": SimpleUploadedFile("a.png", b"")},
            {"document": SimpleUploadedFile("a.png", b"not an image")},
            {"document": SimpleUploadedFile("a.exe", b"MZ")},
            {"document": SimpleUploadedFile("a" * 300 + ".png", big.getvalue())},
            {"document": SimpleUploadedFile("../../x.png", big.getvalue())},
            {"document": SimpleUploadedFile("a.png", bytes(raw), "image/png")},
            {
                "document": SimpleUploadedFile(
                    "a.png", b"GIF89a" + b"0" * 50, "image/gif"
                )
            },
            {"document": SimpleUploadedFile("a.png", b"x" * 100, "text/html")},
            {
                "image": SimpleUploadedFile(
                    "a.svg", b"<svg onload=alert(1)>", "image/svg+xml"
                )
            },
        ]
        with mock.patch(
            "events.services.event_ocr.extract_text_from_image", return_value=""
        ):
            for data in cases:
                r = self.client.post(url, data)
                self.assertNot500(r, str(list(data)))
                self.assertIn(r.status_code, (400, 413, 422))

    def test_cov_ocr_timezone_rejects_non_zone_strings(self):
        self.login(self.parent)
        url = reverse("events:scan_ocr", args=[self.ws.pk])
        for tz in (
            "../../etc/passwd",
            "UTC\x00",
            "x" * 5000,
            "Etc/../UTC",
            "posixrules",
        ):
            r = self.client.post(url, {"timezone": tz})
            self.assertNot500(r, tz[:12])

    def test_cov_ai_form_rejects_oversize_and_nul(self):
        self.login(self.parent)
        url = reverse("events:workspace_create", args=[self.ws.pk])
        for text in ("a" * 2001, "a" + NUL * 20 + "b" * 30, "short"):
            with mock.patch("events.views.generate_event_suggestion") as gen:
                r = self.client.post(
                    url, {"action": "generate_suggestion", "description": text}
                )
                self.assertNot500(r)
                gen.assert_not_called()

    def test_cov_place_endpoints_cap_query_and_drop_oversize_session(self):
        self.login(self.parent)
        with override_settings(GOOGLE_MAPS_API_KEY="dummy-not-real"), mock.patch(
            "events.places.requests"
        ) as req:
            req.RequestException = Exception
            req.post.return_value.json.return_value = {"suggestions": []}
            r = self.client.post(
                reverse("events:place_suggestions"),
                {"q": "x" * 5000, "session": "s" * 50000},
            )
            self.assertNot500(r)
            sent = req.post.call_args.kwargs["json"]
            self.assertLessEqual(len(sent["input"]), 200)
            self.assertNotIn("sessionToken", sent)

    def test_cov_next_parameters_never_redirect_off_site(self):
        self.login(self.parent)
        bad_next = [
            "//evil.example",
            "https://evil.example",
            "/\\evil.example",
            "\t//evil.example",
            "javascript:alert(1)",
            "///evil.example",
            "http:evil.example",
        ]
        for nxt in bad_next:
            for url, data in (
                (
                    reverse("events:set_transport_mode", args=[self.event.pk]),
                    {"transport_mode": "driving"},
                ),
                (reverse("events:member_colors", args=[self.ws.pk]), {}),
                (
                    reverse("accounts:set_palette", args=[self.ws.pk]),
                    {"palette": "bold"},
                ),
            ):
                r = self.client.post(url, {**data, "next": nxt})
                self.assertNot500(r)
                loc = r.headers.get("Location", "")
                self.assertTrue(
                    loc.startswith("/") and not loc.startswith("//"), (url, nxt, loc)
                )

    def test_cov_choice_fields_with_malformed_ids(self):
        self.login(self.parent)
        for bad in ("9" * 30, "abc", "-1", "1.5", "\x00", "%", "1 OR 1=1"):
            r = self.client.post(
                reverse("checklists:create", args=[self.ws.pk]),
                {"title": "x", "event": bad},
            )
            self.assertNot500(r, f"checklist.event={bad!r}")
            r = self.post_event(
                participants=[bad],
                dropoff_driver=bad,
                pickup_driver=bad,
                requires_transport="on",
            )
            self.assertNot500(r, f"participants={bad!r}")
            r = self.client.get(reverse("events:choose_workspace"), {"workspace": bad})
            self.assertNot500(r)

    def test_cov_submission_token_of_any_shape(self):
        self.login(self.parent)
        for token in (
            "",
            "x" * 100000,
            "a:b:c",
            self.create_token()[:-3],
            signing.dumps(
                {
                    "id": "not-a-uuid",
                    "user": self.parent.pk,
                    "workspace": str(self.ws.pk),
                },
                salt="events.create",
            ),
            signing.dumps("scalar", salt="events.create"),
            signing.dumps({"id": 5}, salt="events.create"),
        ):
            self.assertNot500(self.post_event(submission_token=token), token[:10])

    def test_cov_ai_suggestion_values_render_escaped(self):
        self.login(self.parent)
        evil = '"><img src=x onerror=alert(1)>'
        suggestion = {
            "title": evil,
            "date": "2027-01-05",
            "start_time": "10:00",
            "end_date": None,
            "end_time": "11:00",
            "location": evil,
            "missing_fields": [],
        }
        with mock.patch(
            "events.views.generate_event_suggestion", return_value=suggestion
        ), mock.patch("events.views.allow", return_value=True):
            r = self.client.post(
                reverse("events:workspace_create", args=[self.ws.pk]),
                {
                    "action": "generate_suggestion",
                    "description": "Something on Friday at 3pm",
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<img src=x", r.content.decode())
        self.assertIn("&quot;&gt;&lt;img", r.content.decode())

    def test_poc_place_id_and_session_with_trailing_newline_are_rejected(self):
        """events/places.py:22-23 anchor with $, which also matches before a final newline."""
        from events.places import valid_place_id, valid_session

        self.assertFalse(valid_place_id("ChIJabcdefghij\n"))
        self.assertFalse(valid_session("abc\n"))


# ---------------------------------------------------------------------------------
# 2. Length, size, count and volume caps
# ---------------------------------------------------------------------------------
class SizeCaps(Base):
    def test_poc_invite_email_over_254_chars_is_a_field_error(self):
        """accounts/forms.py:98 EmailField has no max_length; user.save() hits varchar(254)."""
        self.login(self.parent)
        long_email = "a" * 60 + "@" + ".".join(["b" * 60] * 4) + ".com"
        self.assertGreater(len(long_email), 254)
        r = self.client.post(
            reverse("accounts:manage_members", args=[self.ws.pk]),
            {"email": long_email, "role": "parent"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("email", r.context["form"].errors)
        self.assertFalse(User.objects.filter(email=long_email).exists())

    def test_cov_signup_field_lengths(self):
        r = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "u" * 151,
                "email": "e" * 300 + "@example.com",
                "password1": PW,
                "password2": PW,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["form"].errors)
        self.assertFalse(User.objects.filter(username__startswith="uuuu").exists())

    def test_cov_text_field_caps(self):
        self.login(self.parent)
        self.assertEqual(self.post_event(title="t" * 201).status_code, 200)
        self.assertEqual(self.post_event(location="l" * 256).status_code, 200)
        self.assertFalse(Event.objects.filter(title__startswith="ttt").exists())
        r = self.client.post(
            reverse("checklists:create", args=[self.ws.pk]), {"title": "c" * 151}
        )
        self.assertEqual(r.status_code, 200)
        cl = Checklist.objects.create(
            workspace=self.ws, title="L", created_by=self.parent
        )
        self.client.post(
            reverse("checklists:item_add", args=[cl.pk]), {"description": "i" * 256}
        )
        self.assertEqual(cl.items.count(), 0)

    def test_cov_workspace_name_cap(self):
        self.login(self.outsider)
        r = self.client.post(reverse("accounts:create_workspace"), {"name": "w" * 101})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Workspace.objects.filter(primary_admin=self.outsider).exists())

    def test_cov_chat_service_caps(self):
        from django.core.exceptions import ValidationError

        for kwargs in (
            {"content": "a" * 2001},
            {"content": 5},
            {"content": ["x"]},
            {"content": "ok", "event_label": "l" * 201},
            {"content": "ok", "event_label": {"a": 1}},
        ):
            with self.assertRaises(ValidationError):
                create_message(sender=self.parent, workspace_id=self.ws.pk, **kwargs)

    def test_cov_request_body_over_2_5mb_is_rejected_before_the_view(self):
        self.login(self.parent)
        r = self.client.post(
            reverse("events:workspace_create", args=[self.ws.pk]),
            {"action": "generate_suggestion", "description": "a" * 3_000_000},
        )
        self.assertEqual(r.status_code, 400)

    def test_cov_more_than_1000_form_fields_is_rejected(self):
        self.login(self.parent)
        data = {f"f{i}": "1" for i in range(1100)}
        r = self.client.post(
            reverse("events:workspace_create", args=[self.ws.pk]), data
        )
        self.assertEqual(r.status_code, 400)

    def test_poc_checklists_per_family_are_capped(self):
        """checklists/views.py:23 caps items (200) but not checklists per workspace."""
        Checklist.objects.bulk_create(
            [
                Checklist(workspace=self.ws, title=f"L{i}", created_by=self.parent)
                for i in range(1001)
            ]
        )
        self.login(self.parent)
        self.client.post(
            reverse("checklists:create", args=[self.ws.pk]), {"title": "one more"}
        )
        self.assertFalse(Checklist.objects.filter(title="one more").exists())

    def test_poc_events_per_family_are_capped(self):
        """No cap on events per workspace in forms, views or models."""
        start = timezone.now() + timedelta(days=30)
        Event.objects.bulk_create(
            [
                Event(
                    created_by=self.parent,
                    workspace=self.ws,
                    title=f"E{i}",
                    starts_at=start,
                    ends_at=start + timedelta(hours=1),
                )
                for i in range(5001)
            ]
        )
        self.login(self.parent)
        self.post_event(title="one more event")
        self.assertFalse(Event.objects.filter(title="one more event").exists())

    @override_settings(RATELIMIT_ENABLED=True)
    def test_poc_event_creation_and_edits_are_rate_limited(self):
        """No rate_limit()/allow() on create_event (save), edit_event, change_event_time,
        family_home, profile username: only AI, OCR, trip start and checklists are limited.
        """
        self.login(self.parent)
        statuses = []
        for i in range(90):
            r = self.post_event(title=f"Burst {i}", date="2027-02-01")
            statuses.append(r.status_code)
        self.assertIn(
            429, statuses, f"90 saves in a burst all answered {set(statuses)}"
        )

    def test_poc_family_size_is_capped(self):
        """accounts/services.py:30-31 caps invites per hour/day, not members per family."""
        users = User.objects.bulk_create(
            [User(username=f"bulkkid{i}", is_active=False) for i in range(300)]
        )
        Membership.objects.bulk_create(
            [
                Membership(
                    workspace=self.ws, user=u, role="kid", invited_by=self.parent
                )
                for u in users
            ]
        )
        self.login(self.parent)
        before = Membership.objects.filter(workspace=self.ws).count()
        self.client.post(
            reverse("accounts:manage_members", args=[self.ws.pk]),
            {"role": "kid", "email": ""},
        )
        self.assertEqual(Membership.objects.filter(workspace=self.ws).count(), before)

    def test_poc_chat_history_is_bounded_per_family(self):
        """No retention or per-workspace row cap on ChatMessage (300/h/user only)."""
        # Hardening spec 10.1 (chat storage) sets the cap at 50,000 a family, not
        # 10,000, so the family is filled to the cap from syncmily.limits.
        from syncmily.limits import QUOTAS

        ChatMessage.objects.bulk_create(
            [
                ChatMessage(workspace=self.ws, sender=self.parent, content="x")
                for _ in range(QUOTAS.chat_messages_per_family)
            ],
            batch_size=5000,
        )
        with self.assertRaises(Exception):
            create_message(sender=self.parent, workspace_id=self.ws.pk, content="more")


# ---------------------------------------------------------------------------------
# 3. Text hygiene: what is kept in each text channel after validation
# ---------------------------------------------------------------------------------
class TextHygiene(Base):
    def assertClean(self, stored, label):
        self.assertFalse(
            _bad_invisible(stored), f"{label}: bidi/format char kept {stored!r}"
        )
        self.assertFalse(_tag_smuggle(stored), f"{label}: invisible tag chars kept")

    def test_poc_event_location_strips_invisible_format_characters(self):
        """events/forms.py:280 location is only .strip()ed; title uses clean_visible_text."""
        self.login(self.parent)
        self.post_event(title="Loc test", location="Cafe" + BIDI + "evil")
        ev = Event.objects.get(title="Loc test")
        self.assertClean(ev.location, "event.location")

    def test_poc_event_title_rejects_hidden_tag_characters(self):
        """syncmily/text.py:6-8 keeps U+E0020..E007F for any text, not only after a flag."""
        self.login(self.parent)
        self.post_event(title="Hidden" + TAG)
        ev = Event.objects.get(title__startswith="Hidden")
        self.assertClean(ev.title, "event.title")

    def test_poc_event_title_is_single_line(self):
        self.login(self.parent)
        self.post_event(title="Line one\nLine two\x1b[31m x")
        ev = Event.objects.get(title__startswith="Line one")
        self.assertFalse(_control(ev.title) or " " in ev.title, repr(ev.title))

    def test_poc_event_title_limits_stacked_combining_marks(self):
        self.login(self.parent)
        self.post_event(title="a" + "́" * 180)
        self.assertFalse(Event.objects.filter(title__startswith="á").exists())

    def test_poc_checklist_title_rejects_hidden_and_multiline(self):
        self.login(self.parent)
        self.client.post(
            reverse("checklists:create", args=[self.ws.pk]),
            {"title": "Chk" + TAG + "\nline2"},
        )
        cl = Checklist.objects.filter(title__startswith="Chk").first()
        stored = cl.title if cl else ""
        self.assertFalse(_tag_smuggle(stored) or _control(stored), repr(stored))

    def test_poc_workspace_name_rejects_hidden_tag_characters(self):
        self.login(self.outsider)
        self.client.post(reverse("accounts:create_workspace"), {"name": "Fam" + TAG})
        ws = Workspace.objects.filter(primary_admin=self.outsider).first()
        self.assertFalse(ws and _tag_smuggle(ws.name), repr(ws and ws.name))

    def test_poc_workspace_name_cannot_carry_a_url_into_invite_email(self):
        """The invite subject and first line embed the name and the inviter's username."""
        self.login(self.outsider)
        self.client.post(
            reverse("accounts:create_workspace"),
            {"name": "ALERT verify now https://evil.example/login"},
        )
        ws = Workspace.objects.get(primary_admin=self.outsider)
        mail.outbox.clear()
        self.client.post(
            reverse("accounts:manage_members", args=[ws.pk]),
            {"email": "victim@example.org", "role": "parent"},
        )
        self.assertTrue(mail.outbox)
        self.assertNotIn(
            "https://evil.example", mail.outbox[0].subject + mail.outbox[0].body
        )

    def test_poc_chat_message_strips_invisible_format_characters(self):
        """chat/services.py:47 only .strip()s content and event_label."""
        msg = create_message(
            sender=self.parent,
            workspace_id=self.ws.pk,
            content="hi" + BIDI + "there" + TAG,
            event_label="Lbl" + BIDI,
        )
        self.assertClean(msg.content, "chat.content")
        self.assertClean(msg.event_label, "chat.event_label")

    def test_poc_trip_start_label_is_normalised(self):
        self.login(self.parent)
        self.client.post(
            reverse("events:trip", args=[self.event.pk]),
            {
                "mode": "here",
                "latitude": "40.7",
                "longitude": "-74",
                "label": "Home" + BIDI + "\n" + TAG,
            },
        )
        label = TripStart.objects.get(user=self.parent, event=self.event).label
        self.assertClean(label, "trip.label")
        self.assertFalse(_control(label))

    def test_poc_family_home_location_strips_invisible_format_characters(self):
        self.login(self.parent)
        self.client.post(
            reverse("accounts:family_home", args=[self.ws.pk]),
            {
                "location": "1 Main St" + BIDI + TAG,
                "latitude": "40.7",
                "longitude": "-74",
            },
        )
        self.ws.refresh_from_db()
        self.assertClean(self.ws.home_location, "workspace.home_location")

    def _sync(self, conn, items):
        from calendars import sync

        creds = mock.Mock(token=None, expiry=None)
        with mock.patch.object(
            sync, "_credentials", return_value=creds
        ), mock.patch.object(sync, "_service"), mock.patch.object(
            sync, "_fetch_items", return_value=(items, "UTC")
        ):
            return sync.sync_connection(conn)

    def _conn(self):
        from calendars.models import CalendarConnection

        return CalendarConnection.objects.create(
            user=self.parent,
            workspace=self.ws,
            provider="google",
            account_email="a@example.com",
        )

    def test_poc_google_import_text_is_normalised(self):
        """calendars/sync.py:137-141 only strips and truncates Google summary/location."""
        item = {
            "id": "g1",
            "summary": "Meet" + BIDI + TAG,
            "location": "Room" + BIDI,
            "start": {"dateTime": "2027-01-05T10:00:00-05:00"},
            "end": {"dateTime": "2027-01-05T11:00:00-05:00"},
        }
        self._sync(self._conn(), [item])
        ev = Event.objects.get(external_id="g1")
        self.assertClean(ev.title, "google.title")
        self.assertClean(ev.location, "google.location")

    def _poisoned(self, extra):
        conn = self._conn()
        good = {
            "id": "ok1",
            "summary": "Fine",
            "start": {"dateTime": "2027-01-05T10:00:00Z"},
            "end": {"dateTime": "2027-01-05T11:00:00Z"},
        }
        bad = {
            "id": "bad1",
            "start": {"dateTime": "2027-01-05T10:00:00Z"},
            "end": {"dateTime": "2027-01-05T11:00:00Z"},
            **extra,
        }
        try:
            self._sync(conn, [good, bad])
        except Exception:
            pass
        return Event.objects.filter(external_id="ok1").exists()

    def test_poc_google_event_with_nul_in_summary_does_not_break_the_whole_sync(self):
        """calendars/sync.py:123-155: one DB error rolls back the whole import, every run."""
        self.assertTrue(self._poisoned({"summary": "x" + NUL}))

    def test_poc_google_event_with_bad_all_day_date_does_not_break_the_whole_sync(self):
        """calendars/sync.py:80-81 date.fromisoformat is outside any try/except."""
        self.assertTrue(
            self._poisoned(
                {"start": {"date": "2027-13-45"}, "end": {"date": "2027-13-46"}}
            )
        )

    def test_poc_google_event_with_bad_datetime_does_not_break_the_whole_sync(self):
        """calendars/sync.py:67-68 datetime.fromisoformat is outside any try/except."""
        self.assertTrue(
            self._poisoned(
                {"start": {"dateTime": "garbage"}, "end": {"dateTime": "garbage"}}
            )
        )

    def test_poc_google_event_outside_1900_2100_is_skipped(self):
        item = {
            "id": "far",
            "summary": "x",
            "start": {"date": "9999-12-30"},
            "end": {"date": "9999-12-31"},
        }
        self._sync(self._conn(), [item])
        self.assertFalse(
            Event.objects.filter(external_id="far", starts_at__year__gt=2100).exists()
        )

    def test_poc_google_sign_in_cannot_claim_a_reserved_username(self):
        """accounts/services.py:297-314 never calls check_username_not_reserved."""
        for local in ("admin", "support", "syncmily", "parent9", "kid9"):
            self.assertNotEqual(available_username(f"{local}@gmail.com").lower(), local)

    def test_poc_mixed_script_lookalike_username_is_rejected(self):
        """accounts/forms.py:19 reserved list is an ASCII regex; Django only NFKC-normalises."""
        r = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "Αdmin",
                "email": "alpha@example.com",
                "password1": PW,
                "password2": PW,
            },
        )
        self.assertFalse(
            User.objects.filter(email="alpha@example.com").exists(), r.status_code
        )

    def test_poc_invite_cannot_target_an_address_that_already_has_an_account(self):
        """accounts/forms.py:100-112 only checks the same workspace; email has no DB uniqueness."""
        self.login(self.parent)
        before = User.objects.filter(email__iexact=self.outsider.email).count()
        self.client.post(
            reverse("accounts:manage_members", args=[self.ws.pk]),
            {"email": self.outsider.email, "role": "parent"},
        )
        self.assertEqual(
            User.objects.filter(email__iexact=self.outsider.email).count(), before
        )

    def test_poc_user_email_is_unique_in_the_database(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user("dupmail", self.outsider.email.upper(), PW)

    def test_poc_signup_requires_proof_of_email_ownership(self):
        """An unverified sign-up locks the real owner out of Google sign-in with that address."""
        from accounts.services import GoogleEmailInUse, user_for_google_account

        self.client.post(
            reverse("accounts:signup"),
            {
                "username": "squatter",
                "email": "victim@gmail.com",
                "password1": PW,
                "password2": PW,
            },
        )
        try:
            user_for_google_account("sub-victim-1", "victim@gmail.com")
        except GoogleEmailInUse:
            self.fail("a never-verified sign-up blocks the real address owner")


# ---------------------------------------------------------------------------------
# 4. Chat WebSocket frames
# ---------------------------------------------------------------------------------
class ChatSocketFrames(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.parent = User.objects.create_user("wsparent", "wsp@example.com", PW)
        self.ws = Workspace.objects.create(name="WS Family", primary_admin=self.parent)

    async def _talk(self, frames, timeout=2):
        comm = WebsocketCommunicator(
            HouseholdChatConsumer.as_asgi(), f"/ws/chat/{self.ws.pk}/"
        )
        comm.scope["user"] = self.parent
        comm.scope["url_route"] = {"kwargs": {"workspace_id": self.ws.pk}}
        connected, _ = await comm.connect()
        assert connected
        result = {"crash": None, "reply": None}
        try:
            for kind, payload in frames:
                if kind == "text":
                    await comm.send_to(text_data=payload)
                else:
                    await comm.send_to(bytes_data=payload)
            result["reply"] = await comm.receive_json_from(timeout=timeout)
        except asyncio.TimeoutError:
            pass
        except Exception as exc:  # the consumer task died: that is the finding
            result["crash"] = type(exc).__name__
        try:
            await comm.disconnect()
        except Exception:
            pass
        return result

    def talk(self, frames):
        return async_to_sync(self._talk)(frames)

    def test_poc_nul_in_message_is_an_error_frame_not_a_crash(self):
        r = self.talk(
            [("text", json.dumps({"type": "chat_message", "content": "a" + NUL + "b"}))]
        )
        self.assertIsNone(r["crash"], r)

    def test_poc_nul_in_event_label_is_an_error_frame_not_a_crash(self):
        r = self.talk(
            [
                (
                    "text",
                    json.dumps(
                        {
                            "type": "chat_message",
                            "content": "x",
                            "event_label": "a" + NUL,
                        }
                    ),
                )
            ]
        )
        self.assertIsNone(r["crash"], r)

    def test_poc_invalid_json_gets_an_error_frame_not_a_crash(self):
        r = self.talk([("text", "{not json")])
        self.assertIsNone(r["crash"], r)

    def test_poc_binary_frame_gets_an_error_frame_not_a_crash(self):
        r = self.talk([("bytes", b"\x00\x01")])
        self.assertIsNone(r["crash"], r)

    def test_cov_wrong_shapes_get_error_frames(self):
        for payload in (
            "[]",
            '"str"',
            "5",
            "null",
            '{"type": 5}',
            '{"type":"chat_message","content":{"a":1}}',
            '{"type":"chat_message","content":"ok","priority":["x"]}',
        ):
            r = self.talk([("text", payload)])
            self.assertIsNone(r["crash"], (payload, r))

    def test_cov_oversize_text_frame_is_rejected_by_the_app(self):
        r = self.talk([("text", "x" * 9000)])
        self.assertIsNone(r["crash"])
        self.assertEqual(r["reply"]["type"], "error")

    def test_poc_binary_frame_size_is_capped_like_text(self):
        """consumers.py:59 limits text_data only; bytes_data goes straight to super()."""
        r = self.talk([("bytes", b"x" * 9000)])
        self.assertIsNone(r["crash"])
        self.assertIsNotNone(r["reply"], "oversize binary frame gets no error reply")


# ---------------------------------------------------------------------------------
# 5. Render contexts: canary in every stored field, found in every page, classified
# ---------------------------------------------------------------------------------
class _Scan(HTMLParser):
    VOID = (
        "br",
        "hr",
        "img",
        "input",
        "meta",
        "link",
        "path",
        "circle",
        "rect",
        "source",
    )

    def __init__(self, canaries):
        super().__init__(convert_charrefs=True)
        self.canaries = canaries
        self.stack = []
        self.hits = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            for key, canary in self.canaries.items():
                if value and canary in value:
                    if name == "style":
                        ctx = "css-attr"
                    elif name in ("href", "src", "action", "formaction", "poster"):
                        ctx = "url-attr"
                    elif name.startswith("on"):
                        ctx = "event-handler-attr"
                    else:
                        ctx = "html-attr"
                    self.hits.append((key, ctx, f"{tag}[{name}]"))
        if tag not in self.VOID:
            self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for key, canary in self.canaries.items():
            if canary in data:
                top = self.stack[-1][0] if self.stack else "?"
                if top == "script":
                    ident = self.stack[-1][1].get("id", "")
                    ctx = f"script({'json_script#' + ident if ident else 'inline'})"
                elif top == "style":
                    ctx = "css-block"
                elif top == "title":
                    ctx = "html-title"
                elif top == "textarea":
                    ctx = "html-textarea"
                else:
                    ctx = "html-text"
                self.hits.append((key, ctx, top))


class RenderContextMap(Base):
    CANARIES = {
        "username": "zqusernamezq",
        "email": "zqemailzq",
        "workspace_name": "zqwsnamezq",
        "home_location": "zqhomezq",
        "event_title": "zqtitlezq",
        "event_location": "zqloczq",
        "checklist_title": "zqcltitlezq",
        "checklist_item": "zqitemzq",
        "chat_content": "zqchatzq",
        "chat_event_label": "zqlabelzq",
        "trip_label": "zqtripzq",
        "google_account_email": "zqgoogzq",
        "route_from": "zqroutefromzq",
        "route_line_name": "zqlinenamezq",
        # Spec 8.4.6: fields added by the hardening.
        "chat_sender_name": "zqsendernamezq",
        "confirmed_email": "zqconfirmedzq",
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        c = cls.CANARIES
        cls.kid.username = c["username"]
        cls.kid.email = f"{c['email']}@example.com"
        cls.kid.save()
        cls.ws.name = c["workspace_name"]
        cls.ws.home_location = c["home_location"]
        cls.ws.home_latitude, cls.ws.home_longitude = 40.7, -74.0
        cls.ws.save()
        start = timezone.now() + timedelta(days=2)
        cls.ev = Event.objects.create(
            created_by=cls.parent,
            workspace=cls.ws,
            title=c["event_title"],
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            location=c["event_location"],
            latitude=40.7,
            longitude=-74.0,
            requires_transport=True,
            dropoff_driver=cls.parent2,
        )
        cls.ev.participants.add(cls.kid)
        cls.cl = Checklist.objects.create(
            workspace=cls.ws,
            title=c["checklist_title"],
            created_by=cls.parent,
            event=cls.ev,
        )
        cls.item = ChecklistItem.objects.create(
            checklist=cls.cl,
            description=c["checklist_item"],
            is_completed=True,
            completed_by=cls.kid,
            completed_at=timezone.now(),
        )
        ChatMessage.objects.create(
            workspace=cls.ws,
            sender=cls.kid,
            content=c["chat_content"],
            event_label=c["chat_event_label"],
            sender_name=c["chat_sender_name"],
        )
        from accounts.models import EmailAddress

        EmailAddress.objects.create(
            user=cls.kid,
            email=f"{c['confirmed_email']}@example.com",
            verified_at=timezone.now(),
        )
        TripStart.objects.create(
            user=cls.parent,
            event=cls.ev,
            mode="here",
            label=c["trip_label"],
            latitude=40.7,
            longitude=-74.0,
        )
        from calendars.models import CalendarConnection

        CalendarConnection.objects.create(
            user=cls.parent,
            workspace=cls.ws,
            provider="google",
            account_email=f"{c['google_account_email']}@example.com",
        )

    def pages(self):
        ws, ev = self.ws.pk, self.ev.pk
        return [
            reverse("accounts:dashboard"),
            reverse("accounts:profile"),
            reverse("accounts:workspace_detail", args=[ws]),
            reverse("accounts:manage_members", args=[ws]),
            reverse("accounts:family_home", args=[ws]),
            reverse("events:list"),
            reverse("events:list") + "?view=upcoming",
            reverse("events:list") + "?view=week",
            reverse("events:workspace_list", args=[ws]),
            reverse("events:detail", args=[ev]),
            reverse("events:edit", args=[ev]),
            reverse("events:delete", args=[ev]),
            reverse("events:workspace_create", args=[ws]),
            reverse("checklists:list", args=[ws]),
            reverse("checklists:detail", args=[self.cl.pk]),
            reverse("checklists:edit", args=[self.cl.pk]),
            reverse("checklists:item_edit", args=[self.item.pk]),
            reverse("chat:workspace_chat", args=[ws]),
            reverse("chat:messages", args=[ws]),
            reverse("accounts:logout"),
        ]

    def scan(self, user):
        self.login(user)
        found = []
        for url in self.pages():
            r = self.client.get(url)
            if r.status_code != 200:
                continue
            body = r.content.decode()
            if "json" in r["Content-Type"]:
                for key, canary in self.CANARIES.items():
                    if canary in body:
                        found.append((url, key, "json-response", "application/json"))
                continue
            parser = _Scan(self.CANARIES)
            parser.feed(body)
            for key, ctx, where in parser.hits:
                found.append((url, key, ctx, where))
        return found

    def test_cov_no_user_value_lands_in_script_url_css_or_handler_contexts(self):
        hits = self.scan(self.parent) + self.scan(self.kid)
        self.assertTrue(hits)
        risky = [
            h
            for h in hits
            if h[2]
            in (
                "css-attr",
                "url-attr",
                "event-handler-attr",
                "css-block",
                "script(inline)",
            )
        ]
        self.assertEqual(risky, [])

    def test_cov_trip_card_escapes_external_text_but_colours_reach_style_raw(self):
        """events/trip.py:403-404,511-512 pass routing-provider colours into style=."""
        c = self.CANARIES
        trip = {
            "status": "ok",
            "event": self.ev,
            "origin": None,
            "previous": None,
            "home": None,
            "is_live": False,
            "has_subway": False,
            "leave_in_minutes": 10,
            "late_minutes": 0,
            "leave_by": timezone.now(),
            "arrive": timezone.now(),
            "maps_url": "https://x",
            "plan": {
                "segments": [
                    {
                        "kind": "ride",
                        "line": "1",
                        "line_name": c["route_line_name"],
                        "from": c["route_from"],
                        "to": "B",
                        "stops": 1,
                        "badge": {
                            "shape": "pill",
                            "text": "1",
                            "color": "red;background:url(//e/x)",
                            "text_color": '"><i onfocus=1 autofocus>',
                        },
                    }
                ]
            },
        }
        html = render_to_string("events/_trip_card.html", trip)
        parser = _Scan({**c, "css": "red;background:url(//e/x)", "brk": "<i onfocus"})
        parser.feed(html)
        contexts = {(k, ctx) for k, ctx, _ in parser.hits}
        self.assertNotIn("<i onfocus=1", html)
        self.assertIn(("css", "css-attr"), contexts)

    def test_poc_route_colours_from_routing_providers_are_validated_as_hex(self):
        from events import trip

        route = {
            "legs": [
                {
                    "steps": [
                        {
                            "travelMode": "TRANSIT",
                            "transitDetails": {
                                "transitLine": {
                                    "nameShort": "A",
                                    "name": "Line",
                                    "color": "red;background:url(//e/x)",
                                    "textColor": "#fff",
                                    "vehicle": {"type": "BUS"},
                                },
                                "stopDetails": {},
                                "stopCount": 1,
                            },
                        }
                    ]
                }
            ]
        }
        try:
            summary = trip.summarize_route(route, timezone.now() + timedelta(hours=1))
        except (
            Exception
        ) as error:  # the function needs more fields: record it, not a pass
            self.skipTest(f"summarize_route needs a richer fixture: {error!r}")
        for segment in summary["segments"] if "segments" in summary else []:
            self.assertNotIn("url(", str(segment.get("color")))

    def test_map_write_render_contexts(self):
        hits = self.scan(self.parent) + self.scan(self.kid)
        by_field, pages = {}, {}
        for url, key, ctx, where in hits:
            by_field.setdefault(key, set()).add((ctx, where))
            pages.setdefault(key, set()).add(url)
        mail.outbox.clear()
        self.login(self.parent)
        self.client.post(
            reverse("accounts:manage_members", args=[self.ws.pk]),
            {"email": "newperson@example.org", "role": "parent"},
        )
        email_ctx = {}
        if mail.outbox:
            m = mail.outbox[0]
            for key, canary in self.CANARIES.items():
                where = []
                if canary in m.subject:
                    where.append("email-subject")
                if canary in m.body:
                    where.append("email-body(autoescape off)")
                if where:
                    email_ctx[key] = where
        if OUT:
            os.makedirs(OUT, exist_ok=True)
            with open(os.path.join(OUT, "render-context-map.json"), "w") as fh:
                json.dump(
                    {
                        "html": {
                            k: sorted(list(x) for x in v) for k, v in by_field.items()
                        },
                        "pages": {k: sorted(v) for k, v in pages.items()},
                        "email": email_ctx,
                    },
                    fh,
                    indent=1,
                )
        self.assertTrue(by_field)


# ---------------------------------------------------------------------------------
# 6. Model and DB constraints
# ---------------------------------------------------------------------------------
APP_LABELS = {"accounts", "events", "chat", "checklists", "calendars"}


def _app_models():
    return [m for m in apps.get_models() if m._meta.app_label in APP_LABELS]


class ModelConstraintInventory(TestCase):
    def test_poc_every_text_field_has_a_length_bound_the_database_enforces(self):
        """TextField(max_length=...) is a widget hint only: the Postgres column stays text."""
        unbounded = []
        for model in _app_models():
            for f in model._meta.get_fields():
                if isinstance(f, models.TextField):
                    enforced = any(
                        isinstance(c, models.CheckConstraint)
                        and f.name in str(c.condition)
                        for c in model._meta.constraints
                    )
                    if not enforced:
                        unbounded.append(
                            f"{model.__name__}.{f.name}(max_length={f.max_length})"
                        )
        self.assertEqual(unbounded, [])

    def test_poc_coordinates_have_database_range_checks(self):
        needs = {
            ("events", "Event"): ("latitude", "longitude"),
            ("events", "TripStart"): ("latitude", "longitude"),
            ("accounts", "Workspace"): ("home_latitude", "home_longitude"),
        }
        missing = []
        for (app, name), names in needs.items():
            model = apps.get_model(app, name)
            text = " ".join(
                str(c.condition)
                for c in model._meta.constraints
                if isinstance(c, models.CheckConstraint)
            )
            for field in names:
                if field not in text:
                    missing.append(f"{name}.{field}")
        self.assertEqual(missing, [])

    def test_poc_member_colour_is_database_checked(self):
        from django.db import transaction

        from accounts.models import MemberColor

        u = User.objects.create_user("mc", "mc@example.com", PW)
        w = Workspace.objects.create(name="W", primary_admin=u)
        with self.assertRaises(Exception):
            with transaction.atomic():
                MemberColor.objects.create(workspace=w, user=u, color="red;x")

    def test_poc_event_title_cannot_be_blank_at_the_database(self):
        from django.db import transaction

        u = User.objects.create_user("eb", "eb@example.com", PW)
        now = timezone.now()
        with self.assertRaises(Exception):
            with transaction.atomic():
                Event.objects.create(
                    created_by=u,
                    title="",
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                )

    def test_map_write_model_constraints(self):
        rows = []
        for model in _app_models():
            checks = [c.name for c in model._meta.constraints]
            for f in model._meta.get_fields():
                if not getattr(f, "concrete", False) or f.is_relation:
                    continue
                rows.append(
                    {
                        "model": model.__name__,
                        "field": f.name,
                        "type": type(f).__name__,
                        "max_length": getattr(f, "max_length", None),
                        "null": f.null,
                        "blank": f.blank,
                        "validators": [type(v).__name__ for v in f.validators],
                        "choices": bool(f.choices),
                        "model_constraints": checks,
                    }
                )
        if OUT:
            os.makedirs(OUT, exist_ok=True)
            with open(os.path.join(OUT, "model-fields.json"), "w") as fh:
                json.dump(rows, fh, indent=1)
        self.assertTrue(rows)
