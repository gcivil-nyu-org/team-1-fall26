from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from ..routing import RouteEstimate, estimate_travel_time


class TravelRoutingTests(SimpleTestCase):
    def setUp(self):
        self.start = SimpleNamespace(
            latitude=40.694542,
            longitude=-73.9867908,
            ends_at=datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc),
        )
        self.end = SimpleNamespace(
            latitude=40.7580,
            longitude=-73.9855,
        )

    @patch("events.routing.estimate_driving_seconds", return_value=1080)
    def test_driving_route(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "driving")

        self.assertEqual(result, RouteEstimate(18, "estimated"))
        mock_route.assert_called_once_with(self.start, self.end)

    @patch("events.routing.estimate_valhalla_seconds", return_value=4800)
    def test_walking_route(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "walking")

        self.assertEqual(result, RouteEstimate(80, "estimated"))
        mock_route.assert_called_once_with(self.start, self.end, "walking")

    @patch("events.routing.estimate_valhalla_seconds", return_value=1980)
    def test_cycling_route(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "cycling")

        self.assertEqual(result, RouteEstimate(33, "estimated"))
        mock_route.assert_called_once_with(self.start, self.end, "cycling")

    @patch("events.routing.estimate_transit_seconds", return_value=2040)
    def test_transit_route(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "transit")

        self.assertEqual(result, RouteEstimate(34, "estimated"))
        mock_route.assert_called_once_with(
            self.start,
            self.end,
            departure_time=self.start.ends_at,
        )

    def test_missing_coordinates(self):
        self.end.latitude = None

        for mode in ("driving", "walking", "cycling", "transit"):
            with self.subTest(mode=mode):
                result = estimate_travel_time(self.start, self.end, mode)
                self.assertEqual(result.status, "unverified")
                self.assertIsNone(result.duration_minutes)

    def test_invalid_transport_mode(self):
        result = estimate_travel_time(self.start, self.end, "flying")

        self.assertEqual(result, RouteEstimate(None, "unverified"))

    @patch("events.routing.estimate_valhalla_seconds", return_value=None)
    def test_unavailable_routing_service(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "walking")

        self.assertEqual(result, RouteEstimate(None, "unverified"))

    @patch("events.routing.estimate_driving_seconds", return_value=61)
    def test_duration_rounds_up(self, mock_route):
        result = estimate_travel_time(self.start, self.end, "driving")

        self.assertEqual(result.duration_minutes, 2)
