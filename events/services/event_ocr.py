"""OCR document scanning and structured event extraction for Syncmily."""

from __future__ import annotations

import logging
import shutil
import threading
import time as time_module
import re
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytesseract
from dateutil import parser as dateutil_parser
from django.utils import timezone
from PIL import (
    Image,
    ImageEnhance,
    ImageFilter,
    ImageOps,
    ImageStat,
    UnidentifiedImageError,
)

logger = logging.getLogger(__name__)


def ocr_available():
    """True when the Tesseract binary is installed on this server."""
    return shutil.which("tesseract") is not None


MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
OCR_TIMEOUT_SECONDS = 15
# One request must finish well inside nginx's 60 s: stop trying fallback passes
# once this much time has gone.
OCR_TIME_BUDGET_SECONDS = 25
# At most two scans decode and run Tesseract at once.
OCR_SLOTS = threading.BoundedSemaphore(2)
ALLOWED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
ALLOWED_MIME_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp"}
)
REQUIRED_EVENT_FIELDS = ("title", "date", "start_time", "end_time")

MISSING_FIELD_MESSAGES = {
    "title": "Couldn't identify the event title. Please enter it manually.",
    "date": "Couldn't identify the event date. Please enter it manually.",
    "start_time": "Couldn't identify the start time. Please enter it manually.",
    "end_time": "Couldn't identify the end time. Please enter it manually.",
}

MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

WEEKDAY_MAP = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)
MONTH_WORD_RE = re.compile(rf"\b{MONTH_PATTERN}\b", re.IGNORECASE)
WEEKDAY_PATTERN = (
    r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|"
    r"thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
)
DASH_CHARS = r"[\-\u2012\u2013\u2014\u2015~]"

DEADLINE_PREFIX_RE = re.compile(
    r"\b(?:rsvp|register(?:\s+by)?|registration(?:\s+deadline|\s+closes)?|"
    r"deadline|due(?:\s+by|\s+on)?|sign\s+up\s+by|submit\s+by|"
    r"reply\s+by|respond\s+by|confirm\s+by|tickets?\s+by|"
    r"early\s+bird|last\s+day\s+to|published|posted|printed|issued|"
    r"updated|revised|created\s+on|copyright|dob|born)\b",
    re.IGNORECASE,
)
ALTERNATE_DATE_RE = re.compile(
    r"\b(?:rain\s+date|backup\s+date|alternate\s+date|snow\s+date)\b",
    re.IGNORECASE,
)
EVENT_DATE_PREFIX_RE = re.compile(
    r"\b(?:when|date|event\s+date|starts?(?:\s+on)?|takes\s+place|"
    r"join\s+us(?:\s+on)?|save\s+the\s+date|scheduled\s+for|hosted\s+on)\b",
    re.IGNORECASE,
)

TIME_NON_EVENT_PREFIX_RE = re.compile(
    r"\b(?:rsvp\s+by|register\s+by|deadline|due\s+by|submit\s+by|"
    r"reply\s+by|published|updated)\b",
    re.IGNORECASE,
)
TIME_DOORS_PREFIX_RE = re.compile(
    r"\b(?:doors?\s+open|check[\s\-]?in|arrival|registration\s+opens)\b",
    re.IGNORECASE,
)
TIME_STARTS_PREFIX_RE = re.compile(
    r"\b(?:time|when|starts?(?:\s+at)?|event\s+time|show\s+starts?|"
    r"program\s+begins|begins?(?:\s+at)?|hours?)\b",
    re.IGNORECASE,
)

LOCATION_LABEL_RE = re.compile(
    r"^(?:where|location|venue|place|address|site|held\s+at|room)\s*[:\-–—]\s*(.+)$",
    re.IGNORECASE,
)
TITLE_LABEL_RE = re.compile(
    r"^(?:event|title|activity|program|subject|what)\s*[:\-–—]\s*(.+)$",
    re.IGNORECASE,
)
OTHER_LABEL_RE = re.compile(
    r"^(?:when|date|time|rsvp|contact|phone|tel|email|cost|price|fee|"
    r"ages?|grades?|notes?|details?|host(?:ed\s+by)?|"
    r"brought\s+to\s+you\s+by)\s*[:\-–—]",
    re.IGNORECASE,
)

STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9.\-'\s]{2,45}\s+"
    r"(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|"
    r"drive|dr\.?|lane|ln\.?|way|parkway|pkwy\.?|court|ct\.?|"
    r"place|pl\.?|terrace|ter\.?|circle|cir\.?|plaza|square|sq\.?)\b",
    re.IGNORECASE,
)
CITY_STATE_ZIP_RE = re.compile(
    r"\b[A-Za-z.\s]{2,30},\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\b"
)
VENUE_KEYWORD_RE = re.compile(
    r"\b(?:elementary|middle\s+school|high\s+school|school|academy|"
    r"center|centre|gymnasium|gym|auditorium|cafeteria|library|"
    r"park|field|fields|hall|room|studio|church|temple|synagogue|"
    r"mosque|museum|clinic|hospital|theater|theatre|club|playground|"
    r"rec\s+center|pool|arena|campus|pavilion|courtyard|zoo|aquarium)\b",
    re.IGNORECASE,
)
INLINE_AT_VENUE_RE = re.compile(
    r"(?:^|\s)(?:@|at)\s+([A-Z][A-Za-z0-9&'.,\-/\s]{2,90})",
)
AT_STOPWORDS_RE = re.compile(
    r"^(?:least|all|this|that|the\s+moment|the\s+end|any|your|no|"
    r"home|noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b)",
    re.IGNORECASE,
)

PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
EMAIL_OR_URL_RE = re.compile(
    r"(?:https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.-]+)",
    re.IGNORECASE,
)
BOILERPLATE_LINE_RE = re.compile(
    r"^(?:save\s+the\s+date|you(?:'re|\s+are)\s+invited!?|invitation|"
    r"school\s+notice|parent\s+notice|family\s+notice|community\s+notice|"
    r"flyer|newsletter|reminder|announcement|weekly\s+update|"
    r"dear\s+(?:parents?|families|guardians?|caregivers?)[,:]?|"
    r"hello\s+(?:parents?|families)[,:]?|please\s+join\s+us!?|"
    r"join\s+us!?|rsvp\b.*|for\s+more\s+info(?:rmation)?\b.*|"
    r"questions\??.*|contact\s+us\b.*|free\s+admission!?|all\s+are\s+welcome!?)$",
    re.IGNORECASE,
)
INVITE_PREFIX_RE = re.compile(
    r"^(?:you(?:'re|\s+are)\s+invited\s+to(?:\s+the)?|"
    r"(?:please\s+)?join\s+us\s+for(?:\s+the)?|"
    r"come\s+(?:to|celebrate)(?:\s+the)?|"
    r"welcome\s+to(?:\s+the)?)\s+",
    re.IGNORECASE,
)

