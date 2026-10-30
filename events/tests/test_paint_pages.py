"""The screens moved onto the paint-chip design: each renders on body.paint with its key
controls.

One test class per area; the shared helpers build a small family (Ana runs it, Mateo is
a parent,
Lucía is a kid) on the mockup's week of Mon 26 Oct 2026.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace

from ..day import vertical_day
from ..models import Event
from ..timeline import build_day_timeline, family_members

NEW_YORK = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEW_YORK)


class FamilyMixin:
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.ana = User.objects.create_user(
            username="ana", first_name="Ana", password="FamilyTest2026!"
        )
        cls.mateo = User.objects.create_user(username="mateo", first_name="Mateo")
        cls.lucia = User.objects.create_user(username="lucia", first_name="Lucía")
        cls.workspace = Workspace.objects.create(
            name="Reyes family", primary_admin=cls.ana
        )
        for user, role in (
            (cls.mateo, Membership.Role.PARENT),
            (cls.lucia, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.ana,
                accepted_at=timezone.now(),
            )

    def plan(self, title, starts_at, minutes, people=(), **extra):
        event = Event.objects.create(
            created_by=self.ana,
            workspace=self.workspace,
            title=title,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=minutes),
            timezone_name="America/New_York",
            **extra,
        )
        event.participants.set(people)
        return event

    def assertPaint(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<body class="paint"')
        self.assertNotContains(response, "event_form.css")
        for tag in re.findall(r"<script\b[^>]*>", response.content.decode()):
            if 'type="application/json"' not in tag:
                self.assertIn(" src=", tag)


class SchedulePageTests(FamilyMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.ana)
        self.url = reverse("events:workspace_list", args=[self.workspace.pk])

    def test_day_is_a_paint_grid_with_a_column_per_member(self):
        self.plan("Swim", at(27, 16), 60, [self.lucia])
        response = self.client.get(self.url, {"view": "day", "date": "2026-10-27"})
        self.assertPaint(response)
        self.assertContains(response, "<h1>Tuesday 27 October</h1>", html=True)
        self.assertContains(response, 'class="daygrid"')
        self.assertContains(
            response, 'class="lane" aria-labelledby="lane-1" data-hue="a"'
        )
        self.assertContains(response, "Lucía")
        self.assertContains(
            response, 'data-clock="[16:00] – [17:00]">4:00 PM – 5:00 PM</time>'
        )
        self.assertContains(response, 'aria-label="Schedule period"', count=1)
        self.assertContains(response, 'aria-label="Change day"')
        self.assertContains(response, "data-dialog")

    def test_add_event_in_the_bar_opens_on_the_shown_day(self):
        response = self.client.get(self.url, {"view": "day", "date": "2026-10-27"})
        create = reverse("events:workspace_create", args=[self.workspace.pk])
        self.assertContains(
            response,
            f'href="{create}?date=2026-10-27&amp;timezone=America%2FNew_York"'
            " data-dialog",
        )

    def test_list_groups_plans_by_day_with_member_colours(self):
        self.plan("Book club", timezone.now() + timedelta(days=2), 60, [self.ana])
        response = self.client.get(self.url, {"view": "upcoming"})
        self.assertPaint(response)
        self.assertContains(response, "<h1>Coming up</h1>", html=True)
        self.assertContains(response, 'class="day-sep"')
        self.assertContains(response, 'data-hue="a"')
        self.assertContains(response, "Book club")

    def test_week_view_still_lists_the_week(self):
        self.plan("Soccer", at(28, 17, 30), 90, [self.lucia])
        response = self.client.get(self.url, {"view": "week", "date": "2026-10-28"})
        self.assertPaint(response)
        self.assertContains(response, "Week of 26 October")
        self.assertContains(response, "Soccer")

    def test_parents_get_the_colour_editor_and_kids_do_not(self):
        response = self.client.get(self.url, {"view": "day"})
        self.assertContains(response, "Individual colors")
        self.assertContains(response, 'name="color-%s"' % self.ana.pk)
        self.client.force_login(self.lucia)
        self.assertNotContains(
            self.client.get(self.url, {"view": "day"}), "Individual colors"
        )

    def test_bad_date_keeps_the_page_and_opens_the_date_form(self):
        response = self.client.get(self.url, {"timezone": "Mars/Base"})
        self.assertPaint(response)
        self.assertContains(response, '<details class="date-tab" open>')

    def test_day_view_keeps_its_tools_in_the_header(self):
        response = self.client.get(self.url, {"view": "day"})
        content = response.content.decode()
        self.assertNotContains(response, 'class="foot-tools"')
        self.assertNotContains(response, "Change Google Account")
        # Date and time zone comes first, then Colors with both kinds of colour.
        self.assertLess(
            content.index('class="date-tab"'), content.index('class="palette-tab"')
        )
        self.assertContains(response, "Overall colors")
        self.assertContains(response, "Individual colors")
        # One row per member with a swatch that saves on change: no Save button.
        self.assertContains(response, 'class="member-colour"')
        self.assertContains(response, "data-autosave")
        self.assertNotContains(
            response,
            '<button class="btn btn-quiet" type="submit">Save colours</button></div>',
        )

    def test_stylesheets_carry_a_hash_of_their_content(self):
        import hashlib
        import re

        from django.contrib.staticfiles import finders

        content = self.client.get(self.url, {"view": "day"}).content.decode()
        for path in ("events/shell.css", "events/paint.css", "events/paint.js"):
            with open(finders.find(path), "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()[:10]
            self.assertIn(f"{path}?v={digest}", content)
        self.assertIsNone(re.search(r"\.(css|js)\?v=\d{1,3}\b", content))


class VerticalDayTests(FamilyMixin, TestCase):
    def day(self, date, events):
        start = datetime.combine(date, datetime.min.time(), tzinfo=NEW_YORK)
        members = family_members([self.workspace])
        timeline = build_day_timeline(
            Event.objects.filter(pk__in=[e.pk for e in events]).prefetch_related(
                "participants"
            ),
            members,
            start,
            start + timedelta(days=1),
            NEW_YORK,
            timezone.now(),
        )
        return vertical_day(timeline, NEW_YORK)

    def lane(self, day, user):
        return next(lane for lane in day["lanes"] if lane["user"] == user)

    def test_grid_opens_at_seven_and_runs_to_midnight(self):
        day = self.day(
            datetime(2026, 10, 27).date(),
            [self.plan("Swim", at(27, 16), 60, [self.lucia])],
        )
        self.assertEqual(day["span_hours"], 17)
        self.assertEqual(day["hours"][0]["starts_at"].hour, 7)
        [slot] = self.lane(day, self.lucia)["slots"]
        self.assertEqual(slot["top"], round(9 / 17 * 100, 3))
        self.assertEqual(slot["time_label"], "4:00 PM – 5:00 PM")

    def test_an_early_plan_opens_the_grid_earlier(self):
        day = self.day(
            datetime(2026, 10, 29).date(),
            [self.plan("Boston trip", at(29, 6), 60, [self.ana])],
        )
        self.assertEqual(day["hours"][0]["starts_at"].hour, 6)

    def test_overnight_plan_shows_its_last_hour_and_both_dates(self):
        sleepover = self.plan(
            "Sleepover", at(30, 18), 14 * 60, [self.lucia], is_tentative=True
        )
        day = self.day(datetime(2026, 10, 31).date(), [sleepover])
        [slot] = self.lane(day, self.lucia)["slots"]
        self.assertTrue(slot["clipped"])
        self.assertEqual(slot["top"], 0)
        self.assertEqual(slot["time_label"], "Oct 30, 6:00 PM – 8:00 AM")
        self.assertEqual(slot["time_clock"], "Oct 30, [18:00] – [08:00]")

    def test_summary_counts_plans_and_flags_double_bookings(self):
        calm = self.day(
            datetime(2026, 10, 27).date(),
            [
                self.plan("Swim", at(27, 16), 60, [self.lucia]),
                self.plan("Book club", at(27, 19), 90, [self.ana]),
            ],
        )
        self.assertEqual(calm["summary"], "Two plans, nobody double-booked")
        self.assertEqual(calm["free_note"], "Mateo has nothing planned.")
        busy = self.day(
            datetime(2026, 10, 28).date(),
            [
                self.plan("Carpool", at(28, 17), 60, [self.mateo]),
                self.plan("Gym", at(28, 17, 30), 60, [self.mateo]),
            ],
        )
        self.assertEqual(busy["summary"], "Two plans, Mateo double-booked")
        side_by_side = self.lane(busy, self.mateo)["slots"]
        self.assertEqual(sorted(slot["column"] for slot in side_by_side), [0, 1])

    def test_empty_day(self):
        day = self.day(datetime(2026, 10, 26).date(), [])
        self.assertEqual(day["summary"], "Nothing planned")
        self.assertEqual(day["free_note"], "")


class EventPageTests(FamilyMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.ana)
        self.party = self.plan(
            "Halloween party", at(31, 14), 150, [self.lucia], location="Priya’s house"
        )
        self.plan("Trick-or-treat", at(31, 17), 120, [self.mateo, self.lucia])

    def test_detail_shows_people_place_and_the_same_day(self):
        response = self.client.get(reverse("events:detail", args=[self.party.pk]))
        self.assertPaint(response)
        self.assertContains(
            response,
            '<h1 id="event-heading" data-sheet-hide>Halloween party</h1>',
            html=True,
        )
        self.assertContains(response, 'class="pill" translate="no" data-hue="c"')
        self.assertContains(response, "Open in Maps")
        self.assertContains(response, "Same day")
        self.assertContains(response, "Trick-or-treat")
        self.assertContains(response, "Add another event that day")

    def test_add_event_form_keeps_every_field(self):
        response = self.client.get(
            reverse("events:workspace_create", args=[self.workspace.pk])
        )
        self.assertPaint(response)
        for name in (
            "title",
            "date",
            "start_time",
            "end_date",
            "end_time",
            "timezone",
            "location",
            "latitude",
            "longitude",
            "transport_mode",
            "is_tentative",
            "participants",
            "submission_token",
        ):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'class="who-pick"')
        self.assertContains(response, 'value="preview"')
        self.assertContains(response, 'value="save"')
        self.assertContains(response, "location_autocomplete.js")

    def test_one_family_skips_the_family_picker(self):
        response = self.client.get(reverse("events:choose_workspace"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.url.startswith(
                reverse("events:workspace_create", args=[self.workspace.pk])
            )
        )

    def test_choose_family_page(self):
        Workspace.objects.create(name="Grandma's house", primary_admin=self.ana)
        response = self.client.get(reverse("events:choose_workspace"))
        self.assertPaint(response)
        self.assertContains(response, "Which family is this for?")
        self.assertContains(response, 'name="workspace"')


class FamilyPageTests(FamilyMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.ana)

    def test_family_page_lists_members_in_their_colours_with_an_invite_form(self):
        response = self.client.get(
            reverse("accounts:workspace_detail", args=[self.workspace.pk])
        )
        self.assertPaint(response)
        self.assertContains(response, "<h1>Reyes family</h1>", html=True)
        self.assertContains(response, "3 members")
        self.assertContains(response, 'class="row" data-hue="a"')
        self.assertContains(response, "Parent, admin")
        self.assertContains(
            response,
            f'action="{reverse("accounts:manage_members", args=[self.workspace.pk])}"',
        )
        self.assertContains(response, 'name="role"')
        self.assertContains(response, "Connect Google Calendar")
        self.assertContains(response, "Save colours")

    def test_primary_admin_with_a_membership_row_is_listed_once(self):
        Membership.objects.create(
            workspace=self.workspace,
            user=self.ana,
            role=Membership.Role.PARENT,
            invited_by=self.ana,
            accepted_at=timezone.now(),
        )
        response = self.client.get(
            reverse("accounts:manage_members", args=[self.workspace.pk])
        )
        self.assertPaint(response)
        self.assertContains(response, "In the family (3)")
        self.assertContains(response, "Parent, admin", count=1)

    def test_pending_invites_show_their_username(self):
        self.client.post(
            reverse("accounts:manage_members", args=[self.workspace.pk]),
            {"email": "grandma@example.com", "role": "parent"},
        )
        response = self.client.get(
            reverse("accounts:workspace_detail", args=[self.workspace.pk])
        )
        self.assertContains(response, "grandma@example.com")
        self.assertContains(response, "not joined yet")

    def test_new_family_page(self):
        response = self.client.get(reverse("accounts:create_workspace"))
        self.assertPaint(response)
        self.assertContains(response, "Start a family")
        self.assertContains(response, 'name="name"')


class ChatPageTests(FamilyMixin, TestCase):
    def test_bar_goes_straight_to_the_family_thread(self):
        self.client.force_login(self.lucia)
        thread = reverse("chat:workspace_chat", args=[self.workspace.pk])
        self.assertContains(
            self.client.get(reverse("accounts:dashboard")), f'href="{thread}"'
        )

    def test_thread_draws_messages_in_each_senders_colour(self):
        from chat.models import ChatMessage

        ChatMessage.objects.create(
            workspace=self.workspace, sender=self.mateo, content="Pickup at 3?"
        )
        self.client.force_login(self.ana)
        response = self.client.get(
            reverse("chat:workspace_chat", args=[self.workspace.pk])
        )
        self.assertPaint(response)
        self.assertContains(response, 'class="msg" data-hue="b"')
        self.assertContains(response, "Pickup at 3?")
        self.assertContains(response, 'id="chat-members"')
        self.assertContains(response, 'role="log"')
        self.assertContains(response, 'tabindex="0"')
        self.assertContains(response, "Coming up")

    def test_chat_home_opens_the_only_family(self):
        self.client.force_login(self.ana)
        response = self.client.get(reverse("chat:home"))
        self.assertRedirects(
            response, reverse("chat:workspace_chat", args=[self.workspace.pk])
        )

    def test_chat_home_lists_families(self):
        Workspace.objects.create(name="Grandma's house", primary_admin=self.ana)
        self.client.force_login(self.ana)
        response = self.client.get(reverse("chat:home"))
        self.assertPaint(response)
        self.assertContains(
            response, reverse("chat:workspace_chat", args=[self.workspace.pk])
        )


class ChecklistPageTests(FamilyMixin, TestCase):
    def setUp(self):
        from checklists.models import Checklist, ChecklistItem

        self.party = self.plan("Halloween party", at(31, 14), 150, [self.lucia])
        self.checklist = Checklist.objects.create(
            workspace=self.workspace,
            event=self.party,
            title="Halloween",
            created_by=self.ana,
        )
        self.item = ChecklistItem.objects.create(
            checklist=self.checklist,
            description="Buy candy",
            is_completed=True,
            completed_by=self.mateo,
            completed_at=at(26, 9),
            position=0,
        )
        ChecklistItem.objects.create(
            checklist=self.checklist, description="Glue wings", position=1
        )

    def test_lists_take_their_event_persons_colour(self):
        self.client.force_login(self.ana)
        response = self.client.get(reverse("checklists:list", args=[self.workspace.pk]))
        self.assertPaint(response)
        self.assertContains(response, 'data-hue="c"')
        self.assertContains(response, "1 of 2 done")
        self.assertContains(response, "New checklist")

    def test_items_are_checkboxes_with_who_did_them_and_confirmed_deletes(self):
        self.client.force_login(self.ana)
        response = self.client.get(
            reverse("checklists:detail", args=[self.checklist.pk])
        )
        self.assertPaint(response)
        self.assertContains(
            response, 'role="checkbox" aria-checked="true" aria-label="Buy candy"'
        )
        self.assertContains(
            response, 'role="checkbox" aria-checked="false" aria-label="Glue wings"'
        )
        self.assertContains(response, "Done by")
        self.assertContains(response, "Mateo")
        self.assertContains(response, "data-confirm=", count=3)
        self.assertNotContains(response, "onsubmit")
        self.assertContains(
            response, reverse("checklists:item_add", args=[self.checklist.pk])
        )

    def test_kids_can_tick_but_not_manage(self):
        self.client.force_login(self.lucia)
        response = self.client.get(
            reverse("checklists:detail", args=[self.checklist.pk])
        )
        self.assertPaint(response)
        self.assertContains(
            response, reverse("checklists:item_toggle", args=[self.item.pk])
        )
        self.assertNotContains(response, "Delete list")
        self.assertNotContains(
            response, reverse("checklists:item_add", args=[self.checklist.pk])
        )

    def test_forms(self):
        self.client.force_login(self.ana)
        for url in (
            reverse("checklists:create", args=[self.workspace.pk]),
            reverse("checklists:edit", args=[self.checklist.pk]),
            reverse("checklists:item_edit", args=[self.item.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertPaint(response)
                self.assertContains(response, 'class="btn" type="submit"')
        self.assertContains(
            self.client.get(reverse("checklists:create", args=[self.workspace.pk])),
            "Halloween party, Sat 31 Oct 2026",
        )


class AccountPageTests(FamilyMixin, TestCase):
    def test_profile_settings_open_in_place(self):
        self.client.force_login(self.ana)
        response = self.client.get(reverse("accounts:profile"))
        self.assertPaint(response)
        self.assertContains(response, "Your account")
        self.assertContains(response, 'name="action" value="username"')
        self.assertContains(response, 'name="action" value="password"')
        self.assertContains(response, "Turn on two-step sign-in")
        self.assertContains(response, "parent and admin of Reyes family")

    def test_profile_reopens_the_setting_with_errors(self):
        self.client.force_login(self.mateo)
        response = self.client.post(
            reverse("accounts:profile"), {"action": "username", "username": "ANA"}
        )
        self.assertContains(response, '<details class="setting" open>')
        self.assertContains(response, "already taken")

    def test_two_step_setup_shows_the_steps_and_code(self):
        self.client.force_login(self.ana)
        response = self.client.get(reverse("accounts:two_step_setup"))
        self.assertPaint(response)
        self.assertContains(response, 'class="steps"')
        self.assertContains(response, 'class="qr"')
        self.assertContains(response, 'autocomplete="one-time-code"')


class SignInPageTests(FamilyMixin, TestCase):
    def test_sign_in_pages_share_the_paint_deck_layout(self):
        from accounts.services import invite_member, make_invite_token

        invite = invite_member(
            workspace=self.workspace,
            invited_by=self.ana,
            email="kid@example.com",
            role=Membership.Role.KID,
        )
        pages = {
            reverse("accounts:login"): 200,
            reverse("accounts:signup"): 200,
            reverse("accounts:accept_invite", args=[make_invite_token(invite)]): 200,
            reverse("accounts:accept_invite", args=["not-a-token"]): 400,
        }
        for url, status in pages.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status)
                self.assertContains(response, '<body class="paint"', status_code=status)
                self.assertContains(
                    response,
                    '<main id="main-content" class="auth">',
                    status_code=status,
                )
                self.assertContains(response, 'class="auth-deck"', status_code=status)
                self.assertNotContains(response, 'class="bar ui"', status_code=status)

    def test_invite_page_names_who_invited_and_the_username(self):
        from accounts.services import invite_member, make_invite_token

        invite = invite_member(
            workspace=self.workspace,
            invited_by=self.ana,
            email="kid@example.com",
            role=Membership.Role.KID,
        )
        response = self.client.get(
            reverse("accounts:accept_invite", args=[make_invite_token(invite)])
        )
        self.assertContains(response, "Join Reyes family")
        self.assertContains(response, "Ana</span> invited you as a kid")
        self.assertContains(response, invite.user.username)


class KidViewTests(FamilyMixin, TestCase):
    def test_kid_gets_the_paint_kid_view_in_their_colour(self):
        self.plan("Swim", timezone.now() + timedelta(days=1), 60, [self.lucia])
        self.plan("Book club", timezone.now() + timedelta(days=1), 60, [self.ana])
        self.client.force_login(self.lucia)
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertPaint(response)
        self.assertTemplateUsed(response, "accounts/kid_dashboard.html")
        self.assertContains(response, 'class="kid" data-hue="c"')
        self.assertContains(response, 'Hi <span translate="no">Lucía</span>')
        self.assertContains(response, 'class="kid-week"')
        self.assertContains(response, "Swim")
        self.assertNotContains(response, "Book club</b>")
        self.assertContains(response, "You</span>")
        self.assertNotContains(response, "Lucía!")
        self.assertContains(response, "Reyes family")
