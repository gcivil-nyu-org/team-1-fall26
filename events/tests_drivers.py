from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection
from calendars.sync import sync_connection
from calendars.tests import FakeCalendarService

from .access import event_drivers
from .forms import EventDriverForm
from .models import Event


class DriverAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="FamilyOwner")
        cls.parent = User.objects.create_user(username="FamilyParent")
        cls.kid = User.objects.create_user(username="FamilyKid")
        cls.pending = User.objects.create_user(username="PendingAdult")
        cls.inactive = User.objects.create_user(username="InactiveAdult", is_active=False)
        cls.outsider = User.objects.create_user(username="OtherFamilyAdult")
        cls.workspace = Workspace.objects.create(name="Driver family", primary_admin=cls.owner)
        cls.other_workspace = Workspace.objects.create(name="Other family", primary_admin=cls.outsider)
        for user, role, accepted in (
            (cls.parent, Membership.Role.PARENT, True),
            (cls.kid, Membership.Role.KID, True),
            (cls.pending, Membership.Role.PARENT, False),
            (cls.inactive, Membership.Role.PARENT, True),
        ):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=role,
                invited_by=cls.owner, accepted_at=timezone.now() if accepted else None,
            )
        cls.event_day = timezone.localdate() + timedelta(days=1)
        cls.starts_at = datetime.combine(cls.event_day, time(15), ZoneInfo("America/New_York"))
        cls.event = Event.objects.create(
            workspace=cls.workspace, created_by=cls.owner, title="Soccer practice",
            starts_at=cls.starts_at, ends_at=cls.starts_at + timedelta(hours=1),
        )

    def setUp(self):
        self.client.force_login(self.owner)
        self.detail_url = reverse("events:detail", args=[self.event.pk])
        self.data = {
            "requires_transport": "on",
            "dropoff_driver": self.owner.pk,
            "pickup_driver": self.parent.pk,
        }

    def assign(self):
        return self.client.post(self.detail_url, self.data)

    def test_eligible_drivers_are_only_active_accepted_adults_in_this_workspace(self):
        self.assertSetEqual(set(event_drivers(self.workspace)), {self.owner, self.parent})
        self.assertFalse(event_drivers(None).exists())
        form = EventDriverForm(instance=self.event)
        for field in ("dropoff_driver", "pickup_driver"):
            self.assertSetEqual(set(form.fields[field].queryset), {self.owner, self.parent})

    def test_owner_can_assign_separate_drivers_and_reopen_the_event(self):
        response = self.assign()
        self.assertRedirects(response, self.detail_url + "?timezone=America%2FNew_York")
        self.event.refresh_from_db()
        self.assertTrue(self.event.requires_transport)
        self.assertEqual(self.event.dropoff_driver, self.owner)
        self.assertEqual(self.event.pickup_driver, self.parent)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Drop-off:")
        self.assertContains(response, "Pickup:")
        form = response.context["driver_form"]
        self.assertEqual(form["dropoff_driver"].value(), self.owner.pk)
        self.assertEqual(form["pickup_driver"].value(), self.parent.pk)

    def test_parent_can_reassign_a_driver_without_changing_other_event_details(self):
        self.assign()
        self.client.force_login(self.parent)
        response = self.client.post(self.detail_url, {
            **self.data, "pickup_driver": self.owner.pk,
            "title": "Tampered title", "workspace": self.other_workspace.pk,
            "created_by": self.parent.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.pickup_driver, self.owner)
        self.assertEqual(self.event.dropoff_driver, self.owner)
        self.assertEqual(self.event.title, "Soccer practice")
        self.assertEqual(self.event.workspace, self.workspace)
        self.assertEqual(self.event.created_by, self.owner)
        self.assertEqual(self.event.starts_at, self.starts_at)

    def test_either_trip_can_remain_unassigned_and_assignments_can_be_cleared(self):
        self.assign()
        response = self.client.post(self.detail_url, {**self.data, "dropoff_driver": ""})
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.dropoff_driver)
        self.assertEqual(self.event.pickup_driver, self.parent)
        response = self.client.post(self.detail_url, {"requires_transport": "on"}, follow=True)
        self.assertContains(response, "Not assigned")
        response = self.client.post(self.detail_url, {}, follow=True)
        self.assertContains(response, "No transportation requested")
        self.event.refresh_from_db()
        self.assertFalse(self.event.requires_transport)
        self.assertIsNone(self.event.pickup_driver)
        self.assertIsNone(self.event.dropoff_driver)

    def test_rejects_ineligible_driver_ids_without_changing_existing_assignments(self):
        self.assign()
        for field in ("dropoff_driver", "pickup_driver"):
            for user_id in (self.kid.pk, self.pending.pk, self.inactive.pk, self.outsider.pk, 999999, "bad"):
                with self.subTest(field=field, user_id=user_id):
                    response = self.client.post(self.detail_url, {**self.data, field: user_id})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(field, response.context["driver_form"].errors)
                    self.event.refresh_from_db()
                    self.assertEqual(self.event.dropoff_driver, self.owner)
                    self.assertEqual(self.event.pickup_driver, self.parent)

    def test_requires_transport_before_a_driver_can_be_assigned(self):
        response = self.client.post(self.detail_url, {**self.data, "requires_transport": ""})
        self.assertIn("dropoff_driver", response.context["driver_form"].errors)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.dropoff_driver)
        self.assertFalse(self.event.requires_transport)

    def test_child_can_view_assignments_but_cannot_change_them(self):
        self.assign()
        self.client.force_login(self.kid)
        response = self.client.get(self.detail_url)
        self.assertIsNone(response.context["driver_form"])
        self.assertContains(response, self.parent.username)
        self.assertNotContains(response, "Save driver assignments")
        self.assertEqual(self.client.post(self.detail_url, self.data).status_code, 403)

    def test_outsiders_and_pending_members_cannot_read_or_change_assignments(self):
        for user in (self.outsider, self.pending):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.detail_url).status_code, 404)
                self.assertEqual(self.assign().status_code, 404)

    def test_anonymous_requests_require_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.detail_url).status_code, 302)
        self.assertEqual(self.assign().status_code, 302)
        self.event.refresh_from_db()
        self.assertFalse(self.event.requires_transport)

    def test_revoked_caregiver_cannot_submit_an_already_open_form(self):
        self.client.force_login(self.parent)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)
        Membership.objects.filter(user=self.parent).update(role=Membership.Role.KID)
        self.assertEqual(self.assign().status_code, 403)

    def test_driver_membership_is_rechecked_on_submission(self):
        self.client.get(self.detail_url)
        Membership.objects.filter(user=self.parent).delete()
        response = self.assign()
        self.assertIn("pickup_driver", response.context["driver_form"].errors)
        self.event.refresh_from_db()
        self.assertFalse(self.event.requires_transport)

    def test_personal_events_cannot_have_driver_assignments(self):
        self.event.workspace = None
        self.event.save(update_fields=["workspace"])
        self.assertIsNone(self.client.get(self.detail_url).context["driver_form"])
        self.assertEqual(self.assign().status_code, 403)

    def test_post_requires_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.detail_url, self.data).status_code, 403)

    def test_get_does_not_modify_assignments_and_other_methods_are_rejected(self):
        self.client.get(self.detail_url, self.data)
        self.event.refresh_from_db()
        self.assertFalse(self.event.requires_transport)
        self.assertEqual(self.client.put(self.detail_url, self.data).status_code, 405)

    def test_assignment_keeps_the_selected_display_timezone(self):
        response = self.client.post(self.detail_url + "?timezone=Asia%2FTokyo", self.data)
        self.assertRedirects(response, self.detail_url + "?timezone=Asia%2FTokyo")
        self.event.refresh_from_db()
        self.assertEqual(self.event.timezone_name, "America/New_York")
        self.assertEqual(self.event.starts_at, self.starts_at)

    def test_database_failure_keeps_saved_values_and_offers_retry(self):
        with patch.object(Event, "save", side_effect=DatabaseError):
            response = self.assign()
        self.assertContains(response, "Driver assignments could not be saved. Please try again.")
        self.assertEqual(response.context["driver_form"]["pickup_driver"].value(), str(self.parent.pk))
        self.event.refresh_from_db()
        self.assertFalse(self.event.requires_transport)

    def test_model_validation_rejects_children_other_families_and_personal_events(self):
        self.event.requires_transport = True
        for user in (self.kid, self.pending, self.inactive, self.outsider):
            with self.subTest(user=user.username):
                self.event.pickup_driver = user
                with self.assertRaises(ValidationError):
                    self.event.full_clean()
        self.event.pickup_driver = self.owner
        self.event.workspace = None
        with self.assertRaises(ValidationError):
            self.event.full_clean()

    def test_database_requires_transport_and_a_workspace_for_driver_fields(self):
        for values in (
            {"pickup_driver": self.owner},
            {"requires_transport": True, "dropoff_driver": self.owner, "workspace": None},
        ):
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Event.objects.filter(pk=self.event.pk).update(**values)

    def test_all_schedule_views_and_both_dashboards_show_updated_assignments(self):
        self.assign()
        self.client.post(self.detail_url, {**self.data, "pickup_driver": self.owner.pk})
        for user in (self.owner, self.kid):
            self.client.force_login(user)
            for view in ("day", "week", "upcoming"):
                with self.subTest(user=user.username, view=view):
                    response = self.client.get(reverse("events:workspace_list", args=[self.workspace.pk]), {
                        "view": view, "date": self.event_day.isoformat(),
                    })
                    self.assertContains(response, "Drop-off:")
                    self.assertContains(response, "Pickup:")
                    self.assertContains(response, self.owner.username)
            response = self.client.get(reverse("accounts:dashboard"), {"week": self.event_day.isoformat()})
            self.assertContains(response, "Pickup:")
            self.assertContains(response, self.owner.username)

    def test_google_resync_preserves_local_driver_assignments(self):
        connection = CalendarConnection.objects.create(
            user=self.owner, workspace=self.workspace, provider="google",
        )
        self.event.source = Event.Source.GOOGLE
        self.event.calendar_connection = connection
        self.event.external_id = "soccer"
        self.event.save()
        self.assign()
        item = {
            "id": "soccer", "summary": "Updated soccer practice",
            "start": {"dateTime": self.starts_at.isoformat()},
            "end": {"dateTime": (self.starts_at + timedelta(hours=2)).isoformat()},
        }
        with patch("calendars.sync._service", return_value=FakeCalendarService([[item]])):
            sync_connection(connection)
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Updated soccer practice")
        self.assertTrue(self.event.requires_transport)
        self.assertEqual(self.event.pickup_driver, self.parent)
        self.assertEqual(self.event.dropoff_driver, self.owner)

    def creation_submission(self, **fields):
        url = reverse("events:workspace_create", args=[self.workspace.pk])
        token = self.client.get(url).context["form"]["submission_token"].value()
        data = {
            "title": "School pickup",
            "date": self.event_day.isoformat(),
            "start_time": "17:00",
            "end_time": "18:00",
            "timezone": "America/New_York",
            "transport_mode": "driving",
            "submission_token": token,
            "action": "save",
            **fields,
        }
        return url, data

    def test_create_form_offers_optional_family_drivers(self):
        url, _ = self.creation_submission()
        form = self.client.get(url).context["form"]
        for name in ("requires_transport", "dropoff_driver", "pickup_driver"):
            self.assertFalse(form.fields[name].required)
        for name in ("dropoff_driver", "pickup_driver"):
            self.assertSetEqual(set(form.fields[name].queryset), {self.owner, self.parent})

    def test_creation_previews_and_saves_both_drivers_with_the_event(self):
        url, data = self.creation_submission(**self.data, participants=[self.kid.pk])
        response = self.client.post(url, {**data, "action": "preview"})
        self.assertContains(response, "Drop-off:")
        self.assertContains(response, "Pickup:")
        self.assertEqual(Event.objects.count(), 1)
        preview = response.context["event_details"]
        self.assertEqual(preview.dropoff_driver, self.owner)
        self.assertEqual(preview.pickup_driver, self.parent)
        response = self.client.post(url, data)
        event = Event.objects.exclude(pk=self.event.pk).get()
        self.assertRedirects(response, reverse("events:detail", args=[event.pk]) + "?timezone=America%2FNew_York")
        self.assertTrue(event.requires_transport)
        self.assertEqual(event.dropoff_driver, self.owner)
        self.assertEqual(event.pickup_driver, self.parent)
        self.assertSetEqual(set(event.participants.all()), {self.kid})

    def test_creation_does_not_require_transportation_or_any_drivers(self):
        for fields in ({}, {"requires_transport": "on"}):
            with self.subTest(fields=fields):
                url, data = self.creation_submission(**fields)
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
                event = Event.objects.latest("pk")
                self.assertEqual(event.requires_transport, bool(fields))
                self.assertIsNone(event.dropoff_driver)
                self.assertIsNone(event.pickup_driver)

    def test_creation_can_assign_just_one_driver(self):
        for field in ("dropoff_driver", "pickup_driver"):
            with self.subTest(field=field):
                url, data = self.creation_submission(requires_transport="on", **{field: self.parent.pk})
                self.assertEqual(self.client.post(url, data).status_code, 302)
                event = Event.objects.latest("pk")
                self.assertEqual(getattr(event, field), self.parent)
                other = "pickup_driver" if field == "dropoff_driver" else "dropoff_driver"
                self.assertIsNone(getattr(event, other))

    def test_creation_rejects_ineligible_driver_ids_before_saving(self):
        for field in ("dropoff_driver", "pickup_driver"):
            for user in (self.kid, self.pending, self.inactive, self.outsider):
                with self.subTest(field=field, user=user.username):
                    url, data = self.creation_submission(**{**self.data, field: user.pk})
                    response = self.client.post(url, data)
                    self.assertIn(field, response.context["form"].errors)
                    self.assertEqual(Event.objects.count(), 1)

    def test_creation_checks_driver_eligibility_again_after_preview(self):
        url, data = self.creation_submission(**self.data)
        self.assertIsNotNone(self.client.post(url, {**data, "action": "preview"}).context["event_details"])
        Membership.objects.filter(user=self.parent).delete()
        response = self.client.post(url, data)
        self.assertIn("pickup_driver", response.context["form"].errors)
        self.assertEqual(Event.objects.count(), 1)

    def test_creation_retry_does_not_duplicate_or_change_driver_assignments(self):
        url, data = self.creation_submission(**self.data)
        self.client.post(url, data)
        self.client.post(url, {**data, "pickup_driver": self.owner.pk})
        self.assertEqual(Event.objects.count(), 2)
        self.assertEqual(Event.objects.exclude(pk=self.event.pk).get().pickup_driver, self.parent)

    def test_creation_by_second_caregiver_can_include_driver_assignments(self):
        self.client.force_login(self.parent)
        url, data = self.creation_submission(**self.data)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        event = Event.objects.exclude(pk=self.event.pk).get()
        self.assertEqual(event.created_by, self.parent)
        self.assertEqual(event.dropoff_driver, self.owner)
        self.assertEqual(event.pickup_driver, self.parent)