DATE_RANGE_SAME_MONTH_RE = re.compile(
    rf"\b(?:({WEEKDAY_PATTERN})\s*,?\s+)?"
    rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*"
    rf"(?:{DASH_CHARS}|to|through)\s*"
    rf"(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
DATE_RANGE_FULL_RE = re.compile(
    rf"\b(?:({WEEKDAY_PATTERN})\s*,?\s+)?"
    rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?"
    rf"\s*(?:{DASH_CHARS}|to|through)\s*"
    rf"(?:({WEEKDAY_PATTERN})\s*,?\s+)?"
    rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
NAMED_MONTH_DATE_RE = re.compile(
    rf"\b(?:({WEEKDAY_PATTERN})\s*,?\s+)?"
    rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
DAY_MONTH_YEAR_RE = re.compile(
    rf"\b(?:({WEEKDAY_PATTERN})\s*,?\s+)?"
    rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_PATTERN})\.?"
    rf"(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(
    r"\b(20\d{2})[/\-](0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])\b"
)
NUMERIC_DATE_RANGE_RE = re.compile(
    rf"\b(\d{{1,2}})[/\-](\d{{1,2}})[/\-](\d{{2,4}})\s*"
    rf"(?:{DASH_CHARS}|to|through)\s*"
    rf"(\d{{1,2}})[/\-](\d{{1,2}})[/\-](\d{{2,4}})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?(?!\d)")
RELATIVE_DATE_RE = re.compile(
    rf"\b(today|tonight|tomorrow|(?:this|next)\s+(?:coming\s+)?({WEEKDAY_PATTERN}))\b",
    re.IGNORECASE,
)

TIME_TOKEN_PATTERN = (
    r"(?:"
    r"noon|midnight|"
    r"(?:\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)|"
    r"(?:\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?))"
    r")"
)
TIME_RANGE_RE = re.compile(
    rf"(?<![\d/])({TIME_TOKEN_PATTERN})\s*"
    rf"(?:{DASH_CHARS}|\bto\b|\buntil\b|\btill\b|\bthrough\b)\s*"
    rf"({TIME_TOKEN_PATTERN})(?![\d/])",
    re.IGNORECASE,
)
SINGLE_TIME_RE = re.compile(
    rf"(?<![\d/:])({TIME_TOKEN_PATTERN})(?![\d/:])",
    re.IGNORECASE,
)


class EventOCRError(Exception):
    """Raised when document scanning or event extraction fails safely."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class _DateMatch:
    start_date: date
    end_date: date | None
    score: int
    line_index: int
    span: tuple[int, int]
    is_deadline: bool
    is_alternate: bool
    warnings: list[str]


@dataclass
class _TimeMatch:
    start_time: time | None
    end_time: time | None
    score: int
    line_index: int
    span: tuple[int, int]
    is_deadline: bool
    warnings: list[str]


def validate_and_load_image(uploaded_file):
    """Validate uploaded image size, extension, MIME type, and actual binary content."""
    if uploaded_file is None:
        raise EventOCRError("no_file", "Please choose an image to scan.", 400)

    filename = getattr(uploaded_file, "name", "") or ""
    ext = Path(filename).suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise EventOCRError(
            "unsupported_format",
            "Unsupported file type. Please upload a JPG, JPEG, PNG, or WebP image.",
            400,
        )

    content_type = (getattr(uploaded_file, "content_type", "") or "").split(";")[0]
    content_type = content_type.strip().lower()
    if content_type and content_type not in (
        ALLOWED_MIME_TYPES | {"application/octet-stream"}
    ):
        raise EventOCRError(
            "unsupported_format",
            "Unsupported file type. Please upload a JPG, JPEG, PNG, or WebP image.",
            400,
        )

    reported_size = getattr(uploaded_file, "size", None)
    if reported_size is not None:
        if reported_size <= 0:
            raise EventOCRError(
                "corrupted_image",
                "The uploaded image file is empty. Please choose another image.",
                400,
            )
        if reported_size > MAX_IMAGE_SIZE_BYTES:
            raise EventOCRError(
                "file_too_large",
                "This image is too large. Please upload an image under 10 MB.",
                413,
            )

    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    raw_bytes = uploaded_file.read(MAX_IMAGE_SIZE_BYTES + 1)
    if not raw_bytes:
        raise EventOCRError(
            "corrupted_image",
            "The uploaded image file is empty. Please choose another image.",
            400,
        )
    if len(raw_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise EventOCRError(
            "file_too_large",
            "This image is too large. Please upload an image under 10 MB.",
            413,
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw_bytes)) as probed:
                if probed.format not in ALLOWED_IMAGE_FORMATS:
                    raise EventOCRError(
                        "unsupported_format",
                        (
                            "Unsupported image format. Please upload a JPG, JPEG, "
                            "PNG, or WebP image."
                        ),
                        400,
                    )
                if probed.width * probed.height > MAX_IMAGE_PIXELS:
                    raise EventOCRError(
                        "image_too_large",
                        (
                            "This image resolution is too large to process safely. "
                            "Try a smaller photo."
                        ),
                        400,
                    )
                probed.verify()

            loaded = Image.open(BytesIO(raw_bytes))
            loaded.load()
            return loaded
    except EventOCRError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise EventOCRError(
            "image_too_large",
            "This image resolution is too large to process safely.",
            400,
        ) from error
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
    ) as error:
        raise EventOCRError(
            "corrupted_image",
            (
                "We couldn't read this image file. It may be corrupted or "
                "incomplete. Try another photo or enter the event manually."
            ),
            400,
        ) from error


def _normalize_canvas_and_size(image):
    """Apply EXIF orientation, flatten transparency onto white, and normalize scale."""
    transposed = ImageOps.exif_transpose(image)
    if transposed.mode in ("RGBA", "LA") or (
        transposed.mode == "P" and "transparency" in transposed.info
    ):
        rgba = transposed.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        rgb = background
    elif transposed.mode != "RGB":
        rgb = transposed.convert("RGB")
    else:
        rgb = transposed.copy()

    max_dim = max(rgb.width, rgb.height)
    if max_dim > 2400:
        scale = 2400 / max_dim
        rgb = rgb.resize(
            (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
    elif max_dim < 1000:
        scale = min(2.0, 2000 / max(1, max_dim))
        rgb = rgb.resize(
            (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return rgb


def _correct_osd_orientation(rgb_image):
    """Correct 90/180/270 rotation using Tesseract OSD when confidence is adequate."""
    try:
        osd = pytesseract.image_to_osd(
            rgb_image,
            output_type=pytesseract.Output.DICT,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except (
        pytesseract.TesseractNotFoundError,
        pytesseract.TesseractError,
        RuntimeError,
        ValueError,
        KeyError,
        AttributeError,
        OSError,
    ):
        return rgb_image

    if isinstance(osd, dict):
        try:
            rotate = int(osd.get("rotate", 0) or 0)
            confidence = float(osd.get("orientation_conf", 0) or 0)
        except (TypeError, ValueError):
            return rgb_image
    elif isinstance(osd, str):
        rot_match = re.search(r"Rotate:\s*(\d+)", osd)
        conf_match = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        rotate = int(rot_match.group(1)) if rot_match else 0
        confidence = float(conf_match.group(1)) if conf_match else 0.0
    else:
        return rgb_image

    if confidence < 1.5 or rotate not in (90, 180, 270):
        return rgb_image

    transpose_map = {
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }
    return rgb_image.transpose(transpose_map[rotate])


def preprocess_image_for_ocr(image):
    """Prepare oriented, contrast-enhanced grayscale variants for Tesseract OCR."""
    rgb = _normalize_canvas_and_size(image)
    oriented = _correct_osd_orientation(rgb)

    gray = ImageOps.grayscale(oriented)
    mean_luma = ImageStat.Stat(gray).mean[0]
    if mean_luma < 110:
        base_gray = ImageOps.invert(gray)
    else:
        base_gray = gray

    primary = ImageOps.autocontrast(base_gray, cutoff=1)
    primary = ImageEnhance.Contrast(primary).enhance(1.45)
    primary = primary.filter(ImageFilter.SHARPEN)

    denoised = base_gray.filter(ImageFilter.MedianFilter(size=3))
    denoised = ImageOps.autocontrast(denoised, cutoff=2)
    denoised = ImageEnhance.Contrast(denoised).enhance(1.8)
    threshold = max(90, min(195, int(ImageStat.Stat(denoised).mean[0] * 0.92)))
    binarized = denoised.point(lambda px: 255 if px > threshold else 0, mode="1")
    binarized = binarized.convert("L")

    return primary, [binarized]


def _has_event_cue(text):
    if not text:
        return False
    if MONTH_WORD_RE.search(text):
        return True
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", text):
        return True
    if re.search(r"\b\d{1,2}\s*(?:am|pm)\b", text, re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", text):
        return True
    return False


def _score_ocr_text(text):
    if not text:
        return 0
    words = re.findall(r"[A-Za-z0-9]{2,}", text)
    clean_words = re.findall(r"\b[A-Z]?[a-z]{2,}\b", text)
    mixed_case_noise = re.findall(r"[a-z][A-Z]|[A-Z]\d+[A-Z]", text)
    alnum_count = sum(ch.isalnum() for ch in text)
    bonus = 0
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        bonus += 35
    elif re.search(r"\b\d{1,2}[:/]\d{2}\b", text):
        bonus += 20
    if re.search(r"\b(?:am|pm)\b", text, re.IGNORECASE):
        bonus += 25
    if MONTH_WORD_RE.search(text):
        bonus += 35
    return (
        alnum_count
        + len(words) * 2
        + len(clean_words) * 4
        + bonus
        - len(mixed_case_noise) * 8
    )


def extract_text_from_image(image):
    """Run Tesseract OCR on preprocessed image variants and return normalized text."""
    primary, fallbacks = preprocess_image_for_ocr(image)
    deadline = time_module.monotonic() + OCR_TIME_BUDGET_SECONDS

    def _out_of_time():
        return time_module.monotonic() >= deadline

    def _run_ocr(target_image, config):
        return pytesseract.image_to_string(
            target_image,
            lang="eng",
            config=config,
            timeout=OCR_TIMEOUT_SECONDS,
        )

    try:
        primary_text = _run_ocr(primary, "--oem 3 --psm 3")
    except pytesseract.TesseractNotFoundError as error:
        raise EventOCRError(
            "ocr_unavailable",
            (
                "Document scanning is not available on this server right now. "
                "Please enter the event manually."
            ),
            503,
        ) from error
    except (pytesseract.TesseractError, RuntimeError, OSError) as error:
        logger.warning("OCR extraction failed: %s", error)
        raise EventOCRError(
            "ocr_failed",
            (
                "We couldn't process this image right now. "
                "Try again or enter the event manually."
            ),
            500,
        ) from error

    best_text = primary_text
    best_score = _score_ocr_text(primary_text)
    alnum_chars = sum(ch.isalnum() for ch in (primary_text or ""))
    word_tokens = re.findall(r"[A-Za-z0-9]{2,}", primary_text or "")

    if alnum_chars < 25 or len(word_tokens) < 4:
        candidates = [(primary, "--oem 3 --psm 11")]
        for variant in fallbacks:
            candidates.append((variant, "--oem 3 --psm 6"))
        for variant, config in candidates:
            if _out_of_time():
                break
            try:
                candidate_text = _run_ocr(variant, config)
            except (pytesseract.TesseractError, RuntimeError, OSError):
                continue
            score = _score_ocr_text(candidate_text)
            if score > best_score:
                best_score = score
                best_text = candidate_text

    if not _has_event_cue(best_text):
        for transpose_op in (
            Image.Transpose.ROTATE_270,
            Image.Transpose.ROTATE_90,
            Image.Transpose.ROTATE_180,
        ):
            if _out_of_time():
                break
            rotated = primary.transpose(transpose_op)
            try:
                candidate_text = _run_ocr(rotated, "--oem 3 --psm 3")
            except (pytesseract.TesseractError, RuntimeError, OSError):
                continue
            finally:
                rotated.close()
            score = _score_ocr_text(candidate_text)
            if score > best_score:
                best_score = score
                best_text = candidate_text
                if _has_event_cue(candidate_text):
                    break

    normalized = normalize_ocr_text(best_text or "")
    usable_tokens = re.findall(r"[A-Za-z0-9]{2,}", normalized)
    alnum_total = sum(ch.isalnum() for ch in normalized)
    if alnum_total < 4 or len(usable_tokens) < 2:
        raise EventOCRError(
            "no_text",
            (
                "We couldn't read enough information from this image. "
                "Try taking a clearer photo or enter the event manually."
            ),
            422,
        )
    return normalized


def normalize_ocr_text(text):
    """Normalize OCR output whitespace, Unicode characters, and clear time typos."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Normalize common OCR punctuation mistakes inside clear time expressions only.
    text = re.sub(
        r"\b([0-2]?\d)[;.](\d{2}\s*(?:a\.?m\.?|p\.?m\.?))\b",
        r"\1:\2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b[Oo](\d:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)\b",
        r"0\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b([12])[Oo](:\d{2})\b",
        r"\g<1>0\2",
        text,
    )
    text = re.sub(r"\b([ap])\s*\.\s*m\.?\b", r"\1M", text, flags=re.IGNORECASE)

    cleaned_lines = []
    blank_streak = 0
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            blank_streak += 1
            if blank_streak <= 1 and cleaned_lines:
                cleaned_lines.append("")
            continue
        blank_streak = 0
        cleaned_lines.append(line)

    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


def _extract_document_years(text):
    years = []
    for match in re.finditer(r"\b(20[2-9]\d)\b", text):
        yr = int(match.group(1))
        if yr not in years:
            years.append(yr)
    return years


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolve_missing_year(month, day, weekday_name, reference_date, doc_years):
    candidate_years = []
    for yr in doc_years + [reference_date.year, reference_date.year + 1]:
        if yr not in candidate_years:
            candidate_years.append(yr)

    target_wd = WEEKDAY_MAP.get((weekday_name or "").lower())
    if target_wd is not None:
        for yr in candidate_years:
            resolved = _safe_date(yr, month, day)
            if resolved is not None and resolved.weekday() == target_wd:
                return (
                    resolved,
                    f"Year was not listed; inferred {yr}. Please verify this date.",
                )

    if doc_years:
        resolved = _safe_date(doc_years[0], month, day)
        if resolved is not None:
            return (
                resolved,
                f"Year was not listed; inferred {doc_years[0]}. "
                "Please verify this date.",
            )

    current = _safe_date(reference_date.year, month, day)
    if current is not None and (reference_date - current).days <= 30:
        return (
            current,
            f"Year was not listed; inferred {reference_date.year}. "
            "Please verify this date.",
        )
    next_yr = _safe_date(reference_date.year + 1, month, day)
    if next_yr is not None:
        return (
            next_yr,
            f"Year was not listed; inferred {reference_date.year + 1}. "
            "Please verify this date.",
        )
    if current is not None:
        return current, "Please verify this date."
    return None, None


def _resolve_two_digit_year(raw_year, reference_date):
    if len(raw_year) == 4:
        return int(raw_year)
    century = (reference_date.year // 100) * 100
    return century + int(raw_year)


def _overlaps(span_a, spans):
    return any(not (span_a[1] <= b[0] or span_a[0] >= b[1]) for b in spans)


def _line_date_context(line, start_idx):
    local_prefix = line[max(0, start_idx - 45) : start_idx]
    is_deadline = bool(DEADLINE_PREFIX_RE.search(local_prefix)) or (
        bool(DEADLINE_PREFIX_RE.search(line))
        and not EVENT_DATE_PREFIX_RE.search(local_prefix)
        and start_idx > 0
        and not re.search(r"\b(?:on|for|date|when)\b", local_prefix, re.IGNORECASE)
    )
    if DEADLINE_PREFIX_RE.search(line) and not DEADLINE_PREFIX_RE.search(local_prefix):
        # e.g. "Join us on Nov 20! RSVP by Nov 10." -> Nov 20 is NOT deadline.
        if EVENT_DATE_PREFIX_RE.search(local_prefix) or start_idx < 25:
            is_deadline = False
    is_alternate = bool(ALTERNATE_DATE_RE.search(local_prefix)) or bool(
        ALTERNATE_DATE_RE.search(line)
    )
    bonus = 0
    if EVENT_DATE_PREFIX_RE.search(local_prefix) or EVENT_DATE_PREFIX_RE.search(line):
        bonus += 4
    if SINGLE_TIME_RE.search(line) or TIME_RANGE_RE.search(line):
        bonus += 2
    if is_alternate:
        bonus -= 3
    return is_deadline, is_alternate, bonus


def _extract_dates_from_lines(lines, reference_date, doc_years):
    matches = []
    for idx, line in enumerate(lines):
        consumed_spans = []

        # 1. Cross-month or explicit full date ranges: "Nov 20, 2026 - Nov 22, 2026"
        for m in DATE_RANGE_FULL_RE.finditer(line):
            wd1, m1, d1, y1, _wd2, m2, d2, y2 = m.groups()
            month1, day1 = MONTH_NAMES[m1.lower()], int(d1)
            month2, day2 = MONTH_NAMES[m2.lower()], int(d2)
            year2 = int(y2) if y2 else (int(y1) if y1 else None)
            year1 = int(y1) if y1 else year2
            warn_list = []
            if year1 is None:
                start_d, warn1 = _resolve_missing_year(
                    month1, day1, wd1, reference_date, doc_years
                )
                end_d = _safe_date(start_d.year, month2, day2) if start_d else None
                if warn1:
                    warn_list.append(warn1)
            else:
                start_d = _safe_date(year1, month1, day1)
                end_d = _safe_date(year2, month2, day2)
            if start_d is None:
                continue
            if end_d is not None and end_d < start_d and y2 is None:
                end_d = _safe_date(start_d.year + 1, month2, day2)
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=start_d,
                    end_date=end_d if end_d and end_d != start_d else None,
                    score=(10 if not is_dl else -20) + bonus + (2 if y1 or y2 else 0),
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 2. Same-month date ranges: "November 20-22, 2026"
        for m in DATE_RANGE_SAME_MONTH_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            wd, month_str, d1, d2, year_str = m.groups()
            month = MONTH_NAMES[month_str.lower()]
            day1, day2 = int(d1), int(d2)
            if day2 <= day1 or day2 > 31:
                continue
            warn_list = []
            if year_str:
                yr = int(year_str)
                start_d = _safe_date(yr, month, day1)
            else:
                start_d, warn1 = _resolve_missing_year(
                    month, day1, wd, reference_date, doc_years
                )
                if warn1:
                    warn_list.append(warn1)
            if start_d is None:
                continue
            end_d = _safe_date(start_d.year, month, day2)
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=start_d,
                    end_date=end_d if end_d and end_d != start_d else None,
                    score=(10 if not is_dl else -20) + bonus + (2 if year_str else 0),
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 3. Numeric date ranges: "11/20/2026 - 11/22/2026"
        for m in NUMERIC_DATE_RANGE_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            a1, b1, y1, a2, b2, y2 = m.groups()
            d_start, w1 = _parse_numeric_date(a1, b1, y1, reference_date)
            d_end, w2 = _parse_numeric_date(a2, b2, y2, reference_date)
            if d_start is None:
                continue
            warn_list = list(dict.fromkeys(w1 + w2))
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=d_start,
                    end_date=d_end if d_end and d_end > d_start else None,
                    score=(9 if not is_dl else -20) + bonus,
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 4. Named month single date: "Friday, November 20, 2026" or "Nov 20"
        for m in NAMED_MONTH_DATE_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            wd, month_str, day_str, year_str = m.groups()
            month = MONTH_NAMES[month_str.lower()]
            day = int(day_str)
            warn_list = []
            if year_str:
                resolved = _safe_date(int(year_str), month, day)
                if resolved and wd:
                    expected_wd = WEEKDAY_MAP.get(wd.lower())
                    if expected_wd is not None and resolved.weekday() != expected_wd:
                        warn_list.append(
                            "Weekday and date may not match. Please verify this date."
                        )
            else:
                resolved, warn_msg = _resolve_missing_year(
                    month, day, wd, reference_date, doc_years
                )
                if warn_msg:
                    warn_list.append(warn_msg)
            if resolved is None:
                continue
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=resolved,
                    end_date=None,
                    score=(9 if not is_dl else -20) + bonus + (2 if year_str else 0),
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 5. Day-Month(-Year) e.g. "20 November 2026"
        for m in DAY_MONTH_YEAR_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            wd, day_str, month_str, year_str = m.groups()
            month = MONTH_NAMES[month_str.lower()]
            day = int(day_str)
            warn_list = []
            if year_str:
                resolved = _safe_date(int(year_str), month, day)
            else:
                resolved, warn_msg = _resolve_missing_year(
                    month, day, wd, reference_date, doc_years
                )
                if warn_msg:
                    warn_list.append(warn_msg)
            if resolved is None:
                continue
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=resolved,
                    end_date=None,
                    score=(9 if not is_dl else -20) + bonus + (2 if year_str else 0),
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 6. ISO dates: "2026-11-20"
        for m in ISO_DATE_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            yr, mo, dy = (int(v) for v in m.groups())
            resolved = _safe_date(yr, mo, dy)
            if resolved is None:
                continue
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=resolved,
                    end_date=None,
                    score=(10 if not is_dl else -20) + bonus,
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=[],
                )
            )

        # 7. Numeric dates: "11/20/2026", "11/20", "05/06/2026"
        for m in NUMERIC_DATE_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            p1, p2, yr_raw = m.groups()
            resolved, warn_list = _parse_numeric_date(p1, p2, yr_raw, reference_date)
            if resolved is None:
                continue
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=resolved,
                    end_date=None,
                    score=(8 if not is_dl else -20) + bonus + (2 if yr_raw else 0),
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=warn_list,
                )
            )

        # 8. Relative dates: "tomorrow", "next Friday", "this Saturday"
        for m in RELATIVE_DATE_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            phrase = m.group(1).lower()
            wd_str = m.group(2)
            resolved = _resolve_relative_phrase(phrase, wd_str, reference_date)
            if resolved is None:
                continue
            is_dl, is_alt, bonus = _line_date_context(line, m.start())
            consumed_spans.append(m.span())
            matches.append(
                _DateMatch(
                    start_date=resolved,
                    end_date=None,
                    score=(6 if not is_dl else -20) + bonus,
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    is_alternate=is_alt,
                    warnings=[
                        "Relative date resolved from today. Please verify this date."
                    ],
                )
            )

        # 9. Controlled dateutil fallback for explicitly labeled "Date:" / "When:" lines
        if not consumed_spans and re.match(
            r"^(?:when|date|event\s+date)\s*[:\-–—]\s*(.+)$",
            line,
            re.IGNORECASE,
        ):
            value_part = line.split(":", 1)[-1].strip()
            try:
                parsed = dateutil_parser.parse(
                    value_part,
                    default=datetime(reference_date.year, 1, 1),
                    fuzzy=True,
                ).date()
            except (ValueError, OverflowError):
                parsed = None
            if parsed is not None:
                matches.append(
                    _DateMatch(
                        start_date=parsed,
                        end_date=None,
                        score=7,
                        line_index=idx,
                        span=(0, len(line)),
                        is_deadline=False,
                        is_alternate=False,
                        warnings=["Please verify this date."],
                    )
                )

    return matches


