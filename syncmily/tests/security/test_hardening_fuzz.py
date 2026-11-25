"""
Security lane "fuzz": hostile-input regression suite (ids H-FUZZ-*).

Every test asserts the SECURE behaviour, so a test FAILS while the weakness exists.
Nothing here calls Google, Groq, an MTA or the network: those clients are mocked.

What is driven, as the right role through the Django test client:
  * every route in urls.py (see ENDPOINTS), every form field and every
    request.POST/GET/FILES parameter the views read directly;
  * wrong types, missing and duplicated parameters, hostile path segments, oversized
    values, urlencoded and multipart bodies, extra unknown parameters;
  * the household chat WebSocket through channels.testing.WebsocketCommunicator;
  * the OCR text parser, the AI suggestion validator and the Google import mapper,
    whose input is attacker-controlled text from outside the browser.

Run:
  POSTGRES_DB=syncmily_hard_fuzz <venv>/bin/python manage.py test \
      syncmily.tests.security.test_hardening_fuzz --noinput
Env knobs:  FUZZ_EXAMPLES (Hypothesis max_examples per endpoint, default 60, max 200),
            FUZZ_BLNS (path to blns.json), FUZZ_LEDGER (write a JSON ledger there).
"""

import asyncio
import collections
import json
import os
import re
import traceback
import unicodedata
import uuid
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlencode, urlparse

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.signals import got_request_exception
from django.db import connection, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase as HypTestCase
from hypothesis.extra.django import TransactionTestCase as HypTransactionTestCase

from accounts.models import Membership, MemberColor, Workspace
from accounts.services import make_invite_token
from calendars.models import CalendarConnection
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event, TripStart

User = get_user_model()

MAX_EXAMPLES = max(5, min(200, int(os.environ.get("FUZZ_EXAMPLES", "60"))))
DEFAULT_BLNS = (
    "/private/tmp/claude-501/-Users-simonsavine/2ba82833-7b5c-4ed5-b8a5-1316ad57a63f/"
    "scratchpad/blns-dl/blns.json"
)
LEDGER = collections.defaultdict(list)  # soft findings gathered while fuzzing


# ---------------------------------------------------------------------------
# Hostile material
# ---------------------------------------------------------------------------
def _load_blns():
    path = Path(os.environ.get("FUZZ_BLNS", DEFAULT_BLNS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [s for s in data if isinstance(s, str)]
    except (OSError, ValueError):
        # Small stand-in so the suite still runs where the list is missing.
        return [
            "",
            "null",
            "undefined",
            "<script>alert(1)</script>",
            "' OR 1=1 --",
            "{{7*7}}",
            "../../etc/passwd",
            "𝕿𝖍𝖊 𝖖𝖚𝖎𝖈𝖐",
            "‮abc",
            "ǝ" * 20,
        ]


BLNS = _load_blns()

CANARY = "zzcanary"  # an element named <zzcanary> must never reach a page unescaped
MARKUP = [
    f"<{CANARY}>",
    f'"><{CANARY}>',
    f"'><{CANARY}>",
    f"</script><{CANARY}>",
    f"</textarea><{CANARY}>",
    f"<img src=x onerror=alert(1)><{CANARY}>",
    "<script>alert(1)</script>",
    '" onmouseover="alert(1)" x="',
    "' autofocus onfocus='alert(1)",
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    '<img src="data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+">',
    "![x](https://evil.example/a.png)",
    "{{ 7|add:'4242' }}",
    "{% now 'Y' %}",
    "<style>*{background:url(https://evil.example/x)}</style>",
    "</title><svg/onload=alert(1)>",
    "-->" f"<{CANARY}>",
    "<!--",
    "]]>",
    "&lt;b&gt;",
    "${7*7}",
    "\\u0022onerror=\\u0022",
    "%00%0d%0aSet-Cookie:x=1",
]

BIDI_CHARS = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
ZW_CHARS = ["​", "‌", "‍", "﻿", "⁠"]
CTRL_CHARS = [chr(c) for c in (*range(0, 32), 0x7F, 0x85, 0x2028, 0x2029)]
COMBINING = [chr(c) for c in range(0x0300, 0x0370)]

# Named attacks used by the per-field stored-value tests (code, value).
ATTACKS = {
    "nul": "ab\x00cd",
    "control": "ab\x01\x07\x1b[31mcd\x7fef\x85gh",
    "bidi-override": "abc‮def‬",
    "bidi-isolate": "a⁦b⁩c",
    "zero-width-space": "a​b",
    "zero-width-nonjoiner": "a‌b",
    "bom": "﻿ab",
    "word-joiner": "a⁠b",
    "zwj-in-word": "pa‍ss",
    "zalgo-100": "".join(ch + "́" * 100 for ch in "abcde"),
    "zalgo-one-letter-140": "a" + "\u0301" * 140,
    "line-separator": "a b c",
    "newline": "ab\ncd\r\nef",
    "tab": "ab\tcd",
    "long-10000": "x" * 10000,
    "long-unicode-10000": "é" * 10000,
}
EMOJI_OK = "Soccer \U0001f468‍\U0001f469‍\U0001f467 \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f ok"

_BIDI = set(map(ord, BIDI_CHARS)) | {0x200E, 0x200F, 0x061C}
_ZW = {0x200B, 0x200C, 0x2060, 0xFEFF}
# Generic combining-diacritic blocks (the DB CHECK ranges). Spec 10.1 caps these at 3
# per letter and other scripts' marks at 8 per letter.
_GENERIC_MARK_RANGES = (
    (0x0300, 0x036F),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x20D0, 0x20FF),
    (0xFE20, 0xFE2F),
)


def violations(value, multiline=False):
    """Classes of invisible or abusive characters still present in a stored string."""
    found = set()
    run = generic = 0
    previous = ""
    for i, ch in enumerate(value):
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cp == 0:
            found.add("nul")
        elif cat == "Cc" and not (multiline and ch in "\n\t\r"):
            found.add("control")
        elif cp in _BIDI:
            found.add("bidi")
        elif cp in _ZW:
            found.add("zero-width")
        elif cp == 0x200D:
            nxt = value[i + 1] if i + 1 < len(value) else ""
            if (previous.isascii() and previous.isalnum()) or (
                nxt.isascii() and nxt.isalnum()
            ):
                found.add("zwj-in-word")
        elif cp in (0x2028, 0x2029):
            found.add("line-separator")
        if cat in ("Mn", "Me"):
            run += 1
            generic += any(lo <= cp <= hi for lo, hi in _GENERIC_MARK_RANGES)
            if generic > 4 or run > 8:
                found.add("zalgo")
        else:
            run = generic = 0
        previous = ch
    return found


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
def _zalgo(draw):
    letters = draw(
        st.text(
            alphabet=st.characters(categories=["L"], max_codepoint=0x24F),
            min_size=1,
            max_size=6,
        )
    )
    return "".join(
        ch
        + "".join(
            draw(st.lists(st.sampled_from(COMBINING), min_size=100, max_size=100))
        )
        for ch in letters
    )


zalgo = st.composite(_zalgo)()
ALL_UNICODE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), max_size=300
)  # full Unicode, control chars and NUL included; surrogates cannot be sent over HTTP
SPECIAL = st.text(
    alphabet=st.sampled_from(
        [
            *BIDI_CHARS,
            *ZW_CHARS,
            *CTRL_CHARS,
            "a",
            "b",
            " ",
            "<",
            ">",
            '"',
            "'",
            "&",
            "\\",
            "/",
            "%",
            "\U0001f600",
            "é",
        ]
    ),
    max_size=40,
)
HUGE = st.integers(1000, 10000).flatmap(
    lambda n: st.sampled_from(["x", "é", "\U0001f600", "9", "<"]).map(lambda c: c * n)
)
hostile_text = st.one_of(
    st.sampled_from(BLNS),
    st.sampled_from(MARKUP),
    ALL_UNICODE,
    SPECIAL,
    zalgo,
    HUGE,
    st.tuples(st.sampled_from(MARKUP), SPECIAL).map("".join),
)

