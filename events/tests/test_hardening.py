"""Regression tests for the pre-testing-party hardening pass."""

import io
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import Membership, Workspace
from events.ai_suggestions import EventSuggestionError, generate_event_suggestion
from events.forms import EventForm
from events.models import Event
from events.services import event_ocr

UTC = dt_timezone.utc


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="hparent", password="x")
        cls.other = User.objects.create_user(username="hother", password="x")
        cls.workspace = Workspace.objects.create(name="Fam", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.other,
            role=Membership.Role.PARENT,
            invited_by=cls.admin,
            accepted_at=timezone.now(),
        )
        cls.create_url = reverse("events:workspace_create", args=[cls.workspace.pk])
        cls.scan_url = reverse("events:scan_ocr", args=[cls.workspace.pk])

    def form(self, date, start, end, zone="America/New_York", **extra):
        data = {
            "title": "T",
            "date": date,
            "start_time": start,
            "end_time": end,
            "timezone": zone,
            "transport_mode": "transit",
        }
        data.update(extra)
        return EventForm(data, user=self.admin, workspace=self.workspace)

    def make_event(self, start, end, **fields):
        event = Event.objects.create(
            title="Existing",
            starts_at=start,
            ends_at=end,
            timezone_name="UTC",
            workspace=self.workspace,
            created_by=self.admin,
            **fields,
        )
        event.participants.add(self.other)
        return event


def png_bytes(size=(60, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


def upload(size=(60, 30)):
    return SimpleUploadedFile("a.png", png_bytes(size), content_type="image/png")


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitTests(Base):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.admin)

    @patch("events.views.generate_event_suggestion")
    def test_ai_generation_is_limited_per_minute(self, mock_ai):
        mock_ai.return_value = {
            "title": "x",
            "date": None,
            "start_time": None,
            "end_time": None,
            "end_date": None,
            "location": None,
            "missing_fields": [],
        }
        payload = {
            "action": "generate_suggestion",
            "description": "Family dinner on Friday at six.",
            "timezone": "America/New_York",
        }
        for _ in range(5):
            self.assertEqual(
                self.client.post(self.create_url, payload).status_code, 200
            )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_ai.call_count, 5)
        self.assertContains(response, "too many suggestion requests")

    @patch("events.views.scan_event_image")
    def test_ocr_scan_is_limited_with_json_429(self, mock_scan):
        mock_scan.return_value = {"status": "ok"}
        for _ in range(5):
            response = self.client.post(self.scan_url, {"document": upload()})
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            self.scan_url,
            {"document": upload()},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("error", response.json())
        self.assertEqual(mock_scan.call_count, 5)


class OcrBoundsTests(Base):
    def test_oversized_pixel_count_is_rejected_before_load(self):
        data = png_bytes((4000, 4000))  # 16M pixels
        with patch.object(Image.Image, "load", side_effect=AssertionError("loaded")):
            with self.assertRaises(event_ocr.EventOCRError) as ctx:
                event_ocr.validate_and_load_image(
                    SimpleUploadedFile("a.png", data, content_type="image/png")
                )
        self.assertEqual(ctx.exception.code, "image_too_large")

    def test_pixel_cap_is_twelve_million(self):
        self.assertEqual(event_ocr.MAX_IMAGE_PIXELS, 12_000_000)

    def test_view_returns_400_json_for_huge_image(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.scan_url, {"document": upload((4000, 4000))})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")

    def test_busy_scanner_returns_429(self):
        self.client.force_login(self.admin)
        acquired = [event_ocr.OCR_SLOTS.acquire(blocking=False) for _ in range(2)]
        try:
            response = self.client.post(self.scan_url, {"document": upload()})
        finally:
            for ok in acquired:
                if ok:
                    event_ocr.OCR_SLOTS.release()
        self.assertEqual(response.status_code, 429)
        self.assertIn("busy", response.json()["error"])

    def test_slot_is_released_after_a_scan(self):
        self.client.force_login(self.admin)
        with patch(
            "events.services.event_ocr.extract_text_from_image",
            side_effect=event_ocr.EventOCRError("no_text", "none", 422),
        ):
            self.client.post(self.scan_url, {"document": upload()})
        acquired = [event_ocr.OCR_SLOTS.acquire(blocking=False) for _ in range(2)]
        try:
            self.assertTrue(all(acquired))
        finally:
            for ok in acquired:
                if ok:
                    event_ocr.OCR_SLOTS.release()

    def test_time_budget_stops_fallback_passes(self):
        calls = []

        def fake(image, lang, config, timeout):
            calls.append(config)
            return "x"

        image = Image.new("RGB", (1200, 800), "white")
        with (
            patch("events.services.event_ocr.pytesseract.image_to_string", fake),
            patch(
                "events.services.event_ocr.time_module.monotonic",
                side_effect=lambda: 100.0 * len(calls),
            ),
        ):
            with self.assertRaises(event_ocr.EventOCRError):
                event_ocr.extract_text_from_image(image)
        self.assertEqual(len(calls), 1)


