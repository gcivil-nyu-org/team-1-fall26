from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace

from . import routing
from .models import Event
from .travel_conflicts import find_travel_checks

NEW_YORK = ZoneInfo("America/New_York")


def place(lat=40.7, lon=-74.0, ends_at=None):
    return SimpleNamespace(latitude=lat, longitude=lon, ends_at=ends_at)


def fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class ValidCoordinatesTests(SimpleTestCase):
    def test_accepts_normal_coordinates(self):
        self.assertTrue(routing.valid_coordinates(place()))

    def test_rejects_missing_non_numeric_and_out_of_range(self):
        self.assertFalse(routing.valid_coordinates(place(lat=None)))
        self.assertFalse(routing.valid_coordinates(place(lon="x")))
        self.assertFalse(routing.valid_coordinates(place(lat=91)))
        self.assertFalse(routing.valid_coordinates(place(lon=-181)))
        self.assertFalse(routing.valid_coordinates(place(lat=float("nan"))))


class DrivingTests(SimpleTestCase):
    @mock.patch("events.routing.requests.get")
    def test_returns_duration(self, get):
        get.return_value = fake_response({"code": "Ok", "routes": [{"duration": 600}]})
        self.assertEqual(routing.estimate_driving_seconds(place(), place()), 600)

    @mock.patch("events.routing.requests.get")
    def test_returns_none_without_route(self, get):
        get.return_value = fake_response({"code": "NoRoute", "routes": []})
        self.assertIsNone(routing.estimate_driving_seconds(place(), place()))


class ValhallaTests(SimpleTestCase):
    @mock.patch("events.routing.requests.post")
    def test_walking_returns_time(self, post):
        post.return_value = fake_response({"trip": {"summary": {"time": 900}}})
        self.assertEqual(routing.estimate_valhalla_seconds(place(), place(), "walking"), 900)

    @mock.patch("events.routing.requests.post")
    def test_transit_needs_a_transit_leg(self, post):
        walk_only = {"trip": {"summary": {"time": 900}, "legs": [{"maneuvers": [{"travel_type": "foot"}]}]}}
        post.return_value = fake_response(walk_only)
        self.assertIsNone(routing.estimate_valhalla_seconds(place(), place(), "transit"))

    @mock.patch("events.routing.requests.post")
    def test_transit_accepts_transit_info_and_travel_type(self, post):
        for maneuver in ({"transit_info": {"x": 1}}, {"travel_type": "metro"}):
            post.return_value = fake_response(
                {"trip": {"summary": {"time": 1200}, "legs": [{"maneuvers": [maneuver]}]}}
            )
            self.assertEqual(routing.estimate_valhalla_seconds(place(), place(), "transit"), 1200)


class OpenTripPlannerTests(SimpleTestCase):
    def itineraries(self, *items):
        return {"data": {"plan": {"itineraries": list(items)}}}

    @mock.patch("events.routing.requests.post")
    def test_picks_shortest_transit_itinerary(self, post):
        post.return_value = fake_response(
            self.itineraries(
                {"duration": 2000, "legs": [{"mode": "SUBWAY"}]},
                {"duration": 1500, "legs": [{"mode": "BUS"}]},
                {"duration": 300, "legs": [{"mode": "WALK"}]},
            )
        )
        self.assertEqual(routing.estimate_transit_seconds(place(), place()), 1500)

    @mock.patch("events.routing.requests.post")
    def test_uses_given_departure_time(self, post):
        post.return_value = fake_response(self.itineraries())
        departure = datetime(2026, 10, 29, 12, 0, tzinfo=NEW_YORK)
        self.assertIsNone(routing.estimate_transit_seconds(place(), place(), departure))
        variables = post.call_args.kwargs["json"]["variables"]
        self.assertEqual((variables["date"], variables["time"]), ("2026-10-29", "12:00"))

    @mock.patch("events.routing.requests.post")
    def test_graphql_errors_return_none(self, post):
        post.return_value = fake_response({"errors": [{"message": "boom"}]})
        self.assertIsNone(routing.estimate_transit_seconds(place(), place()))