def _parse_numeric_date(part1, part2, year_raw, reference_date):
    a, b = int(part1), int(part2)
    if a == 0 or b == 0:
        return None, []
    warnings_out = []
    if year_raw:
        yr = _resolve_two_digit_year(year_raw, reference_date)
        if len(year_raw) == 2:
            warnings_out.append("Please verify this date.")
    else:
        yr = reference_date.year

    if a <= 12 and b <= 31:
        month, day = a, b
        if b <= 12 and a != b:
            warnings_out.append(
                f"Numeric date {part1}/{part2} could be month/day or day/month. "
                "Please verify this date."
            )
    elif a <= 31 and b <= 12:
        month, day = b, a
        warnings_out.append("Please verify this date.")
    else:
        return None, []

    if not year_raw:
        resolved, warn_yr = _resolve_missing_year(month, day, None, reference_date, [])
        if warn_yr and warn_yr not in warnings_out:
            warnings_out.append(warn_yr)
        return resolved, warnings_out

    return _safe_date(yr, month, day), warnings_out


def _resolve_relative_phrase(phrase, weekday_str, reference_date):
    if phrase in ("today", "tonight"):
        return reference_date
    if phrase == "tomorrow":
        return reference_date + timedelta(days=1)
    if weekday_str:
        target = WEEKDAY_MAP.get(weekday_str.lower())
        if target is None:
            return None
        days_ahead = (target - reference_date.weekday()) % 7
        if phrase.startswith("next ") and days_ahead == 0:
            days_ahead = 7
        elif phrase.startswith("next ") and days_ahead <= 2:
            days_ahead += 7
        return reference_date + timedelta(days=days_ahead)
    return None