class OcrAvailabilityTests(Base):
    def test_scan_tab_hidden_without_tesseract(self):
        self.client.force_login(self.admin)
        with patch("events.views.ocr_available", return_value=False):
            response = self.client.get(self.create_url)
        self.assertNotContains(response, "Scan Event")
        self.assertNotContains(response, "data-ocr-panel")

    def test_scan_tab_shown_with_tesseract(self):
        self.client.force_login(self.admin)
        with patch("events.views.ocr_available", return_value=True):
            response = self.client.get(self.create_url)
        self.assertContains(response, "Scan Event")

    def test_endpoint_still_answers_cleanly_when_missing(self):
        self.client.force_login(self.admin)
        with patch(
            "events.services.event_ocr.pytesseract.image_to_string",
            side_effect=event_ocr.pytesseract.TesseractNotFoundError(),
        ):
            response = self.client.post(self.scan_url, {"document": upload()})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "ocr_unavailable")


class DateBoundsTests(Base):
    def test_year_9999_is_a_field_error_not_a_crash(self):
        form = self.form("9999-12-31", "23:00", "23:59")
        self.assertFalse(form.is_valid())
        self.assertIn("date", form.errors)

    def test_year_0001_tokyo_is_a_field_error(self):
        form = self.form("0001-01-01", "00:30", "01:00", zone="Asia/Tokyo")
        self.assertFalse(form.is_valid())
        self.assertIn("date", form.errors)

    def test_end_date_bounded(self):
        form = self.form("2026-12-01", "10:00", "11:00", end_date="9999-12-31")
        self.assertFalse(form.is_valid())
        self.assertIn("end_date", form.errors)

    def test_post_year_9999_save_is_not_a_500(self):
        self.client.force_login(self.admin)
        token = EventForm(user=self.admin, workspace=self.workspace)[
            "submission_token"
        ].value()
        response = self.client.post(
            self.create_url,
            {
                "action": "save",
                "submission_token": token,
                "title": "T",
                "date": "9999-12-31",
                "start_time": "23:00",
                "end_time": "23:59",
                "timezone": "America/New_York",
                "transport_mode": "transit",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_schedule_views_with_extreme_query_dates(self):
        self.client.force_login(self.admin)
        url = reverse("events:workspace_list", args=[self.workspace.pk])
        for view in ("day", "week", "upcoming"):
            for date in ("9999-12-31", "0001-01-01"):
                response = self.client.get(
                    url, {"view": view, "date": date, "timezone": "Asia/Tokyo"}
                )
                self.assertLess(response.status_code, 500, (view, date))


class OvernightTests(Base):
    def test_blank_end_date_with_earlier_end_time_means_next_day(self):
        form = self.form("2026-12-01", "22:00", "02:00")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.ends_at - form.event.starts_at, timedelta(hours=4))

    def test_equal_times_stay_an_error(self):
        form = self.form("2026-12-01", "22:00", "22:00")
        self.assertFalse(form.is_valid())

    def test_explicit_end_date_is_not_shifted(self):
        form = self.form("2026-12-01", "22:00", "02:00", end_date="2026-12-01")
        self.assertFalse(form.is_valid())


class DstInputTests(Base):
    def test_ambiguous_time_takes_the_first_occurrence(self):
        form = self.form("2026-11-01", "01:30", "03:00")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.event.starts_at.astimezone(UTC),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )  # EDT

    def test_nonexistent_time_names_the_clock_change(self):
        form = self.form("2027-03-14", "02:30", "04:00")
        self.assertFalse(form.is_valid())
        message = " ".join(form.errors["start_time"])
        self.assertIn("2:30 AM doesn't exist on Mar 14, 2027", message)
        self.assertIn("spring forward", message)
        self.assertIn("3:00 AM", message)

    def test_nonexistent_end_time_is_reported_on_end_time(self):
        form = self.form("2027-03-14", "01:00", "02:30")
        self.assertFalse(form.is_valid())
        self.assertIn("end_time", form.errors)


