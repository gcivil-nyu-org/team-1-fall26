"""Logic-abuse hardening tests (lane H-LOGIC, audit branch security/audit-2026-11-18).

Every test_poc_* test asserts the SECURE behaviour, so it FAILS while the problem
exists. test_control_* tests pin behaviour that is already correct, so a later fix
cannot regress it.

The proposed limits come from a real family of about 6 members (2 parents, 4 kids):
roughly 3 plans a day each at the very most, a few dozen checklists, a chat that
rarely goes beyond 100 messages a day. Each limit below leaves that family at least
five times its real use and stops one malicious member from making the pages
unusable.

Nothing leaves the process: Google, Groq, MTA and mail are never called.
"""

import asyncio
import json
import unicodedata
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from chat.tests.ws_refusal import connect_and_check
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event
from syncmily.asgi import application

PASSWORD = "Logic-Hardening-2026!"
BACKEND = "django.contrib.auth.backends.ModelBackend"
NY = ZoneInfo("America/New_York")

# Limits proposed in the report (see each test for the reason).
MAX_EVENTS_PER_DAY = 40
MAX_EVENTS_PER_WORKSPACE = 5000
MAX_CHECKLISTS_PER_WORKSPACE = 200
MAX_MEMBERS_PER_WORKSPACE = 12
MAX_EVENT_DAYS = 90
MAX_CHAT_PER_USER_PER_DAY = 600
MAX_SOCKETS_PER_USER = 6
MAX_LIST_ROWS_RENDERED = 100


def make_family(prefix="fam", kids=1, parents=1):
    """A primary admin, `parents` accepted extra parents and `kids` accepted kids."""
    User = get_user_model()
    admin = User.objects.create_user(
        username=f"{prefix}_admin",
        password=PASSWORD,
        email=f"{prefix}_admin@example.com",
    )
    workspace = Workspace.objects.create(name=f"{prefix} family", primary_admin=admin)
    people = {"admin": admin, "workspace": workspace, "parents": [], "kids": []}
    for index in range(parents):
        user = User.objects.create_user(
            username=f"{prefix}_parent{index}", password=PASSWORD
        )
        Membership.objects.create(
            workspace=workspace,
            user=user,
            role=Membership.Role.PARENT,
            invited_by=admin,
            accepted_at=timezone.now(),
        )
        people["parents"].append(user)
    for index in range(kids):
        user = User.objects.create_user(
            username=f"{prefix}_kid{index}", password=PASSWORD
        )
        Membership.objects.create(
            workspace=workspace,
            user=user,
            role=Membership.Role.KID,
            invited_by=admin,
            accepted_at=timezone.now(),
        )
        people["kids"].append(user)
    return people


def client_for(user):
    client = Client()
    client.force_login(user, backend=BACKEND)
    return client


def local_noon(day, tz=NY):
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)


def bulk_events(workspace, creator, count, day, *, hours=1, title="Seed"):
    """`count` plans on one local day, written straight to the table."""
    start = local_noon(day)
    Event.objects.bulk_create(
        Event(
            created_by=creator,
            workspace=workspace,
            title=f"{title} {index}",
            starts_at=start,
            ends_at=start + timedelta(hours=hours),
            timezone_name="America/New_York",
        )
        for index in range(count)
    )


def event_token(user, workspace):
    return signing.dumps(
        {"id": str(uuid4()), "user": user.pk, "workspace": str(workspace.pk)},
        salt="events.create",
    )


def event_post(user, workspace, **overrides):
    data = {
        "action": "save",
        "submission_token": event_token(user, workspace),
        "title": "Soccer",
        "date": "2027-03-01",
        "start_time": "10:00",
        "end_time": "11:00",
        "timezone": "America/New_York",
        "transport_mode": "transit",
    }
    data.update(overrides)
    return data


def post_event(user, workspace, **overrides):
    return client_for(user).post(
        reverse("events:workspace_create", args=[workspace.pk]),
        event_post(user, workspace, **overrides),
    )


# ---------------------------------------------------------------------------------
# (1) Unbounded creation
# ---------------------------------------------------------------------------------


