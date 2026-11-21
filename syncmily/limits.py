"""Every limit in one place: request sizes, text lengths, quotas, render caps and rate
limits.

settings.py and the request-size middleware import the request numbers. The nginx caps
in .platform/hooks/postdeploy/03_https.sh must match the values here. Forms, model
fields, views, the chat consumer and the nightly jobs read the other numbers from this
module instead of keeping their own copies. Hardening spec sections 4.1 to 4.4, with
the changes in section 10.1, explain each number.
"""

from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# Request size and shape (4.1)
# ---------------------------------------------------------------------------

# Django request parsing (settings.py).
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536  # 64 KiB: the largest real form is under 10 KB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200  # the event form has about 30 inputs plus members
DATA_UPLOAD_MAX_NUMBER_FILES = 2  # only scan-ocr takes a file, and it reads one
FILE_UPLOAD_MAX_MEMORY_SIZE = 1048576  # 1 MiB: larger scans spool to temp disk

# nginx body caps (03_https.sh), as the strings nginx reads.
NGINX_BODY_CAP_DEFAULT = "128k"
NGINX_BODY_CAP_SCAN = "12m"
NGINX_BODY_CAP_WEBSOCKET = "1k"

# Caps the request-size middleware applies by Content-Length, in bytes.
SCAN_BODY_CAP_BYTES = (
    11 * 1024 * 1024
)  # scan-ocr: 11 MiB, above the 10 MB photo promise
DEFAULT_BODY_CAP_BYTES = 128 * 1024  # every other path: 128 KiB

# JSON request bodies (safety.parse_json_body). No endpoint parses JSON today.
JSON_BODY_MAX_BYTES = 64 * 1024
JSON_MAX_DEPTH = 5

# One chat WebSocket text frame, in characters. Binary frames are refused.
WEBSOCKET_FRAME_MAX_CHARS = 8192

# Sign-in form fields.
LOGIN_USERNAME_MAX = 150
PASSWORD_MAX = 1024

# ---------------------------------------------------------------------------
# Text (2.2, 2.3, 3.1)
# ---------------------------------------------------------------------------

# The normalizer keeps at most this many marks on one base character: 3 from the
# generic combining-diacritic blocks (the ones the database check counts), 8 in all,
# so Indic, Hebrew and Tibetan text keeps its real stacks.
GENERIC_MARKS_PER_BASE = 3
MARKS_PER_BASE = 8
# Joiners in one emoji sequence. The longest real sequence (a family) has 3.
ZWJ_PER_EMOJI = 7

# validate_text refuses raw input longer than RAW_TEXT_FACTOR x max + RAW_TEXT_SLACK
# before any Unicode work, so the normalizer never runs on megabytes.
RAW_TEXT_FACTOR = 4
RAW_TEXT_SLACK = 64


@dataclass(frozen=True)
class TextLengths:
    """Longest stored value per field, counted in code points after normalization."""

    event_title: int = 200
    event_location: int = 255
    checklist_title: int = 150
    checklist_item: int = 255
    family_name: int = 100
    family_home: int = 255
    trip_label: int = 255
    chat_message: int = 2000
    chat_event_label: int = 200
    chat_sender_name: int = 30
    person_name: int = 150  # User.first_name and last_name
    search_min: int = 2
    search: int = 200
    description_min: int = 10
    description: int = 2000
    username_min: int = 3
    username: int = 30
    email: int = 254
    timezone_name: int = 64
    google_external_id: int = 1024
    place_id: int = 300
    place_session: int = 64
    oauth_state: int = 128
    oauth_code: int = 512


TEXT = TextLengths()

# ---------------------------------------------------------------------------
# Per-object quotas (4.2 with 10.1). Always on, also in tests.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quotas:
    members_per_family: int = 12  # accepted plus pending
    pending_invite_days: int = 14  # pending memberships expire after this
    invite_link_days: int = 7
    families_per_account: int = 1
    # Events people create. Past events never count, there is no lifetime cap, and
    # Google imports count toward neither number.
    events_per_day: int = 40  # per family, on the event's local day
    events_per_30_days: int = 1500  # per family, by creation time
    event_max_days: int = 90
    # Google import, per calendar connection.
    google_events_per_connection: int = 2000
    google_pages_per_sync: int = 8
    google_page_size: int = 250
    checklists_per_family: int = 200
    items_per_checklist: int = 200
    chat_messages_per_family: int = 50_000
    chat_trim_to: int = 45_000  # the nightly trim, only for a family over the cap
    chat_retention_days: int = 365
    chat_messages_per_user_per_day: int = 600
    chat_sockets_per_user: int = 5
    chat_sockets_per_family: int = 30
    calendar_connections_per_account: int = 1


QUOTAS = Quotas()

# Every event starts on or after the first day and ends before the day after the
# last day, in the event's own time zone.
EVENT_FIRST_DAY = date(1900, 1, 1)
EVENT_LAST_DAY = date(2100, 12, 31)