def _parse_single_time_token(token, fallback_meridiem=None):
    raw = token.strip().lower()
    if raw == "noon":
        return time(12, 0), "pm", False
    if raw == "midnight":
        return time(0, 0), "am", False

    meridiem = None
    if "a" in raw:
        meridiem = "am"
    elif "p" in raw:
        meridiem = "pm"

    numeric = re.sub(r"[a-z.\s]", "", raw)
    if ":" in numeric:
        hour_str, minute_str = numeric.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    else:
        hour, minute = int(numeric), 0

    if minute < 0 or minute > 59:
        return None, None, False

    if meridiem:
        if hour < 1 or hour > 12:
            return None, None, False
        if meridiem == "am":
            hour24 = 0 if hour == 12 else hour
        else:
            hour24 = 12 if hour == 12 else hour + 12
        return time(hour24, minute), meridiem, False

    if 13 <= hour <= 23:
        return time(hour, minute), "pm", False
    if hour == 0:
        return time(0, minute), "am", False

    if fallback_meridiem in ("am", "pm") and 1 <= hour <= 12:
        if fallback_meridiem == "am":
            hour24 = 0 if hour == 12 else hour
        else:
            hour24 = 12 if hour == 12 else hour + 12
        return time(hour24, minute), fallback_meridiem, False

    if 1 <= hour <= 12:
        # Bare 12h clock without AM/PM -> infer daytime cautiously and warn.
        hour24 = hour if 7 <= hour <= 11 else (12 if hour == 12 else hour + 12)
        return time(hour24, minute), None, True

    return None, None, False


