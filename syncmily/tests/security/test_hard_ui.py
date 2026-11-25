"""
Security lane "hard-ui": the UI must survive hostile text.

Every test_poc_* asserts the SECURE behaviour, so it FAILS while the gap exists.
test_cov_* record controls that already hold (they pass).
Run:
  POSTGRES_DB=syncmily_hard_ui_t python manage.py test syncmily.tests.security.test_hard_ui --noinput
Measured breaks (WebKit screenshots at 390 and 1280 px) are in
/Users/simonsavine/syncmily-security-audit-report/2026-11-18/ui/.
No network: Google is mocked.
"""

import os
import re
import unicodedata
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DataError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection
from chat import services as chat_services
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event
from events.templatetags.paint import person
from syncmily.text import clean_visible_text

User = get_user_model()
PW = "Sup3r-Secret-test-pw!"
BIDI_CONTROLS = "".join(
    chr(c) for c in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
)
RLO = "‮"
MARKS = "".join(chr(c) for c in range(0x0300, 0x0332))  # 50 combining marks
ZALGO = "".join("Z" + MARKS for _ in range(2))
LONG = "W" * 150
ROOT = Path(__file__).resolve().parents[3]
CSS = (ROOT / "events/static/events/paint.css").read_text()

BDI_BLOCK = re.compile(r"<bdi\b[^>]*>.*?</bdi>", re.S | re.I)


def outside_bdi(html):
    """The page with every <bdi>...</bdi> removed: bidi controls left here bleed."""
    return BDI_BLOCK.sub("", html)


def longest_mark_run(text):
    run = best = 0
    for ch in text:
        run = run + 1 if unicodedata.category(ch).startswith("M") else 0
        best = max(best, run)
    return best


def css_rules(css):
    """(selectors, declarations) pairs; good enough for a flat stylesheet."""
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}@]+)\{([^{}]*)\}", body):
        yield [s.strip() for s in match.group(1).split(",")], match.group(2)


WRAPS = re.compile(
    r"overflow-wrap:\s*(anywhere|break-word)|word-break:\s*(break-all|break-word)"
)
GLOBAL = {"*", "html", "body", "main", ".paint", ".page", "#main-content", "main.page"}


def wrapping_selectors():
    out = set()
    for selectors, decl in css_rules(CSS):
        if WRAPS.search(decl):
            out.update(selectors)
    return out


def selector_is_covered(selector, wrapped):
    """Covered when a global rule wraps, or a rule wraps this exact selector or a
    selector that ends in the same last compound (".card-link b")."""
    if wrapped & GLOBAL:
        return True
    last = selector.split()[-1]
    return any(w == selector or w.split()[-1] == last for w in wrapped)


class HardUiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = User.objects.create_user(
            "hui-parent", password=PW, first_name=LONG
        )
        cls.parent2 = User.objects.create_user(
            "hui-parent2", password=PW, first_name=RLO + "Dinner bring cake" * 8
        )
        cls.kid = User.objects.create_user("hui-kid", password=PW, first_name=ZALGO)
        cls.ws = Workspace.objects.create(name="W" * 100, primary_admin=cls.parent)
        now = timezone.now()
        for user, role in (
            (cls.parent, "parent"),
            (cls.parent2, "parent"),
            (cls.kid, "kid"),
        ):
            Membership.objects.create(
                workspace=cls.ws,
                user=user,
                role=role,
                invited_by=cls.parent,
                accepted_at=now,
            )
        cls.today = timezone.localdate()
        cls.tz = timezone.get_current_timezone()

    def at(self, hour, minute=0, day=0):
        return datetime.combine(
            self.today + timedelta(days=day), time(hour, minute), tzinfo=self.tz
        )

    def make_event(self, title, start, end, who=None, **extra):
        event = Event.objects.create(
            created_by=self.parent,
            workspace=self.ws,
            title=title,
            starts_at=start,
            ends_at=end,
            **extra,
        )
        event.participants.set(
            who if who is not None else [self.parent, self.parent2, self.kid]
        )
        return event


