from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import close_old_connections, connections
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageOps
from pytesseract import TesseractError, TesseractNotFoundError

from accounts.models import Membership, Workspace
from events.models import Event
from events.services.event_ocr import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIZE_BYTES,
    EventOCRError,
    _correct_osd_orientation,
    _has_event_cue,
    _normalize_canvas_and_size,
    _score_ocr_text,
    extract_text_from_image,
    normalize_ocr_text,
    ocr_runtime_status,
    parse_event_text,
    preprocess_image_for_ocr,
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

    def test_validate_rejects_bad_mime_empty_stream_disallowed_format_and_huge_pixels(
        self,
    ):
        bad_mime = SimpleUploadedFile(
            "invite.png", _make_image_bytes(), content_type="text/plain"
        )
        with self.assertRaises(EventOCRError):
            validate_and_load_image(bad_mime)

        zero_sized = SimpleUploadedFile("empty.png", b"", content_type="image/png")
        with self.assertRaises(EventOCRError):
            validate_and_load_image(zero_sized)

        class StreamUpload:
            def __init__(self, data, name="stream.png", content_type="image/png"):
                self.name = name
                self.content_type = content_type
                self._buf = io.BytesIO(data)

            def read(self, size=-1):
                return self._buf.read(size)

        with self.assertRaises(EventOCRError):
            validate_and_load_image(StreamUpload(b""))

        with self.assertRaises(EventOCRError):
            validate_and_load_image(StreamUpload(b"x" * (MAX_IMAGE_SIZE_BYTES + 1)))

        gif_as_png = SimpleUploadedFile(
            "animated.png",
            _make_image_bytes(image_format="GIF", mode="P"),
            content_type="image/png",
        )
        with self.assertRaises(EventOCRError):
            validate_and_load_image(gif_as_png)

        small_upload = _make_uploaded_image(size=(20, 20))
        self.assertGreater(MAX_IMAGE_PIXELS, 400)
        with patch("events.services.event_ocr.MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(EventOCRError):
                validate_and_load_image(small_upload)

        with patch("events.services.event_ocr.Image.MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(EventOCRError):
                validate_and_load_image(_make_uploaded_image(size=(20, 20)))

    def test_normalize_canvas_converts_palette_and_downscales_oversized_images(self):
        palette_img = Image.new("P", (1200, 600))
        normalized = _normalize_canvas_and_size(palette_img)
        self.assertEqual(normalized.mode, "RGB")
        self.assertEqual(normalized.size, (1200, 600))

        oversized = Image.new("RGB", (3000, 1500), color=(255, 255, 255))
        scaled = _normalize_canvas_and_size(oversized)
        self.assertEqual(scaled.size, (2400, 1200))

    @patch("events.services.event_ocr.pytesseract.image_to_osd")
    def test_osd_orientation_handles_malformed_low_conf_and_string_responses(
        self, mock_osd
    ):
        landscape = Image.new("RGB", (120, 60), color=(255, 255, 255))

        mock_osd.return_value = {"rotate": "invalid", "orientation_conf": "nan"}
        self.assertEqual(_correct_osd_orientation(landscape).size, (120, 60))

        mock_osd.return_value = {"rotate": 90, "orientation_conf": 0.5}
        self.assertEqual(_correct_osd_orientation(landscape).size, (120, 60))

        mock_osd.return_value = 12345
        self.assertEqual(_correct_osd_orientation(landscape).size, (120, 60))

        mock_osd.return_value = "Rotate: 270\nOrientation confidence: 4.5"
        rotated = _correct_osd_orientation(landscape)
        self.assertEqual(rotated.size, (60, 120))

    def test_preprocess_inverts_dark_background_and_cue_scoring_helpers(self):
        dark_img = Image.new("RGB", (120, 80), color=(15, 15, 15))
        with patch("events.services.event_ocr.pytesseract.image_to_osd") as mock_osd:
            mock_osd.return_value = {"rotate": 0, "orientation_conf": 0}
            primary, extras = preprocess_image_for_ocr(dark_img)
        self.assertEqual(primary.mode, "L")
        self.assertEqual(len(extras), 1)

        self.assertFalse(_has_event_cue(""))
        self.assertTrue(_has_event_cue("Doors 18:30"))
        self.assertTrue(_has_event_cue("Starts 5 pm"))
        self.assertTrue(_has_event_cue("See 11/20/2026"))
        self.assertFalse(_has_event_cue("General Flyer Notice"))

        self.assertEqual(_score_ocr_text(""), 0)
        self.assertGreater(
            _score_ocr_text("Meeting 11:20"),
            _score_ocr_text("Meeting 11/20"),
        )
        self.assertEqual(normalize_ocr_text(""), "")

    @patch("events.services.event_ocr.pytesseract.image_to_osd")
    @patch("events.services.event_ocr.pytesseract.image_to_string")
    def test_extract_text_runs_fallback_passes_rotation_recovery_and_empty_error(
        self, mock_ocr, mock_osd
    ):
        mock_osd.return_value = {"rotate": 0, "orientation_conf": 0}
        sample_img = Image.new("RGB", (120, 80), color=(255, 255, 255))

        mock_ocr.side_effect = [
            "Hi",
            TesseractError(1, "psm 11 failed"),
            "Winter Concert\nDecember 12, 2026\n6:00 PM - 8:00 PM",
        ]
        extracted = extract_text_from_image(sample_img)
        self.assertIn("Winter Concert", extracted)
        self.assertEqual(mock_ocr.call_count, 3)

        mock_ocr.reset_mock()
        mock_ocr.side_effect = [
            "Xyzzy Qwerty Plugh 12345 67890",
            "Field Trip\n11/20/2026\n9:00 AM - 2:00 PM",
        ]
        rotated_text = extract_text_from_image(sample_img)
        self.assertIn("Field Trip", rotated_text)

        mock_ocr.reset_mock()
        mock_ocr.side_effect = ["a 1", "a", "1", "a", "1", "a"]
        with self.assertRaises(EventOCRError):
            extract_text_from_image(sample_img)

    def test_date_parsing_ranges_formats_relative_phrases_and_contexts(self):
        ref = date(2026, 11, 10)

        invalid = parse_event_text(
            "Book Fair\nFebruary 30, 2026\n10:00 AM", reference_date=ref
        )
        self.assertIsNone(invalid["event"]["date"])
        self.assertTrue(invalid["missing_fields"])

        doc_year = parse_event_text(
            "2027 School Calendar\nSpring Concert\nMarch 14\n6:30 PM",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(doc_year["event"]["date"], "2027-03-14")

        rolled = parse_event_text(
            "Winter Gala\nJanuary 15\n7:00 PM",
            reference_date=date(2026, 11, 1),
        )
        self.assertEqual(rolled["event"]["date"], "2027-01-15")

        rsvp_same_line = parse_event_text(
            "Science Fair\nJoin us November 20, 2026! RSVP by November 10, 2026\n"
            "3:30 PM - 5:00 PM",
            reference_date=ref,
        )
        self.assertEqual(rsvp_same_line["event"]["date"], "2026-11-20")

        cross_month = parse_event_text(
            "Fall Camp\nNov 28 - Dec 2, 2026\n9:00 AM", reference_date=ref
        )
        self.assertEqual(cross_month["event"]["date"], "2026-11-28")
        self.assertEqual(cross_month["event"]["end_date"], "2026-12-02")

        cross_year = parse_event_text(
            "Winter Camp\nDec 30 - Jan 2\n9:00 AM", reference_date=ref
        )
        self.assertEqual(cross_year["event"]["date"], "2026-12-30")
        self.assertEqual(cross_year["event"]["end_date"], "2027-01-02")

        same_month_no_year = parse_event_text(
            "Art Week\nNovember 20-22\n10:00 AM - 4:00 PM", reference_date=ref
        )
        self.assertEqual(same_month_no_year["event"]["date"], "2026-11-20")
        self.assertEqual(same_month_no_year["event"]["end_date"], "2026-11-22")

        reversed_range = parse_event_text(
            "Camp\nNovember 25-20, 2026\n9:00 AM", reference_date=ref
        )
        self.assertIsNone(reversed_range["event"]["end_date"])

        numeric_range = parse_event_text(
            "Tournament\n11/20/2026 - 11/22/2026\n8:00 AM - 5:00 PM",
            reference_date=ref,
        )
        self.assertEqual(numeric_range["event"]["date"], "2026-11-20")
        self.assertEqual(numeric_range["event"]["end_date"], "2026-11-22")

        day_month_year = parse_event_text(
            "Music Night\n20 November 2026\n19:00 - 20:30", reference_date=ref
        )
        self.assertEqual(day_month_year["event"]["date"], "2026-11-20")

        day_month_no_year = parse_event_text(
            "Poetry Reading\n20 Nov\n4:00 PM", reference_date=ref
        )
        self.assertEqual(day_month_no_year["event"]["date"], "2026-11-20")

        iso_date = parse_event_text(
            "PTA Meeting\n2026-11-20\n18:30 - 19:30", reference_date=ref
        )
        self.assertEqual(iso_date["event"]["date"], "2026-11-20")

        dd_mm = parse_event_text("Assembly\n20/11/2026\n9:00 AM", reference_date=ref)
        self.assertEqual(dd_mm["event"]["date"], "2026-11-20")

        short_year = parse_event_text(
            "Bake Sale\n11/20/26\n00/10/2026\n10:00 AM", reference_date=ref
        )
        self.assertEqual(short_year["event"]["date"], "2026-11-20")

        mismatch = parse_event_text(
            "Soccer Practice\nMonday, November 20, 2026\n4:00 PM",
            reference_date=ref,
        )
        self.assertEqual(mismatch["event"]["date"], "2026-11-20")
        self.assertTrue(
            any("weekday" in w.lower() for w in mismatch["field_warnings"]["date"])
        )

        today_res = parse_event_text("Playdate\ntoday\n3:00 PM", reference_date=ref)
        self.assertEqual(today_res["event"]["date"], "2026-11-10")

        this_sat = parse_event_text(
            "Game Day\nthis Saturday\n10:00 AM", reference_date=ref
        )
        self.assertEqual(this_sat["event"]["date"], "2026-11-14")

        next_fri = parse_event_text(
            "Study Group\nnext Friday\n4:00 PM", reference_date=ref
        )
        self.assertEqual(next_fri["event"]["date"], "2026-11-13")

        same_weekday = parse_event_text(
            "Study Group\nnext Tuesday\n4:00 PM", reference_date=ref
        )
        self.assertEqual(same_weekday["event"]["date"], "2026-11-17")

        labeled_dateutil = parse_event_text(
            "Town Hall\nDate: 2026 Nov 20\n6:00 PM", reference_date=ref
        )
        self.assertEqual(labeled_dateutil["event"]["date"], "2026-11-20")

        tba = parse_event_text(
            "Town Hall\nDate: To be announced\n6:00 PM", reference_date=ref
        )
        self.assertIsNone(tba["event"]["date"])

    def test_time_parsing_special_tokens_invalid_tokens_and_preferences(self):
        ref = date(2026, 11, 1)

        noon = parse_event_text(
            "Luncheon\nNov 20, 2026\nNoon - 2:00 PM", reference_date=ref
        )
        self.assertEqual(noon["event"]["start_time"], "12:00")
        self.assertEqual(noon["event"]["end_time"], "14:00")

        midnight = parse_event_text(
            "Midnight Countdown\nDec 31, 2026\nStarts at Midnight",
            reference_date=ref,
        )
        self.assertEqual(midnight["event"]["start_time"], "00:00")

        bare_start = parse_event_text(
            "Morning Workshop\nNov 20, 2026\n10:00 - 12:30 PM", reference_date=ref
        )
        self.assertEqual(bare_start["event"]["start_time"], "10:00")
        self.assertEqual(bare_start["event"]["end_time"], "12:30")

        early_24h = parse_event_text(
            "Early Check-in\nNov 20, 2026\n00:30", reference_date=ref
        )
        self.assertEqual(early_24h["event"]["start_time"], "00:30")

        invalid_times = parse_event_text(
            "Bad Times\nNov 20, 2026\n3:75 PM\n14:00 PM",
            reference_date=ref,
        )
        self.assertIsNone(invalid_times["event"]["start_time"])
        self.assertIsNone(invalid_times["event"]["end_time"])

        invalid_range_end = parse_event_text(
            "Half Invalid\nNov 20, 2026\n3:00 PM - 3:75 PM",
            reference_date=ref,
        )
        self.assertEqual(invalid_range_end["event"]["start_time"], "15:00")
        self.assertIsNone(invalid_range_end["event"]["end_time"])

        reversed_times = parse_event_text(
            "Reversed\nNov 20, 2026\n5:00 PM - 3:00 PM",
            reference_date=ref,
        )
        self.assertIsNotNone(reversed_times["event"]["start_time"])

        starts_over_doors = parse_event_text(
            "School Musical\nNov 20, 2026\nDoors open 6:00 PM\nShow starts 7:00 PM",
            reference_date=ref,
        )
        self.assertEqual(starts_over_doors["event"]["start_time"], "19:00")

    def test_location_title_multi_event_multiple_dates_and_invalid_timezone(self):
        ref = date(2026, 11, 1)

        multiline_loc = parse_event_text(
            "Museum Tour\nNov 20, 2026\n10:00 AM\n"
            "Location: Brooklyn Museum\n200 Eastern Pkwy, Brooklyn, NY 11238",
            reference_date=ref,
        )
        self.assertIn("Brooklyn Museum", multiline_loc["event"]["location"])
        self.assertIn("200 Eastern Pkwy", multiline_loc["event"]["location"])

        venue_then_addr = parse_event_text(
            "Band Showcase\nNov 20, 2026\n7:00 PM\n" "PS 321 Auditorium\n180 7th Ave",
            reference_date=ref,
        )
        self.assertIn("PS 321 Auditorium", venue_then_addr["event"]["location"])
        self.assertIn("180 7th Ave", venue_then_addr["event"]["location"])

        inline_at = parse_event_text(
            "Family Picnic at Prospect Park\nNov 20, 2026\nMeet at 3:30 PM",
            reference_date=ref,
        )
        self.assertEqual(inline_at["event"]["location"], "Prospect Park")

        labeled_title = parse_event_text(
            "Oakwood Elementary\nEvent: Spring Recital\n"
            "Contact admin@school.org\n555-123-4567\nhttps://school.org\n"
            "May 15, 2027\n6:00 PM",
            reference_date=ref,
        )
        self.assertEqual(labeled_title["event"]["title"], "Spring Recital")
        self.assertIn("Oakwood Elementary", labeled_title["event"]["location"])

        no_title = parse_event_text(
            "Nov 20, 2026\n3:30 PM\n123 Main Street", reference_date=ref
        )
        self.assertIsNone(no_title["event"]["title"])
        self.assertIn("title", no_title["missing_fields"])

        no_blank_multi = parse_event_text(
            "Dec 4, 2026 - Winter Chorus 3:30 PM\n"
            "Dec 11, 2026 - Band Concert 6:30 PM",
            reference_date=ref,
        )
        self.assertTrue(no_blank_multi["multiple_events"])
        self.assertEqual(len(no_blank_multi["candidates"]), 2)

        multi_dates = parse_event_text(
            "Science Expo\nJoin us Nov 20, 2026 or Nov 21, 2026\n3:00 PM",
            reference_date=ref,
        )
        self.assertEqual(multi_dates["event"]["date"], "2026-11-20")
        self.assertIn("date", multi_dates["field_warnings"])

        rain_only = parse_event_text(
            "Outdoor Picnic\nRain date: November 27, 2026\n12:00 PM",
            reference_date=ref,
        )
        self.assertEqual(rain_only["event"]["date"], "2026-11-27")

        with self.assertRaises(EventOCRError):
            parse_event_text(
                "Fair\nNov 20, 2026\n2:00 PM",
                timezone_name="Mars/Invalid",
                reference_date=ref,
            )

        with self.assertRaises(EventOCRError):
            parse_event_text(
                "Just a casual note with no schedule details",
                reference_date=ref,
            )


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

    @patch("events.views.ocr_available", return_value=True)
    def test_create_event_renders_manual_and_scan_tabs_and_controls(self, _available):
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


class OcrDeploymentHookTests(SimpleTestCase):
    hook_path = (
        Path(settings.BASE_DIR) / ".platform" / "hooks" / "prebuild" / "01_tesseract.sh"
    )

    def test_tesseract_hook_exists_is_executable_and_valid_bash(self):
        self.assertTrue(self.hook_path.is_file())
        mode = self.hook_path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        syntax = subprocess.run(
            ["bash", "-n", str(self.hook_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_tesseract_hook_targets_al2023_and_verifies_hashes_and_langs(self):
        script = self.hook_path.read_text(encoding="utf-8")
        self.assertIn("dnf install -y", script)
        self.assertNotIn("apt-get", script)
        self.assertIn("LEPTONICA_SHA256=", script)
        self.assertIn("TESSERACT_SHA256=", script)
        self.assertIn("ENG_DATA_SHA256=", script)
        self.assertIn("OSD_DATA_SHA256=", script)
        self.assertIn("sha256_check", script)
        self.assertIn("--version", script)
        self.assertIn("--list-langs", script)
        self.assertIn("eng.traineddata", script)
        self.assertIn("osd.traineddata", script)

    def test_tesseract_hook_is_idempotent_when_verified_binary_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            fake_bin = bin_dir / "tesseract"
            fake_bin.write_text(
                "#!/bin/bash\n"
                'if [ "${1:-}" = "--version" ]; then\n'
                '  echo "tesseract 5.5.4"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "${1:-}" = "--list-langs" ]; then\n'
                '  echo "List of available languages:"\n'
                '  echo "eng"\n'
                '  echo "osd"\n'
                "  exit 0\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            symlink_path = Path(tmp) / "usr-bin-tesseract"
            env = {
                **os.environ,
                "TESSERACT_PREFIX": str(prefix),
                "TESSERACT_BIN_SYMLINK": str(symlink_path),
                "SKIP_TESSERACT_INSTALL": "",
            }
            res = subprocess.run(
                ["bash", str(self.hook_path)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("already ready: tesseract 5.5.4", res.stdout)
            self.assertTrue(symlink_path.is_symlink())

    def test_tesseract_hook_does_not_break_deploy_when_dnf_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            empty_bin = Path(tmp) / "empty-bin"
            empty_bin.mkdir()
            # Keep standard POSIX utilities while hiding tesseract and dnf.
            env = {
                "PATH": str(empty_bin) + ":/bin:/usr/bin",
                "TESSERACT_PREFIX": str(prefix),
                "TESSDATA_DIR": str(prefix / "share" / "tessdata"),
                "TESSERACT_BIN_SYMLINK": str(Path(tmp) / "usr-bin-tesseract"),
                "SKIP_TESSERACT_INSTALL": "",
                "REQUIRE_OCR_READY": "false",
            }
            res = subprocess.run(
                ["/bin/bash", str(self.hook_path)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("OCR_READINESS=unavailable", res.stderr)

    def test_tesseract_hook_fails_when_require_ocr_ready_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            empty_bin = Path(tmp) / "empty-bin"
            empty_bin.mkdir()
            env = {
                "PATH": str(empty_bin) + ":/bin:/usr/bin",
                "TESSERACT_PREFIX": str(prefix),
                "TESSDATA_DIR": str(prefix / "share" / "tessdata"),
                "TESSERACT_BIN_SYMLINK": str(Path(tmp) / "usr-bin-tesseract"),
                "SKIP_TESSERACT_INSTALL": "",
                "REQUIRE_OCR_READY": "true",
            }
            res = subprocess.run(
                ["/bin/bash", str(self.hook_path)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("OCR_READINESS=unavailable", res.stderr)

    def test_postdeploy_ocr_readiness_hook_exists_and_valid_bash(self):
        post_hook = (
            Path(settings.BASE_DIR)
            / ".platform"
            / "hooks"
            / "postdeploy"
            / "05_ocr_readiness.sh"
        )
        self.assertTrue(post_hook.is_file())
        self.assertTrue(post_hook.stat().st_mode & stat.S_IXUSR)
        syntax = subprocess.run(
            ["bash", "-n", str(post_hook)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        content = post_hook.read_text(encoding="utf-8")
        self.assertIn("manage.py check_ocr", content)
        self.assertIn("OCR_READINESS=unavailable", content)


class OcrReadinessCommandTests(SimpleTestCase):
    def test_ocr_runtime_status_reports_missing_binary(self):
        with patch("events.services.event_ocr.shutil.which", return_value=None):
            status = ocr_runtime_status(smoke_test=False)
        self.assertFalse(status["ready"])
        self.assertIn("tesseract binary not found", status["error"])

    def test_ocr_runtime_status_reports_missing_languages(self):
        with patch(
            "events.services.event_ocr.shutil.which", return_value="/usr/bin/tesseract"
        ), patch(
            "events.services.event_ocr.pytesseract.get_tesseract_version",
            return_value="5.5.4",
        ), patch(
            "events.services.event_ocr.pytesseract.get_languages",
            return_value=["eng"],
        ):
            status = ocr_runtime_status(smoke_test=False)
        self.assertFalse(status["ready"])
        self.assertEqual(status["missing_languages"], ["osd"])

    def test_check_ocr_command_succeeds_with_smoke_test(self):
        out = io.StringIO()
        sample_result = {
            "status": "success",
            "event": {"title": "Syncmily School Concert"},
        }
        with patch(
            "events.services.event_ocr.shutil.which", return_value="/usr/bin/tesseract"
        ), patch(
            "events.services.event_ocr.pytesseract.get_tesseract_version",
            return_value="5.5.4",
        ), patch(
            "events.services.event_ocr.pytesseract.get_languages",
            return_value=["eng", "osd"],
        ), patch(
            "events.services.event_ocr.scan_event_image",
            return_value=sample_result,
        ):
            call_command("check_ocr", stdout=out)
        self.assertIn("OCR_READINESS=ready", out.getvalue())
        self.assertIn("version=5.5.4", out.getvalue())

    def test_check_ocr_command_raises_when_unavailable(self):
        err = io.StringIO()
        with patch("events.services.event_ocr.shutil.which", return_value=None):
            with self.assertRaises(CommandError):
                call_command("check_ocr", "--no-smoke-test", stderr=err)
        self.assertIn("OCR_READINESS=unavailable", err.getvalue())


class ConcurrentOCRConnectionCleanupTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.parent = User.objects.create_user(
            username="ocr_pool_parent", password="Password123!"
        )
        self.other = User.objects.create_user(
            username="ocr_pool_other", password="Password123!"
        )
        self.workspace = Workspace.objects.create(
            name="Pool Test Family", primary_admin=self.parent
        )
        Membership.objects.create(
            workspace=self.workspace,
            user=self.other,
            role=Membership.Role.PARENT,
            invited_by=self.parent,
            accepted_at=timezone.now(),
        )
        self.scan_url = reverse("events:scan_ocr", args=[self.workspace.pk])

    def _checked_out_connections(self):
        pool = connections["default"]._connection_pools.get("default")
        if not pool:
            return 0
        stats = pool.get_stats()
        return stats.get("pool_size", 0) - stats.get("pool_available", 0)

    def test_concurrent_ocr_workers_close_connections_on_success_and_exception(self):
        sample_text = (
            "Spring Soccer Practice\n"
            "May 20, 2027\n"
            "3:00 PM - 4:30 PM\n"
            "Location: Prospect Park\n"
        )
        before = self._checked_out_connections()

        def worker(idx, raise_exc=False):
            close_old_connections()
            client = Client()
            try:
                client.force_login(self.parent if idx % 2 == 0 else self.other)
                if raise_exc:
                    raise RuntimeError("simulated worker failure")
                return client.post(
                    self.scan_url,
                    {"document": _make_uploaded_image(), "timezone": "UTC"},
                ).status_code
            finally:
                connections.close_all()

        with patch(
            "events.services.event_ocr.pytesseract.image_to_osd",
            side_effect=TesseractError(1, "no osd"),
        ), patch(
            "events.services.event_ocr.pytesseract.image_to_string",
            return_value=sample_text,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                ok_futures = [executor.submit(worker, i, False) for i in range(2)]
                err_future = executor.submit(worker, 2, True)
                statuses = [fut.result(timeout=15) for fut in ok_futures]
                with self.assertRaises(RuntimeError):
                    err_future.result(timeout=15)

        self.assertEqual(statuses, [200, 200])
        self.assertLessEqual(self._checked_out_connections(), before)