def _parse_time_range_pair(start_token, end_token):
    end_t, end_mer, end_uncertain = _parse_single_time_token(end_token)
    if end_t is None:
        return None, None, False

    start_raw = start_token.strip().lower()
    start_has_mer = (
        ("a" in start_raw)
        or ("p" in start_raw)
        or start_raw
        in (
            "noon",
            "midnight",
        )
    )

    if start_has_mer:
        start_t, _, start_uncertain = _parse_single_time_token(start_token)
        if start_t is None:
            return None, None, False
        return start_t, end_t, (start_uncertain or end_uncertain)

    # Start token lacks AM/PM; check whether end_mer applies or morning->afternoon.
    start_t_same, _, start_uncertain = _parse_single_time_token(
        start_token, fallback_meridiem=end_mer if not end_uncertain else None
    )
    if start_t_same is None:
        return None, None, False

    if end_mer == "pm" and not end_uncertain:
        raw_num = re.sub(r"[a-z.\s]", "", start_raw)
        start_h = int(raw_num.split(":")[0]) if ":" in raw_num else int(raw_num)
        if start_t_same > end_t and 7 <= start_h <= 11:
            start_t_am, _, _ = _parse_single_time_token(
                start_token, fallback_meridiem="am"
            )
            if start_t_am is not None and start_t_am < end_t:
                return start_t_am, end_t, False

    return start_t_same, end_t, (start_uncertain or end_uncertain)