class HostileTextEntersThroughRealRoutes(HardUiBase):
    """Chat and Google sync keep bidi overrides and zalgo that the forms already strip."""

    def test_poc_chat_service_strips_bidi_overrides(self):
        message = chat_services.create_message(
            sender=self.parent,
            workspace_id=self.ws.pk,
            content=RLO + "Pickup is at five",
        )
        self.assertFalse(
            set(message.content) & set(BIDI_CONTROLS),
            "chat keeps U+202E: it reverses the neighbouring labels",
        )

    def test_poc_chat_service_limits_stacked_combining_marks(self):
        message = chat_services.create_message(
            sender=self.parent,
            workspace_id=self.ws.pk,
            content=ZALGO,
            event_label=ZALGO,
        )
        self.assertLessEqual(
            longest_mark_run(message.content),
            4,
            "50 stacked marks per letter are stored as typed",
        )
        self.assertLessEqual(longest_mark_run(message.event_label), 4)

    def test_poc_clean_visible_text_limits_stacked_combining_marks(self):
        """clean_visible_text backs the event, checklist and workspace forms."""
        self.assertLessEqual(longest_mark_run(clean_visible_text(ZALGO)), 4)

    def test_poc_google_sync_strips_bidi_in_title_and_location(self):
        from calendars import sync

        connection = CalendarConnection.objects.create(
            user=self.parent,
            workspace=self.ws,
            provider="google",
            calendar_id="primary",
        )
        item = {
            "id": "g1",
            "summary": RLO + "Dentist " + ZALGO,
            "location": RLO + "Main St",
            "start": {"dateTime": self.at(15).isoformat()},
            "end": {"dateTime": self.at(16).isoformat()},
        }
        with mock.patch.object(
            sync, "_credentials", return_value=mock.Mock(token=None, expiry=None)
        ), mock.patch.object(sync, "_service"), mock.patch.object(
            sync, "_fetch_items", return_value=([item], "UTC")
        ):
            sync.sync_connection(connection)
        event = Event.objects.get(external_id="g1")
        self.assertFalse(
            set(event.title + event.location) & set(BIDI_CONTROLS),
            "calendars/sync.py:137 keeps U+202E",
        )
        self.assertLessEqual(longest_mark_run(event.title), 4)

    def test_cov_forms_path_already_strips_bidi(self):
        self.assertEqual(clean_visible_text(RLO + "Dinner"), "Dinner")

    def test_cov_db_rejects_titles_over_the_column_limit(self):
        with self.assertRaises(DataError), transaction.atomic():
            Event.objects.create(
                created_by=self.parent,
                workspace=self.ws,
                title="T" * 2000,
                starts_at=self.at(9),
                ends_at=self.at(10),
            )


class RenderedPagesIsolateBidi(HardUiBase):
    """Whatever got stored, the page must not let it reorder its own labels."""

    def setUp(self):
        self.make_event(
            RLO + "Dinner at seven bring the cake",
            self.at(8),
            self.at(9),
            requires_transport=True,
            dropoff_driver=self.parent2,
            pickup_driver=self.parent,
            location=RLO + "Pier 40",
        )
        cl = Checklist.objects.create(
            workspace=self.ws, title=RLO + "Packing list", created_by=self.parent
        )
        ChecklistItem.objects.create(checklist=cl, description=RLO + "Towels")
        ChatMessage.objects.create(
            workspace=self.ws,
            sender=self.parent2,
            content=RLO + "hello",
            event_label=RLO + "Dinner",
        )

    def pages(self):
        ws = self.ws.pk
        return {
            "dashboard": reverse("accounts:dashboard"),
            "day": reverse("events:list") + f"?view=day&date={self.today}",
            "week": reverse("events:list") + f"?view=week&date={self.today}",
            "upcoming": reverse("events:list") + "?view=upcoming",
            "event": reverse("events:detail", args=[Event.objects.first().pk]),
            "chat": reverse("chat:workspace_chat", args=[ws]),
            "checklists": reverse("checklists:list", args=[ws]),
            "members": reverse("accounts:manage_members", args=[ws]),
        }

    def test_poc_parent_pages_leave_no_raw_bidi_override_outside_bdi(self):
        self.client.force_login(self.parent)
        raw = {}
        for name, url in self.pages().items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            hits = [
                c for c in outside_bdi(response.content.decode()) if c in BIDI_CONTROLS
            ]
            if hits:
                raw[name] = len(hits)
        self.assertEqual(
            raw, {}, "raw bidi controls on these pages (no <bdi>, not stripped)"
        )

    def test_poc_kid_dashboard_leaves_no_raw_bidi_override_outside_bdi(self):
        self.client.force_login(self.kid)
        html = self.client.get(reverse("accounts:dashboard")).content.decode()
        self.assertFalse([c for c in outside_bdi(html) if c in BIDI_CONTROLS])