class UnboundedCreationTests(TestCase):
    """Measured on a private server (see the report): 10,000 plans on one day made
    the day view 15 MB and 8.6 s, the dashboard 3 MB and 4 s; 20,000 checklists made
    the checklist page 11 MB and 1.7 s. Only a parent can add plans or checklists, and
    removing the parent keeps their plans, so the family cannot undo it."""

    @classmethod
    def setUpTestData(cls):
        cls.f = make_family("quota", kids=2, parents=1)
        cls.day = date(2027, 5, 10)

    def test_poc_LOGIC_events_per_day_capped(self):
        # A family of 6 plans at most ~18 things a day; 40 is more than twice that.
        parent = self.f["parents"][0]
        bulk_events(self.f["workspace"], parent, MAX_EVENTS_PER_DAY, self.day)
        before = Event.objects.count()
        response = post_event(
            parent, self.f["workspace"], date=self.day.isoformat(), title="One more"
        )
        self.assertEqual(
            Event.objects.count(),
            before,
            f"plan number {MAX_EVENTS_PER_DAY + 1} on one day was saved "
            f"(status {response.status_code})",
        )

    def test_poc_LOGIC_events_per_workspace_capped(self):
        # 6 people x 3 plans a week x 52 weeks x 3 years is under 3,000; 5,000 stored
        # plans is the ceiling, old ones can be archived.
        parent = self.f["parents"][0]
        start = date(2027, 6, 1)
        per_day = 25
        for offset in range(MAX_EVENTS_PER_WORKSPACE // per_day):
            bulk_events(
                self.f["workspace"], parent, per_day, start + timedelta(days=offset)
            )
        self.assertEqual(Event.objects.count(), MAX_EVENTS_PER_WORKSPACE)
        response = post_event(parent, self.f["workspace"], date="2030-01-01")
        self.assertEqual(
            Event.objects.count(),
            MAX_EVENTS_PER_WORKSPACE,
            f"stored plan {MAX_EVENTS_PER_WORKSPACE + 1} was accepted "
            f"(status {response.status_code})",
        )

    def test_poc_LOGIC_event_creation_has_a_rate_limit(self):
        # Measured: 15 plans/s from one session (56,000 an hour). A person adds a plan
        # every few seconds at most; 30 a minute stops scripts.
        parent = self.f["parents"][0]
        client = client_for(parent)
        url = reverse("events:workspace_create", args=[self.f["workspace"].pk])
        statuses = []
        with override_settings(RATELIMIT_ENABLED=True):
            cache.clear()
            for index in range(45):
                day = date(2028, 1, 1) + timedelta(days=index)
                statuses.append(
                    client.post(
                        url,
                        event_post(
                            parent,
                            self.f["workspace"],
                            date=day.isoformat(),
                            title=f"Spam {index}",
                        ),
                    ).status_code
                )
        self.assertIn(
            429, statuses, f"45 saves in a row never hit a limit: {set(statuses)}"
        )

    def test_poc_LOGIC_checklists_per_workspace_capped(self):
        # A family keeps a few lists per event; 200 is far above a real use.
        parent = self.f["parents"][0]
        workspace = self.f["workspace"]
        Checklist.objects.bulk_create(
            Checklist(workspace=workspace, created_by=parent, title=f"List {index}")
            for index in range(MAX_CHECKLISTS_PER_WORKSPACE)
        )
        response = client_for(parent).post(
            reverse("checklists:create", args=[workspace.pk]), {"title": "One more"}
        )
        self.assertEqual(
            Checklist.objects.filter(workspace=workspace).count(),
            MAX_CHECKLISTS_PER_WORKSPACE,
            f"checklist {MAX_CHECKLISTS_PER_WORKSPACE + 1} was accepted "
            f"(status {response.status_code})",
        )

    def test_poc_LOGIC_checklist_page_is_paginated(self):
        parent = self.f["parents"][0]
        workspace = self.f["workspace"]
        Checklist.objects.bulk_create(
            Checklist(workspace=workspace, created_by=parent, title=f"Row {index}")
            for index in range(300)
        )
        body = client_for(parent).get(reverse("checklists:list", args=[workspace.pk]))
        links = {
            part.split("/")[0]
            for part in body.content.decode().split("/checklists/")[1:]
            if len(part.split("/")[0]) == 36
        }
        self.assertLessEqual(
            len(links),
            MAX_LIST_ROWS_RENDERED,
            f"the checklist page rendered {len(links)} lists in one response",
        )

    def test_poc_LOGIC_day_view_caps_rendered_events(self):
        # Rows already stored (a Google import, a removed parent's plans) must not make
        # the day view unbounded. Measured: 10,000 plans -> 15 MB, 8.6 s.
        workspace = self.f["workspace"]
        bulk_events(workspace, self.f["admin"], 400, self.day, title="Crowd")
        response = client_for(self.f["admin"]).get(
            reverse("events:workspace_list", args=[workspace.pk]),
            {"date": self.day.isoformat(), "view": "day"},
        )
        self.assertEqual(response.status_code, 200)
        ids = set(
            Event.objects.filter(title__startswith="Crowd").values_list("pk", flat=True)
        )
        html = response.content.decode()
        shown = sum(1 for pk in ids if f"/events/{pk}/" in html)
        self.assertLessEqual(
            shown, MAX_LIST_ROWS_RENDERED, f"{shown} of 400 same-day plans rendered"
        )

    def test_poc_LOGIC_dashboard_caps_rendered_events(self):
        workspace = self.f["workspace"]
        today = timezone.localdate()
        bulk_events(workspace, self.f["admin"], 400, today, title="Crowd")
        response = client_for(self.f["admin"]).get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        ids = set(
            Event.objects.filter(title__startswith="Crowd").values_list("pk", flat=True)
        )
        html = response.content.decode()
        shown = sum(1 for pk in ids if f"/events/{pk}/" in html)
        self.assertLessEqual(
            shown, MAX_LIST_ROWS_RENDERED, f"{shown} of 400 same-day plans rendered"
        )

    def test_poc_LOGIC_members_per_workspace_capped(self):
        # A household of 6 plus grandparents and a sitter is 12 at the very most.
        # Pending invites count: today 60 invites a day pile up with no ceiling.
        admin = self.f["admin"]
        workspace = self.f["workspace"]
        User = get_user_model()
        current = workspace.memberships.count()
        for index in range(MAX_MEMBERS_PER_WORKSPACE - current):
            user = User.objects.create(username=f"Pending{index}", is_active=False)
            Membership.objects.create(
                workspace=workspace,
                user=user,
                role=Membership.Role.KID,
                invited_by=admin,
            )
        total = workspace.memberships.count()
        response = client_for(admin).post(
            reverse("accounts:manage_members", args=[workspace.pk]),
            {"email": "", "role": "kid"},
        )
        self.assertEqual(
            workspace.memberships.count(),
            total,
            f"member number {total + 1} was invited (status {response.status_code})",
        )

    def test_control_LOGIC_one_family_per_account(self):
        # Measured: a signed-in user cannot create a second workspace (and a kid
        # cannot create one at all).
        admin = self.f["admin"]
        client = client_for(admin)
        client.post(reverse("accounts:create_workspace"), {"name": "Second"})
        self.assertEqual(Workspace.objects.filter(primary_admin=admin).count(), 1)
        kid_client = client_for(self.f["kids"][0])
        kid_client.post(reverse("accounts:create_workspace"), {"name": "Kid land"})
        self.assertFalse(Workspace.objects.filter(name="Kid land").exists())

    def test_control_LOGIC_items_per_checklist_capped(self):
        parent = self.f["parents"][0]
        checklist = Checklist.objects.create(
            workspace=self.f["workspace"], created_by=parent, title="Packing"
        )
        ChecklistItem.objects.bulk_create(
            ChecklistItem(checklist=checklist, description=f"i{n}", position=n)
            for n in range(200)
        )
        client_for(parent).post(
            reverse("checklists:item_add", args=[checklist.pk]), {"description": "x"}
        )
        self.assertEqual(checklist.items.count(), 200)

    def test_control_LOGIC_one_calendar_connection_per_account(self):
        from calendars.models import CalendarConnection
        from django.db import IntegrityError, transaction

        admin = self.f["admin"]
        CalendarConnection.objects.create(
            user=admin, provider="google", workspace=self.f["workspace"]
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CalendarConnection.objects.create(user=admin, provider="google")

    def test_control_LOGIC_member_colours_bounded_by_members(self):
        # One colour row per (workspace, member): the unique constraint stops growth.
        from accounts.models import MemberColor

        workspace = self.f["workspace"]
        admin = self.f["admin"]
        MemberColor.objects.create(workspace=workspace, user=admin, color="#112233")
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberColor.objects.create(workspace=workspace, user=admin, color="#445566")


# ---------------------------------------------------------------------------------
# (2) State abuse
# ---------------------------------------------------------------------------------


def login_post(username, password=PASSWORD):
    """A real sign-in through the form (which normalizes the name like a browser)."""
    client = Client()
    response = client.post(
        reverse("accounts:login"), {"username": username, "password": password}
    )
    return client, response


def signed_in(client):
    return "_auth_user_id" in client.session


class StateAbuseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.f = make_family("state", kids=1, parents=1)
        cls.workspace = cls.f["workspace"]
        cls.admin = cls.f["admin"]
        cls.parent = cls.f["parents"][0]
        cls.kid = cls.f["kids"][0]

    def _event(self, **kw):
        start = local_noon(date(2027, 3, 1))
        values = dict(
            created_by=self.admin,
            workspace=self.workspace,
            title="Soccer",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        values.update(kw)
        return Event.objects.create(**values)

    def _edit_post(self, event, **overrides):
        data = {
            "action": "save",
            "title": event.title,
            "date": "2027-03-01",
            "start_time": "12:00",
            "end_time": "13:00",
            "timezone": "America/New_York",
            "location": event.location,
            "transport_mode": "transit",
            "allow_overlap": "on",
        }
        data.update(overrides)
        return data

    # --- membership -------------------------------------------------------------

    def test_control_LOGIC_primary_admin_cannot_be_removed_or_leave(self):
        admin_membership = Membership.objects.create(
            workspace=self.workspace,
            user=self.admin,
            role=Membership.Role.PARENT,
            invited_by=self.admin,
            accepted_at=timezone.now(),
        )
        url = reverse(
            "accounts:remove_member", args=[self.workspace.pk, admin_membership.pk]
        )
        client_for(self.admin).post(url)
        self.assertTrue(get_user_model().objects.filter(pk=self.admin.pk).exists())
        # A non-admin parent and a kid cannot remove anyone, the admin included.
        for member in (self.parent, self.kid):
            self.assertEqual(client_for(member).post(url).status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.admin.pk).exists())

    def test_control_LOGIC_kid_cannot_change_roles_or_remove_parents(self):
        parent_membership = Membership.objects.get(user=self.parent)
        client = client_for(self.kid)
        self.assertEqual(
            client.post(
                reverse(
                    "accounts:remove_member",
                    args=[self.workspace.pk, parent_membership.pk],
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                reverse("accounts:manage_members", args=[self.workspace.pk]),
                {"email": "x@example.com", "role": "parent"},
            ).status_code,
            403,
        )
        parent_membership.refresh_from_db()
        self.assertEqual(parent_membership.role, Membership.Role.PARENT)
        kid_membership = Membership.objects.get(user=self.kid)
        client.post(reverse("accounts:profile"), {"action": "role", "role": "parent"})
        kid_membership.refresh_from_db()
        self.assertEqual(kid_membership.role, Membership.Role.KID)

    def test_control_LOGIC_kid_cannot_touch_parents_events(self):
        event = self._event()
        client = client_for(self.kid)
        for name, data in (
            ("events:edit", self._edit_post(event, title="Hacked")),
            ("events:delete", {"action": "delete"}),
            ("events:set_transport_mode", {"transport_mode": "walking"}),
            (
                "events:change_time",
                {
                    "starts_date": "2027-03-02",
                    "starts_time": "10:00",
                    "ends_date": "2027-03-02",
                    "ends_time": "11:00",
                },
            ),
            ("events:detail", {"requires_transport": "on"}),
        ):
            response = client.post(reverse(name, args=[event.pk]), data)
            self.assertIn(response.status_code, (403, 404), name)
        event.refresh_from_db()
        self.assertEqual(event.title, "Soccer")
        self.assertEqual(event.transport_mode, "transit")

    def test_control_LOGIC_invite_dead_after_removal_and_after_use(self):
        from accounts.services import invite_member, make_invite_token

        membership = invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email="newkid@example.com",
            role="kid",
        )
        token = make_invite_token(membership)
        url = reverse("accounts:accept_invite", args=[token])
        # Use it once.
        client = Client()
        client.post(url, {"new_password1": PASSWORD, "new_password2": PASSWORD})
        membership.refresh_from_db()
        self.assertIsNotNone(membership.accepted_at)
        user = get_user_model().objects.get(pk=membership.user_id)
        before = user.password
        # Reusing the link must not set a new password.
        Client().post(
            url,
            {"new_password1": "Other-Pass-77!x", "new_password2": "Other-Pass-77!x"},
        )
        user.refresh_from_db()
        self.assertEqual(user.password, before)
        # A removed person's link is dead.
        second = invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email="gone@example.com",
            role="kid",
        )
        token2 = make_invite_token(second)
        client_for(self.admin).post(
            reverse("accounts:remove_member", args=[self.workspace.pk, second.pk])
        )
        response = Client().post(
            reverse("accounts:accept_invite", args=[token2]),
            {"new_password1": PASSWORD, "new_password2": PASSWORD},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            get_user_model().objects.filter(email="gone@example.com").exists()
        )

    def test_control_LOGIC_resend_kills_older_link(self):
        from accounts.services import invite_member, make_invite_token, reissue_invite

        membership = invite_member(
            workspace=self.workspace, invited_by=self.admin, email="", role="kid"
        )
        Membership.objects.filter(pk=membership.pk).update(
            invited_at=timezone.now() - timedelta(minutes=5)
        )
        membership.refresh_from_db()
        old = make_invite_token(membership)
        reissue_invite(membership)
        response = Client().get(reverse("accounts:accept_invite", args=[old]))
        self.assertEqual(response.status_code, 400)

    def test_poc_LOGIC_invite_refuses_email_that_already_has_an_account(self):
        # Sign-up says "An account with this email already exists", but an invite to
        # the same address creates a second account (in this family) and sends it the
        # invite mail. The admin holds the link, so the admin can finish it.
        other = make_family("elsewhere", kids=0, parents=0)
        victim = other["admin"]
        mail.outbox.clear()
        client_for(self.admin).post(
            reverse("accounts:manage_members", args=[self.workspace.pk]),
            {"email": victim.email, "role": "kid"},
        )
        self.assertEqual(
            get_user_model().objects.filter(email__iexact=victim.email).count(),
            1,
            "a second account now holds the same email address",
        )
        self.assertEqual(
            len(mail.outbox), 0, "mail went to an address that has an account"
        )

    # --- concurrent edits -------------------------------------------------------

    def test_poc_LOGIC_event_edit_lost_update(self):
        # Two parents open the same event. A fixes the title, B (whose form still has
        # the old title) fixes the place. B's save puts the old title back.
        event = self._event()
        parent_b = get_user_model().objects.create_user(
            username="state_parent_b", password=PASSWORD
        )
        Membership.objects.create(
            workspace=self.workspace,
            user=parent_b,
            role=Membership.Role.PARENT,
            invited_by=self.admin,
            accepted_at=timezone.now(),
        )
        stale_for_b = self._edit_post(event, location="Field 9")  # still says "Soccer"
        client_for(self.parent).post(
            reverse("events:edit", args=[event.pk]),
            self._edit_post(event, title="Soccer practice (moved)"),
        )
        client_for(parent_b).post(reverse("events:edit", args=[event.pk]), stale_for_b)
        event.refresh_from_db()
        self.assertEqual(
            event.title,
            "Soccer practice (moved)",
            "the second save silently overwrote the first parent's title",
        )

    def test_poc_LOGIC_checklist_edit_lost_update(self):
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.admin, title="Packing"
        )
        stale = {"title": "Packing", "event": ""}
        client_for(self.parent).post(
            reverse("checklists:edit", args=[checklist.pk]),
            {"title": "Packing for camp", "event": ""},
        )
        client_for(self.admin).post(
            reverse("checklists:edit", args=[checklist.pk]), stale
        )
        checklist.refresh_from_db()
        self.assertEqual(checklist.title, "Packing for camp")

    def test_control_LOGIC_event_create_double_submit_makes_one_event(self):
        client = client_for(self.parent)
        data = event_post(self.parent, self.workspace, title="Once")
        url = reverse("events:workspace_create", args=[self.workspace.pk])
        client.post(url, data)
        client.post(url, data)
        self.assertEqual(Event.objects.filter(title="Once").count(), 1)

    def test_control_LOGIC_checklist_toggle_is_idempotent(self):
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.admin, title="Bag"
        )
        item = ChecklistItem.objects.create(checklist=checklist, description="Water")
        client = client_for(self.kid)
        url = reverse("checklists:item_toggle", args=[item.pk])
        client.post(url, {"completed": "1"})
        client.post(url, {"completed": "1"})
        item.refresh_from_db()
        self.assertTrue(item.is_completed)

    # --- names ------------------------------------------------------------------

    def _rename(self, user, new_name):
        client = client_for(user)
        response = client.post(
            reverse("accounts:profile"), {"action": "username", "username": new_name}
        )
        user.refresh_from_db()
        return response

    def test_control_LOGIC_rename_to_existing_name_refused(self):
        for name in (
            self.parent.username,
            self.parent.username.upper(),
            self.parent.username + " ",
        ):
            before = self.kid.username
            self._rename(self.kid, name)
            self.assertEqual(self.kid.username, before, name)

    def test_control_LOGIC_rename_to_compatibility_form_still_signs_in(self):
        # The model's clean() stores the NFKC form, so sign-in finds the account.
        self._rename(self.kid, "ｋｉｄ９９")
        client, _response = login_post(self.kid.username)
        self.assertTrue(signed_in(client))

    def test_poc_LOGIC_rename_cannot_dodge_reserved_names(self):
        # The reserved-name check runs on the typed text; the model then stores the
        # NFKC form, so fullwidth letters become "admin", "Parent2" and "support".
        from accounts.forms import RESERVED_USERNAME

        for name in ("ａｄｍｉｎ", "Ｐａｒｅｎｔ２", "ｓｕｐｐｏｒｔ"):
            with self.subTest(name):
                self._rename(self.kid, name)
                stored = self.kid.username
                self.assertIsNone(
                    RESERVED_USERNAME.match(unicodedata.normalize("NFKC", stored)),
                    f"{name!r} became the username {stored!r}",
                )
                self.kid.username = "state_kid0"
                self.kid.save(update_fields=["username"])

    def test_poc_LOGIC_rename_cannot_duplicate_a_member_in_another_case_via_fullwidth(
        self,
    ):
        # The case-insensitive check runs on the typed text; the stored name is the
        # NFKC form, so fullwidth capitals become a second "STATE_PARENT0".
        target = self.parent.username  # "state_parent0"
        typed = "".join(
            chr(ord(c.upper()) + 0xFEE0) if "a" <= c <= "z" else c for c in target
        )
        self._rename(self.kid, typed)
        self.assertNotEqual(
            self.kid.username.lower(), target.lower(), "kid now shows as the parent"
        )

    def test_poc_LOGIC_rename_cannot_copy_a_member_with_cyrillic_letters(self):
        # Mom with a Cyrillic o (UTS #39 confusable).
        momma = get_user_model().objects.create_user(username="Mom", password=PASSWORD)
        Membership.objects.create(
            workspace=self.workspace,
            user=momma,
            role=Membership.Role.PARENT,
            invited_by=self.admin,
            accepted_at=timezone.now(),
        )
        fake = "Mоm"  # Cyrillic small o
        self.assertNotEqual(fake, "Mom")
        self._rename(self.kid, fake)
        self.assertNotEqual(self.kid.username, fake, "the kid is now a second 'Mom'")

    def test_poc_LOGIC_invisible_username_refused(self):
        for name in ("ㅤㅤ", "⠀⠀", "ᅟᅠ", "ﾠﾠ"):
            before = self.kid.username
            self._rename(self.kid, name)
            self.assertEqual(
                self.kid.username, before, f"{name!r} accepted as a username"
            )

    def test_poc_LOGIC_signup_cannot_copy_a_member_with_cyrillic_letters(self):
        # The sign-up path has the same gap (new accounts can then be invited by
        # nobody, but they appear in chat only after joining; kept as the same rule).
        momma = get_user_model().objects.create_user(username="Mom2", password=PASSWORD)
        self.assertTrue(momma.pk)
        response = Client().post(
            reverse("accounts:signup"),
            {
                "username": "Mоm2",
                "email": "fake-mom@example.com",
                "password1": PASSWORD + "z",
                "password2": PASSWORD + "z",
            },
        )
        self.assertFalse(
            get_user_model().objects.filter(username="Mоm2").exists(),
            f"sign-up accepted a lookalike of an existing username ({response.status_code})",
        )

    def test_poc_LOGIC_rename_rewrites_old_chat_attribution(self):
        # Chat shows the sender's CURRENT username for every old message, so renaming
        # re-labels history.
        ChatMessage.objects.create(
            workspace=self.workspace, sender=self.kid, content="I broke the lamp"
        )
        old_name = self.kid.username
        self._rename(self.kid, "NotMe")
        response = client_for(self.admin).get(
            reverse("chat:messages", args=[self.workspace.pk])
        )
        senders = {m["sender"] for m in response.json()["messages"]}
        self.assertIn(old_name, senders, f"history now says {senders}")

    def test_poc_LOGIC_rename_is_rate_limited(self):
        names = [f"Alias{index}" for index in range(8)]
        client = client_for(self.kid)
        with override_settings(RATELIMIT_ENABLED=True):
            cache.clear()
            for name in names:
                client.post(
                    reverse("accounts:profile"),
                    {"action": "username", "username": name},
                )
        self.kid.refresh_from_db()
        self.assertNotEqual(
            self.kid.username, names[-1], "8 renames in a row all went through"
        )

    def test_control_LOGIC_signup_normalizes_names(self):
        # Sign-up uses UsernameField (NFKC), so a fullwidth name is stored as ASCII.
        Client().post(
            reverse("accounts:signup"),
            {
                "username": "ｆｕｌｌｗｉｄｔｈ１",
                "email": "fw@example.com",
                "password1": PASSWORD + "q",
                "password2": PASSWORD + "q",
            },
        )
        self.assertTrue(get_user_model().objects.filter(username="fullwidth1").exists())