def _extract_times_from_lines(lines):
    matches = []
    for idx, line in enumerate(lines):
        consumed_spans = []
        is_deadline_line = bool(TIME_NON_EVENT_PREFIX_RE.search(line))
        is_doors_line = bool(TIME_DOORS_PREFIX_RE.search(line))
        has_start_kw = bool(TIME_STARTS_PREFIX_RE.search(line))

        for m in TIME_RANGE_RE.finditer(line):
            start_t, end_t, uncertain = _parse_time_range_pair(m.group(1), m.group(2))
            if start_t is None or end_t is None:
                continue
            local_prefix = line[max(0, m.start() - 30) : m.start()]
            is_dl = bool(TIME_NON_EVENT_PREFIX_RE.search(local_prefix)) or (
                is_deadline_line and not has_start_kw
            )
            warn_list = []
            if uncertain:
                warn_list.append("AM/PM was not specified. Please verify this time.")
            if end_t <= start_t:
                warn_list.append("End time is not after start time. Please verify.")
            score = 12 if not is_dl else -20
            if has_start_kw:
                score += 3
            consumed_spans.append(m.span())
            matches.append(
                _TimeMatch(
                    start_time=start_t,
                    end_time=end_t,
                    score=score,
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    warnings=warn_list,
                )
            )

        for m in SINGLE_TIME_RE.finditer(line):
            if _overlaps(m.span(), consumed_spans):
                continue
            token = m.group(1)
            has_mer = bool(
                re.search(r"(?:am|pm|a\.m\.|p\.m\.|noon|midnight)", token, re.I)
            )
            is_24h = bool(re.match(r"^(?:1[3-9]|2[0-3]|0\d):\d{2}$", token.strip()))
            local_prefix = line[max(0, m.start() - 30) : m.start()]
            if not (
                has_mer
                or is_24h
                or has_start_kw
                or re.search(r"\bat\s*$", local_prefix, re.I)
            ):
                continue
            parsed_t, _, uncertain = _parse_single_time_token(token)
            if parsed_t is None:
                continue
            is_dl = bool(TIME_NON_EVENT_PREFIX_RE.search(local_prefix)) or (
                is_deadline_line and not has_start_kw
            )
            warn_list = []
            if uncertain:
                warn_list.append("AM/PM was not specified. Please verify this time.")
            score = 7 if not is_dl else -20
            if has_start_kw:
                score += 3
            if is_doors_line and not has_start_kw:
                score -= 2
            consumed_spans.append(m.span())
            matches.append(
                _TimeMatch(
                    start_time=parsed_t,
                    end_time=None,
                    score=score,
                    line_index=idx,
                    span=m.span(),
                    is_deadline=is_dl,
                    warnings=warn_list,
                )
            )
    return matches


def _clean_extracted_fragment(text):
    text = re.sub(r"\s+", " ", text or "").strip(" \t\r\n|,;:-–—•")
    return text


def _strip_date_and_time_noise(text):
    cleaned = text
    for pattern in (
        DATE_RANGE_FULL_RE,
        DATE_RANGE_SAME_MONTH_RE,
        NUMERIC_DATE_RANGE_RE,
        NAMED_MONTH_DATE_RE,
        DAY_MONTH_YEAR_RE,
        ISO_DATE_RE,
        NUMERIC_DATE_RE,
        TIME_RANGE_RE,
        SINGLE_TIME_RE,
    ):
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b(?:on|from|at|by|until|till|to|between)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _clean_extracted_fragment(cleaned)


