from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from events.models import Event


class AIEventSuggestionViewTests(TestCase):
    """Integration tests for AI-assisted event creation."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        cls.caregiver = User.objects.create_user(
            username="ai_parent",
            password="TestPassword123!",
        )

        cls.kid = User.objects.create_user(
            username="ai_kid",
            password="TestPassword123!",
        )

        cls.outsider = User.objects.create_user(
            username="ai_outsider",
            password="TestPassword123!",
        )

        cls.workspace = Workspace.objects.create(
            name="AI Test Family",
            primary_admin=cls.caregiver,
        )

        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.caregiver,
            accepted_at=timezone.now(),
        )

    def setUp(self):
        self.url = reverse(
            "events:workspace_create",
            args=[self.workspace.pk],
        )

        self.description = (
            "Family dinner on November 20, 2026 " "from 6 PM to 8 PM at Olive Garden."
        )

        self.suggestion = {
            "title": "Family dinner",
            "date": "2026-11-20",
            "start_time": "18:00",
            "end_date": None,
            "end_time": "20:00",
            "location": "Olive Garden",
            "missing_fields": [],
        }

    def generate(self):
        return self.client.post(
            self.url,
            {
                "action": "generate_suggestion",
                "description": self.description,
                "timezone": "America/New_York",
            },
        )

    @patch("events.views.generate_event_suggestion")
    def test_caregiver_can_generate_suggestion(self, mock_ai):
        """An authorized caregiver can request an AI draft."""

        mock_ai.return_value = self.suggestion

        self.client.force_login(self.caregiver)

        response = self.generate()

        self.assertEqual(response.status_code, 200)

        self.assertTrue(response.context["ai_suggestion_generated"])

        form = response.context["form"]

        self.assertEqual(
            form.initial["title"],
            "Family dinner",
        )

        self.assertEqual(
            form.initial["date"],
            "2026-11-20",
        )

        self.assertEqual(
            form.initial["start_time"],
            "18:00",
        )

        self.assertEqual(
            form.initial["end_time"],
            "20:00",
        )

        self.assertEqual(
            form.initial["location"],
            "Olive Garden",
        )

        mock_ai.assert_called_once()

    @patch("events.views.generate_event_suggestion")
    def test_kid_cannot_generate_suggestion(self, mock_ai):
        """Kids cannot use caregiver-only AI generation."""

        self.client.force_login(self.kid)

        response = self.generate()

        self.assertEqual(response.status_code, 404)

        mock_ai.assert_not_called()

    @patch("events.views.generate_event_suggestion")
    def test_outsider_cannot_generate_suggestion(self, mock_ai):
        """Users outside the family cannot generate its drafts."""

        self.client.force_login(self.outsider)

        response = self.generate()

        self.assertEqual(response.status_code, 404)

        mock_ai.assert_not_called()

    @patch("events.views.generate_event_suggestion")
    def test_suggestion_does_not_create_event(self, mock_ai):
        """Generating an AI suggestion must never save an event."""

        mock_ai.return_value = self.suggestion

        self.client.force_login(self.caregiver)

        before_count = Event.objects.count()

        response = self.generate()

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            Event.objects.count(),
            before_count,
        )

        self.assertTrue(response.context["ai_suggestion_generated"])

    @patch("events.views.generate_event_suggestion")
    def test_generated_draft_can_be_edited(self, mock_ai):
        """Caregivers can edit suggested details before saving."""

        mock_ai.return_value = self.suggestion

        self.client.force_login(self.caregiver)

        response = self.generate()

        self.assertEqual(response.status_code, 200)

        generated_form = response.context["form"]

        # Preserve the original signed submission token.
        token = generated_form["submission_token"].value()

        # Caregiver changes the AI-generated event title.
        edited_data = {
            "action": "preview",
            "submission_token": token,
            "title": "Friday family dinner",
            "date": "2026-11-20",
            "start_time": "18:00",
            "end_time": "20:00",
            "timezone": "America/New_York",
            "location": "Olive Garden",
            "transport_mode": Event.TransportMode.TRANSIT,
        }

        preview_response = self.client.post(
            self.url,
            edited_data,
        )

        self.assertEqual(
            preview_response.status_code,
            200,
        )

        preview_form = preview_response.context["form"]

        self.assertTrue(
            preview_form.is_valid(),
            preview_form.errors,
        )

        self.assertEqual(
            preview_response.context["event_details"].title,
            "Friday family dinner",
        )

        # Previewing the edited event does not save it.
        self.assertEqual(Event.objects.count(), 0)

        # The AI service should not be called a second time.
        mock_ai.assert_called_once()

    @patch("events.views.generate_event_suggestion")
    def test_missing_fields_are_shown_to_caregiver(
        self,
        mock_ai,
    ):
        """Missing AI details are identified before event creation."""

        incomplete_suggestion = {
            **self.suggestion,
            "end_time": None,
            "missing_fields": ["end_time"],
        }

        mock_ai.return_value = incomplete_suggestion

        self.client.force_login(self.caregiver)

        response = self.generate()

        self.assertEqual(response.status_code, 200)

        self.assertIn(
            "end_time",
            response.context["ai_missing_fields"],
        )

        form = response.context["form"]

        self.assertFalse(form.initial.get("end_time"))

        self.assertEqual(Event.objects.count(), 0)