# ---------------------------------------------------------------------------
# Render caps (4.3): a response never grows with the data.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderCaps:
    plans_per_response: int = 100  # then a "+N more" link
    overlap_columns: int = 4
    plans_per_lane: int = 30
    phone_cards_per_page: int = 20
    briefing_plans: int = 30
    checklists_per_page: int = 50
    summary_names: int = 3  # then "and N more"
    summary_chars: int = 160
    display_name_chars: int = 30
    chip_name_chars: int = 24
    list_title_lines: int = 3
    list_title_chars: int = 80
    pill_title_chars: int = 60
    chat_page_size: int = 50


RENDER = RenderCaps()

# ---------------------------------------------------------------------------
# Rate limits (4.4 with 10.1). Time windows obey settings.RATELIMIT_ENABLED.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rate:
    """At most `limit` hits per `window` seconds for each key.

    key is "user", "ip", "user_and_ip" (both must pass) or "recipient" (one email
    address, across every family).
    """

    limit: int
    window: int
    key: str = "user"


MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

RATES = {
    # The default ceiling on every POST, PUT, PATCH and DELETE. Anonymous callers
    # share one campus IP during a class, hence 120 a minute rather than 60.
    "write_minute": Rate(120, MINUTE, "user"),
    "write_hour": Rate(1000, HOUR, "user"),
    "write_anonymous": Rate(120, MINUTE, "ip"),
    # Sign-in and account recovery.
    "login": Rate(150, 10 * MINUTE, "ip"),
    "signup": Rate(50, HOUR, "ip"),
    "password_reset": Rate(30, HOUR, "ip"),
    "password_reset_recipient": Rate(3, HOUR, "recipient"),
    "password_reset_confirm": Rate(10, HOUR, "ip"),
    "two_step_verify": Rate(60, 10 * MINUTE, "ip"),
    # Wrong passwords for one username from any address; at 20 in an hour, password
    # sign-in for that account pauses for 15 minutes (login_account_lock).
    "login_account": Rate(20, HOUR, "user"),
    "login_account_lock": Rate(1, 15 * MINUTE, "user"),
    # Emails a neutral sign-up sends to an address that already has an account.
    "signup_notice": Rate(3, HOUR, "recipient"),
    "two_step_manage": Rate(10, 15 * MINUTE, "user"),
    "profile_password": Rate(5, 15 * MINUTE, "user"),
    "profile_password_ip": Rate(20, HOUR, "ip"),
    "rename": Rate(3, DAY, "user"),
    "verify_email": Rate(3, HOUR, "user"),
    "google_oauth": Rate(10, HOUR, "user_and_ip"),
    # Families and invites.
    "create_workspace": Rate(5, HOUR, "user"),
    "accept_invite": Rate(10, HOUR, "ip"),
    "invite_hour": Rate(10, HOUR, "user"),
    "invite_day": Rate(20, DAY, "user"),
    "invite_new_account": Rate(3, DAY, "user"),  # under 24 hours old or unverified
    "invite_recipient": Rate(1, DAY, "recipient"),
    "remove_member": Rate(20, HOUR, "user"),
    # Plans, checklists and trips.
    "event_write_minute": Rate(30, MINUTE, "user"),
    "event_write_hour": Rate(300, HOUR, "user"),
    "ai_suggestion_minute": Rate(5, MINUTE, "user_and_ip"),
    "ai_suggestion_hour": Rate(20, HOUR, "user_and_ip"),
    "ocr_scan_minute": Rate(5, MINUTE, "user_and_ip"),
    "ocr_scan_hour": Rate(30, HOUR, "user_and_ip"),
    "trip_start": Rate(30, HOUR, "user_and_ip"),
    # Reading the trip card: its answers are cached, this only stops hammering.
    "trip_view": Rate(600, MINUTE, "user"),
    "places": Rate(60, MINUTE, "user"),
    "checklist_create": Rate(60, MINUTE, "user_and_ip"),
    "checklist_item_add": Rate(60, MINUTE, "user_and_ip"),
    "google_sync": Rate(6, HOUR, "user"),
    # Chat. Frames are counted before they are decoded.
    "ws_connect": Rate(30, MINUTE, "user"),
    "chat_burst": Rate(20, 10, "user"),
    "chat_hour": Rate(300, HOUR, "user"),
    "chat_day": Rate(600, DAY, "user"),
    "chat_refused": Rate(50, MINUTE, "user"),  # then the socket closes with 4429
    # Browser CSP violation reports.
    "csp_report": Rate(60, MINUTE, "ip"),
}

# django-axes: wrong passwords for one (username, IP) pair before a lockout.
LOGIN_FAILURES_BEFORE_LOCKOUT = 5
LOGIN_LOCKOUT_MINUTES = 30
# OCR scans that run at once on the server.
OCR_SLOTS = 2