def _extract_location_from_lines(lines):
    # 1. Explicit labeled location
    for idx, line in enumerate(lines):
        match = LOCATION_LABEL_RE.match(line)
        if not match:
            continue
        candidate = _clean_extracted_fragment(match.group(1))
        if idx + 1 < len(lines):
            next_line = lines[idx + 1].strip()
            if (
                next_line
                and not LOCATION_LABEL_RE.match(next_line)
                and not TITLE_LABEL_RE.match(next_line)
                and not OTHER_LABEL_RE.match(next_line)
                and (
                    STREET_ADDRESS_RE.search(next_line)
                    or CITY_STATE_ZIP_RE.search(next_line)
                )
            ):
                candidate = f"{candidate}, {next_line}"
        candidate = _strip_trailing_time_or_rsvp(candidate)
        if candidate and not EMAIL_OR_URL_RE.search(candidate):
            return candidate[:255]

    # 2. Street address line (with optional preceding venue name line)
    for idx, line in enumerate(lines):
        if EMAIL_OR_URL_RE.search(line) or PHONE_RE.search(line):
            continue
        if STREET_ADDRESS_RE.search(line):
            addr = _clean_extracted_fragment(line)
            if idx > 0:
                prev = lines[idx - 1].strip()
                if (
                    prev
                    and VENUE_KEYWORD_RE.search(prev)
                    and not TITLE_LABEL_RE.match(prev)
                    and not OTHER_LABEL_RE.match(prev)
                    and len(prev) <= 80
                ):
                    combined = f"{prev}, {addr}"
                    if len(combined) <= 255:
                        return combined
            return addr[:255]

    # 3. Inline "at <Venue>" or "@ <Venue>"
    for line in lines:
        if DEADLINE_PREFIX_RE.search(line):
            continue
        for match in INLINE_AT_VENUE_RE.finditer(line):
            venue = _clean_extracted_fragment(match.group(1))
            if not venue or AT_STOPWORDS_RE.match(venue):
                continue
            venue = _strip_trailing_time_or_rsvp(venue)
            if venue and len(venue) >= 3 and not EMAIL_OR_URL_RE.search(venue):
                return venue[:255]

    # 4. Standalone venue keyword line
    candidates = []
    for idx, line in enumerate(lines):
        if (
            TITLE_LABEL_RE.match(line)
            or OTHER_LABEL_RE.match(line)
            or DEADLINE_PREFIX_RE.search(line)
            or EMAIL_OR_URL_RE.search(line)
            or PHONE_RE.search(line)
        ):
            continue
        parts = [p.strip() for p in re.split(r"\s*[|•]\s*", line) if p.strip()]
        for part in parts:
            stripped = _strip_date_and_time_noise(part)
            if stripped and VENUE_KEYWORD_RE.search(stripped):
                candidates.append((idx, stripped))
    if len(candidates) >= 2:
        # Prefer a later specific venue line when letterhead precedes a room line.
        return candidates[-1][1][:255]
    if candidates:
        return candidates[0][1][:255]
    return None


