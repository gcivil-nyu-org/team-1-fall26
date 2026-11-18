"""The trip card stays fast and cheap when a planner or the MTA misbehaves, and the
quick time change reads clock-change times like the Edit event form."""

import time as time_module
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace

from .. import mta, trip, upstream
from ..models import Event

NEW_YORK = ZoneInfo("America/New_York")
TIMES_SQ = (40.755, -73.987)
UNION_SQ = (40.7357, -73.9906)


def http_error(status):
    response = mock.Mock(status_code=status)
    return requests.HTTPError(f"{status} error", response=response)


def failing_response(status):
    response = mock.Mock(status_code=status)
    response.raise_for_status.side_effect = http_error(status)
    return response


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class GoogleCoolOffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.arrive_by = datetime(2026, 11, 20, 15, 0, tzinfo=NEW_YORK)

    def trips(self, count):
        """`count` different trips (different cache keys)."""
        return [((40.70 + step / 100, -73.99), UNION_SQ) for step in range(count)]

    def test_a_failed_trip_is_not_asked_again_for_a_minute(self):
        with mock.patch.object(
            trip.requests, "post", side_effect=requests.Timeout("slow")
        ) as post:
            with self.assertRaises(requests.Timeout):
                trip.request_routes(TIMES_SQ, UNION_SQ, self.arrive_by)
            with self.assertRaises(upstream.RecentlyUnavailable):
                trip.request_routes(TIMES_SQ, UNION_SQ, self.arrive_by)
        self.assertEqual(post.call_count, 1)

    def test_one_failing_trip_does_not_switch_google_off_for_everyone(self):
        with mock.patch.object(
            trip.requests, "post", return_value=failing_response(500)
        ):
            with self.assertRaises(requests.HTTPError):
                trip.request_routes(TIMES_SQ, UNION_SQ, self.arrive_by)
        self.assertFalse(upstream.is_cooling_off("google-routes"))
        # Someone else's trip is still asked.
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"routes": []}
        ok.raise_for_status.return_value = None
        with mock.patch.object(trip.requests, "post", return_value=ok) as post:
            trip.request_routes(UNION_SQ, TIMES_SQ, self.arrive_by)
        post.assert_called()

    def test_several_different_failures_mean_google_is_down(self):
        with mock.patch.object(
            trip.requests, "post", side_effect=requests.Timeout("slow")
        ) as post:
            for origin, destination in self.trips(upstream.OUTAGE_THRESHOLD):
                with self.assertRaises(requests.Timeout):
                    trip.request_routes(origin, destination, self.arrive_by)
            self.assertTrue(upstream.is_cooling_off("google-routes"))
            with self.assertRaises(upstream.RecentlyUnavailable):
                trip.request_routes(UNION_SQ, TIMES_SQ, self.arrive_by)
        self.assertEqual(post.call_count, upstream.OUTAGE_THRESHOLD)

    def test_a_rate_limit_from_google_counts_at_once(self):
        with mock.patch.object(
            trip.requests, "post", return_value=failing_response(429)
        ):
            with self.assertRaises(requests.HTTPError):
                trip.request_routes(TIMES_SQ, UNION_SQ, self.arrive_by)
        self.assertTrue(upstream.is_cooling_off("google-routes"))

    def test_a_bad_request_for_one_trip_does_not_switch_google_off(self):
        with mock.patch.object(
            trip.requests, "post", return_value=failing_response(400)
        ) as post:
            for origin in (TIMES_SQ, UNION_SQ):
                with self.assertRaises(requests.HTTPError):
                    trip.request_routes(origin, (40.7, -74.0), self.arrive_by)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(upstream.is_cooling_off("google-routes"))

    def test_uses_a_short_connect_timeout(self):
        payload = {"routes": []}
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        with mock.patch.object(trip.requests, "post", return_value=response) as post:
            trip.request_routes(TIMES_SQ, UNION_SQ, self.arrive_by)
        connect, read = post.call_args.kwargs["timeout"]
        self.assertLessEqual(connect, 5)
        self.assertLessEqual(read, 10)


@override_settings(GOOGLE_MAPS_API_KEY="")
class TransitousCoolOffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.arrive_by = datetime(2026, 11, 20, 15, 0, tzinfo=NEW_YORK)

    def test_a_failed_trip_is_skipped_but_others_are_still_asked(self):
        with mock.patch.object(
            trip.requests, "get", return_value=failing_response(503)
        ) as get:
            with self.assertRaises(requests.HTTPError):
                trip.transitous_options(TIMES_SQ, UNION_SQ, self.arrive_by)
            with self.assertRaises(upstream.RecentlyUnavailable):
                trip.transitous_options(TIMES_SQ, UNION_SQ, self.arrive_by)
            with self.assertRaises(requests.HTTPError):
                trip.transitous_options(UNION_SQ, TIMES_SQ, self.arrive_by)
        self.assertEqual(get.call_count, 2)
        self.assertFalse(upstream.is_cooling_off("transitous"))


class MtaCoolOffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_keeps_serving_the_stale_copy_without_asking_again(self):
        old = {"data": ["old"], "fetched": time_module.time() - 10 * 60}
        cache.set("mta-feed:ace", old, mta.STALE_KEEP_SECONDS)
        with mock.patch.object(
            mta.requests, "get", side_effect=requests.ConnectionError("down")
        ) as get:
            first = mta.feed_snapshot("ace")
            second = mta.feed_snapshot("ace")
        self.assertEqual(get.call_count, 1)
        self.assertTrue(first["stale"])
        self.assertTrue(second["stale"])
        self.assertEqual(second["data"], ["old"])

    def test_with_no_copy_it_fails_fast_during_the_cool_off(self):
        with mock.patch.object(
            mta.requests, "get", side_effect=requests.Timeout("slow")
        ) as get:
            with self.assertRaises(requests.Timeout):
                mta.feed_snapshot("ace")
            with self.assertRaises(upstream.RecentlyUnavailable):
                mta.feed_snapshot("ace")
        self.assertEqual(get.call_count, 1)

    def test_one_feed_down_does_not_stop_another(self):
        with mock.patch.object(
            mta.requests, "get", side_effect=requests.Timeout("slow")
        ):
            with self.assertRaises(requests.Timeout):
                mta.feed_snapshot("ace")
        self.assertFalse(upstream.is_cooling_off("mta-feed:nqrw"))

    def test_a_feed_that_cannot_be_parsed_is_not_an_outage(self):
        response = mock.Mock(status_code=200, content=b"not a feed")
        response.raise_for_status.return_value = None
        with mock.patch.object(
            mta.requests, "get", return_value=response
        ), mock.patch.object(mta, "parse_feed", side_effect=ValueError("bad")):
            with self.assertRaises(ValueError):
                mta.feed_snapshot("ace")
        self.assertFalse(upstream.is_cooling_off("mta-feed:ace"))


class TripStartLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="x")
        cls.kid = User.objects.create_user(username="Lucia", password="x")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.admin,
            accepted_at=django_timezone.now(),
        )
        starts = django_timezone.now() + timedelta(days=2)
        # Driving, so the card needs no planner and the test makes no network calls.
        cls.event = Event.objects.create(
            created_by=cls.admin,
            workspace=cls.workspace,
            title="Practice",
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            latitude=UNION_SQ[0],
            longitude=UNION_SQ[1],
            transport_mode=Event.TransportMode.DRIVING,
        )

    def setUp(self):
        cache.clear()
        self.url = reverse("events:trip", args=[self.event.pk])

    def post_here(self, latitude):
        return self.client.post(
            self.url, {"mode": "here", "latitude": latitude, "longitude": -73.99}
        )

    @override_settings(RATELIMIT_ENABLED=True)
    def test_stops_a_script_that_keeps_changing_the_start(self):
        from ..views import TRIP_STARTS_PER_HOUR

        self.client.force_login(self.kid)
        for step in range(TRIP_STARTS_PER_HOUR):
            self.assertEqual(self.post_here(40.70 + step / 1000).status_code, 200)
        self.assertEqual(self.post_here(40.69).status_code, 429)
        # Reading the card is not limited.
        self.assertEqual(self.client.get(self.url).status_code, 200)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_counts_per_account_so_a_shared_wifi_is_fine(self):
        from ..views import TRIP_STARTS_PER_HOUR

        self.client.force_login(self.kid)
        for step in range(TRIP_STARTS_PER_HOUR):
            self.post_here(40.70 + step / 1000)
        self.client.force_login(self.admin)  # same test client IP
        self.assertEqual(self.post_here(40.71).status_code, 200)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_only_new_places_count_toward_the_limit(self):
        from ..views import TRIP_STARTS_PER_HOUR

        self.client.force_login(self.kid)
        for _ in range(TRIP_STARTS_PER_HOUR + 5):
            # Fixed starting places (cached trips) and typos don't use up the limit.
            self.client.post(self.url, {"mode": "home"})
            self.client.post(self.url, {"mode": "here", "latitude": "nope"})
        self.assertEqual(self.post_here(40.71).status_code, 200)


class EventZoneTests(SimpleTestCase):
    def test_marker_labels_use_the_events_zone(self):
        moment = datetime(2026, 11, 20, 14, 5, tzinfo=datetime_timezone.utc)
        paris = mock.Mock(timezone_name="Europe/Paris")
        labels = trip._clock_labels(moment, trip.event_zone(paris))
        self.assertEqual(labels, {"label": "3:05 PM", "clock": "15:05"})

    def test_a_bad_zone_falls_back_to_the_servers(self):
        broken = mock.Mock(timezone_name="Not/AZone")
        self.assertEqual(
            trip.event_zone(broken), django_timezone.get_current_timezone()
        )


class ChangeTimeClockChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username="Ana", password="x")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)

    def setUp(self):
        starts = datetime(2027, 3, 13, 16, 0, tzinfo=NEW_YORK)
        self.event = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Soccer",
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        self.client.force_login(self.admin)

    def change(self, day, start, end):
        return self.client.post(
            reverse("events:change_time", args=[self.event.pk]),
            {
                "starts_date": day,
                "starts_time": start,
                "ends_date": day,
                "ends_time": end,
            },
            follow=True,
        )

    def test_a_time_in_the_spring_forward_gap_is_refused_and_named(self):
        response = self.change("2027-03-14", "02:30", "04:00")
        self.event.refresh_from_db()
        self.assertEqual(
            self.event.starts_at, datetime(2027, 3, 13, 16, 0, tzinfo=NEW_YORK)
        )
        self.assertContains(response, "spring forward")

    def test_a_normal_time_on_the_change_day_still_saves(self):
        self.change("2027-03-14", "09:00", "10:00")
        self.event.refresh_from_db()
        self.assertEqual(
            self.event.starts_at, datetime(2027, 3, 14, 9, 0, tzinfo=NEW_YORK)
        )
