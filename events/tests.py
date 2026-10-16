from datetime import date, time

from django.test import SimpleTestCase

from .forms import EventForm


class EventFormTests(SimpleTestCase):
    def setUp(self):
        self.event_data = {
            "title": "Soccer practice",
            "date": "2026-10-20",
            "start_time": "15:30",
            "end_time": "16:30",
            "location": "School field",
        }

    def test_valid_event(self):
        form = EventForm(data=self.event_data)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["date"], date(2026, 10, 20))
        self.assertEqual(form.cleaned_data["start_time"], time(15, 30))
        self.assertEqual(form.cleaned_data["end_time"], time(16, 30))

    def test_location_is_optional(self):
        self.event_data.pop("location")
        form = EventForm(data=self.event_data)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["location"], "")

    def test_missing_required_fields(self):
        for field in ("title", "date", "start_time", "end_time"):
            with self.subTest(field=field):
                data = self.event_data.copy()
                data.pop(field)
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors.as_data()[field][0].code, "required")

    def test_blank_title_is_rejected(self):
        self.event_data["title"] = "   "
        form = EventForm(data=self.event_data)

        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_end_time_must_be_after_start_time(self):
        for end_time in ("15:30", "14:30"):
            with self.subTest(end_time=end_time):
                data = {**self.event_data, "end_time": end_time}
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["end_time"], ["End time must be after start time."]
                )

    def test_invalid_dates_and_times(self):
        for field, value in (
            ("date", "2026-02-30"),
            ("start_time", "25:00"),
            ("end_time", "not a time"),
        ):
            with self.subTest(field=field):
                data = {**self.event_data, field: value}
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors.as_data()[field][0].code, "invalid")