# ---------------------------------------------------------------------------------
# (3) Value abuse
# ---------------------------------------------------------------------------------

# Characters that draw nothing but are not whitespace or "format" characters, so
# clean_visible_text keeps them: Hangul fillers, Braille blank, combining grapheme
# joiner, Khmer inherent vowels, variation selectors, Mongolian free variation
# selectors, invisible math operators are Cf (already stripped) and listed as control.
BLANK_BUT_ALLOWED = {
    "HANGUL FILLER U+3164": "ㅤ",
    "HANGUL CHOSEONG FILLER U+115F": "ᅟ",
    "HANGUL JUNGSEONG FILLER U+1160": "ᅠ",
    "HALFWIDTH HANGUL FILLER U+FFA0": "ﾠ",
    "BRAILLE PATTERN BLANK U+2800": "⠀",
    "COMBINING GRAPHEME JOINER U+034F": "͏",
    "KHMER VOWEL INHERENT AQ U+17B4": "឴",
    "KHMER VOWEL INHERENT AA U+17B5": "឵",
    "VARIATION SELECTOR-16 U+FE0F": "️",
    "MONGOLIAN FREE VARIATION SELECTOR U+180B": "᠋",
}
BLANK_ALREADY_REFUSED = {
    "empty": "",
    "spaces": "   ",
    "tab and newline": "\t\n",
    "ZERO WIDTH SPACE": "​",
    "ZERO WIDTH JOINER only": "‍",
    "RIGHT-TO-LEFT OVERRIDE": "‮",
    "BOM": "﻿",
    "WORD JOINER": "⁠",
    "NO-BREAK SPACE": " ",
    "SOFT HYPHEN": "­",
    "INVISIBLE SEPARATOR": "⁣",
}


class ValueAbuseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.f = make_family("value", kids=1, parents=1)
        cls.workspace = cls.f["workspace"]
        cls.admin = cls.f["admin"]
        cls.parent = cls.f["parents"][0]
        cls.kid = cls.f["kids"][0]

    def _saved(self, response):
        return response.status_code == 302 and "/events/" in response["Location"]

    def _event(self, start=None, hours=1):
        start = start or local_noon(date(2027, 3, 1))
        return Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Soccer",
            starts_at=start,
            ends_at=start + timedelta(hours=hours),
            timezone_name="America/New_York",
        )

    # --- dates ------------------------------------------------------------------

    def test_control_LOGIC_create_form_bounds_dates_to_1900_2100(self):
        for bad in ("0001-01-01", "1899-12-31", "2101-01-01", "9999-12-31"):
            before = Event.objects.count()
            response = post_event(self.parent, self.workspace, date=bad)
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(Event.objects.count(), before, bad)

    def test_poc_LOGIC_change_time_has_no_date_bounds(self):
        # The "change time" box on the event page skips the 1900-2100 rule of the form.
        for label, day in (("year 1", "0001-06-01"), ("year 9999", "9999-06-01")):
            with self.subTest(label):
                event = self._event()
                before = event.starts_at
                client_for(self.parent).post(
                    reverse("events:change_time", args=[event.pk]),
                    {
                        "starts_date": day,
                        "starts_time": "10:00",
                        "ends_date": day,
                        "ends_time": "11:00",
                    },
                )
                event.refresh_from_db()
                self.assertEqual(
                    event.starts_at,
                    before,
                    f"{label}: the event was moved to {event.starts_at}",
                )

    def test_poc_LOGIC_event_duration_is_capped(self):
        # A 73,000-day plan (1900-01-01 to 2100-12-31) is a valid form today and shows
        # in every day and week for 200 years. The longest real block is a summer
        # break, about 12 weeks; 90 days leaves room.
        response = post_event(
            self.parent,
            self.workspace,
            date="1900-01-01",
            end_date="2100-12-31",
            title="Forever",
        )
        self.assertFalse(
            Event.objects.filter(title="Forever").exists(),
            f"a {73000}-day event was saved (status {response.status_code})",
        )
        response = post_event(
            self.parent,
            self.workspace,
            date="2027-01-01",
            end_date=(
                date(2027, 1, 1) + timedelta(days=MAX_EVENT_DAYS + 30)
            ).isoformat(),
            title="Long",
        )
        self.assertFalse(
            Event.objects.filter(title="Long").exists(),
            f"a {MAX_EVENT_DAYS + 30}-day event was saved",
        )

    def test_poc_LOGIC_change_time_cannot_stretch_an_event_past_the_cap(self):
        event = self._event()
        before = (event.starts_at, event.ends_at)
        client_for(self.parent).post(
            reverse("events:change_time", args=[event.pk]),
            {
                "starts_date": "2027-03-01",
                "starts_time": "10:00",
                "ends_date": "2100-01-01",
                "ends_time": "11:00",
            },
        )
        event.refresh_from_db()
        self.assertEqual((event.starts_at, event.ends_at), before)

    def test_control_LOGIC_event_duration_up_to_a_month_is_fine(self):
        response = post_event(
            self.parent,
            self.workspace,
            date="2027-07-01",
            end_date="2027-07-30",
            title="Camp",
        )
        self.assertTrue(
            Event.objects.filter(title="Camp").exists(), response.status_code
        )

    def test_control_LOGIC_end_before_start_and_zero_length_refused(self):
        for title, kwargs in (
            ("EndBefore", {"date": "2027-03-02", "end_date": "2027-03-01"}),
            (
                "Zero",
                {"start_time": "10:00", "end_time": "10:00", "end_date": "2027-03-01"},
            ),
        ):
            response = post_event(self.parent, self.workspace, title=title, **kwargs)
            self.assertEqual(response.status_code, 200, title)
            self.assertFalse(Event.objects.filter(title=title).exists(), title)

    def test_control_LOGIC_end_time_before_start_time_runs_past_midnight(self):
        post_event(
            self.parent,
            self.workspace,
            title="Late",
            start_time="22:00",
            end_time="01:00",
        )
        event = Event.objects.get(title="Late")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=3))

    # --- DST and time zones -------------------------------------------------------

    def test_control_LOGIC_spring_forward_gap_is_an_error_not_a_crash(self):
        response = post_event(
            self.parent,
            self.workspace,
            title="Gap",
            date="2027-03-14",
            start_time="02:30",
            end_time="03:30",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "doesn")  # "2:30 AM doesn't exist ..."
        self.assertFalse(Event.objects.filter(title="Gap").exists())
        # The end time can fall in the gap too.
        response = post_event(
            self.parent,
            self.workspace,
            title="GapEnd",
            date="2027-03-14",
            start_time="01:00",
            end_time="02:30",
        )
        self.assertFalse(Event.objects.filter(title="GapEnd").exists())

    def test_control_LOGIC_fall_back_overlap_uses_first_occurrence(self):
        post_event(
            self.parent,
            self.workspace,
            title="Fold",
            date="2026-11-01",
            start_time="01:30",
            end_time="03:00",
        )
        event = Event.objects.get(title="Fold")
        # 01:30 EDT is 05:30 UTC; 03:00 EST is 08:00 UTC: 2.5 hours, not 1.5.
        self.assertEqual(event.starts_at.isoformat(), "2026-11-01T05:30:00+00:00")
        self.assertEqual(
            event.ends_at - event.starts_at, timedelta(hours=2, minutes=30)
        )

    def test_control_LOGIC_event_across_dst_keeps_real_duration(self):
        post_event(
            self.parent,
            self.workspace,
            title="Overnight",
            date="2027-03-13",
            start_time="20:00",
            end_date="2027-03-14",
            end_time="08:00",
        )
        event = Event.objects.get(title="Overnight")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=11))

    def test_control_LOGIC_invalid_time_zones_are_form_errors(self):
        for bad in (
            "Mars/Olympus",
            "../../etc/passwd",
            "UTC\x00",
            "America/New_York ",
            "EST5EDTX",
            "x" * 500,
        ):
            before = Event.objects.count()
            client = client_for(self.parent)
            client.raise_request_exception = False
            response = client.post(
                reverse("events:workspace_create", args=[self.workspace.pk]),
                event_post(self.parent, self.workspace, timezone=bad),
            )
            self.assertEqual(response.status_code, 200, repr(bad))
            self.assertEqual(Event.objects.count(), before)
        for query in (
            "timezone=Mars/Olympus",
            "timezone=" + "x" * 5000,
            "date=0001-01-01&view=week",
            "date=9999-12-31&view=week",
            "date=2027-02-30",
            "date=not-a-date&view=day",
            "view=nonsense",
            "page=99999999999999999999",
            "page=-3",
        ):
            client = client_for(self.admin)
            client.raise_request_exception = False
            response = client.get(
                reverse("events:workspace_list", args=[self.workspace.pk]) + "?" + query
            )
            self.assertEqual(response.status_code, 200, query)
        for query in (
            "week=0001-01-01",
            "week=9999-12-31",
            "week=abc",
            "week=2027-02-30",
        ):
            response = client_for(self.admin).get(
                reverse("accounts:dashboard") + "?" + query
            )
            self.assertEqual(response.status_code, 200, query)

    # --- numbers ---------------------------------------------------------------

    def test_control_LOGIC_coordinates_out_of_range_or_not_finite_refused(self):
        for lat, lng in (
            ("91", "0"),
            ("-91", "0"),
            ("0", "181"),
            ("0", "-181"),
            ("nan", "0"),
            ("inf", "0"),
            ("0", "-inf"),
            ("1e999", "0"),
            ("abc", "1"),
        ):
            before = Event.objects.count()
            response = post_event(
                self.parent,
                self.workspace,
                title="Pin",
                location="Somewhere",
                latitude=lat,
                longitude=lng,
            )
            self.assertEqual(response.status_code, 200, (lat, lng))
            self.assertEqual(Event.objects.count(), before, (lat, lng))
        # Home address the same way.
        for lat, lng in (("100", "0"), ("nan", "0"), ("0", "inf")):
            client_for(self.parent).post(
                reverse("accounts:family_home", args=[self.workspace.pk]),
                {"location": "Home", "latitude": lat, "longitude": lng},
            )
            self.workspace.refresh_from_db()
            self.assertIsNone(self.workspace.home_latitude, (lat, lng))

    def test_control_LOGIC_trip_start_coordinates_checked(self):
        event = self._event()
        client = client_for(self.kid)
        for lat, lng in (("91", "0"), ("nan", "1"), ("0", "inf"), ("x", "1")):
            response = client.post(
                reverse("events:trip", args=[event.pk]),
                {"mode": "here", "latitude": lat, "longitude": lng},
            )
            self.assertEqual(response.status_code, 400, (lat, lng))

    def test_control_LOGIC_huge_numbers_in_urls_and_query_do_not_crash(self):
        self._event()
        huge = 10**40
        client = client_for(self.admin)
        client.raise_request_exception = False
        for url in (
            f"/events/{huge}/",
            f"/events/{huge}/edit/",
            f"/checklists/items/{huge}/edit/",
            f"/accounts/workspaces/{self.workspace.pk}/members/{huge}/resend/",
            reverse("chat:messages", args=[self.workspace.pk]) + f"?before={huge}",
            reverse("chat:messages", args=[self.workspace.pk]) + f"?after={huge}",
            reverse("chat:messages", args=[self.workspace.pk]) + f"?before=-{huge}",
            reverse("chat:messages", args=[self.workspace.pk])
            + "?before="
            + "9" * 5000,
        ):
            response = client.get(url)
            self.assertLess(response.status_code, 500, url[:80])
        response = client.post(
            f"/accounts/workspaces/{self.workspace.pk}/members/{huge}/remove/"
        )
        self.assertLess(response.status_code, 500)

    def test_poc_LOGIC_nul_byte_in_trip_label_does_not_crash(self):
        # PostgreSQL text cannot hold NUL; the label is saved without the form checks
        # that reject NUL elsewhere.
        event = self._event()
        client = client_for(self.kid)
        client.raise_request_exception = False
        response = client.post(
            reverse("events:trip", args=[event.pk]),
            {
                "mode": "here",
                "latitude": "40.7",
                "longitude": "-74.0",
                "label": "a\x00b",
            },
        )
        self.assertLess(
            response.status_code, 500, "NUL in the label gave a server error"
        )

    # --- names ---------------------------------------------------------------

    def _title_saved(self, title):
        Event.objects.filter(title__startswith="NAME").delete()
        response = post_event(self.parent, self.workspace, title=title)
        return self._saved(response)

    def test_control_LOGIC_blank_names_refused_everywhere(self):
        parent = client_for(self.parent)
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.parent, title="Packing"
        )
        for label, text in BLANK_ALREADY_REFUSED.items():
            self.assertFalse(self._title_saved(text), f"event title {label}")
            before = Checklist.objects.count()
            parent.post(
                reverse("checklists:create", args=[self.workspace.pk]), {"title": text}
            )
            self.assertEqual(Checklist.objects.count(), before, f"checklist {label}")
            parent.post(
                reverse("checklists:item_add", args=[checklist.pk]),
                {"description": text},
            )
            self.assertEqual(checklist.items.count(), 0, f"item {label}")
            client_for(self.admin).post(
                reverse("accounts:create_workspace"), {"name": text}
            )
            self.assertEqual(
                Workspace.objects.filter(primary_admin=self.admin).count(),
                1,
                f"workspace {label}",
            )

    def test_poc_LOGIC_blank_looking_names_accepted_in_events(self):
        accepted = [
            label
            for label, text in BLANK_BUT_ALLOWED.items()
            if self._title_saved(text * 3)
        ]
        self.assertEqual(accepted, [], "an event title that draws nothing was saved")

    def test_poc_LOGIC_blank_looking_names_accepted_in_checklists_and_items(self):
        parent = client_for(self.parent)
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.parent, title="Packing"
        )
        accepted = []
        for label, text in BLANK_BUT_ALLOWED.items():
            before = Checklist.objects.count()
            parent.post(
                reverse("checklists:create", args=[self.workspace.pk]),
                {"title": text * 3},
            )
            if Checklist.objects.count() > before:
                accepted.append(f"list:{label}")
            before = checklist.items.count()
            parent.post(
                reverse("checklists:item_add", args=[checklist.pk]),
                {"description": text * 3},
            )
            if checklist.items.count() > before:
                accepted.append(f"item:{label}")
        self.assertEqual(accepted, [])

    def test_poc_LOGIC_blank_looking_workspace_name_accepted(self):
        newcomer = get_user_model().objects.create_user(
            username="value_new", password=PASSWORD
        )
        client_for(newcomer).post(
            reverse("accounts:create_workspace"), {"name": "ㅤㅤ"}
        )
        self.assertFalse(
            Workspace.objects.filter(primary_admin=newcomer).exists(),
            "a family called U+3164 was created",
        )

    def test_poc_LOGIC_combining_mark_flood_accepted(self):
        # "Zalgo" text: one letter with 150 stacked accents draws over the lines above
        # and below in every lane and list.
        zalgo = "a" + "́" * 150
        self.assertFalse(
            self._title_saved(zalgo), "150 stacked combining marks were saved"
        )
        nepali = "क्षि"  # a normal conjunct stays valid
        self.assertTrue(self._title_saved(nepali))

    def test_control_LOGIC_emoji_only_and_plain_names_are_fine(self):
        for title in (
            "\U0001f600",
            "\U0001f468‍\U0001f469‍\U0001f467",
            "\U0001f1fa\U0001f1f8",
            "Café",
        ):
            self.assertTrue(self._title_saved(title), repr(title))

    def test_poc_LOGIC_invisible_only_location_saved(self):
        response = post_event(self.parent, self.workspace, title="Place", location="​​")
        if self._saved(response):
            event = Event.objects.get(title="Place")
            self.assertEqual(
                event.location, "", "location is only zero-width characters"
            )
            self.assertFalse(event.has_directions)

    def test_poc_LOGIC_trip_label_zero_width_only_saved(self):
        from events.models import TripStart

        event = self._event()
        client_for(self.kid).post(
            reverse("events:trip", args=[event.pk]),
            {"mode": "here", "latitude": "40.7", "longitude": "-74.0", "label": "ㅤㅤ"},
        )
        start = TripStart.objects.filter(event=event).first()
        self.assertTrue(
            start is None or start.label == "Your current location",
            start and start.label,
        )