class UnboundedTextAndVolume(HardUiBase):
    def test_poc_day_summary_line_is_bounded_for_long_member_names(self):
        """events/day.py:158 joins every double-booked member's name into one sentence."""
        for user in (self.parent, self.parent2, self.kid):
            self.make_event("A", self.at(10), self.at(11), who=[user])
            self.make_event("B", self.at(10, 30), self.at(11, 30), who=[user])
        self.client.force_login(self.parent)
        html = self.client.get(
            reverse("events:list") + f"?view=day&date={self.today}"
        ).content.decode()
        # Parents see the line in the flag style when someone is double-booked.
        span = re.search(r'<span class="span[^"]*">(.*?)</span>', html, re.S).group(1)
        self.assertLessEqual(
            len(re.sub(r"<[^>]+>", "", span)),
            160,
            "summary sentence grows with name length x members",
        )

    def test_poc_person_filter_truncates_long_display_names(self):
        """Names sit in pills, chips, dots and the nav; 150 characters have no room anywhere."""
        self.assertLessEqual(len(person(self.parent)), 40)

    def test_poc_day_view_caps_overlap_columns(self):
        for i in range(120):
            self.make_event(
                f"Bulk {i:03d}",
                self.at(0) + timedelta(minutes=15 * (i % 60)),
                self.at(0) + timedelta(minutes=15 * (i % 60) + 120),
                who=[self.parent],
            )
        self.client.force_login(self.parent)
        html = self.client.get(
            reverse("events:list") + f"?view=day&date={self.today}"
        ).content.decode()
        widest = max(int(n) for n in re.findall(r"--cols:\s*(\d+)", html))
        self.assertLessEqual(
            widest,
            6,
            "events are squeezed to 1/%d of the lane (unreadable slivers)" % widest,
        )

    def test_cov_week_view_folds_200_events_into_more_link(self):
        for i in range(200):
            self.make_event(
                f"Bulk {i:03d}",
                self.at(0) + timedelta(minutes=5 * i),
                self.at(0) + timedelta(minutes=5 * i + 30),
                who=[self.parent],
            )
        self.client.force_login(self.parent)
        html = self.client.get(reverse("accounts:dashboard")).content.decode()
        self.assertIn(
            "more", html
        )  # the paint-chip week already folds long days into '+N more'

    def test_cov_day_view_with_200_events_renders_in_bounded_time(self):
        for i in range(200):
            self.make_event(
                f"Bulk {i:03d}",
                self.at(0) + timedelta(minutes=5 * i),
                self.at(0) + timedelta(minutes=5 * i + 30),
                who=[self.parent],
            )
        self.client.force_login(self.parent)
        started = timezone.now()
        response = self.client.get(
            reverse("events:list") + f"?view=day&date={self.today}"
        )
        self.assertEqual(response.status_code, 200)
        # A shared CI runner is not a performance baseline: CI_SLOW=1 skips the timing.
        if os.environ.get("CI_SLOW") != "1":
            self.assertLess((timezone.now() - started).total_seconds(), 3)


class CssWrapsUnbrokenText(HardUiBase):
    """Selectors that WebKit measured pushing the page wider than the viewport.
    Fix once with overflow-wrap:anywhere on .paint/main, or per selector."""

    BROKEN = {
        ".pill": "event detail: member and driver pills (page +1434 px at 390)",
        ".card-link b": "checklists list: card title (page +2210 px at 390)",
        ".card-link small": "checklists list: 'For <event>' line",
        ".kid-plan b": "kid dashboard: Coming up card titles",
        ".kid-hero h1": "kid dashboard: 'Hi <name>' (+3119 px at 390)",
        ".kid-hero p": "kid dashboard: 'Today: <titles>'",
        ".kid-week .chip span": "kid dashboard: week chips",
        ".family-dots li span": "kid dashboard: family names",
        ".head h1": "checklist detail: title (+2096 px at 390)",
    }

    def test_poc_css_wraps_every_selector_that_overflowed(self):
        wrapped = wrapping_selectors()
        missing = {
            s: why
            for s, why in self.BROKEN.items()
            if not selector_is_covered(s, wrapped)
        }
        self.assertEqual(
            missing, {}, "no overflow-wrap / word-break rule reaches these selectors"
        )

    def test_cov_title_and_plan_selectors_already_wrap(self):
        wrapped = wrapping_selectors()
        for selector in (".plan b", ".row .grow", ".id b", ".dlg-head h2"):
            self.assertTrue(selector_is_covered(selector, wrapped), selector)