class ConflictTests(Base):
    def setUp(self):
        self.base = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
        self.make_event(self.base, self.base + timedelta(hours=1))

    def attempt(self, start, end, **extra):
        return self.form(
            "2026-12-01", start, end, zone="UTC", participants=[self.other.pk], **extra
        )

    def test_touching_events_do_not_conflict(self):
        form = self.attempt("16:00", "17:00")
        self.assertTrue(form.is_valid(), form.errors)
        form = self.attempt("14:00", "15:00")
        self.assertTrue(form.is_valid(), form.errors)

    def test_real_overlap_still_blocks(self):
        form = self.attempt("15:30", "16:30")
        self.assertFalse(form.is_valid())
        self.assertIn("Scheduling conflict", str(form.non_field_errors()))

    def test_tentative_existing_event_does_not_block(self):
        Event.objects.all().update(is_tentative=True)
        form = self.attempt("15:30", "16:30")
        self.assertTrue(form.is_valid(), form.errors)

    def test_new_tentative_event_does_not_block(self):
        form = self.attempt("15:30", "16:30", is_tentative="on")
        self.assertTrue(form.is_valid(), form.errors)


def groq_response(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


@patch.dict("os.environ", {"GROQ_API_KEY": "secret-test-key"})
class AiHardeningTests(SimpleTestCase):
    GENERIC = "AI suggestions are unavailable right now."

    def call(self):
        return generate_event_suggestion(
            "Family dinner tomorrow at 6 PM.", timezone_name="America/New_York"
        )

    @patch("events.ai_suggestions.requests.post")
    def test_auth_failure_is_generic_and_logged_without_key(self, mock_post):
        import requests

        failure = requests.HTTPError(
            "401 for secret-test-key", response=Mock(status_code=401)
        )
        mock_post.return_value = Mock(raise_for_status=Mock(side_effect=failure))
        with self.assertLogs("events.ai_suggestions", level="ERROR") as logs:
            with self.assertRaises(EventSuggestionError) as ctx:
                self.call()
        self.assertEqual(str(ctx.exception), self.GENERIC)
        self.assertNotIn("secret-test-key", "".join(logs.output))

    def test_missing_key_is_generic(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": ""}):
            with self.assertRaises(EventSuggestionError) as ctx:
                self.call()
        self.assertEqual(str(ctx.exception), self.GENERIC)

    @patch("events.ai_suggestions.requests.post")
    def test_token_budget_is_3000(self, mock_post):
        mock_post.return_value = groq_response({"status": "incomplete"})
        with self.assertRaises(EventSuggestionError):
            self.call()
        self.assertEqual(mock_post.call_args.kwargs["json"]["max_output_tokens"], 3000)

    @patch("events.ai_suggestions.requests.post")
    def test_list_body_is_a_clean_failure(self, mock_post):
        mock_post.return_value = groq_response([1, 2])
        with self.assertRaises(EventSuggestionError):
            self.call()

    @patch("events.ai_suggestions.requests.post")
    def test_non_dict_output_json_is_a_clean_failure(self, mock_post):
        body = {
            "status": "completed",
            "output": [
                "junk",
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps([1])}],
                },
            ],
        }
        mock_post.return_value = groq_response(body)
        with self.assertRaises(EventSuggestionError):
            self.call()


class TitleTests(Base):
    def test_zero_width_only_title_is_rejected(self):
        form = self.form("2026-12-01", "10:00", "11:00", title="​​")
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_rtl_override_is_stripped(self):
        form = self.form("2026-12-01", "10:00", "11:00", title="‮Dentist​ ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["title"], "Dentist")


class AnonymousCreateTests(Base):
    def test_anonymous_post_redirects_to_login(self):
        for payload in ({}, {"action": "save"}, {"action": "generate_suggestion"}):
            response = self.client.post(reverse("events:create"), payload)
            self.assertEqual(response.status_code, 302, payload)
            self.assertIn(reverse("accounts:login"), response.url)


class CssWrapTests(SimpleTestCase):
    def test_long_names_wrap(self):
        from django.contrib.staticfiles import finders

        with open(finders.find("events/paint.css")) as handle:
            css = handle.read()
        for selector in (".paint h1", ".paint .crumbs", ".thread-head .span"):
            rules = [
                line for line in css.splitlines() if line.startswith(selector + " ")
            ]
            self.assertTrue(
                any(
                    "overflow-wrap: anywhere" in r and "min-width: 0" in r
                    for r in rules
                ),
                selector,
            )