WRONG = {
    "int": [
        "abc",
        "1.5",
        "-1",
        "0",
        "9" * 30,
        "9" * 5000,
        "1e3",
        "٣",
        "NaN",
        " 1 ",
        "1\x00",
        "0x10",
        "+1",
        "1_000",
        "",
        "null",
        "[]",
    ],
    "uuid": [
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        "{" + str(uuid.uuid4()) + "}",
        str(uuid.uuid4()).upper(),
        "x" * 10000,
        str(uuid.uuid4())[:-1],
        "",
    ],
    "date": [
        "2026-02-30",
        "0000-00-00",
        "9999-12-31",
        "10000-01-01",
        "-0001-01-01",
        "0001-01-01",
        "2026-W01-1",
        "२०२६-११-१८",
        "18/11/2026",
        "2026-11-18T10:00",
        "tomorrow",
        "1e9",
        "",
    ],
    "time": [
        "25:61",
        "24:00",
        "12:00:60",
        "1e",
        "٠٢:٣٠",
        "10:00:00.999999999",
        "-1:00",
        "noon",
        "",
    ],
    "float": [
        "nan",
        "inf",
        "-inf",
        "1e999",
        "-1e999",
        "1e-999",
        "0x10",
        "１２",
        "1_0",
        "91",
        "-181",
        "181",
        "40,7",
        "",
    ],
    "tz": [
        "Not/AZone",
        "UTC\x00",
        "../../etc/passwd",
        "a" * 10000,
        "US/Pacific-New",
        "utc",
        "",
        "America/New_York\n",
        "Etc/GMT+99",
        "posixrules",
        "localtime",
    ],
    "next": [
        "//evil.example",
        "javascript:alert(1)",
        "\\\\evil.example",
        "/\\evil.example",
        "https://evil.example/",
        "///evil.example",
        "/%09/evil.example",
        "\t//evil.example",
        "data:text/html,x",
        "http:evil.example",
        "//evil.example\\@good",
        " //evil.example",
    ],
    "choice": [],
    "bool": ["on", "off", "true", "2", "-1", "yes", "[]", "1\x00"],
    "text": [],
    "email": [
        "a@b",
        "a@b.c" * 200,
        "<a@b.co>",
        "a b@c.de",
        "a@b.de\nBcc: x@y.z",
        "@",
        "a" * 400 + "@b.co",
        "ａ@b.co",
        "a@ｂ.co",
        "a@xn--bcher-kva.de",
        "a@[127.0.0.1]",
    ],
    "token": ["x" * 10000, "a:b:c", "..", "%00", "ab‮cd"],
    "hex": [
        "#c62828\n",
        "#gggggg",
        "#12345",
        "#1234567",
        "red",
        "url(x)",
        "#c62828;x:y",
    ],
}
MISSING = object()


def param_strategy(kind, valid, choices=None):
    wrong = list(WRONG.get(kind, []))
    if choices:
        wrong += [c + "x" for c in choices] + [c.upper() for c in choices]
    parts = [
        st.just(valid),
        st.just(valid),
        hostile_text,
        st.just(MISSING),
        st.lists(hostile_text, min_size=2, max_size=3),  # duplicated parameter
        st.tuples(st.just(valid), hostile_text).map(
            list
        ),  # valid first, hostile second
        st.tuples(hostile_text, st.just(valid)).map(list),
    ]
    if wrong:
        parts += [st.sampled_from(wrong), st.sampled_from(wrong)]
    return st.one_of(*parts)