# ---------------------------------------------------------------------------------
# Google Calendar import (mocked: no call leaves the process)
# ---------------------------------------------------------------------------------


class GoogleImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.f = make_family("gimp", kids=0, parents=0)
        from calendars.models import CalendarConnection

        cls.connection = CalendarConnection.objects.create(
            user=cls.f["admin"],
            workspace=cls.f["workspace"],
            provider="google",
            account_email="a@example.com",
            access_token="a",
            refresh_token="r",
        )

    def _items(self, count):
        start = timezone.now() + timedelta(days=2)
        return [
            {
                "id": f"id{index}",
                "summary": f"G {index}",
                "start": {"dateTime": (start + timedelta(minutes=index)).isoformat()},
                "end": {
                    "dateTime": (
                        start + timedelta(minutes=index, seconds=30)
                    ).isoformat()
                },
            }
            for index in range(count)
        ], "UTC"

    def test_poc_LOGIC_import_has_a_ceiling(self):
        from calendars import sync

        with mock.patch.object(
            sync, "_fetch_items", return_value=self._items(2600)
        ), mock.patch.object(sync, "_service"), mock.patch.object(
            sync, "_credentials"
        ) as creds:
            creds.return_value.token = "a"
            sync.sync_connection(self.connection)
        self.assertLessEqual(
            Event.objects.filter(source="google").count(),
            2000,
            "all 2,600 imported (a 210-day window: 2,000 is ~10 a day)",
        )

    def test_poc_LOGIC_manual_import_has_a_cooldown(self):
        calls = []
        client = client_for(self.f["admin"])
        with override_settings(RATELIMIT_ENABLED=True), mock.patch(
            "calendars.views.sync_connection",
            side_effect=lambda *a, **k: calls.append(1),
        ):
            cache.clear()
            for _ in range(15):
                client.post(reverse("calendars:google_sync"))
        self.assertLessEqual(
            len(calls),
            6,
            f"{len(calls)} Google API imports in a minute from one button",
        )


