from datetime import date, timedelta
import io
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageOps
from pytesseract import TesseractError, TesseractNotFoundError

from accounts.models import Membership, Workspace
from events.models import Event
from events.services.event_ocr import (
    MAX_IMAGE_SIZE_BYTES,
    EventOCRError,
    _normalize_canvas_and_size,
    extract_text_from_image,
    parse_event_text,
    scan_event_image,
    validate_and_load_image,
)


def _make_image_bytes(
    image_format="PNG",
    size=(240, 120),
    color=(255, 255, 255),
    mode="RGB",
):
    buf = io.BytesIO()
    img = Image.new(mode, size, color=color)
    img.save(buf, format=image_format)
    return buf.getvalue()


def _make_uploaded_image(
    name="flyer.png",
    image_format="PNG",
    content_type="image/png",
    mode="RGB",
    size=(240, 120),
):
    return SimpleUploadedFile(
        name,
        _make_image_bytes(image_format=image_format, mode=mode, size=size),
        content_type=content_type,
    )


class EventOCRServiceUnitTests(TestCase):
    """Unit tests for image validation, preprocessing, OCR, and event parsing."""

    def test_validate_supports_jpg_png_and_webp(self):
        for name, fmt, mime, mode in (
            ("invite.jpg", "JPEG", "image/jpeg", "RGB"),
            ("invite.jpeg", "JPEG", "image/jpeg", "RGB"),
            ("poster.png", "PNG", "image/png", "RGBA"),
            ("schedule.webp", "WEBP", "image/webp", "RGB"),
        ):
            uploaded = _make_uploaded_image(
                name=name,
                image_format=fmt,
                content_type=mime,
                mode=mode,
            )
            loaded = validate_and_load_image(uploaded)
            normalized = _normalize_canvas_and_size(loaded)
            try:
                self.assertEqual(normalized.mode, "RGB")
            finally:
                normalized.close()
                loaded.close()

    def test_validate_applies_exif_orientation(self):
        img = Image.new("RGB", (120, 60), color=(240, 240, 240))
        exif = img.getexif()
        # 6 = 90 degrees clockwise stored, requiring transpose to portrait dimensions.
        exif[ImageOps.ExifTags.Base.Orientation] = 6
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())
        uploaded = SimpleUploadedFile(
            "rotated.jpg",
            buf.getvalue(),
            content_type="image/jpeg",
        )
        loaded = validate_and_load_image(uploaded)
        normalized = _normalize_canvas_and_size(loaded)
        try:
            self.assertLess(normalized.width, normalized.height)
        finally:
            normalized.close()
            loaded.close()

    def test_validate_rejects_missing_file(self):
        with self.assertRaises(EventOCRError) as ctx:
            validate_and_load_image(None)
        self.assertEqual(ctx.exception.code, "no_file")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_validate_rejects_oversized_image(self):
        huge = SimpleUploadedFile(
            "huge.jpg",
            b"x" * (MAX_IMAGE_SIZE_BYTES + 1),
            content_type="image/jpeg",
        )
        with self.assertRaises(EventOCRError) as ctx:
            validate_and_load_image(huge)
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertEqual(ctx.exception.http_status, 413)

    def test_validate_rejects_unsupported_extension_and_mime(self):
        pdf = SimpleUploadedFile(
            "schedule.pdf",
            b"%PDF-1.4 sample content",
            content_type="application/pdf",
        )
        with self.assertRaises(EventOCRError) as ctx:
            validate_and_load_image(pdf)
        self.assertEqual(ctx.exception.code, "unsupported_format")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_validate_rejects_spoofed_extension_with_non_image_bytes(self):
        spoofed = SimpleUploadedFile(
            "not_an_image.jpg",
            b"This is plain text disguised as a JPEG file.",
            content_type="image/jpeg",
        )
        with self.assertRaises(EventOCRError) as ctx:
            validate_and_load_image(spoofed)
        self.assertEqual(ctx.exception.code, "corrupted_image")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_validate_rejects_corrupted_truncated_png(self):
        raw_png = _make_image_bytes("PNG")
        corrupted = SimpleUploadedFile(
            "broken.png",
            raw_png[:32],
            content_type="image/png",
        )
        with self.assertRaises(EventOCRError) as ctx:
            validate_and_load_image(corrupted)
        self.assertEqual(ctx.exception.code, "corrupted_image")
        self.assertEqual(ctx.exception.http_status, 400)

    @patch("events.services.event_ocr.pytesseract.image_to_osd")
    @patch("events.services.event_ocr.pytesseract.image_to_string")
    def test_extract_text_handles_osd_rotation_and_low_quality_image(
        self, mock_to_string, mock_osd
    ):
        mock_osd.return_value = {"rotate": 90, "orientation_conf": 8.5}
        mock_to_string.return_value = (
            "School Science Fair\nNovember 20, 2026\n3:30 PM - 5:00 PM\n"
            "Brooklyn Elementary School"
        )
        # Small, low-resolution dark image exercises inversion and upscaling.
        img = Image.new("RGB", (180, 90), color=(20, 20, 20))
        try:
            text = extract_text_from_image(img)
        finally:
            img.close()
        self.assertIn("School Science Fair", text)
        mock_osd.assert_called_once()
        self.assertEqual(mock_to_string.call_count, 1)

    @patch(
        "events.services.event_ocr.pytesseract.image_to_osd",
        side_effect=TesseractNotFoundError(),
    )
    @patch(
        "events.services.event_ocr.pytesseract.image_to_string",
        side_effect=TesseractNotFoundError(),
    )
    def test_extract_text_raises_friendly_error_when_tesseract_missing(
        self, _mock_to_string, _mock_osd
    ):
        img = Image.new("RGB", (200, 100), color=(255, 255, 255))
        try:
            with self.assertRaises(EventOCRError) as ctx:
                extract_text_from_image(img)
        finally:
            img.close()
        self.assertEqual(ctx.exception.code, "ocr_unavailable")
        self.assertEqual(ctx.exception.http_status, 503)

    @patch(
        "events.services.event_ocr.pytesseract.image_to_osd",
        return_value="Rotate: 0\nOrientation confidence: 0.0",
    )
    @patch(
        "events.services.event_ocr.pytesseract.image_to_string",
        side_effect=TesseractError(1, "failed"),
    )
    def test_extract_text_raises_friendly_error_on_tesseract_failure(
        self, _mock_to_string, _mock_osd
    ):
        img = Image.new("RGB", (200, 100), color=(255, 255, 255))
        try:
            with self.assertRaises(EventOCRError) as ctx:
                extract_text_from_image(img)
        finally:
            img.close()
        self.assertEqual(ctx.exception.code, "ocr_failed")
        self.assertEqual(ctx.exception.http_status, 500)

    def test_parse_complete_flyer_all_fields(self):
        text = (
            "BROOKLYN ELEMENTARY PTA PRESENTS\n"
            "School Science Fair\n"
            "Date: Friday, November 20, 2026\n"
            "Time: 3:30–5:00 PM\n"
            "Location: Brooklyn Elementary School\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["event"]["title"], "School Science Fair")
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["start_time"], "15:30")
        self.assertEqual(result["event"]["end_time"], "17:00")
        self.assertIsNone(result["event"]["end_date"])
        self.assertEqual(result["event"]["location"], "Brooklyn Elementary School")
        self.assertFalse(result["multiple_events"])

    def test_parse_24_hour_and_numeric_date_formats(self):
        text = (
            "Swim Meet\n"
            "11/20/2026\n"
            "15:30-17:00\n"
            "Where: Flushing Meadows Aquatic Center\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event"]["title"], "Swim Meet")
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["start_time"], "15:30")
        self.assertEqual(result["event"]["end_time"], "17:00")
        self.assertEqual(
            result["event"]["location"],
            "Flushing Meadows Aquatic Center",
        )

    def test_parse_missing_year_uses_weekday_and_adds_verification_warning(self):
        # Nov 20, 2026 is a Friday.
        text = "Winter Art Night\nFriday, Nov 20\n6:00 PM to 8:00 PM\n"
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["start_time"], "18:00")
        self.assertEqual(result["event"]["end_time"], "20:00")
        self.assertIn("date", result["field_warnings"])
        self.assertIn(
            "Please verify this date.",
            " ".join(result["field_warnings"]["date"]),
        )

    def test_parse_single_start_time_never_invents_end_time(self):
        text = (
            "Parent Teacher Conference\n"
            "November 20, 2026\n"
            "Starts at 3:30 PM\n"
            "Room 204, Lincoln Middle School\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["event"]["start_time"], "15:30")
        self.assertIsNone(result["event"]["end_time"])
        self.assertIn("end_time", result["missing_fields"])
        self.assertIn(
            "enter it manually",
            result["missing_field_messages"]["end_time"].lower(),
        )

    def test_parse_distinguishes_event_date_from_rsvp_and_publication_dates(self):
        text = (
            "Published: October 15, 2026\n"
            "Fall Harvest Potluck\n"
            "Please RSVP by November 10, 2026.\n"
            "Registration deadline: November 12, 2026\n"
            "Event Date: November 20, 2026\n"
            "5:30 PM - 7:30 PM\n"
            "Community Hall\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["event"]["title"], "Fall Harvest Potluck")
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["start_time"], "17:30")
        self.assertEqual(result["event"]["end_time"], "19:30")

    def test_parse_flags_ambiguous_numeric_date_and_bare_time_range(self):
        text = "Soccer Practice\n05/06/2026\n3:30 - 5:00\nRed Hook Field\n"
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 5, 1),
        )
        self.assertEqual(result["event"]["date"], "2026-05-06")
        self.assertIn("date", result["field_warnings"])
        self.assertIn(
            "Please verify this date.",
            " ".join(result["field_warnings"]["date"]),
        )
        self.assertIn("start_time", result["field_warnings"])

    def test_parse_relative_date_adds_warning(self):
        text = "Book Club Meetup\nTomorrow at 4:00 PM - 5:00 PM\nLocal Library\n"
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 10),
        )
        self.assertEqual(result["event"]["date"], "2026-11-11")
        self.assertIn("date", result["field_warnings"])
        self.assertIn(
            "Please verify this date.",
            " ".join(result["field_warnings"]["date"]),
        )

    def test_parse_multi_day_range(self):
        text = (
            "Family Science Camp\n"
            "November 20-22, 2026\n"
            "9:00 AM - 4:00 PM\n"
            "Prospect Park Center\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["end_date"], "2026-11-22")

    def test_parse_multiple_events_separates_candidates(self):
        text = (
            "Holiday Chorus Rehearsal\n"
            "December 4, 2026\n"
            "3:30 PM - 4:45 PM\n"
            "Music Room\n"
            "\n"
            "Winter Concert\n"
            "December 11, 2026\n"
            "6:30 PM - 8:00 PM\n"
            "Main Auditorium\n"
        )
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertTrue(result["multiple_events"])
        self.assertEqual(len(result["candidates"]), 2)
        first, second = result["candidates"]
        self.assertEqual(first["event"]["title"], "Holiday Chorus Rehearsal")
        self.assertEqual(first["event"]["date"], "2026-12-04")
        self.assertEqual(first["event"]["location"], "Music Room")
        self.assertEqual(second["event"]["title"], "Winter Concert")
        self.assertEqual(second["event"]["date"], "2026-12-11")
        self.assertEqual(second["event"]["location"], "Main Auditorium")

    def test_parse_missing_start_time_leaves_blank_and_reports_message(self):
        text = "Picture Day\nNovember 20, 2026\nSchool Gymnasium\n"
        result = parse_event_text(
            text,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["event"]["start_time"])
        self.assertIsNone(result["event"]["end_time"])
        self.assertIn("start_time", result["missing_fields"])
        self.assertIn(
            "Couldn't identify the start time",
            result["missing_field_messages"]["start_time"],
        )

    def test_parse_empty_or_non_event_text_raises_errors(self):
        with self.assertRaises(EventOCRError) as empty_ctx:
            parse_event_text("   \n  ")
        self.assertEqual(empty_ctx.exception.code, "no_text")
        self.assertEqual(empty_ctx.exception.http_status, 422)

        with self.assertRaises(EventOCRError) as noise_ctx:
            parse_event_text(
                "Please remember to pack a water bottle and label all jackets."
            )
        self.assertEqual(noise_ctx.exception.code, "no_event_info")
        self.assertEqual(noise_ctx.exception.http_status, 422)

    @patch("events.services.event_ocr.extract_text_from_image")
    def test_scan_event_image_pipeline(self, mock_extract):
        mock_extract.return_value = (
            "School Science Fair\n"
            "November 20, 2026\n"
            "3:30 PM - 5:00 PM\n"
            "Location: Brooklyn Elementary School\n"
        )
        uploaded = _make_uploaded_image()
        result = scan_event_image(
            uploaded,
            timezone_name="America/New_York",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event"]["title"], "School Science Fair")
        self.assertEqual(result["event"]["date"], "2026-11-20")
        self.assertEqual(result["event"]["start_time"], "15:30")
        self.assertEqual(result["event"]["end_time"], "17:00")
        mock_extract.assert_called_once()


class EventOCRViewAndWorkflowTests(TestCase):
    """Integration tests for OCR endpoint permissions, UI tabs, and event creation."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.caregiver = User.objects.create_user(
            username="ocr_parent",
            first_name="Maya",
            password="TestPassword123!",
        )
        cls.co_parent = User.objects.create_user(
            username="ocr_coparent",
            first_name="Jordan",
            password="TestPassword123!",
        )
        cls.kid = User.objects.create_user(
            username="ocr_kid",
            first_name="Leo",
            password="TestPassword123!",
        )
        cls.outsider = User.objects.create_user(
            username="ocr_outsider",
            first_name="Pat",
            password="TestPassword123!",
        )
        cls.workspace = Workspace.objects.create(
            name="OCR Family",
            primary_admin=cls.caregiver,
        )
        for user, role in (
            (cls.co_parent, Membership.Role.PARENT),
            (cls.kid, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.caregiver,
                accepted_at=timezone.now(),
            )
        cls.create_url = reverse("events:workspace_create", args=[cls.workspace.pk])
        cls.scan_url = reverse("events:scan_ocr", args=[cls.workspace.pk])
        cls.tomorrow = timezone.localdate() + timedelta(days=2)

    def test_create_event_renders_manual_and_scan_tabs_and_controls(self):
        self.client.force_login(self.caregiver)
        response = self.client.get(self.create_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual Entry")
        self.assertContains(response, "Scan Event")
        self.assertContains(response, 'aria-selected="true"')
        self.assertContains(response, "data-ocr-dropzone")
        self.assertContains(response, "Browse Files")
        self.assertContains(response, "Take Photo")
        self.assertContains(response, 'capture="environment"')
        self.assertContains(response, "Replace Image")
        self.assertContains(response, "Remove Image")
        self.assertContains(response, "Extract Event Details")
        self.assertContains(response, "Continue with These Details")
        self.assertContains(
            response, "Reading your document and finding event details..."
        )
        self.assertContains(response, f'data-ocr-url="{self.scan_url}"')
        self.assertContains(response, "events/event_ocr.js")
        self.assertContains(response, "data-ocr-script=")

    def test_edit_event_preserves_clean_form_without_scan_tabs(self):
        now = timezone.now() + timedelta(days=1)
        event = Event.objects.create(
            workspace=self.workspace,
            created_by=self.caregiver,
            title="Existing Piano Lesson",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        self.client.force_login(self.caregiver)
        response = self.client.get(reverse("events:edit", args=[event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-ocr-panel")
        self.assertNotContains(response, "data-event-mode-tabs")

    @patch("events.views.scan_event_image")
    def test_unauthenticated_request_redirects_to_login(self, mock_scan):
        response = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)
        mock_scan.assert_not_called()

    @patch("events.views.scan_event_image")
    def test_kid_cannot_access_ocr_endpoint(self, mock_scan):
        self.client.force_login(self.kid)
        response = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 404)
        mock_scan.assert_not_called()

    @patch("events.views.scan_event_image")
    def test_outsider_cannot_access_ocr_endpoint(self, mock_scan):
        self.client.force_login(self.outsider)
        response = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 404)
        mock_scan.assert_not_called()

    def test_get_method_not_allowed_on_ocr_endpoint(self):
        self.client.force_login(self.caregiver)
        response = self.client.get(self.scan_url)
        self.assertEqual(response.status_code, 405)

    def test_csrf_protection_enforced_on_ocr_endpoint(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.caregiver)
        response = csrf_client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 403)

    @patch("events.services.event_ocr.extract_text_from_image")
    def test_caregiver_and_co_parent_scan_returns_structured_draft_without_saving(
        self, mock_extract
    ):
        mock_extract.return_value = (
            "School Science Fair\n"
            "November 20, 2026\n"
            "3:30 PM - 5:00 PM\n"
            "Location: Brooklyn Elementary School\n"
        )
        for user in (self.caregiver, self.co_parent):
            self.client.force_login(user)
            response = self.client.post(
                self.scan_url,
                {
                    "document": _make_uploaded_image(
                        name="science_fair.webp",
                        image_format="WEBP",
                        content_type="image/webp",
                    ),
                    "timezone": "America/New_York",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.content)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["event"]["title"], "School Science Fair")
            self.assertEqual(payload["event"]["date"], "2026-11-20")
            self.assertEqual(payload["event"]["start_time"], "15:30")
            self.assertEqual(payload["event"]["end_time"], "17:00")
            self.assertEqual(
                payload["event"]["location"],
                "Brooklyn Elementary School",
            )
        self.assertEqual(Event.objects.count(), 0)

    def test_endpoint_rejects_invalid_timezone(self):
        self.client.force_login(self.caregiver)
        response = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "Mars/Olympus"},
        )
        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "invalid_timezone")

    def test_endpoint_rejects_oversized_and_corrupted_images(self):
        self.client.force_login(self.caregiver)

        oversized = SimpleUploadedFile(
            "large.png",
            b"x" * (MAX_IMAGE_SIZE_BYTES + 1),
            content_type="image/png",
        )
        res_large = self.client.post(
            self.scan_url,
            {"document": oversized, "timezone": "America/New_York"},
        )
        self.assertEqual(res_large.status_code, 413)
        self.assertEqual(json.loads(res_large.content)["error_code"], "file_too_large")

        corrupted = SimpleUploadedFile(
            "bad.jpg",
            b"\xff\xd8\xff\xe0broken-jpeg-data",
            content_type="image/jpeg",
        )
        res_corrupt = self.client.post(
            self.scan_url,
            {"document": corrupted, "timezone": "America/New_York"},
        )
        self.assertEqual(res_corrupt.status_code, 400)
        self.assertEqual(
            json.loads(res_corrupt.content)["error_code"], "corrupted_image"
        )

    @patch("events.services.event_ocr.extract_text_from_image")
    def test_endpoint_handles_no_text_and_ocr_runtime_failure(self, mock_extract):
        self.client.force_login(self.caregiver)

        mock_extract.return_value = "   "
        res_empty = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(res_empty.status_code, 422)
        payload_empty = json.loads(res_empty.content)
        self.assertEqual(payload_empty["error_code"], "no_text")

        mock_extract.side_effect = RuntimeError("unexpected crash")
        res_err = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(res_err.status_code, 500)
        payload_err = json.loads(res_err.content)
        self.assertEqual(payload_err["status"], "error")
        self.assertIn("manually", payload_err["error"].lower())

    @patch("events.services.event_ocr.extract_text_from_image")
    def test_scanned_event_can_be_reviewed_edited_and_saved(self, mock_extract):
        mock_extract.return_value = (
            "School Science Fair\n"
            f"{self.tomorrow.strftime('%B %d, %Y')}\n"
            "3:30 PM - 5:00 PM\n"
            "Location: Brooklyn Elementary School\n"
        )
        self.client.force_login(self.caregiver)

        # Step 1: Load Add Event page and capture existing submission_token.
        page_res = self.client.get(self.create_url)
        token = page_res.context["form"]["submission_token"].value()

        # Step 2: Scan uploaded flyer (no DB event created).
        scan_res = self.client.post(
            self.scan_url,
            {"document": _make_uploaded_image(), "timezone": "America/New_York"},
        )
        self.assertEqual(scan_res.status_code, 200)
        scanned = json.loads(scan_res.content)["event"]
        self.assertEqual(Event.objects.count(), 0)

        # Step 3: User reviews populated values, edits title, selects family member,
        # and checks preview step.
        review_payload = {
            "action": "preview",
            "submission_token": token,
            "title": f"{scanned['title']} (Grade 5)",
            "date": scanned["date"],
            "start_time": scanned["start_time"],
            "end_time": scanned["end_time"],
            "timezone": "America/New_York",
            "location": scanned["location"],
            "transport_mode": Event.TransportMode.DRIVING,
            "participants": [str(self.kid.pk)],
        }
        preview_res = self.client.post(self.create_url, review_payload)
        self.assertEqual(preview_res.status_code, 200)
        self.assertIsNotNone(preview_res.context["event_details"])
        self.assertEqual(Event.objects.count(), 0)

        # Step 4: Save event after review using the preserved submission_token.
        save_payload = {**review_payload, "action": "save"}
        save_res = self.client.post(self.create_url, save_payload)
        self.assertEqual(save_res.status_code, 302)
        self.assertEqual(Event.objects.count(), 1)
        saved = Event.objects.get()
        self.assertEqual(saved.title, "School Science Fair (Grade 5)")
        self.assertEqual(saved.location, "Brooklyn Elementary School")
        self.assertEqual(list(saved.participants.all()), [self.kid])

    def test_manual_entry_creation_still_works_normally(self):
        self.client.force_login(self.caregiver)
        token = (
            self.client.get(self.create_url).context["form"]["submission_token"].value()
        )
        res = self.client.post(
            self.create_url,
            {
                "action": "save",
                "submission_token": token,
                "title": "Manual Dentist Visit",
                "date": self.tomorrow.isoformat(),
                "start_time": "10:00",
                "end_time": "11:00",
                "timezone": "America/New_York",
                "transport_mode": Event.TransportMode.DRIVING,
            },
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(Event.objects.filter(title="Manual Dentist Visit").exists())