def _strip_trailing_time_or_rsvp(text):
    cleaned = re.split(
        r"\b(?:rsvp|for\s+more|call\s+\d|from\s+\d{1,2}[:\s])\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = TIME_RANGE_RE.sub("", cleaned)
    cleaned = SINGLE_TIME_RE.sub("", cleaned)
    return _clean_extracted_fragment(cleaned)


PRESENTER_INTRO_RE = re.compile(
    r"\b(?:presents|invites\s+you(?:\s+to)?|"
    r"welcomes\s+you(?:\s+to)?|announces)\s*[:!]*$",
    re.IGNORECASE,
)


def _looks_like_non_title_line(line):
    if not line or len(line) < 2:
        return True
    if BOILERPLATE_LINE_RE.match(line) or PRESENTER_INTRO_RE.search(line):
        return True
    if OTHER_LABEL_RE.match(line) or LOCATION_LABEL_RE.match(line):
        return True
    if EMAIL_OR_URL_RE.search(line) or PHONE_RE.search(line):
        return True
    if DEADLINE_PREFIX_RE.search(line) and not EVENT_DATE_PREFIX_RE.search(line):
        return True
    if STREET_ADDRESS_RE.match(line) or CITY_STATE_ZIP_RE.fullmatch(line):
        return True
    stripped = _strip_date_and_time_noise(line)
    if not stripped or len(re.findall(r"[A-Za-z]{2,}", stripped)) == 0:
        return True
    return False


def _extract_title_from_lines(lines, picked_location):
    for line in lines:
        match = TITLE_LABEL_RE.match(line)
        if match:
            title = _clean_extracted_fragment(match.group(1))
            if title:
                return title[:200]

    candidates = []
    for idx, line in enumerate(lines):
        segments = [seg.strip() for seg in re.split(r"\s*[|•]\s*", line) if seg.strip()]
        for seg in segments:
            if _looks_like_non_title_line(seg):
                continue
            cleaned = INVITE_PREFIX_RE.sub("", seg).strip()
            cleaned = _strip_date_and_time_noise(cleaned)
            # Remove trailing "at <picked_location>" or "@ <picked_location>"
            if picked_location:
                cleaned = re.sub(
                    rf"\s*(?:@|\bat)\s+{re.escape(picked_location)}$",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                )
            cleaned = _clean_extracted_fragment(cleaned)
            if not cleaned or len(re.findall(r"[A-Za-z]{2,}", cleaned)) == 0:
                continue
            if picked_location and cleaned.lower() == picked_location.lower():
                continue
            if STREET_ADDRESS_RE.search(cleaned):
                continue
            candidates.append((idx, cleaned))

    if not candidates:
        return None

    # If the first line looks like a school/organization name and the next candidate
    # looks like the actual event name, prefer the event name!
    if len(candidates) >= 2 and candidates[0][0] <= 1:
        first_txt = candidates[0][1]
        second_txt = candidates[1][1]
        if (
            VENUE_KEYWORD_RE.search(first_txt)
            and not VENUE_KEYWORD_RE.search(second_txt)
            and len(second_txt) >= 4
        ):
            return second_txt[:200]

    return candidates[0][1][:200]


def _split_event_blocks(lines, reference_date, doc_years):
    """Split text into distinct event blocks only when multiple event dates appear."""
    # First check blank-line-separated paragraphs.
    paragraphs = []
    current = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(current)

    if len(paragraphs) > 1:
        event_paragraphs = []
        for para in paragraphs:
            para_dates = [
                d
                for d in _extract_dates_from_lines(para, reference_date, doc_years)
                if not d.is_deadline and not d.is_alternate
            ]
            if para_dates:
                event_paragraphs.append(para)
        if len(event_paragraphs) >= 2:
            # Attach leading non-date header to nothing; return each event paragraph.
            return event_paragraphs

    # Check line-by-line schedule items (each line has its own event date + time/title)
    non_empty = [ln for ln in lines if ln]
    line_blocks = []
    current_block = []
    current_has_date = False
    for line in non_empty:
        line_dates = [
            d
            for d in _extract_dates_from_lines([line], reference_date, doc_years)
            if not d.is_deadline and not d.is_alternate
        ]
        if line_dates and current_has_date:
            line_blocks.append(current_block)
            current_block = [line]
            current_has_date = True
        else:
            current_block.append(line)
            if line_dates:
                current_has_date = True
    if current_block:
        line_blocks.append(current_block)

    dated_blocks = [
        blk
        for blk in line_blocks
        if any(
            not d.is_deadline and not d.is_alternate
            for d in _extract_dates_from_lines(blk, reference_date, doc_years)
        )
    ]
    if len(dated_blocks) >= 2:
        # Ensure each block also has some distinct title/time content (avoid splitting
        # a single event whose date was repeated on two lines).
        distinct_dates = {
            _extract_dates_from_lines(blk, reference_date, doc_years)[0].start_date
            for blk in dated_blocks
        }
        if len(distinct_dates) >= 2:
            return dated_blocks

    return [non_empty]


def _format_display_date(iso_date_str):
    if not iso_date_str:
        return None
    parsed = date.fromisoformat(iso_date_str)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _format_display_time(hhmm_str):
    if not hhmm_str:
        return None
    parsed = time.fromisoformat(hhmm_str)
    hour12 = parsed.hour % 12 or 12
    meridiem = "AM" if parsed.hour < 12 else "PM"
    return f"{hour12}:{parsed.minute:02d} {meridiem}"


def _parse_single_block(lines, reference_date, doc_years):
    date_matches = _extract_dates_from_lines(lines, reference_date, doc_years)
    time_matches = _extract_times_from_lines(lines)
    location = _extract_location_from_lines(lines)
    title = _extract_title_from_lines(lines, location)

    field_warnings = {}

    def _add_warning(field, msg):
        bucket = field_warnings.setdefault(field, [])
        if msg not in bucket:
            bucket.append(msg)

    non_deadline_dates = [d for d in date_matches if not d.is_deadline]
    primary_dates = [d for d in non_deadline_dates if not d.is_alternate]

    chosen_date_match = None
    if primary_dates:
        primary_dates.sort(key=lambda d: (-d.score, d.line_index))
        chosen_date_match = primary_dates[0]
        distinct_primary = {d.start_date for d in primary_dates}
        if len(distinct_primary) > 1:
            _add_warning(
                "date",
                "Multiple dates were found in this document. Please verify this date.",
            )
        elif any(d.is_alternate for d in non_deadline_dates):
            _add_warning(
                "date",
                "An alternate date was also listed. Please verify this date.",
            )
    elif non_deadline_dates:
        non_deadline_dates.sort(key=lambda d: (-d.score, d.line_index))
        chosen_date_match = non_deadline_dates[0]
        _add_warning("date", "Please verify this date.")

    if chosen_date_match:
        for warn in chosen_date_match.warnings:
            _add_warning("date", warn)

    non_deadline_times = [t for t in time_matches if not t.is_deadline]
    chosen_time_match = None
    if non_deadline_times:
        non_deadline_times.sort(key=lambda t: (-t.score, t.line_index))
        chosen_time_match = non_deadline_times[0]
        for warn in chosen_time_match.warnings:
            _add_warning("start_time", warn)
            if chosen_time_match.end_time is not None:
                _add_warning("end_time", warn)

    event_date = chosen_date_match.start_date.isoformat() if chosen_date_match else None
    end_date = (
        chosen_date_match.end_date.isoformat()
        if chosen_date_match and chosen_date_match.end_date
        else None
    )
    start_time = (
        chosen_time_match.start_time.strftime("%H:%M")
        if chosen_time_match and chosen_time_match.start_time
        else None
    )
    end_time = (
        chosen_time_match.end_time.strftime("%H:%M")
        if chosen_time_match and chosen_time_match.end_time
        else None
    )

    event_data = {
        "title": title,
        "date": event_date,
        "start_time": start_time,
        "end_date": end_date,
        "end_time": end_time,
        "location": location,
    }

    missing_fields = [field for field in REQUIRED_EVENT_FIELDS if not event_data[field]]
    missing_field_messages = {
        field: MISSING_FIELD_MESSAGES[field] for field in missing_fields
    }

    warnings_list = [
        {"field": field, "message": message}
        for field, messages_for_field in field_warnings.items()
        for message in messages_for_field
    ]

    display = {
        "title": title,
        "date": _format_display_date(event_date),
        "start_time": _format_display_time(start_time),
        "end_date": _format_display_date(end_date),
        "end_time": _format_display_time(end_time),
        "location": location,
    }

    return {
        "event": event_data,
        "display": display,
        "missing_fields": missing_fields,
        "missing_field_messages": missing_field_messages,
        "field_warnings": field_warnings,
        "warnings": warnings_list,
    }


def parse_event_text(text, *, timezone_name=None, reference_date=None):
    """Parse normalized OCR text into event candidates without inventing data."""
    if not isinstance(text, str) or not text.strip():
        raise EventOCRError(
            "no_text",
            (
                "We couldn't read enough information from this image. "
                "Try taking a clearer photo or enter the event manually."
            ),
            422,
        )

    tz_name = timezone_name or timezone.get_current_timezone_name()
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError) as error:
        raise EventOCRError(
            "invalid_timezone", "Choose a valid timezone.", 400
        ) from error

    ref_date = reference_date or timezone.localdate(timezone=zone)
    normalized = normalize_ocr_text(text)
    lines = normalized.split("\n")
    doc_years = _extract_document_years(normalized)

    blocks = _split_event_blocks(lines, ref_date, doc_years)[:5]
    candidates = [_parse_single_block(block, ref_date, doc_years) for block in blocks]

    usable_candidates = [
        cand
        for cand in candidates
        if cand["event"]["date"]
        or cand["event"]["start_time"]
        or (cand["event"]["title"] and cand["event"]["location"])
    ]
    if not usable_candidates:
        raise EventOCRError(
            "no_event_info",
            (
                "We couldn't find enough event details in this image. "
                "Try taking a clearer photo or enter the event manually."
            ),
            422,
        )

    primary = usable_candidates[0]
    multiple_events = len(usable_candidates) > 1
    top_warnings = list(primary["warnings"])
    if multiple_events:
        top_warnings.insert(
            0,
            {
                "field": None,
                "message": (
                    f"{len(usable_candidates)} possible events were found in this "
                    "document. Choose the one you want to add below."
                ),
            },
        )

    return {
        "status": "partial" if primary["missing_fields"] else "success",
        "event": primary["event"],
        "display": primary["display"],
        "missing_fields": primary["missing_fields"],
        "missing_field_messages": primary["missing_field_messages"],
        "field_warnings": primary["field_warnings"],
        "warnings": top_warnings,
        "multiple_events": multiple_events,
        "candidates": usable_candidates,
        "raw_text": normalized[:2000],
    }


def scan_event_image(uploaded_file, *, timezone_name=None, reference_date=None):
    """Validate, preprocess, OCR, and parse an uploaded event document image."""
    if not OCR_SLOTS.acquire(blocking=False):
        raise EventOCRError(
            "busy", "Scanning is busy, try again in a moment.", http_status=429
        )
    try:
        image = validate_and_load_image(uploaded_file)
        try:
            extracted_text = extract_text_from_image(image)
        finally:
            image.close()
    finally:
        OCR_SLOTS.release()
    return parse_event_text(
        extracted_text,
        timezone_name=timezone_name,
        reference_date=reference_date,
    )
