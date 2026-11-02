from datetime import date, datetime, time, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace

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
        form = EventForm(self.data, instance=self.event, user=self.owner)

        self.assertFalse(form.is_valid())
        self.assertIn("Soccer practice", str(form.non_field_errors()))

    def test_edit_rejects_invalid_time_and_dst_transition(self):
        for changes, field in (
            ({"end_time": "17:00"}, "end_time"),
            ({"date": "2026-03-08", "start_time": "02:30"}, "start_time"),
            ({"date": "2026-11-01", "start_time": "01:30"}, "start_time"),
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
