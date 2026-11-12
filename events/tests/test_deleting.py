from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection
from checklists.models import Checklist

from ..models import Event


class EventDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.parent = get_user_model().objects.create_user(username="parent")
        cls.kid = get_user_model().objects.create_user(username="kid")
        cls.pending = get_user_model().objects.create_user(username="pending")
        cls.outsider = get_user_model().objects.create_user(username="outsider")
        cls.workspace = Workspace.objects.create(
            name="Our family", primary_admin=cls.owner
        )
        for user, role, accepted_at in (
            (cls.parent, Membership.Role.PARENT, django_timezone.now()),
            (cls.kid, Membership.Role.KID, django_timezone.now()),
            (cls.pending, Membership.Role.PARENT, None),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.owner,
                accepted_at=accepted_at,
            )
        cls.event = Event.objects.create(
            workspace=cls.workspace,
            created_by=cls.owner,
            title="Family dinner",
            starts_at=datetime(2026, 11, 5, 23, tzinfo=timezone.utc),
            ends_at=datetime(2026, 11, 6, 0, tzinfo=timezone.utc),
            timezone_name="America/New_York",
            location="School",
        )
        cls.event.participants.set([cls.kid])

    def setUp(self):
        self.client.force_login(self.owner)
        self.url = reverse("events:delete", args=[self.event.pk])
        self.detail_url = reverse("events:detail", args=[self.event.pk])
        self.schedule_url = reverse("events:workspace_list", args=[self.workspace.pk])

    def assertEventExists(self):
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertEqual(list(self.event.participants.all()), [self.kid])

    def test_get_shows_confirmation_without_deleting(self):
        for user in (self.owner, self.parent):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertContains(response, "Delete event?")
                self.assertContains(response, "Family dinner")
                self.assertContains(response, "School")
                self.assertContains(response, "Thursday 5 November 2026")
                self.assertContains(response, "This cannot be undone.")
                self.assertContains(response, "Linked checklists will be kept.")
                self.assertContains(response, 'name="csrfmiddlewaretoken"')
                self.assertContains(response, 'name="action" value="delete"')
                self.assertContains(response, "data-full-page")
                self.assertEventExists()

    def test_keep_event_returns_to_details_without_deleting(self):
        response = self.client.get(self.url)

        self.assertContains(
            response, f'href="{self.detail_url}?timezone=America%2FNew_York"'
        )
        self.assertContains(response, "Keep event")
        self.assertContains(response, "data-autofocus")
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)
        self.assertEventExists()

    def test_only_caregivers_see_the_delete_link(self):
        for user in (self.owner, self.parent):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.detail_url)
                self.assertContains(
                    response, f'href="{self.url}?timezone=America/New_York"'
                )
                self.assertContains(response, "Delete event")
        self.client.force_login(self.kid)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_owner_can_confirm_deletion_and_return_to_schedule(self):
        self.client.get(self.url)
        response = self.client.post(self.url, {"action": "delete"}, follow=True)

        self.assertRedirects(
            response,
            self.schedule_url + "?date=2026-11-05&view=day&timezone=America%2FNew_York",
        )
        self.assertContains(response, "Your event has been deleted.")
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertEqual(list(response.context["page"]), [])
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertFalse(
            Event.participants.through.objects.filter(event_id=self.event.pk).exists()
        )
        self.assertTrue(Workspace.objects.filter(pk=self.workspace.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.kid.pk).exists())

    def test_accepted_parent_can_delete_another_caregivers_event(self):
        self.client.force_login(self.parent)
        response = self.client.post(self.url, {"action": "delete"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())

    def test_unconfirmed_post_does_not_delete(self):
        for data in ({}, {"action": "cancel"}, {"action": "preview"}):
            with self.subTest(data=data):
                response = self.client.post(self.url, data)
                self.assertContains(response, "Choose Delete event to confirm")
                self.assertEventExists()
        self.client.get(self.url, {"action": "delete"})
        self.assertEventExists()

    def test_anonymous_visitors_must_sign_in(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            response = method(self.url)
            self.assertRedirects(
                response,
                reverse("accounts:login") + "?next=" + self.url,
                fetch_redirect_response=False,
            )
        self.assertEventExists()

    def test_children_pending_members_and_outsiders_cannot_delete(self):
        for user in (self.kid, self.pending, self.outsider):
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 404)
                self.assertEqual(
                    self.client.post(self.url, {"action": "delete"}).status_code, 404
                )
                self.assertEventExists()

    def test_revoked_membership_prevents_confirmation(self):
        self.client.force_login(self.parent)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Membership.objects.filter(user=self.parent).delete()

        self.assertEqual(
            self.client.post(self.url, {"action": "delete"}).status_code, 404
        )
        self.assertEventExists()

    def test_personal_event_cannot_be_deleted_as_a_family_event(self):
        self.event.workspace = None
        self.event.save(update_fields=["workspace"])

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.post(self.url, {"action": "delete"}).status_code, 404
        )
        self.assertEventExists()

    def test_imported_event_stays_managed_in_google_calendar(self):
        connection = CalendarConnection.objects.create(
            user=self.owner,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        self.event.source = Event.Source.GOOGLE
        self.event.calendar_connection = connection
        self.event.external_id = "google-event"
        self.event.save()

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.post(self.url, {"action": "delete"}).status_code, 404
        )
        self.assertNotContains(self.client.get(self.detail_url), self.url)
        self.assertEventExists()
        self.assertTrue(CalendarConnection.objects.filter(pk=connection.pk).exists())

    def test_delete_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        client.get(self.url)

        self.assertEqual(client.post(self.url, {"action": "delete"}).status_code, 403)
        self.assertEventExists()

    def test_linked_checklist_and_other_events_are_preserved(self):
        checklist = Checklist.objects.create(
            workspace=self.workspace,
            event=self.event,
            created_by=self.owner,
            title="Bring dinner",
        )
        other = Event.objects.create(
            workspace=self.workspace,
            created_by=self.owner,
            title="Other event",
            starts_at=self.event.starts_at + timedelta(days=1),
            ends_at=self.event.ends_at + timedelta(days=1),
        )

        self.client.post(self.url, {"action": "delete", "event_id": other.pk})

        checklist.refresh_from_db()
        self.assertIsNone(checklist.event_id)
        self.assertTrue(Event.objects.filter(pk=other.pk).exists())

    def test_database_failure_rolls_back_deletion_and_keeps_confirmation_usable(self):
        delete = Event.delete

        def fail_after_delete(event, *args, **kwargs):
            delete(event, *args, **kwargs)
            raise DatabaseError

        with patch.object(Event, "delete", fail_after_delete):
            response = self.client.post(self.url, {"action": "delete"})

        self.assertContains(response, "The event could not be deleted.")
        self.assertContains(
            response, f'action="{self.url}?timezone=America%2FNew_York"'
        )
        self.assertEventExists()

    def test_deletion_returns_to_the_displayed_timezone_and_local_day(self):
        url = self.url + "?timezone=Asia%2FTokyo"
        response = self.client.get(url)
        self.assertContains(response, "Friday 6 November 2026")
        self.assertContains(response, "Asia/Tokyo")

        response = self.client.post(url, {"action": "delete"})

        self.assertRedirects(
            response,
            self.schedule_url + "?date=2026-11-06&view=day&timezone=Asia%2FTokyo",
        )

    def test_invalid_timezone_falls_back_to_event_timezone(self):
        response = self.client.get(self.url + "?timezone=invalid")

        self.assertContains(response, "America/New_York")
        self.assertContains(response, "Thursday 5 November 2026")
        self.assertEventExists()

    def test_repeated_confirmation_and_unsupported_methods(self):
        self.assertEqual(self.client.delete(self.url).status_code, 405)
        self.client.post(self.url, {"action": "delete"})

        self.assertEqual(
            self.client.post(self.url, {"action": "delete"}).status_code, 404
        )
        self.assertEqual(self.client.get(self.url).status_code, 404)
