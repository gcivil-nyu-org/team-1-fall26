from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection

from ..forms import EventForm
from ..models import Event


class EventEditingFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.parent = get_user_model().objects.create_user(username="parent")
        cls.kid = get_user_model().objects.create_user(username="kid")
        cls.outsider = get_user_model().objects.create_user(username="outsider")
        cls.pending = get_user_model().objects.create_user(username="pending")
        cls.workspace = Workspace.objects.create(
            name="Our family", primary_admin=cls.owner
        )
        for user, role, accepted_at in (
            (cls.parent, Membership.Role.PARENT, django_timezone.now()),
            (cls.kid, Membership.Role.KID, django_timezone.now()),
            (cls.pending, Membership.Role.KID, None),
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
            latitude=40.7,
            longitude=-74.0,
            requires_transport=True,
            dropoff_driver=cls.owner,
            pickup_driver=cls.parent,
            is_tentative=True,
        )
        cls.event.participants.set([cls.kid])

    def setUp(self):
        self.data = {
            "title": "Family dinner",
            "date": "2026-11-05",
            "start_time": "18:00",
            "end_time": "19:00",
            "timezone": "America/New_York",
            "location": "School",
            "latitude": "40.7",
            "longitude": "-74.0",
            "transport_mode": "transit",
            "participants": [self.kid.pk],
            "requires_transport": "on",
            "dropoff_driver": self.owner.pk,
            "pickup_driver": self.parent.pk,
            "is_tentative": "on",
        }

    def test_existing_event_prefills_local_times_and_details(self):
        form = EventForm(instance=self.event, user=self.parent)

        self.assertEqual(form["date"].value(), date(2026, 11, 5))
        self.assertEqual(form["start_time"].value(), time(18))
        self.assertEqual(form["end_time"].value(), time(19))
        self.assertIsNone(form["end_date"].value())
        self.assertEqual(form["timezone"].value(), "America/New_York")
        self.assertEqual(form["participants"].value(), [self.kid.pk])
        for name, value in (
            ("title", "Family dinner"),
            ("location", "School"),
            ("latitude", 40.7),
            ("longitude", -74.0),
            ("requires_transport", True),
            ("dropoff_driver", self.owner.pk),
            ("pickup_driver", self.parent.pk),
            ("is_tentative", True),
        ):
            with self.subTest(field=name):
                self.assertEqual(form[name].value(), value)
        self.assertNotIn("submission_token", form.fields)

    def test_overnight_event_prefills_local_end_date(self):
        self.event.ends_at += timedelta(days=1)

        form = EventForm(instance=self.event, user=self.owner)

        self.assertEqual(form["end_date"].value(), date(2026, 11, 6))

    def test_unchanged_event_does_not_conflict_with_itself(self):
        form = EventForm(self.data, instance=self.event, user=self.parent)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.pk, self.event.pk)
        self.assertEqual(form.event.created_by, self.owner)
        self.assertEqual(form.event.submission_id, self.event.submission_id)
        self.assertEqual(form.event.workspace, self.workspace)

    def test_changed_details_are_validated_without_saving_or_mutating_instance(self):
        data = {
            **self.data,
            "title": "Updated dinner",
            "date": "2026-11-06",
            "start_time": "17:00",
            "end_time": "18:00",
            "location": "Park",
            "latitude": "",
            "longitude": "",
            "participants": [self.parent.pk],
        }
        form = EventForm(data, instance=self.event, user=self.parent)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.title, "Updated dinner")
        self.assertEqual(form.event.location, "Park")
        self.assertIsNone(form.event.latitude)
        self.assertIsNone(form.event.longitude)
        self.assertEqual(
            form.event.starts_at, datetime(2026, 11, 6, 22, tzinfo=timezone.utc)
        )
        self.assertEqual(list(form.cleaned_data["participants"]), [self.parent])
        self.assertEqual(self.event.title, "Family dinner")
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Family dinner")
        self.assertEqual(list(self.event.participants.all()), [self.kid])
        self.assertEqual(Event.objects.count(), 1)

    def test_another_event_still_causes_a_scheduling_conflict(self):
        other = Event.objects.create(
            workspace=self.workspace,
            created_by=self.owner,
            title="Soccer practice",
            starts_at=self.event.starts_at,
            ends_at=self.event.ends_at,
        )
        other.participants.set([self.kid])
        data = {k: v for k, v in self.data.items() if k != "is_tentative"}
        form = EventForm(data, instance=self.event, user=self.owner)

        self.assertFalse(form.is_valid())
        self.assertIn("Soccer practice", str(form.non_field_errors()))

    def test_edit_rejects_invalid_time_and_dst_transition(self):
        for changes, field in (
            ({"end_time": "18:00"}, "end_time"),
            ({"date": "2026-03-08", "start_time": "02:30"}, "start_time"),
        ):
            with self.subTest(changes=changes):
                form = EventForm(
                    {**self.data, **changes}, instance=self.event, user=self.owner
                )
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_edit_rejects_outside_and_pending_participants(self):
        for participant in (self.outsider, self.pending):
            with self.subTest(participant=participant):
                form = EventForm(
                    {**self.data, "participants": [participant.pk]},
                    instance=self.event,
                    user=self.owner,
                )
                self.assertFalse(form.is_valid())
                self.assertIn("participants", form.errors)

    def test_existing_event_controls_workspace_and_preserves_import_metadata(self):
        other_workspace = Workspace.objects.create(
            name="Other family", primary_admin=self.outsider
        )
        self.event.source = Event.Source.GOOGLE
        self.event.external_id = "google-event-id"
        form = EventForm(
            self.data,
            instance=self.event,
            user=self.parent,
            workspace=other_workspace,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.workspace, self.workspace)
        self.assertEqual(form.event.source, Event.Source.GOOGLE)
        self.assertEqual(form.event.external_id, "google-event-id")