PATH_HOSTILE = st.one_of(
    st.sampled_from(
        [
            "0",
            "-1",
            "9" * 30,
            "9" * 4301,
            "1e3",
            "abc",
            "%00",
            "..",
            "%2e%2e",
            "99999999999999999999",
            "9223372036854775808",
            "2147483648",
            "٣",
            str(uuid.uuid4()),
            "00000000-0000-0000-0000-000000000000",
            "x" * 3000,
            "%C0%AF",
            "%ff%fe",
            "a%2Fb",
            "a%5Cb",
        ]
    ),
    hostile_text.map(lambda s: quote(s, safe="")),
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
class World(HypTestCase):
    """A household, a lone user with no family, and a pending invite."""

    @classmethod
    def setUpTestData(cls):
        cls.parent = User.objects.create_user("fz_parent", "fz_p@example.com", None)
        cls.parent2 = User.objects.create_user("fz_parent2", "fz_p2@example.com", None)
        cls.kid = User.objects.create_user("fz_kid", "fz_k@example.com", None)
        cls.loner = User.objects.create_user("fz_loner", "fz_l@example.com", None)
        cls.outsider = User.objects.create_user("fz_outsider", "fz_o@example.com", None)
        cls.ws = Workspace.objects.create(
            name="Fuzz Family",
            primary_admin=cls.parent,
            home_location="1 Main St",
            home_latitude=40.7,
            home_longitude=-74.0,
        )
        now = timezone.now()
        for user, role in ((cls.parent2, "parent"), (cls.kid, "kid")):
            Membership.objects.create(
                workspace=cls.ws,
                user=user,
                role=role,
                invited_by=cls.parent,
                accepted_at=now,
            )
        pending_user = User.objects.create_user(
            "fz_pending", "fz_pending@example.com", None, is_active=False
        )
        cls.pending = Membership.objects.create(
            workspace=cls.ws,
            user=pending_user,
            role="kid",
            invited_by=cls.parent,
            invited_at=now,
        )
        start = now.replace(microsecond=0) + timedelta(hours=26)
        cls.event = Event.objects.create(
            created_by=cls.parent,
            workspace=cls.ws,
            title="Soccer",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York",
            location="Park",
            latitude=40.7,
            longitude=-74.0,
        )
        cls.event.participants.add(cls.kid, cls.parent2)
        cls.conn = CalendarConnection.objects.create(
            user=cls.parent,
            workspace=cls.ws,
            provider="google",
            access_token="t",
            refresh_token="r",
        )
        cls.google_event = Event.objects.create(
            created_by=cls.parent,
            workspace=cls.ws,
            title="Imported",
            source="google",
            calendar_connection=cls.conn,
            starts_at=start,
            ends_at=start + timedelta(hours=2),
            external_id="g1",
            timezone_name="America/New_York",
        )
        cls.checklist = Checklist.objects.create(
            workspace=cls.ws, created_by=cls.parent, title="Bag", event=cls.event
        )
        cls.item = ChecklistItem.objects.create(
            checklist=cls.checklist, description="Water"
        )
        ChatMessage.objects.create(workspace=cls.ws, sender=cls.parent, content="hi")

    # -- helpers ---------------------------------------------------------
    def as_user(self, role):
        user = {
            "parent": self.parent,
            "parent2": self.parent2,
            "kid": self.kid,
            "loner": self.loner,
            "outsider": self.outsider,
        }.get(role)
        self.client.logout()
        if user is not None:
            self.client.force_login(
                user, backend="django.contrib.auth.backends.ModelBackend"
            )
        return user

    def ctx(self):
        return {
            "ws": str(self.ws.pk),
            "event": self.event.pk,
            "google_event": self.google_event.pk,
            "checklist": str(self.checklist.pk),
            "item": self.item.pk,
            "pending": self.pending.pk,
            "ws_hex": self.ws.pk.hex,
            "invite": make_invite_token(self.pending),
        }


# ---------------------------------------------------------------------------
# Endpoint table: every route in urls.py
# ---------------------------------------------------------------------------
def P(kind, valid, choices=None):
    return (kind, valid, choices)


TZ = "America/New_York"
EVENT_FORM = lambda c: {  # noqa: E731
    "action": P("choice", "preview", ["save", "preview", "generate_suggestion"]),
    "title": P("text", "Soccer practice"),
    "date": P("date", "2031-05-04"),
    "start_time": P("time", "10:00"),
    "end_date": P("date", ""),
    "end_time": P("time", "11:00"),
    "timezone": P("tz", TZ),
    "location": P("text", "Central Park"),
    "latitude": P("float", "40.78"),
    "longitude": P("float", "-73.96"),
    "transport_mode": P(
        "choice", "driving", ["driving", "transit", "walking", "cycling"]
    ),
    "allow_overlap": P("bool", "on"),
    "is_tentative": P("bool", ""),
    "participants": P("int", ""),
    "requires_transport": P("bool", ""),
    "dropoff_driver": P("int", ""),
    "pickup_driver": P("int", ""),
    "submission_token": P("token", ""),
    "description": P("text", "Take Emma to the dentist next Friday at 3 PM"),
}
GET_COMMON = {
    "date": P("date", "2031-05-04"),
    "week": P("date", "2031-05-04"),
    "timezone": P("tz", TZ),
    "view": P("choice", "day", ["day", "week", "month", "upcoming"]),
    "time": P("time", "10:00"),
    "page": P("int", "1"),
    "workspace": P("uuid", ""),
}


class Spec(dict):
    pass


def S(name, url, args, method, roles, params, json_=False):
    return Spec(
        name=name,
        url=url,
        args=args,
        method=method,
        roles=roles,
        params=params,
        json=json_,
    )


ANY = ["parent", "parent2", "kid", "loner", "anon"]
PARENTS = ["parent", "parent2", "kid", "outsider"]
ENDPOINTS = [
    # -- accounts --------------------------------------------------------
    S(
        "accounts_create_workspace",
        "accounts:create_workspace",
        lambda c: [],
        "POST",
        ["loner", "parent"],
        {"name": P("text", "The Reyes family")},
    ),
    S(
        "accounts_workspace_detail",
        "accounts:workspace_detail",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "accounts_manage_members_get",
        "accounts:manage_members",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "accounts_manage_members_post",
        "accounts:manage_members",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {
            "email": P("email", "new@example.com"),
            "role": P("choice", "kid", ["kid", "parent", "caregiver"]),
        },
    ),
    S(
        "accounts_resend_invite",
        "accounts:resend_invite",
        lambda c: [c["ws"], c["pending"]],
        "POST",
        PARENTS,
        {},
    ),
    S(
        "accounts_remove_member",
        "accounts:remove_member",
        lambda c: [c["ws"], c["pending"]],
        "POST",
        PARENTS,
        {},
    ),
    S("accounts_dashboard", "accounts:dashboard", lambda c: [], "GET", ANY, GET_COMMON),
    S("accounts_day_seen", "accounts:day_seen", lambda c: [], "POST", ANY, {}),
    S("accounts_profile_get", "accounts:profile", lambda c: [], "GET", ANY, {}),
    S(
        "accounts_profile_post",
        "accounts:profile",
        lambda c: [],
        "POST",
        ANY,
        {
            "action": P("choice", "username", ["username", "password"]),
            "username": P("text", "fz_newname"),
            "old_password": P("text", "x"),
            "new_password1": P("text", "Zx9-hardpass-pass"),
            "new_password2": P("text", "Zx9-hardpass-pass"),
        },
    ),
    S(
        "accounts_accept_invite_get",
        "accounts:accept_invite",
        lambda c: [c["invite"]],
        "GET",
        ["anon", "parent"],
        {},
    ),
    S(
        "accounts_accept_invite_post",
        "accounts:accept_invite",
        lambda c: [c["invite"]],
        "POST",
        ["anon"],
        {
            "new_password1": P("text", "Zx9-hardpass-pass"),
            "new_password2": P("text", "Zx9-hardpass-pass"),
        },
    ),
    S(
        "accounts_signup",
        "accounts:signup",
        lambda c: [],
        "POST",
        ["anon"],
        {
            "username": P("text", "fz_new_user"),
            "email": P("email", "fznew@example.com"),
            "password1": P("text", "Zx9-hardpass-pass"),
            "password2": P("text", "Zx9-hardpass-pass"),
        },
    ),
    S(
        "accounts_google_login",
        "accounts:google_login",
        lambda c: [],
        "POST",
        ["anon"],
        {},
    ),
    S(
        "accounts_google_connect",
        "accounts:google_connect",
        lambda c: [],
        "POST",
        ANY,
        {},
    ),
    S(
        "accounts_google_disconnect",
        "accounts:google_disconnect",
        lambda c: [],
        "POST",
        ANY,
        {},
    ),
    S(
        "accounts_google_callback",
        "accounts:google_login_callback",
        lambda c: [],
        "GET",
        ["anon", "parent"],
        {"state": P("token", "s"), "code": P("text", "c"), "error": P("text", "")},
    ),
    S(
        "accounts_set_palette",
        "accounts:set_palette",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {
            "palette": P("choice", "market", ["market", "garden", "crayon"]),
            "next": P("next", "/"),
        },
    ),
    S(
        "accounts_set_my_palette",
        "accounts:set_my_palette",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {
            "palette": P("choice", "market", ["market", "garden", "crayon"]),
            "next": P("next", "/"),
        },
    ),
    S(
        "accounts_family_home_get",
        "accounts:family_home",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        {},
    ),
    S(
        "accounts_family_home_post",
        "accounts:family_home",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {
            "action": P("choice", "save", ["save", "clear"]),
            "location": P("text", "10 Downing St"),
            "latitude": P("float", "40.7"),
            "longitude": P("float", "-74.0"),
        },
    ),
    S(
        "accounts_login",
        "accounts:login",
        lambda c: [],
        "POST",
        ["anon"],
        {
            "username": P("text", "fz_parent"),
            "password": P("text", "wrong"),
            "next": P("next", "/"),
        },
    ),
    S(
        "accounts_two_step_verify",
        "accounts:two_step_verify",
        lambda c: [],
        "POST",
        ANY,
        {"code": P("text", "123456")},
    ),
    S(
        "accounts_two_step_setup_get",
        "accounts:two_step_setup",
        lambda c: [],
        "GET",
        ANY,
        {},
    ),
    S(
        "accounts_two_step_setup_post",
        "accounts:two_step_setup",
        lambda c: [],
        "POST",
        ANY,
        {"code": P("text", "123456")},
    ),
    S(
        "accounts_two_step_disable",
        "accounts:two_step_disable",
        lambda c: [],
        "POST",
        ANY,
        {"code": P("text", "123456"), "password": P("text", "x")},
    ),
    S(
        "accounts_two_step_backup",
        "accounts:two_step_backup_codes",
        lambda c: [],
        "POST",
        ANY,
        {"code": P("text", "123456")},
    ),
    S(
        "accounts_password_reset",
        "accounts:password_reset",
        lambda c: [],
        "POST",
        ["anon"],
        {"email": P("email", "fz_p@example.com")},
    ),
    S(
        "accounts_password_reset_confirm",
        "accounts:password_reset_confirm",
        lambda c: ["MQ", "set-password"],
        "POST",
        ["anon"],
        {"new_password1": P("text", "x"), "new_password2": P("text", "x")},
    ),
    S("accounts_logout", "accounts:logout", lambda c: [], "POST", ANY, {}),
    # -- events ----------------------------------------------------------
    S("events_list", "events:list", lambda c: [], "GET", ANY, GET_COMMON),
    S(
        "events_choose_workspace",
        "events:choose_workspace",
        lambda c: [],
        "GET",
        ANY,
        {
            "workspace": P("uuid", ""),
            "date": P("date", ""),
            "time": P("time", ""),
            "timezone": P("tz", TZ),
        },
    ),
    S(
        "events_workspace_list",
        "events:workspace_list",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "events_create_get",
        "events:workspace_create",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "events_create_post",
        "events:workspace_create",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        EVENT_FORM(None),
    ),
    S(
        "events_create_anon",
        "events:create",
        lambda c: [],
        "POST",
        ANY,
        EVENT_FORM(None),
    ),
    S(
        "events_scan_ocr",
        "events:scan_ocr",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {"timezone": P("tz", TZ), "document": P("file", "ok"), "image": P("file", "")},
        True,
    ),
    S(
        "events_member_colors",
        "events:member_colors",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {
            "color-1": P("hex", "#c62828"),
            "color-2": P("hex", "#c62828"),
            "color-99999": P("hex", "#c62828"),
            "next": P("next", "/"),
        },
    ),
    S("events_timezones", "events:timezones", lambda c: [], "GET", ANY, {}, True),
    S(
        "events_place_suggestions",
        "events:place_suggestions",
        lambda c: [],
        "POST",
        ANY,
        {"q": P("text", "central park"), "session": P("token", "abc-123")},
        True,
    ),
    S(
        "events_place_details",
        "events:place_details",
        lambda c: [],
        "POST",
        ANY,
        {"id": P("token", "ChIJabcdefghij1234"), "session": P("token", "abc-123")},
        True,
    ),
    S(
        "events_detail_get",
        "events:detail",
        lambda c: [c["event"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "events_detail_post",
        "events:detail",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        {
            "requires_transport": P("bool", "on"),
            "dropoff_driver": P("int", "1"),
            "pickup_driver": P("int", "1"),
            "timezone": P("tz", TZ),
        },
    ),
    S(
        "events_edit_get",
        "events:edit",
        lambda c: [c["event"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "events_edit_post",
        "events:edit",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        EVENT_FORM(None),
    ),
    S(
        "events_delete",
        "events:delete",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        {"action": P("choice", "keep", ["delete", "keep"]), "timezone": P("tz", TZ)},
    ),
    S(
        "events_transport",
        "events:set_transport_mode",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        {
            "transport_mode": P(
                "choice", "walking", ["driving", "transit", "walking", "cycling"]
            ),
            "next": P("next", "/"),
        },
    ),
    S(
        "events_weekends",
        "events:weekends",
        lambda c: [],
        "POST",
        ANY,
        {"next": P("next", "/")},
    ),
    S(
        "events_trip_get",
        "events:trip",
        lambda c: [c["event"]],
        "GET",
        PARENTS,
        {"format": P("choice", "json", ["json", "html"])},
        False,
    ),
    S(
        "events_trip_post",
        "events:trip",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        {
            "mode": P("choice", "here", ["here", "home", "previous", "other"]),
            "latitude": P("float", "40.7"),
            "longitude": P("float", "-74.0"),
            "label": P("text", "Here"),
            "format": P("choice", "json", ["json"]),
        },
    ),
    S(
        "events_change_time",
        "events:change_time",
        lambda c: [c["event"]],
        "POST",
        PARENTS,
        {
            "starts_date": P("date", "2031-05-04"),
            "starts_time": P("time", "10:00"),
            "ends_date": P("date", "2031-05-04"),
            "ends_time": P("time", "11:00"),
        },
    ),
    # -- calendars -------------------------------------------------------
    S(
        "calendars_connect",
        "calendars:google_connect",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {"next": P("next", "/")},
    ),
    S(
        "calendars_callback",
        "calendars:google_callback",
        lambda c: [],
        "GET",
        ANY,
        {
            "state": P("token", "s"),
            "code": P("text", "c"),
            "error": P("choice", "", ["access_denied"]),
        },
    ),
    S("calendars_sync", "calendars:google_sync", lambda c: [], "POST", PARENTS, {}),
    S(
        "calendars_auto_sync",
        "calendars:google_auto_sync",
        lambda c: [],
        "POST",
        PARENTS,
        {"auto_sync": P("choice", "on", ["on", "off"])},
    ),
    S(
        "calendars_disconnect",
        "calendars:google_disconnect",
        lambda c: [],
        "POST",
        PARENTS,
        {},
    ),
    # -- chat ------------------------------------------------------------
    S("chat_home", "chat:home", lambda c: [], "GET", ANY, {}),
    S("chat_workspace", "chat:workspace_chat", lambda c: [c["ws"]], "GET", PARENTS, {}),
    S(
        "chat_messages",
        "chat:messages",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        {"before": P("int", "10"), "after": P("int", "1")},
        True,
    ),
    # -- checklists ------------------------------------------------------
    S(
        "checklists_list",
        "checklists:list",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        GET_COMMON,
    ),
    S(
        "checklists_create_get",
        "checklists:create",
        lambda c: [c["ws"]],
        "GET",
        PARENTS,
        {},
    ),
    S(
        "checklists_create_post",
        "checklists:create",
        lambda c: [c["ws"]],
        "POST",
        PARENTS,
        {"title": P("text", "Bag"), "event": P("int", "1")},
    ),
    S(
        "checklists_detail",
        "checklists:detail",
        lambda c: [c["checklist"]],
        "GET",
        PARENTS,
        {},
    ),
    S(
        "checklists_edit",
        "checklists:edit",
        lambda c: [c["checklist"]],
        "POST",
        PARENTS,
        {"title": P("text", "Bag"), "event": P("int", "1")},
    ),
    S(
        "checklists_delete",
        "checklists:delete",
        lambda c: [c["checklist"]],
        "POST",
        PARENTS,
        {"confirm": P("text", "yes")},
    ),
    S(
        "checklists_item_add",
        "checklists:item_add",
        lambda c: [c["checklist"]],
        "POST",
        PARENTS,
        {"description": P("text", "Water bottle")},
    ),
    S(
        "checklists_item_edit",
        "checklists:item_edit",
        lambda c: [c["item"]],
        "POST",
        PARENTS,
        {"description": P("text", "Water bottle")},
    ),
    S(
        "checklists_item_delete",
        "checklists:item_delete",
        lambda c: [c["item"]],
        "POST",
        PARENTS,
        {},
    ),
    S(
        "checklists_item_toggle",
        "checklists:item_toggle",
        lambda c: [c["item"]],
        "POST",
        PARENTS,
        {"completed": P("choice", "1", ["0", "1"])},
    ),
]


# ---------------------------------------------------------------------------
# Response checks
# ---------------------------------------------------------------------------
LEAK = re.compile(
    rf"<{CANARY}|<script>alert\(|<img src=x onerror|onmouseover=\"alert|<svg/onload|"
    r"</title><svg",
    re.I,
)


def location_is_safe(resp, host="testserver"):
    loc = resp.headers.get("Location", "")
    if not loc:
        return True
    if loc.startswith("https://accounts.google.com/o/oauth2/"):
        return (
            True  # the sign-in hand-off to Google, built by the server, not from input
        )
    parsed = urlparse(loc)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    if loc.startswith(("//", "\\")) or "\\" in loc.split("?")[0]:
        return False
    return not parsed.netloc or parsed.netloc == host


class Trapped:
    """Collects exceptions Django turned into a 500 page, as a short frame summary."""

    def __init__(self):
        self.errors = []

    def __call__(self, sender, request=None, **kwargs):
        import sys

        exc = sys.exc_info()[1]
        frames = [
            f"{Path(f.filename).name}:{f.lineno} {f.name}"
            for f in traceback.extract_tb(exc.__traceback__)
            if "site-packages/django/db" not in f.filename
        ][-6:]
        self.errors.append(
            f"{type(exc).__name__}: {str(exc)[:160]} @ " + " > ".join(frames)
        )


def describe(spec, role, method, path, data, urlenc):
    shown = {
        k: (
            v if not isinstance(v, str) or len(v) < 80 else f"{v[:60]}…<{len(v)} chars>"
        )
        for k, v in data.items()
    }
    return f"{spec['name']} role={role} {method} {path[:120]} urlencoded={urlenc} data={shown!r}"


def build_files(value):
    if value is MISSING:
        return None
    if isinstance(value, list):
        value = value[0]
    return value


def make_file(draw_value):
    """draw_value: a hostile text; turn it into an upload with hostile name/type/bytes."""
    name = draw_value[:80] or "a.png"
    body = draw_value.encode("utf-8", "ignore")
    try:
        return SimpleUploadedFile(
            name, body, content_type=draw_value[:60] or "image/png"
        )
    except Exception:
        # Django rejects some names (such as "/") before the app sees them.
        return SimpleUploadedFile("a.png", body, content_type="image/png")


@st.composite
def requests_for(draw, spec):
    role = draw(st.sampled_from(spec["roles"]))
    data = {}
    for name, (kind, valid, choices) in spec["params"].items():
        value = draw(param_strategy(kind if kind != "file" else "text", valid, choices))
        if value is MISSING:
            continue
        data[name] = value
    for _ in range(
        draw(st.integers(0, 2))
    ):  # extra unknown parameters, hostile names too
        data[draw(hostile_text.map(lambda s: s[:300]))] = draw(hostile_text)
    path_swap = draw(st.one_of(st.none(), st.tuples(st.integers(0, 3), PATH_HOSTILE)))
    urlenc = draw(st.booleans())
    return role, data, path_swap, urlenc


# ---------------------------------------------------------------------------
# 1. Endpoint fuzz (generated test per route)
# ---------------------------------------------------------------------------
class EndpointFuzz(World):
    maxDiff = None

    def run_case(self, spec, case):
        role, data, path_swap, urlenc = case
        c = self.ctx()
        args = list(spec["args"](c))
        path = reverse(spec["url"], args=args)
        if path_swap is not None and args:
            idx, hostile = path_swap
            idx %= len(args)
            path = path.replace(str(args[idx]), hostile, 1)
        self.as_user(role)
        self.client.raise_request_exception = False
        trap = Trapped()
        got_request_exception.connect(trap)
        files = {}
        try:
            body = dict(data)
            if "document" in spec["params"] and spec["json"]:
                for key in ("document", "image"):
                    if key in body:
                        files[key] = make_file(
                            body.pop(key) if isinstance(body[key], str) else "x"
                        )
            with mock.patch(
                "events.views.places.enabled", return_value=True
            ), mock.patch("events.views.places.suggest", return_value=[]), mock.patch(
                "events.views.places.details", return_value=None
            ), mock.patch(
                "events.views.generate_event_suggestion", side_effect=_fake_suggestion
            ), mock.patch(
                "events.services.event_ocr.extract_text_from_image",
                return_value="Soccer May 4 2031 10:00 AM",
            ), mock.patch(
                "calendars.views.google.revoke", return_value=True
            ), mock.patch(
                "events.views.find_travel_checks", return_value=[]
            ), mock.patch(
                "accounts.views.google.verify_login", side_effect=Exception("x")
            ):
                if spec["method"] == "GET":
                    resp = self.client.get(path, body)
                elif files:
                    resp = self.client.post(path, {**body, **files})
                elif urlenc:
                    resp = self.client.post(
                        path,
                        urlencode(body, doseq=True),
                        content_type="application/x-www-form-urlencoded",
                    )
                else:
                    resp = self.client.post(path, body)
        finally:
            got_request_exception.disconnect(trap)
        label = describe(spec, role, spec["method"], path, data, urlenc)
        if (
            resp.status_code >= 500
            and (spec["name"], trap.errors[0].split(":")[0] if trap.errors else "")
            in COVERED_ELSEWHERE
        ):
            # Reported by a dedicated test (RequestShapes); keep hunting for other 5xx here.
            LEDGER["covered-5xx"].append((spec["name"], trap.errors[0][:120]))
            return
        self.assertLess(
            resp.status_code,
            500,
            f"{label}\nstatus={resp.status_code}\n{trap.errors[:1]}",
        )
        self.assertFalse(trap.errors, f"{label}\n{trap.errors[:1]}")
        self.assertTrue(
            location_is_safe(resp),
            f"unsafe redirect {resp.headers.get('Location')!r}\n{label}",
        )
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code in (403, 404, 405) or 300 <= resp.status_code < 400:
            if spec["json"] and not ctype.startswith("application/json"):
                LEDGER["json-endpoint-html-" + str(resp.status_code)].append(
                    spec["name"]
                )
        elif (
            spec["json"]
            and resp.status_code == 400
            and not ctype.startswith("application/json")
        ):
            LEDGER["json-endpoint-html-400"].append(
                spec["name"]
            )  # RequestShapes asserts it
        elif spec["json"]:
            self.assertTrue(
                ctype.startswith("application/json"),
                f"JSON endpoint answered {resp.status_code} {ctype!r}\n{label}",
            )
            json.loads(resp.content)
        if "text/html" in ctype and resp.status_code != 404:
            text = resp.content.decode("utf-8", "replace")
            self.assertIsNone(LEAK.search(text), f"unescaped markup reflected\n{label}")


COVERED_ELSEWHERE = {("accounts_login", "DataError")}


def _fake_suggestion(description, *, timezone_name, **kw):
    return {
        "title": "<zzcanary>‮",
        "date": None,
        "start_time": None,
        "end_date": None,
        "end_time": None,
        "location": '"><zzcanary>',
        "missing_fields": ["date"],
    }


def _install(spec):
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=list(HealthCheck),
        phases=[Phase.explicit, Phase.generate, Phase.shrink],
    )
    @given(case=requests_for(spec))
    def test(self, case):
        self.run_case(spec, case)

    test.__name__ = f"test_fuzz_{spec['name']}"
    setattr(EndpointFuzz, test.__name__, test)


for _spec in ENDPOINTS:
    _install(_spec)


# ---------------------------------------------------------------------------
# 2. Body-size and parameter-count limits
# ---------------------------------------------------------------------------
class BodyLimits(World):
    def setUp(self):
        self.as_user("parent")
        self.client.raise_request_exception = False

    def test_oversized_urlencoded_body_is_refused(self):
        """A 6 MB form body must be refused by Django (DATA_UPLOAD_MAX_MEMORY_SIZE)."""
        url = reverse("checklists:item_add", args=[str(self.checklist.pk)])
        resp = self.client.post(
            url,
            "description=" + "a" * (6 * 1024 * 1024),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertIn(resp.status_code, (400, 413), resp.status_code)

    def test_too_many_form_fields_is_refused(self):
        url = reverse("checklists:item_add", args=[str(self.checklist.pk)])
        resp = self.client.post(url, {f"k{i}": "v" for i in range(5000)})
        self.assertIn(resp.status_code, (400, 413), resp.status_code)

    def test_large_upload_is_rejected_by_django_before_view_reads_it(self):
        """Django has no total upload ceiling: a 40 MB file reaches the OCR view."""
        big = SimpleUploadedFile(
            "a.png", b"\x89PNG" + b"0" * (40 * 1024 * 1024), content_type="image/png"
        )
        url = reverse("events:scan_ocr", args=[str(self.ws.pk)])
        with mock.patch("events.views.scan_event_image") as scan:
            scan.side_effect = lambda f, **kw: (_ for _ in ()).throw(
                __import__("events.services.event_ocr", fromlist=["x"]).EventOCRError(
                    "too_big", "big", 400
                )
            )
            resp = self.client.post(url, {"document": big})
        self.assertIn(resp.status_code, (400, 413))
        self.assertFalse(
            scan.called,
            "a 40 MB upload reached the OCR view: only nginx (client_max_body_size "
            "12m) caps file uploads; Django has no ceiling of its own",
        )


# ---------------------------------------------------------------------------
# 3. Stored values: nothing invisible or abusive survives, per field
# ---------------------------------------------------------------------------
def event_token(client, ws):
    resp = client.get(reverse("events:workspace_create", args=[ws.pk]))
    m = re.search(
        r'name="submission_token"[^>]*value="([^"]+)"', resp.content.decode()
    ) or re.search(
        r'value="([^"]+)"[^>]*name="submission_token"', resp.content.decode()
    )
    return m.group(1) if m else ""


class StoredValues(World):
    """For each text field a user can write, push every attack and read the DB back."""

    CORPUS = {
        **ATTACKS,
        **{f"blns-{i}": s for i, s in enumerate(BLNS) if s},
        **{f"markup-{i}": s for i, s in enumerate(MARKUP)},
    }

    def _event_post(self, url, title="Soccer", location="", token=True, **extra):
        body = {
            "action": "save",
            "title": title,
            "date": "2031-05-04",
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": TZ,
            "location": location,
            "latitude": "40.7" if location else "",
            "longitude": "-74.0" if location else "",
            "transport_mode": "driving",
            "participants": [self.kid.pk],
            "allow_overlap": "on",
            **extra,
        }
        if token:
            body["submission_token"] = event_token(self.client, self.ws)
        return self.client.post(url, body)

    # each reader returns the stored strings after submitting `value`, or None if refused
    def f_event_title(self, v):
        before = set(Event.objects.values_list("pk", flat=True))
        r = self._event_post(
            reverse("events:workspace_create", args=[self.ws.pk]), title=v
        )
        e = Event.objects.exclude(pk__in=before).first()
        return r, [(e.title, False)] if e else None

    def f_event_location(self, v):
        before = set(Event.objects.values_list("pk", flat=True))
        r = self._event_post(
            reverse("events:workspace_create", args=[self.ws.pk]), location=v
        )
        e = Event.objects.exclude(pk__in=before).first()
        return r, [(e.location, False)] if e else None

    def f_event_edit_title(self, v):
        r = self.client.post(
            reverse("events:edit", args=[self.event.pk]),
            {
                "action": "save",
                "title": v,
                "date": "2031-05-04",
                "start_time": "10:00",
                "end_time": "11:00",
                "timezone": TZ,
                "location": "",
                "transport_mode": "driving",
            },
        )
        self.event.refresh_from_db()
        return r, [(self.event.title, False)] if self.event.title != "Soccer" else None

    def f_checklist_title(self, v):
        before = set(Checklist.objects.values_list("pk", flat=True))
        r = self.client.post(
            reverse("checklists:create", args=[self.ws.pk]), {"title": v}
        )
        new = Checklist.objects.exclude(pk__in=before).first()
        return r, [(new.title, False)] if new else None

    def f_checklist_item(self, v):
        before = set(ChecklistItem.objects.values_list("pk", flat=True))
        r = self.client.post(
            reverse("checklists:item_add", args=[self.checklist.pk]), {"description": v}
        )
        new = ChecklistItem.objects.exclude(pk__in=before).first()
        return r, [(new.description, False)] if new else None

    def f_checklist_item_edit(self, v):
        r = self.client.post(
            reverse("checklists:item_edit", args=[self.item.pk]), {"description": v}
        )
        self.item.refresh_from_db()
        return r, (
            [(self.item.description, False)]
            if self.item.description != "Water"
            else None
        )

    def f_family_home(self, v):
        r = self.client.post(
            reverse("accounts:family_home", args=[self.ws.pk]),
            {"location": v, "latitude": "40.7", "longitude": "-74.0"},
        )
        self.ws.refresh_from_db()
        return r, (
            [(self.ws.home_location, False)]
            if self.ws.home_location != "1 Main St"
            else None
        )

    def f_trip_label(self, v):
        r = self.client.post(
            reverse("events:trip", args=[self.event.pk]),
            {"mode": "here", "latitude": "40.7", "longitude": "-74.0", "label": v},
        )
        t = TripStart.objects.filter(user=self.parent, event=self.event).first()
        stored = [(t.label, False)] if t else None
        TripStart.objects.filter(user=self.parent, event=self.event).delete()
        return r, stored

    def f_username_profile(self, v):
        r = self.client.post(
            reverse("accounts:profile"), {"action": "username", "username": v}
        )
        self.parent.refresh_from_db()
        return r, (
            [(self.parent.username, False)]
            if self.parent.username != "fz_parent"
            else None
        )

    def f_chat_service(self, v):
        from chat.services import create_message

        try:
            m = create_message(
                sender=self.parent,
                workspace_id=self.ws.pk,
                content=v,
                event_label=v[:200],
            )
        except ValidationError:
            return None, None
        return None, [(m.content, True), (m.event_label, False)]

    def f_google_import(self, v):
        from calendars import sync

        item = {
            "id": "gx",
            "summary": v,
            "location": v,
            "status": "confirmed",
            "start": {"dateTime": "2031-05-04T10:00:00-04:00"},
            "end": {"dateTime": "2031-05-04T11:00:00-04:00"},
        }
        with mock.patch.object(
            sync, "_credentials", return_value=mock.Mock(token="t", expiry=None)
        ), mock.patch.object(sync, "_service"), mock.patch.object(
            sync, "_fetch_items", return_value=([item], "America/New_York")
        ):
            try:
                sync.sync_connection(self.conn)
            except Exception as exc:  # noqa: BLE001
                return exc, None
        e = Event.objects.filter(external_id="gx").first()
        stored = [(e.title, False), (e.location, False)] if e else None
        Event.objects.filter(external_id="gx").delete()
        return None, stored

    FIELDS = {
        "event.title": "f_event_title",
        "event.location": "f_event_location",
        "event.title(edit)": "f_event_edit_title",
        "checklist.title": "f_checklist_title",
        "checklist-item.description": "f_checklist_item",
        "checklist-item.description(edit)": "f_checklist_item_edit",
        "workspace.home_location": "f_family_home",
        "tripstart.label": "f_trip_label",
        "user.username(profile)": "f_username_profile",
        "chat.content+event_label(service)": "f_chat_service",
        "event.title+location(google-import)": "f_google_import",
    }

    def probe(self, field):
        reader = getattr(self, self.FIELDS[field])
        found = {}
        for code, value in self.CORPUS.items():
            self.as_user("parent")
            self.client.raise_request_exception = False
            try:
                with transaction.atomic():
                    resp, stored = reader(value)
            except Exception as exc:  # noqa: BLE001
                found.setdefault("exception:" + type(exc).__name__, (code, value))
                continue
            if isinstance(resp, Exception):
                found.setdefault("exception:" + type(resp).__name__, (code, value))
                continue
            if resp is not None and getattr(resp, "status_code", 0) >= 500:
                found.setdefault("http-5xx", (code, value))
                continue
            LEDGER[f"accepted:{field}"].append(code if stored else "rejected:" + code)
            for text, multiline in stored or []:
                for kind in violations(text, multiline):
                    found.setdefault(kind, (code, value))
                if len(text) > 255 and field not in (
                    "chat.content+event_label(service)",
                ):
                    found.setdefault("overlong-stored", (code, value))
        return found

    def check(self, field):
        found = self.probe(field)
        shown = {
            k: (c, repr(v[:60]) + (f"…<{len(v)}>" if len(v) > 60 else ""))
            for k, (c, v) in found.items()
        }
        self.assertFalse(found, f"{field}: stored values keep {shown}")

    def test_emoji_survive_sanitising(self):
        """Control: the cleaning must not destroy ZWJ family emoji or flag tags."""
        self.as_user("parent")
        _, stored = self.f_checklist_title(EMOJI_OK)
        self.assertIsNotNone(stored)
        self.assertIn("‍", stored[0][0])
        self.assertIn("\U000e007f", stored[0][0])


def _install_stored(field):
    def test(self):
        self.check(field)

    safe = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    test.__name__ = f"test_stored_{safe}"
    setattr(StoredValues, test.__name__, test)


for _field in StoredValues.FIELDS:
    _install_stored(_field)


class StoredValuesNewUsers(World):
    """Fields that need a fresh account each time (a person has one family, one name)."""

    def _fresh(self, i):
        user = User.objects.create_user(f"fz_fresh{i}", f"fresh{i}@example.com", None)
        self.client.logout()
        self.client.force_login(
            user, backend="django.contrib.auth.backends.ModelBackend"
        )
        return user

    def _probe(self, submit, reader, multiline=False):
        found = {}
        for i, (code, value) in enumerate({**StoredValues.CORPUS}.items()):
            self.client.raise_request_exception = False
            try:
                user = self._fresh(i)
                with transaction.atomic():
                    resp = submit(value)
                if resp.status_code >= 500:
                    found.setdefault("http-5xx", (code, value))
                    continue
                text = reader(user)
            except Exception as exc:  # noqa: BLE001
                found.setdefault("exception:" + type(exc).__name__, (code, value))
                continue
            for kind in violations(text or "", multiline):
                found.setdefault(kind, (code, value))
        shown = {k: (c, repr(v[:60])) for k, (c, v) in found.items()}
        self.assertFalse(found, f"stored values keep {shown}")

    def test_stored_workspace_name(self):
        self._probe(
            lambda v: self.client.post(
                reverse("accounts:create_workspace"), {"name": v}
            ),
            lambda u: (
                (
                    Workspace.objects.filter(primary_admin=u).first()
                    or mock.Mock(name="")
                ).name
                if Workspace.objects.filter(primary_admin=u).exists()
                else ""
            ),
        )

    def test_stored_signup_username_and_email(self):
        n = [0]

        def submit(v):
            n[0] += 1
            self.client.logout()
            return self.client.post(
                reverse("accounts:signup"),
                {
                    "username": v,
                    "email": f"x{n[0]}@example.com",
                    "password1": "Zx9-hardpass-pass",
                    "password2": "Zx9-hardpass-pass",
                },
            )

        self._probe(
            submit,
            lambda u: (
                User.objects.order_by("-pk").first().username
                if User.objects.filter(email=f"x{n[0]}@example.com").exists()
                else ""
            ),
        )

    def test_stored_invite_email(self):
        def submit(v):
            self.client.logout()
            self.client.force_login(
                self.parent, backend="django.contrib.auth.backends.ModelBackend"
            )
            return self.client.post(
                reverse("accounts:manage_members", args=[self.ws.pk]),
                {"email": v, "role": "parent"},
            )

        self._probe(
            submit, lambda u: (User.objects.order_by("-pk").first().email or "")
        )


# ---------------------------------------------------------------------------
# 4. Rendered pages escape everything (tag/attribute inventory against a benign twin)
# ---------------------------------------------------------------------------
class Inventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = collections.Counter()
        self.json_blocks = []
        self._in_json = False

    def handle_starttag(self, tag, attrs):
        names = tuple(sorted(k for k, _ in attrs))
        self.tags[(tag, names)] += 1
        for k, v in attrs:
            if k.startswith("on") or (
                k in ("href", "src", "action", "formaction")
                and v
                and v.strip().lower().startswith(("javascript:", "data:", "vbscript:"))
            ):
                self.tags[("DANGEROUS", f"{tag}:{k}")] += 1
        self._in_json = (
            tag == "script" and dict(attrs).get("type") == "application/json"
        )

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_json = False

    def handle_data(self, data):
        if self._in_json:
            self.json_blocks.append(data)


def inventory(html):
    parser = Inventory()
    parser.feed(html)
    return parser


HOSTILE_TEXTS = [
    f'"><{CANARY} onload=alert(1)>',
    f"</script><{CANARY}>",
    f"'><{CANARY}>{{{{7*7}}}}",
    f"</textarea></title></style><{CANARY}>",
    "<img src=x onerror=alert(1)>",
    '" onmouseover="alert(1)" a="',
    "javascript:alert(1)",
    "‮<b>x</b>",
    "${7*7}{%x%}",
]


class RenderedPages(World):
    """Write hostile text straight to the DB (as old rows or a Google import could), then
    render every page that shows it as parent and kid. A hostile page must have the same
    tags and attribute names as a page built from harmless text."""

    def seed(self, text):
        short = text[:150]
        self.event.title = short[:200]
        self.event.location = text[:250]
        self.event.save()
        self.google_event.title = short
        self.google_event.save()
        self.checklist.title = short
        self.checklist.save()
        self.item.description = text[:250]
        self.item.save()
        Workspace.objects.filter(pk=self.ws.pk).update(
            name=short[:100], home_location=text[:250]
        )
        ChatMessage.objects.all().delete()
        from chat.services import serialize_message  # noqa: F401

        ChatMessage.objects.create(
            workspace=self.ws, sender=self.parent, content=text, event_label=short[:190]
        )
        TripStart.objects.update_or_create(
            user=self.parent,
            event=self.event,
            defaults={
                "mode": "here",
                "label": text[:250],
                "latitude": 40.7,
                "longitude": -74.0,
            },
        )
        User.objects.filter(pk=self.parent2.pk).update(
            username=short[:20] or "x", first_name=short[:30]
        )
        MemberColor.objects.update_or_create(
            workspace=self.ws, user=self.kid, defaults={"color": "#c62828"}
        )

    def pages(self):
        ws, ev, cl = self.ws.pk, self.event.pk, self.checklist.pk
        day = "?view=day&date=2031-05-04&timezone=America/New_York"
        return [
            reverse("accounts:dashboard"),
            reverse("accounts:profile"),
            reverse("accounts:workspace_detail", args=[ws]),
            reverse("accounts:manage_members", args=[ws]),
            reverse("accounts:family_home", args=[ws]),
            reverse("events:detail", args=[ev]),
            reverse("events:edit", args=[ev]),
            reverse("events:list"),
            reverse("events:workspace_list", args=[ws]) + day,
            reverse("events:workspace_list", args=[ws]) + "?view=week&date=2031-05-04",
            reverse("events:workspace_list", args=[ws]) + "?view=month&date=2031-05-04",
            reverse("events:workspace_list", args=[ws]) + "?view=upcoming",
            reverse("events:workspace_create", args=[ws]),
            reverse("events:delete", args=[ev]),
            reverse("events:trip", args=[ev]),
            reverse("events:trip", args=[ev]) + "?format=json",
            reverse("chat:workspace_chat", args=[ws]),
            reverse("chat:messages", args=[ws]),
            reverse("checklists:list", args=[ws]),
            reverse("checklists:detail", args=[cl]),
            reverse("checklists:edit", args=[cl]),
            reverse("checklists:item_edit", args=[self.item.pk]),
        ]

    def render_all(self, role):
        out = {}
        self.as_user(role)
        self.client.raise_request_exception = False
        with mock.patch("events.views.places.enabled", return_value=False):
            for url in self.pages():
                trap = Trapped()
                got_request_exception.connect(trap)
                try:
                    resp = self.client.get(url)
                finally:
                    got_request_exception.disconnect(trap)
                if resp.status_code >= 500:
                    out[url] = ("5xx", (resp.status_code, trap.errors[:1]))
                else:
                    out[url] = (
                        "ok",
                        resp.content.decode("utf-8", "replace"),
                        resp.headers.get("Content-Type", ""),
                    )
        return out

    def test_hostile_text_renders_escaped_on_every_page(self):
        problems = []
        for role in ("parent", "kid", "parent2"):
            self.seed("Harmless words")
            base = self.render_all(role)
            for hostile in HOSTILE_TEXTS:
                self.seed(hostile)
                page = self.render_all(role)
                for url, result in page.items():
                    if result[0] == "5xx":
                        problems.append(
                            f"{role} {url}: HTTP {result[1]} with {hostile!r}"
                        )
                        continue
                    body, ctype = result[1], result[2]
                    if ctype.startswith("application/json"):
                        try:
                            json.loads(body)
                        except ValueError:
                            problems.append(f"{role} {url}: invalid JSON")
                        continue
                    if LEAK.search(body):
                        problems.append(
                            f"{role} {url}: raw markup in page for {hostile!r}"
                        )
                    if base[url][0] != "ok":
                        continue
                    a, b = inventory(base[url][1]), inventory(body)
                    if a.tags != b.tags:
                        diff = (b.tags - a.tags) + (a.tags - b.tags)
                        problems.append(
                            f"{role} {url}: DOM shape changed by {hostile!r}: "
                            f"{list(diff.items())[:3]}"
                        )
                    for block in b.json_blocks:
                        try:
                            json.loads(block)
                        except ValueError:
                            problems.append(
                                f"{role} {url}: embedded JSON broken by {hostile!r}"
                            )
        self.assertFalse(
            problems, "\n".join(problems[:15]) + f"\n({len(problems)} total)"
        )

    def test_detector_catches_a_leak(self):
        """Control for this class: the shape check must fail on a real injection."""
        safe = inventory('<p title="a">&lt;zzcanary&gt;</p>')
        leaked = inventory('<p title="a"><zzcanary onload=x></p>')
        broke_attr = inventory('<p title="a" onmouseover="x" a="">hi</p>')
        self.assertNotEqual(safe.tags, leaked.tags)
        self.assertNotEqual(safe.tags, broke_attr.tags)
        self.assertTrue(LEAK.search("<zzcanary>"))

    def test_malformed_stored_colour_does_not_take_every_page_down(self):
        """The colour is validated on write, but rows can also come from the admin or an
        old migration: one bad value must not give the whole family a 500 page."""
        # The colour CHECK (spec 8.5) now refuses these rows. Drop it inside this
        # test's transaction, which rolls back, so the pages are still tested
        # against rows an old release or raw SQL could have stored.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "ALTER TABLE accounts_membercolor DROP CONSTRAINT membercolor_color_hex"
            )
        bad = []
        for value in ("><zz", "red", "#12", "#gggggg", "url(x)", "#c628;x"):
            MemberColor.objects.update_or_create(
                workspace=self.ws, user=self.kid, defaults={"color": value}
            )
            for url, result in self.render_all("parent").items():
                if result[0] == "5xx":
                    bad.append((value, url[:40], result[1][1][:1]))
        self.assertFalse(
            bad, f"{len(bad)} pages 500 on a malformed MemberColor, e.g. {bad[:2]}"
        )

    def test_no_data_or_remote_images_in_pages(self):
        """Pages must not embed attacker-chosen images: every img/src stays same-origin."""
        self.seed("Harmless words")
        bad = []
        for role in ("parent", "kid"):
            for url, result in self.render_all(role).items():
                if result[0] != "ok" or not result[2].startswith("text/html"):
                    continue
                for src in re.findall(
                    r"""<(?:img|source|video|audio|iframe|object|embed)\b[^>]*?\bsrc=["']([^"']+)""",
                    result[1],
                    re.I,
                ):
                    if src.startswith(("data:", "http:", "https:", "//")):
                        bad.append((role, url, src[:60]))
        self.assertFalse(bad, bad[:5])


# ---------------------------------------------------------------------------
# 5. Stand-alone validators fed attacker-controlled text from outside
# ---------------------------------------------------------------------------
class OutsideTextParsers(World):
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    @given(text=hostile_text)
    @example(text="Soccer \x00 May 4 2031 ‮10:00 AM @ <zzcanary>")
    def test_ocr_text_parser_never_crashes_and_returns_clean_text(self, text):
        from events.services.event_ocr import parse_event_text
        from events.services.event_ocr import EventOCRError

        try:
            result = parse_event_text(text[:5000], timezone_name=TZ)
        except EventOCRError:
            return
        for key in ("title", "location"):
            value = (result or {}).get(key) or ""
            self.assertFalse(
                violations(value), f"OCR {key} keeps {violations(value)}: {value!r}"
            )

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    @given(
        title=st.one_of(
            hostile_text, st.integers(), st.none(), st.lists(st.text()), st.booleans()
        ),
        location=st.one_of(hostile_text, st.integers(), st.none()),
        when=st.one_of(hostile_text, st.none(), st.integers()),
    )
    def test_ai_suggestion_validator_rejects_or_cleans(self, title, location, when):
        from events.ai_suggestions import EventSuggestionError, _validate_suggestion

        data = {
            "title": title,
            "date": when,
            "start_time": when,
            "end_date": when,
            "end_time": when,
            "location": location,
        }
        try:
            result = _validate_suggestion(data)
        except EventSuggestionError:
            return
        for key in ("title", "location"):
            value = result.get(key) or ""
            self.assertIsInstance(value, str)
            self.assertFalse(
                violations(value), f"AI {key} keeps {violations(value)}: {value!r}"
            )

    def test_place_id_and_session_regexes_reject_trailing_newline(self):
        from events import places

        self.assertFalse(places.valid_place_id("ChIJabcdefghij\n"))
        self.assertFalse(places.valid_session("abc-123\n"))

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    @given(text=hostile_text)
    def test_clean_visible_text_is_idempotent_and_clean(self, text):
        from accounts.forms import clean_display_text
        from syncmily.text import clean_visible_text

        once = clean_visible_text(text)
        self.assertEqual(clean_visible_text(once), once)
        self.assertFalse(violations(clean_display_text(text)))


# ---------------------------------------------------------------------------
# 6. Chat WebSocket
# ---------------------------------------------------------------------------
GOOD_ORIGIN = b"http://localhost:8000"


async def _next_output(communicator, timeout):
    try:
        msg = await asyncio.wait_for(communicator.output_queue.get(), timeout)
    except asyncio.TimeoutError:
        return None
    if isinstance(msg, BaseException):
        raise msg
    return msg


@override_settings(RATELIMIT_ENABLED=False)
class ChatSocketFuzz(HypTransactionTestCase):
    def setUp(self):
        self.fixture()

    def fixture(self):
        """Rebuilt for every Hypothesis example: the table flush removes the rows."""
        tag = uuid.uuid4().hex[:8]
        self.parent = User.objects.create_user(f"ws_{tag}", f"{tag}@example.com", None)
        self.ws = Workspace.objects.create(name="WS Fuzz", primary_admin=self.parent)
        client = Client()
        client.force_login(
            self.parent, backend="django.contrib.auth.backends.ModelBackend"
        )
        self.cookie = client.cookies["sessionid"].value

    def communicator(self):
        from syncmily.asgi import application

        headers = [
            (b"host", b"localhost"),
            (b"origin", GOOD_ORIGIN),
            (b"cookie", f"sessionid={self.cookie}".encode()),
        ]
        return WebsocketCommunicator(
            application, f"/ws/chat/{self.ws.id}/", headers=headers
        )

    async def exchange(self, frame):
        """Send one frame; return ("ok"|"error"|"closed"|"crash:<Exc>", detail)."""
        comm = self.communicator()
        connected, _ = await comm.connect()
        if not connected:
            return "refused", None
        try:
            if isinstance(frame, bytes):
                await comm.send_input({"type": "websocket.receive", "bytes": frame})
            else:
                await comm.send_input({"type": "websocket.receive", "text": frame})
            deadline = 1.5
            while True:
                getter = asyncio.ensure_future(comm.output_queue.get())
                done, _ = await asyncio.wait(
                    {getter, comm.future},
                    timeout=deadline,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if getter not in done:
                    getter.cancel()
                    if comm.future in done:
                        exc = comm.future.exception()
                        if exc is not None:
                            return "crash:" + type(exc).__name__, str(exc)[:150]
                        return "closed", "app finished"
                    return "silent", None
                msg = getter.result()
                if isinstance(msg, BaseException):
                    return "crash:" + type(msg).__name__, str(msg)[:150]
                if msg["type"] == "websocket.close":
                    if comm.future.done() and comm.future.exception() is not None:
                        exc = comm.future.exception()
                        return "crash:" + type(exc).__name__, str(exc)[:150]
                    return "closed", msg.get("code")
                if msg["type"] == "websocket.send":
                    payload = json.loads(msg.get("text") or "{}")
                    return (
                        "error" if payload.get("type") == "error" else "ok"
                    ), payload
                deadline = 0.3
        finally:
            try:
                await comm.disconnect()
            except BaseException:  # noqa: BLE001
                pass

    def run_frame(self, frame):
        return async_to_sync(self.exchange)(frame)

    @staticmethod
    def chat(content, **kw):
        return json.dumps({"type": "chat_message", "content": content, **kw})

    def test_malformed_frames_never_crash_or_close_the_socket(self):
        cases = {
            "bytes-frame": b"\x00\x01binary",
            "bytes-frame-oversize": b"x" * 9000,
            "not-json": "not json at all",
            "empty-text": "",
            "json-list": "[1,2,3]",
            "json-null": "null",
            "json-number": "42",
            "json-string": '"hello"',
            "json-nan": '{"type": NaN}',
            "deep-nesting": "[" * 4000 + "]" * 4000,
            "deep-nesting-object": '{"a":' * 1500 + "1" + "}" * 1500,
            "huge-number": '{"type":"chat_message","content":' + "9" * 5000 + "}",
            "type-list": json.dumps({"type": ["chat_message"], "content": "x"}),
            "type-null": json.dumps({"type": None}),
            "content-int": json.dumps({"type": "chat_message", "content": 5}),
            "content-list": json.dumps({"type": "chat_message", "content": ["a"]}),
            "content-dict": json.dumps({"type": "chat_message", "content": {"a": 1}}),
            "content-null": json.dumps({"type": "chat_message", "content": None}),
            "content-missing": json.dumps({"type": "chat_message"}),
            "label-int": json.dumps(
                {"type": "chat_message", "content": "x", "event_label": 5}
            ),
            "label-dict": json.dumps(
                {"type": "chat_message", "content": "x", "event_label": {}}
            ),
            "priority-dict": json.dumps(
                {"type": "chat_message", "content": "x", "priority": {}}
            ),
            "priority-list": json.dumps(
                {"type": "chat_message", "content": "x", "priority": [1]}
            ),
            "content-2001": self.chat("a" * 2001),
            "content-8192-frame": "x" * 8193,
            "content-nul": self.chat("a\x00b"),
            "content-lone-surrogate": '{"type":"chat_message","content":"\\ud800"}',
            "content-nul-escaped": '{"type":"chat_message","content":"a\\u0000b"}',
            "label-nul": self.chat("x", event_label="a\x00b"),
            "label-lone-surrogate": '{"type":"chat_message","content":"x",'
            '"event_label":"\\udc00"}',
            "priority-nul": self.chat("x", priority="high\x00"),
            "duplicate-keys": '{"type":"chat_message","type":"x","content":"a","content":"b"}',
            "emoji-2000": self.chat("\U0001f600" * 2000),
            "whitespace-only": self.chat(" \n\t "),
            "zero-width-only": self.chat("​‮﻿"),
        }
        bad = {}
        for name, frame in cases.items():
            kind, detail = self.run_frame(frame)
            if kind not in ("ok", "error"):
                bad[name] = (kind, detail)
        self.assertFalse(bad, f"chat socket mishandles frames: {bad}")

    def test_accepted_chat_content_is_clean(self):
        leaks = {}
        for code, value in {
            **ATTACKS,
            **{f"blns-{i}": s for i, s in enumerate(BLNS[:150]) if s},
        }.items():
            if len(value) > 2000:
                continue
            kind, payload = self.run_frame(self.chat(value, event_label=value[:200]))
            if kind != "ok":
                continue
            found = violations(payload["content"], multiline=True) | violations(
                payload["event_label"]
            )
            for f in found:
                leaks.setdefault(f, (code, value[:50]))
        self.assertFalse(leaks, f"chat stores/broadcasts {leaks}")

    # fmt: off
    # Hypothesis seeds a derandomized test from the function's source text, so this one
    # stays as first written; reformatting it changes which examples run.
    @settings(max_examples=min(MAX_EXAMPLES, 40), deadline=None, derandomize=True, database=None,
              suppress_health_check=list(HealthCheck))
    @given(content=hostile_text.map(lambda s: s[:2000]),
           extra=st.dictionaries(st.sampled_from(["priority", "event_label", "type", "x"]),
                                 st.one_of(hostile_text.map(lambda s: s[:300]), st.integers(),
                                           st.none(), st.lists(st.integers(), max_size=2)),
                                 max_size=3))
    def test_property_hostile_messages_get_ok_or_error(self, content, extra):
        self.fixture()
        frame = json.dumps({"type": "chat_message", "content": content, **extra})
        kind, detail = self.run_frame(frame)
        self.assertIn(kind, ("ok", "error"), f"{kind} {detail} for {frame[:200]!r}")
    # fmt: on


# ---------------------------------------------------------------------------
# 6b. Request shapes that need no generator to show the problem
# ---------------------------------------------------------------------------
class RequestShapes(World):
    def setUp(self):
        self.client.raise_request_exception = False

    def post(self, name, path, data=None, **kw):
        trap = Trapped()
        got_request_exception.connect(trap)
        try:
            resp = self.client.post(path, data or {}, **kw)
        finally:
            got_request_exception.disconnect(trap)
        return resp, trap.errors

    def test_login_with_overlong_username_is_a_normal_failure(self):
        bad = {}
        for n in (150, 151, 255, 256, 1000, 100000):
            resp, errors = self.post(
                "login",
                reverse("accounts:login"),
                {"username": "u" * n, "password": "wrong"},
            )
            if resp.status_code >= 500:
                bad[n] = errors[:1]
        self.assertFalse(bad, f"login answers 5xx for long usernames: {bad}")

    def test_login_with_overlong_password_is_a_normal_failure(self):
        resp, errors = self.post(
            "login",
            reverse("accounts:login"),
            {"username": "fz_parent", "password": "p" * 1_000_000},
        )
        self.assertLess(resp.status_code, 500, errors[:1])

    def test_password_reset_and_two_step_with_huge_values(self):
        bad = {}
        self.as_user("parent")
        for name, path, field in (
            ("reset", reverse("accounts:password_reset"), "email"),
            ("two_step_verify", reverse("accounts:two_step_verify"), "code"),
            ("two_step_setup", reverse("accounts:two_step_setup"), "code"),
        ):
            resp, errors = self.post(name, path, {field: "9" * 100000})
            if resp.status_code >= 500:
                bad[name] = errors[:1]
        self.assertFalse(bad, bad)

    def test_json_endpoints_answer_json_for_every_error(self):
        """A 400 from a JSON route must be JSON: the browser code calls res.json()."""
        self.as_user("parent")
        bad = []
        with mock.patch("events.views.places.enabled", return_value=True):
            cases = [
                ("place_details empty id", reverse("events:place_details"), {"id": ""}),
                ("place_details bad id", reverse("events:place_details"), {"id": "x"}),
                (
                    "place_details newline id",
                    reverse("events:place_details"),
                    {"id": "ChIJabcdefghij\n"},
                ),
            ]
            for label, path, data in cases:
                resp, _ = self.post(label, path, data)
                if resp.status_code >= 400 and not resp.headers.get(
                    "Content-Type", ""
                ).startswith("application/json"):
                    bad.append(
                        (label, resp.status_code, resp.headers.get("Content-Type"))
                    )
        resp = self.client.get(
            reverse("chat:messages", args=[str(self.ws.pk)]), {"before": "abc"}
        )
        self.assertTrue(resp.headers["Content-Type"].startswith("application/json"))
        resp = self.client.get(
            reverse("chat:messages", args=[str(self.ws.pk)]), {"before": "9" * 30}
        )
        if resp.status_code >= 500:
            bad.append(("chat_messages before=10^30", resp.status_code, ""))
        resp = self.client.get(
            reverse("chat:messages", args=[str(self.ws.pk)]),
            {"after": "-9223372036854775809"},
        )
        if resp.status_code >= 500:
            bad.append(("chat_messages after=-2^63-1", resp.status_code, ""))
        self.assertFalse(bad, bad)

    # Places counts as configured here, so the answer does not depend on whether
    # the environment has a Google key (without one the view answers 503 on purpose).
    @mock.patch("events.places.suggest", return_value=[])
    @mock.patch("events.places.enabled", return_value=True)
    def test_malformed_multipart_to_json_endpoint_is_json(self, *_mocks):
        self.as_user("parent")
        resp = self.client.post(
            reverse("events:place_suggestions"),
            b'--b\r\nContent-Disposition: form-data; name="q"\r\n\r\nabc',
            content_type="multipart/form-data; boundary=b",
        )
        self.assertLess(resp.status_code, 500)
        if resp.status_code >= 400:
            self.assertTrue(
                resp.headers.get("Content-Type", "").startswith("application/json"),
                f"{resp.status_code} {resp.headers.get('Content-Type')}",
            )


# ---------------------------------------------------------------------------
# 7. Ledger
# ---------------------------------------------------------------------------
class ZZLedger(World):
    def test_zz_write_ledger(self):
        path = os.environ.get("FUZZ_LEDGER")
        if path:
            Path(path).write_text(json.dumps(LEDGER, indent=1, default=str))