class EstimateTravelTimeTests(SimpleTestCase):
    def test_unsupported_mode_is_unverified(self):
        self.assertEqual(routing.estimate_travel_time(place(), place(), "teleport").status, "unverified")

    def test_bad_coordinates_are_unverified(self):
        self.assertEqual(routing.estimate_travel_time(place(lat=None), place(), "driving").status, "unverified")
        self.assertEqual(routing.estimate_travel_time(place(), place(lon=None), "driving").status, "unverified")

    @mock.patch("events.routing.estimate_driving_seconds", return_value=610)
    def test_driving_rounds_up_to_minutes(self, _):
        result = routing.estimate_travel_time(place(), place(), "driving")
        self.assertEqual((result.duration_minutes, result.status), (11, "estimated"))

    @mock.patch("events.routing.estimate_valhalla_seconds", return_value=600)
    def test_walking_uses_valhalla(self, valhalla):
        result = routing.estimate_travel_time(place(), place(), "walking")
        self.assertEqual(result.duration_minutes, 10)
        self.assertEqual(valhalla.call_args.args[2], "walking")

    @mock.patch("events.routing.estimate_transit_seconds", return_value=1200)
    def test_transit_uses_otp(self, _):
        self.assertEqual(routing.estimate_travel_time(place(), place(), "transit").duration_minutes, 20)

    @mock.patch("events.routing.estimate_driving_seconds", return_value=None)
    def test_missing_duration_is_unverified(self, _):
        self.assertEqual(routing.estimate_travel_time(place(), place(), "driving").status, "unverified")

    @mock.patch("events.routing.estimate_driving_seconds", side_effect=requests.ConnectionError)
    def test_network_failure_is_unverified(self, _):
        result = routing.estimate_travel_time(place(), place(), "driving")
        self.assertEqual((result.duration_minutes, result.status), (None, "unverified"))


class TravelCheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.kid = User.objects.create_user(username="Lucia")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace, user=cls.kid, role=Membership.Role.KID,
            invited_by=cls.admin, accepted_at=django_timezone.now(),
        )
        cls.start = datetime(2026, 10, 29, 9, 0, tzinfo=NEW_YORK)

    def make(self, title, offset_hours, location="Somewhere", mode="driving"):
        starts = self.start + timedelta(hours=offset_hours)
        event = Event.objects.create(
            created_by=self.admin, workspace=self.workspace, title=title,
            starts_at=starts, ends_at=starts + timedelta(minutes=30),
            location=location, transport_mode=mode,
        )
        event.participants.set([self.kid])
        return event

    def test_event_without_workspace_has_no_checks(self):
        self.assertEqual(find_travel_checks(SimpleNamespace(workspace_id=None), [self.kid]), [])

    @mock.patch("events.travel_conflicts.estimate_travel_time")
    def test_flags_insufficient_and_sufficient_gaps(self, estimate):
        before = self.make("Before", 0)
        main = self.make("Main", 1)  # 30 min gap after Before
        after = self.make("After", 3)  # 90 min gap after Main
        estimate.return_value = routing.RouteEstimate(45, "estimated")
        checks = find_travel_checks(main, [self.kid])
        by_pair = {(c.previous_event.pk, c.next_event.pk): c for c in checks}
        self.assertEqual(by_pair[(before.pk, main.pk)].status, "insufficient")
        self.assertEqual(by_pair[(before.pk, main.pk)].available_minutes, 30)
        self.assertEqual(by_pair[(main.pk, after.pk)].status, "sufficient")

    @mock.patch("events.travel_conflicts.estimate_travel_time")
    def test_unverified_status_passes_through(self, estimate):
        self.make("Before", 0)
        main = self.make("Main", 1)
        estimate.return_value = routing.RouteEstimate(None, "unverified")
        checks = find_travel_checks(main, [self.kid])
        self.assertEqual([c.status for c in checks], ["unverified"])

    @mock.patch("events.travel_conflicts.estimate_travel_time")
    def test_skips_neighbours_without_location(self, estimate):
        self.make("Before", 0, location="  ")
        main = self.make("Main", 1)
        self.assertEqual(find_travel_checks(main, [self.kid]), [])
        estimate.assert_not_called()