class EventEditingViewTests(TestCase):
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
        self.url = reverse("events:edit", args=[self.event.pk])
        self.detail_url = reverse("events:detail", args=[self.event.pk])
        self.data = {
            "title": "Updated dinner",
            "date": "2026-11-06",
            "start_time": "18:00",
            "end_time": "19:00",
            "timezone": "America/New_York",
            "location": "Park",
            "transport_mode": "walking",
            "participants": [self.parent.pk],
            "action": "save",
        }

    def assertEventUnchanged(self):
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Family dinner")
        self.assertEqual(self.event.location, "School")
        self.assertEqual(
            self.event.starts_at, datetime(2026, 11, 5, 23, tzinfo=timezone.utc)
        )
        self.assertEqual(list(self.event.participants.all()), [self.kid])
        self.assertEqual(Event.objects.count(), 1)

    def test_owner_and_accepted_parent_can_open_prefilled_page(self):
        for user in (self.owner, self.parent):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertContains(response, "<h1>Edit event</h1>", html=True)
                self.assertContains(response, "Save changes")
                self.assertContains(response, "Cancel")
                self.assertContains(response, f'action="{self.url}"')
                self.assertContains(response, 'name="csrfmiddlewaretoken"')
                self.assertContains(response, 'class="panel event-form"')
                self.assertEqual(
                    response.context["form"]["title"].value(), "Family dinner"
                )
                self.assertEqual(
                    response.context["form"]["participants"].value(), [self.kid.pk]
                )
                self.assertNotContains(response, 'value="save">Add event')
        self.assertEventUnchanged()

    def test_edit_link_is_visible_to_family_caregivers(self):
        for user in (self.owner, self.parent):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.detail_url)
                self.assertContains(
                    response, f'href="{self.url}" data-dialog>Edit event'
                )
        self.client.force_login(self.kid)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_anonymous_visitors_must_sign_in_for_get_and_post(self):
        self.client.logout()
        for method, data in ((self.client.get, {}), (self.client.post, self.data)):
            response = method(self.url, data)
            self.assertRedirects(
                response,
                reverse("accounts:login") + "?next=" + self.url,
                fetch_redirect_response=False,
            )
        self.assertEventUnchanged()

    def test_children_pending_members_and_outsiders_cannot_edit(self):
        for user in (self.kid, self.pending, self.outsider):
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 404)
                self.assertEqual(self.client.post(self.url, self.data).status_code, 404)
                self.assertEventUnchanged()

    def test_membership_revoked_after_opening_form_prevents_saving(self):
        self.client.force_login(self.parent)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Membership.objects.filter(user=self.parent).delete()

        self.assertEqual(self.client.post(self.url, self.data).status_code, 404)
        self.assertEventUnchanged()

    def test_personal_events_are_not_editable_as_family_events(self):
        self.event.workspace = None
        self.event.save(update_fields=["workspace"])

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 404)
        self.assertEventUnchanged()

    def test_google_imports_remain_read_only(self):
        connection = CalendarConnection.objects.create(
            user=self.owner,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        self.event.source = Event.Source.GOOGLE
        self.event.calendar_connection = connection
        self.event.external_id = "imported-event"
        self.event.save()

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 404)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Change it there.")
        self.assertNotContains(response, self.url)
        self.assertEventUnchanged()

    def test_owner_and_parent_can_save_changes_to_the_existing_event(self):
        submission_id = self.event.submission_id
        created_at = self.event.created_at
        for user in (self.owner, self.parent):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.post(self.url, self.data, follow=True)
                self.assertRedirects(
                    response, self.detail_url + "?timezone=America%2FNew_York"
                )
                self.assertContains(response, "Your event has been updated.")
                self.assertContains(response, "Updated dinner")
                self.assertContains(response, "Park")
                self.event.refresh_from_db()
                self.assertEqual(self.event.title, "Updated dinner")
                self.assertEqual(self.event.location, "Park")
                self.assertEqual(self.event.transport_mode, Event.TransportMode.WALKING)
                self.assertEqual(
                    self.event.starts_at, datetime(2026, 11, 6, 23, tzinfo=timezone.utc)
                )
                self.assertEqual(
                    self.event.ends_at, datetime(2026, 11, 7, 0, tzinfo=timezone.utc)
                )
                self.assertEqual(list(self.event.participants.all()), [self.parent])
                self.assertEqual(self.event.created_by, self.owner)
                self.assertEqual(self.event.workspace, self.workspace)
                self.assertEqual(self.event.submission_id, submission_id)
                self.assertEqual(self.event.created_at, created_at)
                self.assertGreater(self.event.updated_at, created_at)
                self.assertEqual(Event.objects.count(), 1)

    def test_preview_checks_travel_without_saving(self):
        with patch("events.views.find_travel_checks", return_value=[]) as travel:
            response = self.client.post(self.url, {**self.data, "action": "preview"})

        self.assertContains(response, "Review your event")
        self.assertContains(response, "Save your changes")
        self.assertEqual(response.context["event_details"].title, "Updated dinner")
        self.assertEqual(response.context["form"]["location"].value(), "Park")
        travel.assert_called_once()
        self.assertEqual(travel.call_args.args[0].pk, self.event.pk)
        self.assertEventUnchanged()

    def test_post_without_save_action_does_not_write(self):
        data = {name: value for name, value in self.data.items() if name != "action"}

        self.assertEqual(self.client.post(self.url, data).status_code, 200)
        self.assertEventUnchanged()

    def test_invalid_details_stay_on_form_and_do_not_save(self):
        response = self.client.post(self.url, {**self.data, "end_time": "18:00"})

        self.assertContains(response, "End time must be after start time.")
        self.assertEqual(response.context["form"]["location"].value(), "Park")
        self.assertEventUnchanged()

    def test_outside_participant_is_rejected(self):
        response = self.client.post(
            self.url, {**self.data, "participants": [self.outsider.pk]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("participants", response.context["form"].errors)
        self.assertEventUnchanged()

    def test_conflict_with_another_event_prevents_save(self):
        other = Event.objects.create(
            workspace=self.workspace,
            created_by=self.owner,
            title="Soccer practice",
            starts_at=self.event.starts_at + timedelta(days=1),
            ends_at=self.event.ends_at + timedelta(days=1),
        )
        other.participants.set([self.parent])

        response = self.client.post(self.url, self.data)

        self.assertContains(response, "Scheduling conflict:")
        self.assertContains(response, "Soccer practice")
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Family dinner")

    def test_posted_identity_and_workspace_fields_cannot_move_event(self):
        other_workspace = Workspace.objects.create(
            name="Other family", primary_admin=self.outsider
        )
        response = self.client.post(
            self.url,
            {
                **self.data,
                "workspace": other_workspace.pk,
                "created_by": self.outsider.pk,
                "source": Event.Source.GOOGLE,
                "submission_id": "tampered",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.workspace, self.workspace)
        self.assertEqual(self.event.created_by, self.owner)
        self.assertEqual(self.event.source, Event.Source.SYNCMILY)

    def test_event_and_participants_roll_back_together_on_database_error(self):
        with patch.object(
            type(self.event.participants), "set", side_effect=DatabaseError
        ):
            response = self.client.post(self.url, self.data)

        self.assertContains(response, "The event could not be saved.")
        self.assertEqual(response.context["form"]["title"].value(), "Updated dinner")
        self.assertEventUnchanged()

    def test_model_validation_failure_during_save_keeps_entered_details(self):
        with patch.object(
            Event, "full_clean", side_effect=[None, ValidationError("Invalid event.")]
        ):
            response = self.client.post(self.url, self.data)

        self.assertContains(response, "Invalid event.")
        self.assertEqual(response.context["form"]["title"].value(), "Updated dinner")
        self.assertEventUnchanged()

    def test_clearing_participants_makes_event_a_whole_family_plan(self):
        response = self.client.post(self.url, {**self.data, "participants": []})

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertFalse(self.event.participants.exists())
        self.client.force_login(self.kid)
        self.assertContains(self.client.get(self.detail_url), "Whole family")

    def test_saved_event_appears_on_its_new_schedule_day(self):
        self.client.post(self.url, self.data)
        schedule_url = reverse("events:workspace_list", args=[self.workspace.pk])
        for day, expected in (("2026-11-05", []), ("2026-11-06", [self.event])):
            with self.subTest(day=day):
                response = self.client.get(
                    schedule_url,
                    {"date": day, "view": "day", "timezone": "America/New_York"},
                )
                self.assertEqual(list(response.context["page"]), expected)
        self.assertContains(response, "Updated dinner")
        self.client.force_login(self.kid)
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)

    def test_editing_timezone_updates_saved_timestamps_and_redirect(self):
        response = self.client.post(
            self.url, {**self.data, "timezone": "Europe/London"}
        )

        self.assertRedirects(response, self.detail_url + "?timezone=Europe%2FLondon")
        self.event.refresh_from_db()
        self.assertEqual(self.event.timezone_name, "Europe/London")
        self.assertEqual(
            self.event.starts_at, datetime(2026, 11, 6, 18, tzinfo=timezone.utc)
        )

    def test_save_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        client.get(self.url)

        self.assertEqual(client.post(self.url, self.data).status_code, 403)
        self.assertEventUnchanged()

    def test_missing_event_and_unsupported_method(self):
        url = reverse("events:edit", args=[self.event.pk + 100])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.put(self.url).status_code, 405)