# ---------------------------------------------------------------------------------
# Chat socket (real sessions, as in the websocket lane)
# ---------------------------------------------------------------------------------

GOOD_ORIGIN = b"http://localhost:8000"


def socket_for(workspace, user):
    client = client_for(user)
    cookie = client.cookies["sessionid"].value
    headers = [
        (b"host", b"localhost"),
        (b"origin", GOOD_ORIGIN),
        (b"cookie", f"sessionid={cookie}".encode()),
    ]
    return WebsocketCommunicator(
        application, f"/ws/chat/{workspace.id}/", headers=headers
    )


async def next_output(communicator, timeout=2):
    try:
        message = await asyncio.wait_for(communicator.output_queue.get(), timeout)
    except asyncio.TimeoutError:
        return None
    if isinstance(message, Exception):
        return message
    return message


async def send_and_read(communicator, payload, timeout=2):
    """Send a frame; return ('json', dict) / ('closed', code) / ('crash', exc) / ('none', None)."""
    if isinstance(payload, str):
        await communicator.send_to(text_data=payload)
    else:
        await communicator.send_json_to(payload)
    message = await next_output(communicator, timeout)
    if message is None:
        return "none", None
    if isinstance(message, Exception):
        return "crash", message
    if message.get("type") == "websocket.close":
        return "closed", message.get("code")
    if message.get("type") == "websocket.send":
        return "json", json.loads(message["text"])
    return "other", message


class ChatValueTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.f = make_family("chatv", kids=1, parents=0)

    def _run(self, payload, user=None):
        comm = socket_for(self.f["workspace"], user or self.f["kids"][0])

        async def go():
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            try:
                result = await send_and_read(comm, payload)
            except Exception as exc:  # the application died while we were talking
                return "crash", exc
            try:
                await comm.disconnect()
            except Exception as exc:
                return "crash", exc
            return result

        return async_to_sync(go)()

    def _says(self, content, **extra):
        return {"type": "chat_message", "content": content, **extra}

    def test_poc_LOGIC_chat_nul_byte_does_not_crash_the_socket(self):
        kind, value = self._run(self._says("hi\x00there"))
        self.assertNotEqual(
            kind, "crash", f"NUL in a chat message crashed the consumer: {value!r}"
        )
        # Postgres refuses NUL in a query parameter too, so check the stored rows in
        # Python instead of with content__contains="\x00".
        self.assertFalse(
            any(
                "\x00" in c
                for c in ChatMessage.objects.values_list("content", flat=True)
            )
        )

    def test_poc_LOGIC_chat_lone_surrogate_does_not_crash_the_socket(self):
        kind, value = self._run('{"type":"chat_message","content":"hi \\ud800 there"}')
        self.assertNotEqual(
            kind, "crash", f"a lone surrogate crashed the consumer: {value!r}"
        )

    def test_poc_LOGIC_chat_event_label_nul_does_not_crash_the_socket(self):
        kind, value = self._run(self._says("hello", event_label="a\x00b"))
        self.assertNotEqual(
            kind, "crash", f"NUL in the event label crashed the consumer: {value!r}"
        )

    def test_poc_LOGIC_chat_invisible_only_message_refused(self):
        refused_already = ("​​", "‮", "﻿", "   ", "⁠")
        for text in refused_already:
            kind, value = self._run(self._says(text))
            self.assertEqual(
                value.get("type") if kind == "json" else kind, "error", repr(text)
            )
        accepted = []
        for label, text in BLANK_BUT_ALLOWED.items():
            kind, value = self._run(self._says(text * 3))
            if kind == "json" and value.get("type") == "chat_message":
                accepted.append(label)
        self.assertEqual(accepted, [], "chat accepted messages that draw nothing")

    def test_poc_LOGIC_chat_has_a_daily_ceiling(self):
        # Per hour the limit is 300 (control below). That is 7,200 messages and 14 MB a
        # day per account, forever. A chatty family sends ~100 a day.
        user = self.f["kids"][0]
        comm = socket_for(self.f["workspace"], user)

        async def go():
            await comm.connect()
            errors = 0
            sent = MAX_CHAT_PER_USER_PER_DAY + 80
            with override_settings(RATELIMIT_ENABLED=True):
                for index in range(sent):
                    # Reset the short windows only; a daily counter is not touched.
                    cache.delete(f"rl:chat_burst:{user.pk}:10")
                    cache.delete(f"rl:chat_hour:{user.pk}:3600")
                    kind, value = await send_and_read(comm, self._says(f"m{index}"), 3)
                    if kind == "json" and value.get("type") == "error":
                        errors += 1
            await comm.disconnect()
            return errors

        errors = async_to_sync(go)()
        self.assertGreater(errors, 0, "700 messages in a row were all accepted")

    def test_control_LOGIC_chat_has_hourly_and_burst_limits(self):
        user = self.f["kids"][0]
        comm = socket_for(self.f["workspace"], user)

        async def go():
            await comm.connect()
            seen = []
            with override_settings(RATELIMIT_ENABLED=True):
                for index in range(30):
                    kind, value = await send_and_read(comm, self._says(f"b{index}"), 3)
                    seen.append(value.get("type") if kind == "json" else kind)
            await comm.disconnect()
            return seen

        seen = async_to_sync(go)()
        self.assertIn("error", seen)
        self.assertEqual(seen.count("chat_message"), 20)

    def test_poc_LOGIC_one_account_can_open_unlimited_sockets(self):
        user = self.f["kids"][0]
        comms = [
            socket_for(self.f["workspace"], user)
            for _ in range(MAX_SOCKETS_PER_USER + 10)
        ]

        async def go():
            sockets, accepted = [], 0
            for comm in comms:
                connected, _ = await connect_and_check(comm)
                if connected:
                    accepted += 1
                    sockets.append(comm)
            for comm in sockets:
                await comm.disconnect()
            return accepted

        accepted = async_to_sync(go)()
        self.assertLessEqual(
            accepted,
            MAX_SOCKETS_PER_USER,
            f"{accepted} sockets open at once for one account (a person has a phone, "
            "a laptop and a tablet)",
        )


