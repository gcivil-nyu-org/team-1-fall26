from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection
from events.models import Event

from ..forms import ChecklistForm
from ..models import Checklist, ChecklistItem


class HardeningBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.parent = User.objects.create_user(username="hp1", password="pw-12345-x")
        self.parent2 = User.objects.create_user(username="hp2", password="pw-12345-x")
        self.kid = User.objects.create_user(username="hkid", password="pw-12345-x")
        self.workspace = Workspace.objects.create(name="Fam", primary_admin=self.parent)
        for user, role in (
            (self.parent2, Membership.Role.PARENT),
            (self.kid, Membership.Role.KID),
        ):
            Membership.objects.create(
                user=user,
                workspace=self.workspace,
                role=role,
                invited_by=self.parent,
                accepted_at=timezone.now(),
            )
        self.checklist = Checklist.objects.create(
            title="List", workspace=self.workspace, created_by=self.parent
        )
        cache.clear()

    def event(self, **kwargs):
        defaults = dict(
            title="Secret dinner",
            workspace=self.workspace,
            created_by=self.parent,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
        )
        defaults.update(kwargs)
        return Event.objects.create(**defaults)


class KidPrivacyTests(HardeningBase):
    def test_kid_does_not_see_parents_only_event_label(self):
        event = self.event()
        event.participants.set([self.parent, self.parent2])
        self.checklist.event = event
        self.checklist.save()
        self.client.force_login(self.kid)

        lst = self.client.get(reverse("checklists:list", args=[self.workspace.pk]))
        detail = self.client.get(reverse("checklists:detail", args=[self.checklist.pk]))

        self.assertEqual(lst.status_code, 200)
        self.assertNotContains(lst, "Secret dinner")
        self.assertNotContains(detail, "Secret dinner")

    def test_kid_sees_label_for_visible_event(self):
        event = self.event(title="Open picnic")
        self.checklist.event = event
        self.checklist.save()
        self.client.force_login(self.kid)

        detail = self.client.get(reverse("checklists:detail", args=[self.checklist.pk]))

        self.assertContains(detail, "Open picnic")


class EventDropdownTests(HardeningBase):
    def google_event(self, owner, title):
        connection = CalendarConnection.objects.create(
            user=owner, provider="google", workspace=self.workspace
        )
        return self.event(
            title=title,
            created_by=owner,
            source=Event.Source.GOOGLE,
            calendar_connection=connection,
            external_id=f"ext-{title}",
        )

    def test_other_parents_google_events_are_not_offered(self):
        theirs = self.google_event(self.parent2, "Their dentist")
        mine = self.google_event(self.parent, "My dentist")
        shared = self.event(title="Family picnic")

        form = ChecklistForm(workspace=self.workspace, user=self.parent)
        ids = set(form.fields["event"].queryset.values_list("pk", flat=True))

        self.assertNotIn(theirs.pk, ids)
        self.assertIn(mine.pk, ids)
        self.assertIn(shared.pk, ids)


class ItemAddMessageTests(HardeningBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.parent)
        self.url = reverse("checklists:item_add", args=[self.checklist.pk])

    def test_empty_item_shows_error(self):
        response = self.client.post(self.url, {"description": "  "}, follow=True)

        self.assertContains(response, "This field is required.")
        self.assertEqual(self.checklist.items.count(), 0)

    def test_too_long_item_shows_error(self):
        response = self.client.post(self.url, {"description": "x" * 256}, follow=True)

        self.assertContains(response, "at most 255 characters")
        self.assertEqual(self.checklist.items.count(), 0)


class ToggleTests(HardeningBase):
    def test_explicit_state_is_idempotent(self):
        item = ChecklistItem.objects.create(checklist=self.checklist, description="a")
        self.client.force_login(self.parent)
        url = reverse("checklists:item_toggle", args=[item.pk])

        self.client.post(url, {"completed": "1"})
        self.client.post(url, {"completed": "1"})
        item.refresh_from_db()
        self.assertTrue(item.is_completed)

        self.client.post(url, {"completed": "0"})
        self.client.post(url, {"completed": "0"})
        item.refresh_from_db()
        self.assertFalse(item.is_completed)
        self.assertIsNone(item.completed_by)

    def test_without_value_it_still_toggles(self):
        item = ChecklistItem.objects.create(checklist=self.checklist, description="a")
        self.client.force_login(self.parent)
        self.client.post(reverse("checklists:item_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertTrue(item.is_completed)


class TitleErrorTests(HardeningBase):
    def errors(self, title):
        self.client.force_login(self.parent)
        response = self.client.post(
            reverse("checklists:create", args=[self.workspace.pk]),
            {"title": title, "event": ""},
        )
        self.assertEqual(response.status_code, 200)
        return response.context["form"].errors["title"]

    def test_long_title_shows_one_error(self):
        errors = self.errors("x" * 151)
        self.assertEqual(len(errors), 1)
        self.assertIn("at most 150", errors[0])

    def test_blank_title_shows_one_error(self):
        self.assertEqual(len(self.errors("")), 1)

    def test_whitespace_title_shows_one_error(self):
        self.assertEqual(len(self.errors("   ")), 1)


class InvisibleTextTests(HardeningBase):
    def test_invisible_titles_are_rejected(self):
        self.client.force_login(self.parent)
        for title in ("​​", "‮", "‮abc​"):
            with self.subTest(title=title):
                response = self.client.post(
                    reverse("checklists:create", args=[self.workspace.pk]),
                    {"title": title, "event": ""},
                )
                if title == "‮abc​":
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(Checklist.objects.filter(title="abc").exists())
                else:
                    self.assertEqual(response.status_code, 200)

    def test_invisible_items_are_rejected(self):
        self.client.force_login(self.parent)
        url = reverse("checklists:item_add", args=[self.checklist.pk])
        self.client.post(url, {"description": "​‮"})
        self.assertEqual(self.checklist.items.count(), 0)


class FloodTests(HardeningBase):
    def test_item_cap(self):
        ChecklistItem.objects.bulk_create(
            ChecklistItem(checklist=self.checklist, description=f"i{n}", position=n)
            for n in range(200)
        )
        self.client.force_login(self.parent)
        response = self.client.post(
            reverse("checklists:item_add", args=[self.checklist.pk]),
            {"description": "one more"},
            follow=True,
        )
        self.assertEqual(self.checklist.items.count(), 200)
        self.assertContains(response, "200 items")

    @override_settings(RATELIMIT_ENABLED=True)
    def test_item_add_is_rate_limited(self):
        self.client.force_login(self.parent)
        url = reverse("checklists:item_add", args=[self.checklist.pk])
        codes = [
            self.client.post(url, {"description": f"i{n}"}).status_code
            for n in range(62)
        ]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 302)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_checklist_create_is_rate_limited(self):
        self.client.force_login(self.parent)
        url = reverse("checklists:create", args=[self.workspace.pk])
        codes = [
            self.client.post(url, {"title": f"t{n}", "event": ""}).status_code
            for n in range(62)
        ]
        self.assertIn(429, codes)
