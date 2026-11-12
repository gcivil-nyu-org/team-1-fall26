from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from events.models import Event


class ProgressiveEventFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(
            username="progressive_admin",
            first_name="Alex",
            password="Password123!",
        )
        cls.parent = User.objects.create_user(
            username="progressive_parent",
            first_name="Sam",
            password="Password123!",
        )
        cls.kid = User.objects.create_user(
            username="progressive_kid",
            first_name="Riley",
            password="Password123!",
        )
        cls.workspace = Workspace.objects.create(
            name="Progressive family", primary_admin=cls.admin
        )
        for user, role in (
            (cls.parent, Membership.Role.PARENT),
            (cls.kid, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.admin,
                accepted_at=timezone.now(),
            )
        cls.tomorrow = timezone.localdate() + timedelta(days=1)
        cls.create_url = reverse("events:workspace_create", args=[cls.workspace.pk])

    def setUp(self):
        self.client.force_login(self.admin)

    def submission_token(self, url=None):
        response = self.client.get(url or self.create_url)
        return response.context["form"]["submission_token"].value()

    def create_payload(self, **overrides):
        payload = {
            "title": "Soccer practice",
            "date": self.tomorrow.isoformat(),
            "start_time": "15:00",
            "end_time": "16:30",
            "timezone": "America/New_York",
            "transport_mode": "driving",
            "submission_token": self.submission_token(),
            "action": "save",
        }
        payload.update(overrides)
        return payload

    def test_progressive_form_initial_dom_structure(self):
        response = self.client.get(self.create_url)
        self.assertEqual(response.status_code, 200)

        # Form root and progressive identifier
        self.assertContains(response, "data-event-form")
        self.assertContains(response, 'id="event-form"')

        # Title & date initial visibility and new labels
        self.assertContains(response, "What are we planning?")
        self.assertContains(response, 'placeholder="Add an event title"')
        self.assertContains(response, "When?")
        self.assertContains(response, 'Where <span class="opt">optional</span>')

        # Times container hidden initially
        self.assertContains(response, "data-progressive-times hidden")
        self.assertContains(response, "data-toggle-end-date")
        self.assertContains(response, "data-end-date-field hidden")

        # Who's going compact summary and drawer
        self.assertContains(response, "data-who-summary")
        self.assertContains(response, "data-who-chips")
        self.assertContains(response, "data-who-empty")
        self.assertContains(response, "Whole family")
        self.assertContains(response, "data-who-toggle")
        self.assertContains(response, "data-who-drawer hidden")

        # Progressive location details and drivers hidden initially
        self.assertContains(response, "data-progressive-location-details hidden")
        self.assertContains(response, "data-progressive-drivers hidden")

        # More options button and disclosure container removed
        self.assertNotContains(response, "More options")
        self.assertNotContains(response, "data-more-options")
        self.assertNotContains(response, "more-options")

        # Time zone label and dropdown removed from event form, rendered as hidden field
        self.assertNotContains(response, '<label for="id_timezone">')
        self.assertNotContains(response, 'id="id_timezone"><option')
        self.assertContains(response, 'type="hidden" name="timezone"')

        # Not confirmed yet checkbox directly visible in main form
        self.assertContains(response, "Not confirmed yet")
        self.assertContains(response, 'name="is_tentative"')

        # Progressive script included
        self.assertContains(response, "events/progressive_event_form.js")

    def test_progressive_times_revealed_on_validation_error(self):
        # Submitting an invalid start time keeps times visible so error is seen.
        payload = self.create_payload(start_time="not-a-time", action="preview")
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_valid())
        self.assertContains(response, "data-progressive-times")
        self.assertNotContains(response, "data-progressive-times hidden")

    def test_end_date_revealed_when_end_date_provided_or_error(self):
        # Submitting with an end_date shows the end date field and checks toggle.
        next_day = self.tomorrow + timedelta(days=1)
        payload = self.create_payload(
            end_date=next_day.isoformat(),
            end_time="10:00",
            action="preview",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-toggle-end-date checked")
        self.assertNotContains(response, "data-end-date-field hidden")

    def test_location_details_revealed_when_location_entered(self):
        payload = self.create_payload(
            location="Riverside Park",
            action="preview",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-progressive-location-details hidden")

    def test_drivers_revealed_when_requires_transport_is_checked(self):
        payload = self.create_payload(
            location="Riverside Park",
            requires_transport="on",
            action="preview",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-progressive-drivers hidden")

    def test_tentative_checkbox_directly_visible_and_persisted(self):
        # Preview with tentative shows checked state and review facts
        # without More Options.
        payload = self.create_payload(is_tentative="on", action="preview")
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_is_tentative" checked')
        self.assertContains(response, "Not confirmed yet")
        self.assertNotContains(response, "More options")
        self.assertNotContains(response, "data-more-options")

        # Save with tentative persists is_tentative=True to database
        payload_save = self.create_payload(is_tentative="on", action="save")
        response_save = self.client.post(self.create_url, payload_save)
        self.assertEqual(response_save.status_code, 302)
        event = Event.objects.latest("pk")
        self.assertTrue(event.is_tentative)

    def test_event_creation_without_explicit_timezone_uses_default(self):
        payload = self.create_payload(action="save")
        del payload["timezone"]
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.latest("pk")
        self.assertEqual(event.timezone_name, timezone.get_current_timezone_name())

    def test_modal_and_full_page_do_not_show_timezone_or_more_options(self):
        # Full-page create view
        full_page = self.client.get(self.create_url)
        self.assertEqual(full_page.status_code, 200)
        self.assertNotContains(full_page, "More options")
        self.assertNotContains(full_page, '<label for="id_timezone">')
        self.assertNotContains(full_page, 'id="id_timezone"><option')
        self.assertContains(full_page, 'type="hidden" name="timezone"')
        self.assertContains(full_page, "Not confirmed yet")

        # GET with custom timezone in query param (modal / calendar link)
        modal_get = self.client.get(self.create_url, {"timezone": "America/Chicago"})
        self.assertEqual(modal_get.status_code, 200)
        self.assertNotContains(modal_get, "More options")
        self.assertNotContains(modal_get, '<label for="id_timezone">')
        self.assertNotContains(modal_get, 'id="id_timezone"><option')
        self.assertContains(
            modal_get,
            'type="hidden" name="timezone" value="America/Chicago"',
        )

    def test_requires_transport_unchecked_clears_drivers_on_clean(self):
        # If client passes driver IDs while requires_transport is unchecked / False,
        # clean() clears drivers to None so stale data is never saved
        payload = self.create_payload(
            requires_transport="",
            dropoff_driver=self.parent.pk,
            pickup_driver=self.admin.pk,
            action="save",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.latest("pk")
        self.assertFalse(event.requires_transport)
        self.assertIsNone(event.dropoff_driver)
        self.assertIsNone(event.pickup_driver)

    def test_requires_transport_checked_saves_drivers(self):
        payload = self.create_payload(
            requires_transport="on",
            dropoff_driver=self.parent.pk,
            pickup_driver=self.admin.pk,
            action="save",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.latest("pk")
        self.assertTrue(event.requires_transport)
        self.assertEqual(event.dropoff_driver, self.parent)
        self.assertEqual(event.pickup_driver, self.admin)

    def test_coordinates_without_location_rejected(self):
        payload = self.create_payload(
            location="",
            latitude=40.7128,
            longitude=-74.0060,
            action="save",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("location", form.errors)
        self.assertIn(
            "A location is required when coordinates are provided.",
            form.errors["location"],
        )

    def test_end_time_before_start_time_same_day_rejected(self):
        payload = self.create_payload(
            start_time="17:00",
            end_time="16:00",
            action="save",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("end_time", form.errors)

    def test_overnight_event_with_end_date_succeeds(self):
        next_day = self.tomorrow + timedelta(days=1)
        payload = self.create_payload(
            start_time="22:00",
            end_date=next_day.isoformat(),
            end_time="02:00",
            action="save",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.latest("pk")
        tz = ZoneInfo("America/New_York")
        local_start = event.starts_at.astimezone(tz)
        local_end = event.ends_at.astimezone(tz)
        self.assertEqual(local_start.date(), self.tomorrow)
        self.assertEqual(local_start.hour, 22)
        self.assertEqual(local_end.date(), next_day)
        self.assertEqual(local_end.hour, 2)

    def test_preview_action_shows_review_facts(self):
        payload = self.create_payload(
            title="Piano Recital",
            location="Carnegie Hall",
            participants=[self.kid.pk],
            action="preview",
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review your event")
        self.assertContains(response, "Piano Recital")
        self.assertContains(response, "Carnegie Hall")
        self.assertContains(response, "Riley")

    def test_unauthenticated_user_sees_can_save_notice(self):
        self.client.logout()
        response = self.client.get(reverse("events:create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "To save an event, choose a family where you’re a parent.",
        )