# ---------------------------------------------------------------------------------
# More value and state abuse found while reading the forms
# ---------------------------------------------------------------------------------


def long_email(length):
    """A valid address (Django's validator accepts up to 320 characters) of `length`."""
    domain = ".".join(["d" * 60] * 3) + ".com"
    local = "u" * (length - len(domain) - 1)
    return f"{local}@{domain}"


class LengthAndPrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.f = make_family("len", kids=2, parents=1)

    def test_control_LOGIC_long_email_does_not_crash_invite(self):
        # The form's EmailField has no max_length, the column holds 254.
        email = long_email(290)
        self.assertGreater(len(email), 254)
        client = client_for(self.f["admin"])
        client.raise_request_exception = False
        response = client.post(
            reverse("accounts:manage_members", args=[self.f["workspace"].pk]),
            {"email": email, "role": "parent"},
        )
        self.assertLess(
            response.status_code, 500, "a 290-character email gave a server error"
        )

    def test_control_LOGIC_long_email_does_not_crash_signup(self):
        client = Client(raise_request_exception=False)
        response = client.post(
            reverse("accounts:signup"),
            {
                "username": "longmail",
                "email": long_email(290),
                "password1": PASSWORD + "w",
                "password2": PASSWORD + "w",
            },
        )
        self.assertLess(
            response.status_code, 500, "a 290-character email gave a server error"
        )

    def test_poc_LOGIC_removing_a_member_must_not_publish_their_private_plans(self):
        # A plan for one kid (or one parent) is hidden from the other kids. When that
        # person is removed the plan has no participants left, and "no participants"
        # means the whole family: every kid can now read it.
        workspace = self.f["workspace"]
        kid_a, kid_b = self.f["kids"]
        start = local_noon(date(2027, 4, 1))
        event = Event.objects.create(
            created_by=self.f["admin"],
            workspace=workspace,
            title="Doctor for A",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        event.participants.set([kid_a])
        self.assertEqual(
            client_for(kid_b)
            .get(reverse("events:detail", args=[event.pk]))
            .status_code,
            404,
        )
        membership = Membership.objects.get(user=kid_a)
        client_for(self.f["admin"]).post(
            reverse("accounts:remove_member", args=[workspace.pk, membership.pk])
        )
        self.assertFalse(get_user_model().objects.filter(pk=kid_a.pk).exists())
        response = client_for(kid_b).get(reverse("events:detail", args=[event.pk]))
        self.assertEqual(
            response.status_code,
            404,
            "after the removal the other kid can read the private plan",
        )
