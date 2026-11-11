from datetime import datetime, timezone
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from events.ai_forms import AIEventSuggestionForm
from events.ai_suggestions import (
    EventSuggestionError,
    _validate_suggestion,
    generate_event_suggestion,
)


class AIEventSuggestionFormTests(SimpleTestCase):

    def test_accepts_valid_description(self):
        form = AIEventSuggestionForm(
            data={"description": ("Family dinner on November 20 at 6 PM.")}
        )
        self.assertTrue(form.is_valid())

    def test_rejects_short_description(self):
        form = AIEventSuggestionForm(data={"description": "Dinner"})
        self.assertFalse(form.is_valid())

    def test_rejects_empty_description(self):
        form = AIEventSuggestionForm(data={"description": ""})
        self.assertFalse(form.is_valid())


class AIEventValidationTests(SimpleTestCase):

    def setUp(self):
        self.valid_data = {
            "title": "Family dinner",
            "date": "2026-11-20",
            "start_time": "18:00",
            "end_date": None,
            "end_time": "20:00",
            "location": "Olive Garden",
        }

    def test_valid_event_details(self):
        result = _validate_suggestion(self.valid_data)
        self.assertEqual(result["title"], "Family dinner")
        self.assertEqual(result["start_time"], "18:00")
        self.assertEqual(result["missing_fields"], [])

    def test_missing_end_time_is_detected(self):
        self.valid_data["end_time"] = None
        result = _validate_suggestion(self.valid_data)
        self.assertIn("end_time", result["missing_fields"])

    def test_invalid_date_is_rejected(self):
        self.valid_data["date"] = "2026-99-99"
        with self.assertRaises(EventSuggestionError):
            _validate_suggestion(self.valid_data)

    def test_invalid_time_is_rejected(self):
        self.valid_data["start_time"] = "29:99"
        with self.assertRaises(EventSuggestionError):
            _validate_suggestion(self.valid_data)

    def test_unknown_fields_are_rejected(self):
        self.valid_data["extra"] = "unexpected"
        with self.assertRaises(EventSuggestionError):
            _validate_suggestion(self.valid_data)


class AIEventServiceTests(SimpleTestCase):

    def setUp(self):
        self.valid_data = {
            "title": "Family dinner",
            "date": "2026-11-20",
            "start_time": "18:00",
            "end_date": None,
            "end_time": "20:00",
            "location": "Olive Garden",
        }

    @patch.dict("os.environ", {"GROQ_API_KEY": "test-key"})
    @patch("events.ai_suggestions.requests.post")
    def test_generates_suggestion(self, mock_post):
        import json

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(self.valid_data),
                        }
                    ],
                }
            ],
        }
        mock_post.return_value = response

        result = generate_event_suggestion(
            "Family dinner on November 20 from 6 to 8 PM.",
            timezone_name="America/New_York",
            reference_datetime=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["title"], "Family dinner")
        self.assertEqual(result["date"], "2026-11-20")
        self.assertEqual(result["missing_fields"], [])
        mock_post.assert_called_once()

    def test_missing_api_key(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": ""}):
            with self.assertRaises(EventSuggestionError):
                generate_event_suggestion(
                    "Family dinner tomorrow at 6 PM.",
                    timezone_name="America/New_York",
                )

    @patch.dict("os.environ", {"GROQ_API_KEY": "test-key"})
    @patch("events.ai_suggestions.requests.post")
    def test_invalid_ai_response(self, mock_post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "output": [],
        }
        mock_post.return_value = response

        with self.assertRaises(EventSuggestionError):
            generate_event_suggestion(
                "Family dinner tomorrow at 6 PM.",
                timezone_name="America/New_York",
            )
