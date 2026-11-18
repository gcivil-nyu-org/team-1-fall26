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

from .. import mta, trip
from ..models import Event, TripStart

NEW_YORK = ZoneInfo("America/New_York")
TIMES_SQ = (40.755, -73.987)
UNION_SQ = (40.7357, -73.9906)


def fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def iso(moment):
    return moment.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def route_payload(depart, arrive):
    """A Routes API answer: walk 4 min, ride the Q 3 stops, walk 6 min."""
    return {
        "routes": [
            {
                "duration": "1380s",
                "legs": [
                    {
                        "steps": [
                            {"travelMode": "WALK", "staticDuration": "120s"},
                            {"travelMode": "WALK", "staticDuration": "120s"},
                            {
                                "travelMode": "TRANSIT",
                                "staticDuration": "600s",
                                "transitDetails": {
                                    "stopDetails": {
                                        "departureStop": {
                                            "name": "Times Sq-42 St",
                                            "location": {
                                                "latLng": {
                                                    "latitude": TIMES_SQ[0],
                                                    "longitude": TIMES_SQ[1],
                                                }
                                            },
                                        },
                                        "arrivalStop": {
                                            "name": "14 St-Union Sq",
                                            "location": {
                                                "latLng": {
                                                    "latitude": UNION_SQ[0],
                                                    "longitude": UNION_SQ[1],
                                                }
                                            },
                                        },
                                        "departureTime": iso(depart),
                                        "arrivalTime": iso(arrive),
                                    },
                                    "headsign": "Coney Island-Stillwell Av",
                                    "stopCount": 3,
                                    "transitLine": {
                                        "name": "Broadway Express",
                                        "nameShort": "Q",
                                        "color": "#fccc0a",
                                        "textColor": "#000000",
                                        "vehicle": {
                                            "type": "SUBWAY",
                                            "name": {"text": "Subway"},
                                        },
                                    },
                                },
                            },
                            {"travelMode": "WALK", "staticDuration": "360s"},
                        ]
                    }
                ],
            }
        ]
    }


def transitous_leg(mode, start, end, **extra):
    return {
        "mode": mode,
        "startTime": iso(start),
        "endTime": iso(end),
        "duration": int((end - start).total_seconds()),
        **extra,
    }


def transitous_itinerary(depart, minutes=6, transfers=0, walk=4, line="N"):
    """Walk to Times Sq, ride `line` for `minutes`, walk to Union Sq."""
    board = depart
    off = board + timedelta(minutes=minutes)
    start = board - timedelta(minutes=walk)
    end = off + timedelta(minutes=walk)
    stop = trip.SUBWAY_ROUTE_PREFIX
    return {
        "startTime": iso(start),
        "endTime": iso(end),
        "transfers": transfers,
        "legs": [
            transitous_leg("WALK", start, board),
            transitous_leg(
                "SUBWAY",
                board,
                off,
                routeId=f"{stop}{line}",
                routeShortName=line,
                routeLongName="Broadway Express",
                routeColor="f6bc26",
                routeTextColor="000000",
                headsign="Coney Island-Stillwell Av",
                intermediateStops=[
                    {"name": "34 St-Herald Sq", "stopId": f"{stop}R17S"}
                ],
                **{
                    "from": {
                        "name": "Times Sq-42 St",
                        "stopId": f"{stop}R16S",
                        "lat": TIMES_SQ[0],
                        "lon": TIMES_SQ[1],
                    },
                    "to": {
                        "name": "14 St-Union Sq",
                        "stopId": f"{stop}R20S",
                        "lat": UNION_SQ[0],
                        "lon": UNION_SQ[1],
                    },
                },
            ),
            transitous_leg("WALK", off, end),
        ],
    }


def transitous_payload(depart, arrive_by):
    """Three options, as Transitous sends them: two arrive in time, one too late."""
    return {
        "itineraries": [
            transitous_itinerary(depart - timedelta(minutes=10)),
            transitous_itinerary(depart),
            transitous_itinerary(arrive_by),
        ],
        "direct": [],
    }


# Planned with the free Transitous path, whatever key the local .env has.
@override_settings(GOOGLE_MAPS_API_KEY="")
class TransitousPlanTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.arrive_by = datetime(2026, 11, 12, 15, 0, 30, 123456, tzinfo=NEW_YORK)

    def plan(self, payload):
        with mock.patch.object(
            trip.requests, "get", return_value=fake_response(payload)
        ) as get:
            plan = trip.plan_with_transitous(TIMES_SQ, UNION_SQ, self.arrive_by)
        return plan, get.call_args.kwargs["params"]

    def test_takes_the_latest_trip_that_still_arrives_in_time(self):
        depart = self.arrive_by - timedelta(minutes=20)

        plan, _ = self.plan(transitous_payload(depart, self.arrive_by))

        self.assertEqual(plan["leave_by"], depart - timedelta(minutes=4))
        self.assertEqual(
            [segment["kind"] for segment in plan["segments"]], ["walk", "ride", "walk"]
        )
        ride = plan["segments"][1]
        self.assertEqual(
            (ride["line"], ride["stops"], ride["color"]), ("N", 2, "#f6bc26")
        )
        self.assertEqual(ride["from_point"], TIMES_SQ)

    def test_sends_whole_seconds_because_fractions_confuse_transitous(self):
        _, params = self.plan({"itineraries": []})

        self.assertEqual(params["time"], "2026-11-12T20:00:30Z")
        self.assertEqual(params["fromPlace"], f"{TIMES_SQ[0]},{TIMES_SQ[1]}")

    def test_ignores_trips_from_another_day(self):
        wrong_day = transitous_itinerary(self.arrive_by - timedelta(days=2))

        plan, _ = self.plan({"itineraries": [wrong_day]})

        self.assertIsNone(plan)

    def test_walk_only_direct_option(self):
        start = self.arrive_by - timedelta(minutes=25)
        walk = {
            "startTime": iso(start),
            "endTime": iso(start + timedelta(minutes=20)),
            "legs": [transitous_leg("WALK", start, start + timedelta(minutes=20))],
        }

        plan, _ = self.plan({"itineraries": [], "direct": [walk]})

        self.assertTrue(plan["walk_only"])
        self.assertEqual(plan["segments"][0]["minutes"], 20)


class SummarizeRouteTests(SimpleTestCase):
    def test_walks_merge_and_leave_by_counts_the_walk_to_the_station(self):
        depart = datetime(2026, 11, 12, 14, 41, tzinfo=NEW_YORK)
        arrive = depart + timedelta(minutes=10)

        plan = trip.summarize_route(route_payload(depart, arrive)["routes"][0], arrive)

        kinds = [segment["kind"] for segment in plan["segments"]]
        self.assertEqual(kinds, ["walk", "ride", "walk"])
        self.assertEqual(plan["segments"][0]["minutes"], 4)
        ride = plan["segments"][1]
        self.assertEqual(
            (ride["line"], ride["from"], ride["to"], ride["stops"]),
            ("Q", "Times Sq-42 St", "14 St-Union Sq", 3),
        )
        self.assertEqual(plan["leave_by"], depart - timedelta(minutes=4))
        self.assertEqual(plan["arrive"], arrive + timedelta(minutes=6))
        self.assertFalse(plan["walk_only"])

    def test_walk_only_route_leaves_its_duration_before_the_start(self):
        arrive_by = datetime(2026, 11, 12, 15, tzinfo=NEW_YORK)
        route = {
            "duration": "900s",
            "legs": [{"steps": [{"travelMode": "WALK", "staticDuration": "900s"}]}],
        }

        plan = trip.summarize_route(route, arrive_by)

        self.assertTrue(plan["walk_only"])
        self.assertEqual(plan["leave_by"], arrive_by - timedelta(minutes=15))


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def stop_update(stop_id, when):
    return field(2, field(4, stop_id) + field(3, field(2, when)))


def trip_entity(route_id, stops):
    update = field(1, field(1, "trip-1") + field(5, route_id)) + b"".join(
        stop_update(*stop) for stop in stops
    )
    return field(2, field(1, "e1") + field(3, update))


class MtaFeedTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 19, 0, tzinfo=datetime_timezone.utc)
        base = int(self.now.timestamp())
        header = field(1, field(1, "2.0") + field(3, base))
        self.feed = header + b"".join(
            [
                # Downtown Q: Times Sq (R16S) then Union Sq (R20S), in 2 and 9 minutes.
                trip_entity(
                    "Q",
                    [("R16S", base + 120), ("R17S", base + 200), ("R20S", base + 400)],
                ),
                trip_entity("Q", [("R16S", base + 540), ("R20S", base + 800)]),
                # Uptown Q passes Union Sq first, so it is going the wrong way.
                trip_entity("Q", [("R20S", base + 60), ("R16S", base + 300)]),
                # Another line at the same station.
                trip_entity("N", [("R16S", base + 30), ("R20S", base + 300)]),
                # Already left.
                trip_entity("Q", [("R16S", base - 60), ("R20S", base + 200)]),
            ]
        )

    def test_parses_routes_stops_and_times(self):
        trips = mta.parse_feed(self.feed)

        self.assertEqual(len(trips), 5)
        self.assertEqual(trips[0].route_id, "Q")
        self.assertEqual(
            trips[0].stops[0], mta.StopTime("R16S", int(self.now.timestamp()) + 120)
        )

    def test_next_departures_picks_the_line_direction_and_future_trains(self):
        stations = (
            mta.Station("R16", "Times Sq-42 St", *TIMES_SQ),
            mta.Station("R20", "14 St-Union Sq", *UNION_SQ),
        )
        with mock.patch.object(
            mta, "stations", return_value=stations
        ), mock.patch.object(
            mta, "fetch_trips", return_value=mta.parse_feed(self.feed)
        ):
            departures = mta.next_departures("Q", TIMES_SQ, UNION_SQ, now=self.now)

        self.assertEqual(
            departures,
            [self.now + timedelta(minutes=2), self.now + timedelta(minutes=9)],
        )

    def test_unknown_line_or_far_station_gives_nothing(self):
        self.assertEqual(
            mta.next_departures("Z9", TIMES_SQ, UNION_SQ, now=self.now), []
        )
        self.assertEqual(mta.next_departures("Q", (0, 0), UNION_SQ, now=self.now), [])

    def test_bundled_station_list_loads(self):
        names = {station.name for station in mta.stations()}
        self.assertIn("Times Sq-42 St", names)
        self.assertGreater(len(mta.stations()), 400)


def alert(kind, routes, stations=(), start=0, end=0, header="Heads up"):
    return mta.Alert(
        id=kind,
        kind=kind,
        header=header,
        routes=frozenset(routes),
        stations=frozenset(stations),
        periods=((start, end),),
    )


class AlertFeedTests(SimpleTestCase):
    def test_parses_types_lines_stations_and_periods(self):
        content = b"""{"entity": [{"id": "a1", "alert": {
            "active_period": [{"start": 100, "end": 200}],
            "informed_entity": [
                {"route_id": "Q"}, {"route_id": "Q", "stop_id": "R16S"}],
            "header_text": {"translation": [
                {"text": "<b>no</b>", "language": "en-html"},
                {"text": "No  [Q] at Times Sq", "language": "en"}]},
            "transit_realtime.mercury_alert": {
                "alert_type": "Planned - Stops Skipped"}}},
            {"id": "a2", "alert": {"informed_entity": [{"agency_id": "MTA"}]}}]}"""

        [parsed] = mta.parse_alerts(content)

        self.assertEqual(parsed.kind, "Planned - Stops Skipped")
        self.assertEqual(parsed.base_kind, "Stops Skipped")
        self.assertTrue(parsed.blocks and parsed.is_planned)
        self.assertEqual(parsed.routes, {"Q"})
        self.assertEqual(parsed.stations, {"R16"})
        self.assertEqual(parsed.periods, ((100, 200),))
        self.assertEqual(parsed.header, "No [Q] at Times Sq")

    def test_active_during_handles_open_ended_alerts(self):
        now = datetime(2026, 11, 12, 15, tzinfo=NEW_YORK)
        ts = int(now.timestamp())
        hour = timedelta(hours=1)

        self.assertTrue(alert("Delays", ["Q"], start=ts - 60).active_during(now, now))
        self.assertFalse(
            alert("Delays", ["Q"], start=ts + 7200, end=ts + 9000).active_during(
                now, now + hour
            )
        )


# Planned with the free Transitous path, whatever key the local .env has.
@override_settings(GOOGLE_MAPS_API_KEY="")
class ChooseTripTests(SimpleTestCase):
    """Leave as late as possible, arrive on time, avoid suspended stations."""

    def setUp(self):
        cache.clear()
        self.arrive_by = datetime(2026, 11, 12, 15, 0, tzinfo=NEW_YORK)
        self.now = self.arrive_by - timedelta(minutes=40)

    def options(self, *itineraries):
        payload = {"itineraries": list(itineraries), "direct": []}
        with mock.patch.object(
            trip.requests, "get", return_value=fake_response(payload)
        ):
            return trip.transitous_options(TIMES_SQ, UNION_SQ, self.arrive_by)

    def test_skipped_boarding_station_rules_a_trip_out_and_says_why(self):
        late_q = transitous_itinerary(self.arrive_by - timedelta(minutes=12), line="Q")
        early_n = transitous_itinerary(self.arrive_by - timedelta(minutes=20), line="N")
        plans = self.options(early_n, late_q)
        skip = alert("Stops Skipped", ["Q"], stations=["R16"])
        for plan in plans:
            trip.check_alerts(plan, [skip])

        chosen = trip.choose(plans, self.arrive_by)
        scheduled = max(plans, key=trip._lateness_key)

        self.assertEqual(chosen["segments"][1]["line"], "N")
        self.assertEqual(trip.switch_reason(chosen, scheduled), "Q stops skipped")

    def test_skipped_stop_in_the_middle_does_not_matter(self):
        plans = self.options(
            transitous_itinerary(self.arrive_by - timedelta(minutes=12))
        )
        trip.check_alerts(plans[0], [alert("Stops Skipped", ["N"], stations=["R17"])])

        self.assertIsNone(plans[0]["blocked_by"])
        self.assertEqual(len(plans[0]["alerts"]), 0)

    def test_line_wide_delay_is_shown_but_does_not_block(self):
        plans = self.options(
            transitous_itinerary(self.arrive_by - timedelta(minutes=12))
        )
        trip.check_alerts(plans[0], [alert("Delays", ["N"])])

        self.assertIsNone(plans[0]["blocked_by"])
        self.assertEqual([a.kind for a in plans[0]["alerts"]], ["Delays"])

    def test_a_delayed_train_lets_you_leave_later(self):
        depart = self.arrive_by - timedelta(minutes=15)
        plans = self.options(transitous_itinerary(depart))
        status = trip.MtaStatus()
        with mock.patch.object(
            trip.mta, "next_departures", return_value=[depart + timedelta(minutes=3)]
        ), mock.patch.object(
            trip.mta, "feed_snapshot", return_value={"fetched": 1, "data": []}
        ):
            trip.check_live(plans[0], self.now, status)

        self.assertEqual(plans[0]["shift"], timedelta(minutes=3))
        self.assertEqual(
            plans[0]["live_leave_by"], plans[0]["leave_by"] + timedelta(minutes=3)
        )

    def test_when_nothing_arrives_on_time_the_earliest_arrival_wins(self):
        depart = self.arrive_by - timedelta(minutes=12)
        plans = self.options(transitous_itinerary(depart))
        plans[0]["live_leave_by"] = plans[0]["leave_by"] + timedelta(minutes=20)
        plans[0]["live_arrive"] = plans[0]["arrive"] + timedelta(minutes=20)

        chosen = trip.choose(plans, self.arrive_by)

        self.assertIs(chosen, plans[0])
        self.assertGreater(chosen["live_arrive"], self.arrive_by)


def mta_ok(alerts=()):
    """Patch the MTA so cards see fresh data with `alerts` and no live trains."""
    fresh = {"data": list(alerts), "fetched": django_timezone.now().timestamp()}
    return (
        mock.patch.object(trip.mta, "alerts_snapshot", return_value=fresh),
        mock.patch.object(trip.mta, "feed_snapshot", return_value=fresh),
        mock.patch.object(trip.mta, "next_departures", return_value=[]),
    )


# Planned with the free Transitous path, whatever key the local .env has.
@override_settings(GOOGLE_MAPS_API_KEY="")
class TripCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.kid = User.objects.create_user(username="Lucia", password="FamilyTest2026!")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.admin,
            accepted_at=django_timezone.now(),
        )

    def setUp(self):
        cache.clear()
        self.now = django_timezone.now()
        local = django_timezone.localtime(self.now)
        # Keep both plans on today's date whatever time the tests run.
        self.start = max(
            local + timedelta(minutes=30),
            local.replace(hour=12, minute=0, second=0, microsecond=0),
        )
        if self.start.date() != local.date():
            self.start = local + timedelta(seconds=90)
        self.meeting = self.event("Midtown meeting", self.start, UNION_SQ, [self.admin])
        self.client.force_login(self.admin)

    def event(self, title, starts_at, point, people=(), hours=1, **fields):
        event = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title=title,
            location=title,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=hours),
            latitude=point[0],
            longitude=point[1],
            **fields,
        )
        event.participants.set(people)
        return event

    def earlier(self, people=()):
        day_start = django_timezone.localtime(self.start).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        starts = max(self.start - timedelta(minutes=60), day_start)
        if starts >= self.start:
            self.skipTest("too close to midnight for an earlier plan today")
        lunch = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Lunch",
            location="Times Square",
            starts_at=starts,
            ends_at=max(starts, self.start - timedelta(minutes=30)),
            latitude=TIMES_SQ[0],
            longitude=TIMES_SQ[1],
        )
        lunch.participants.set(people)
        return lunch

    def url(self, event=None, json=False):
        url = reverse("events:trip", args=[(event or self.meeting).pk])
        return url + "?format=json" if json else url

    def set_home(self):
        Workspace.objects.filter(pk=self.workspace.pk).update(
            home_location="12 Elm St", home_latitude=40.7, home_longitude=-74.0
        )

    def get_card(self, alerts=(), method="get", data=None, url=None, payload=None):
        depart = self.start - timedelta(minutes=20)
        payload = payload or transitous_payload(depart, self.start)
        patches = mta_ok(alerts)
        with mock.patch.object(
            trip.requests, "get", return_value=fake_response(payload)
        ) as get, patches[0], patches[1], patches[2]:
            response = getattr(self.client, method)(url or self.url(), data or {})
        self.transitous = get
        return response

    def test_starts_from_the_previous_event_by_default(self):
        self.earlier([self.admin])

        response = self.get_card()

        self.assertContains(response, 'class="trip-leave"')
        self.assertContains(response, ">Lunch</button>")
        self.assertContains(response, 'data-trip-mode="previous" aria-pressed="true"')
        self.assertContains(response, "Times Sq-42 St → 14 St-Union Sq")
        sent = self.transitous.call_args.kwargs["params"]
        self.assertEqual(sent["fromPlace"], f"{TIMES_SQ[0]},{TIMES_SQ[1]}")
        self.assertSetEqual(
            set(response["Cache-Control"].split(", ")), {"no-store", "private"}
        )

    def test_without_an_earlier_plan_or_home_it_asks_where_to_start(self):
        response = self.get_card()

        self.assertContains(response, "Where from?")
        self.assertContains(response, "data-trip-locate")
        self.assertContains(response, "Set home")

    def test_falls_back_to_home_when_there_is_no_earlier_plan(self):
        self.set_home()

        response = self.get_card()

        self.assertContains(response, 'data-trip-mode="home" aria-pressed="true"')
        self.assertEqual(
            self.transitous.call_args.kwargs["params"]["fromPlace"], "40.7,-74.0"
        )

    def test_choosing_home_is_saved_for_this_person_and_event(self):
        self.earlier([self.admin])
        self.set_home()

        response = self.get_card(method="post", data={"mode": "home"})

        self.assertContains(response, 'data-trip-mode="home" aria-pressed="true"')
        self.assertEqual(
            TripStart.objects.get(user=self.admin, event=self.meeting).mode, "home"
        )
        self.assertEqual(
            self.transitous.call_args.kwargs["params"]["fromPlace"], "40.7,-74.0"
        )

    def test_somewhere_else_uses_a_searched_place(self):
        response = self.get_card(
            method="post",
            data={
                "mode": "here",
                "latitude": "40.71",
                "longitude": "-74.01",
                "label": "Grandma's",
            },
        )

        self.assertContains(response, "Grandma&#x27;s")
        saved = TripStart.objects.get(user=self.admin, event=self.meeting)
        self.assertEqual((saved.latitude, saved.longitude), (40.71, -74.01))

    def test_bad_start_choices_are_rejected(self):
        for data in (
            {"mode": "space"},
            {"mode": "here"},
            {"mode": "here", "latitude": "x", "longitude": "1"},
            {"mode": "here", "latitude": "91", "longitude": "0"},
            {"mode": "here", "latitude": "nan", "longitude": "0"},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url(), data).status_code, 400)

    def test_alerts_on_the_trip_are_shown(self):
        self.earlier([self.admin])

        response = self.get_card(
            alerts=[alert("Delays", ["N"], header="N trains are delayed")]
        )

        self.assertContains(response, "N trains are delayed")
        self.assertContains(response, 'class="trip-warn-text">Delays')

    def test_far_off_events_show_the_planned_time_and_when_live_starts(self):
        far = self.event(
            "Next week", self.now + timedelta(days=7), UNION_SQ, [self.admin]
        )
        depart = far.starts_at - timedelta(minutes=20)
        patches = mta_ok()
        with mock.patch.object(
            trip.requests,
            "get",
            return_value=fake_response(transitous_payload(depart, far.starts_at)),
        ), patches[0], patches[1], patches[2] as live:
            response = self.client.post(
                self.url(far), {"mode": "here", "latitude": "40.7", "longitude": "-74"}
            )

        self.assertContains(response, "Planned · live from")
        live.assert_not_called()

    def test_live_dot_needs_fresh_mta_data(self):
        soon = self.event(
            "Soon", self.now + timedelta(minutes=40), UNION_SQ, [self.admin]
        )
        url = self.url(soon)
        depart = soon.starts_at - timedelta(minutes=15)
        payload = transitous_payload(depart, soon.starts_at)

        response = self.get_card(
            method="post",
            data={"mode": "here", "latitude": "40.7", "longitude": "-74"},
            url=url,
            payload=payload,
        )
        self.assertContains(response, "trip-live-dot")

        stale = {"data": [], "fetched": 0, "stale": True}
        with mock.patch.object(
            trip.requests, "get", return_value=fake_response(payload)
        ), mock.patch.object(
            trip.mta, "alerts_snapshot", return_value=stale
        ), mock.patch.object(
            trip.mta, "feed_snapshot", return_value=stale
        ), mock.patch.object(
            trip.mta, "next_departures", return_value=[]
        ):
            response = self.client.get(url)
        self.assertContains(response, "Live MTA info unavailable")
        self.assertNotContains(response, "trip-live-dot")

    def test_mta_outage_still_plans_and_says_so(self):
        # Live MTA info only applies within LIVE_LEAD of leaving, so the trip leaves
        # soon whatever time the tests run (a noon meeting was not live before 9:40 AM).
        soon = self.event(
            "Soon", self.now + timedelta(minutes=40), UNION_SQ, [self.admin]
        )
        depart = soon.starts_at - timedelta(minutes=15)
        with mock.patch.object(
            trip.requests,
            "get",
            return_value=fake_response(transitous_payload(depart, soon.starts_at)),
        ), mock.patch.object(
            trip.mta, "alerts_snapshot", side_effect=requests.ConnectionError
        ), mock.patch.object(
            trip.mta, "feed_snapshot", side_effect=requests.ConnectionError
        ), mock.patch.object(
            trip.mta, "next_departures", side_effect=requests.ConnectionError
        ):
            response = self.client.post(
                self.url(soon), {"mode": "here", "latitude": "40.7", "longitude": "-74"}
            )

        self.assertContains(response, 'class="trip-leave"')
        self.assertContains(response, "Live MTA info unavailable")

    def test_planner_failure_and_no_route(self):
        self.earlier([self.admin])
        with mock.patch.object(
            trip.requests, "get", side_effect=requests.ConnectionError
        ):
            self.assertContains(self.client.get(self.url()), "Couldn’t plan the trip")
        cache.clear()
        self.assertContains(
            self.get_card(payload={"itineraries": []}), "gets there in time"
        )

    def test_marker_json_for_timelines(self):
        self.earlier([self.admin])

        data = self.get_card(url=self.url(json=True)).json()

        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["line"], "N")
        self.assertIn("leave_by", data)
        self.assertRegex(data["label"], r"^\d{1,2}:\d\d (AM|PM)$")

    def test_only_subway_and_bus_events_get_a_card(self):
        car = self.event(
            "Drive",
            self.start,
            UNION_SQ,
            [self.admin],
            transport_mode=Event.TransportMode.DRIVING,
        )

        self.assertNotContains(self.client.get(self.url(car)), "trip-card")
        self.assertNotContains(
            self.client.get(reverse("events:detail", args=[car.pk])), "data-trip-url"
        )

    def test_past_events_get_no_card(self):
        past = self.event("Yesterday", self.now - timedelta(days=1), UNION_SQ)

        self.assertNotContains(self.client.get(self.url(past)), "trip-card")
        self.assertNotContains(
            self.client.get(reverse("events:detail", args=[past.pk])), "data-trip-url"
        )

    def test_event_page_has_the_card_slot(self):
        response = self.client.get(reverse("events:detail", args=[self.meeting.pk]))

        self.assertContains(response, f'data-trip-url="{self.url()}"')
        self.assertContains(response, "events/trip.js")

    def test_day_timeline_has_a_leave_marker_in_my_lane(self):
        response = self.client.get(
            reverse("events:workspace_list", args=[self.workspace.pk]),
            {"view": "day", "date": self.start.date().isoformat()},
        )

        self.assertContains(response, f'data-trip-marker="{self.url(json=True)}"')
        self.assertContains(response, "data-at=")

    def test_kids_cannot_plan_trips_to_events_hidden_from_them(self):
        self.client.force_login(self.kid)

        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_kids_use_home_without_seeing_the_address(self):
        self.set_home()
        family_trip = self.event("Zoo", self.start, UNION_SQ)
        self.client.force_login(self.kid)

        response = self.get_card(url=self.url(family_trip))

        self.assertContains(response, 'class="trip-leave"')
        self.assertNotContains(response, "12 Elm St")
        self.assertNotContains(response, "Set home")

    def test_previous_event_ignores_plans_the_viewer_is_not_part_of(self):
        lunch = self.earlier([self.kid])

        self.assertIsNone(trip.previous_event(self.meeting, self.admin))
        lunch.participants.set([])
        self.assertEqual(trip.previous_event(self.meeting, self.admin), lunch)


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class GoogleTripTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_google_is_used_when_a_key_is_set(self):
        arrive_by = django_timezone.now() + timedelta(hours=1)
        depart = arrive_by - timedelta(minutes=20)
        payload = route_payload(depart, depart + timedelta(minutes=10))
        with mock.patch.object(
            trip.requests, "post", return_value=fake_response(payload)
        ) as post, mock.patch.object(trip.requests, "get") as get:
            provider, plans = trip.trip_options(TIMES_SQ, UNION_SQ, arrive_by)

        get.assert_not_called()
        self.assertEqual(provider, "google")
        self.assertEqual(plans[0]["segments"][1]["line"], "Q")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Goog-Api-Key"], "test-key")


class FamilyHomeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.kid = User.objects.create_user(username="Lucia", password="FamilyTest2026!")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.admin,
            accepted_at=django_timezone.now(),
        )
        cls.url = reverse("accounts:family_home", args=[cls.workspace.pk])

    def test_parent_sets_and_clears_the_home(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            self.url,
            {"location": "12 Elm St", "latitude": "40.7", "longitude": "-74.0"},
        )

        self.assertEqual(response.status_code, 302)
        self.workspace.refresh_from_db()
        self.assertTrue(self.workspace.has_home)
        self.assertEqual(self.workspace.home_location, "12 Elm St")
        self.client.post(self.url, {"action": "clear"})
        self.workspace.refresh_from_db()
        self.assertFalse(self.workspace.has_home)

    def test_address_must_come_from_the_suggestions(self):
        self.client.force_login(self.admin)

        response = self.client.post(self.url, {"location": "somewhere"})

        self.assertContains(response, "Pick your address from the suggestions")
        self.workspace.refresh_from_db()
        self.assertFalse(self.workspace.has_home)

    def test_kids_cannot_open_or_change_it(self):
        self.client.force_login(self.kid)

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.post(
                self.url, {"location": "x", "latitude": "1", "longitude": "1"}
            ).status_code,
            404,
        )


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class GoogleAddressFallbackTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_retries_by_address_when_the_pins_get_no_route(self):
        arrive_by = django_timezone.now() + timedelta(hours=2)
        depart = arrive_by - timedelta(minutes=40)
        found = route_payload(depart, depart + timedelta(minutes=20))
        with mock.patch.object(
            trip.requests,
            "post",
            side_effect=[fake_response({}), fake_response(found)],
        ) as post:
            routes = trip.request_routes(
                TIMES_SQ, (40.6446, -73.7797), arrive_by, ("", "JFK Airport, NY")
            )

        self.assertEqual(len(routes), 1)
        first, second = (call.kwargs["json"] for call in post.call_args_list)
        self.assertIn("location", first["destination"])
        self.assertEqual(second["destination"], {"address": "JFK Airport, NY"})
        self.assertIn("location", second["origin"])

    def test_no_retry_without_an_address(self):
        arrive_by = django_timezone.now() + timedelta(hours=2)
        with mock.patch.object(
            trip.requests, "post", return_value=fake_response({})
        ) as post:
            self.assertEqual(trip.request_routes(TIMES_SQ, UNION_SQ, arrive_by), [])

        self.assertEqual(post.call_count, 1)


class LineBadgeTests(SimpleTestCase):
    def ride(self, line, vehicle="SUBWAY", route_id=None, **extra):
        return {"line": line, "vehicle": vehicle, "route_id": route_id or line, **extra}

    def test_subway_bullets_use_official_mta_colours(self):
        q = trip.line_badge(self.ride("Q"))
        a = trip.line_badge(self.ride("A"))

        self.assertEqual(
            (q["shape"], q["color"], q["text_color"]), ("bullet", "#FCCC0A", "#000")
        )
        self.assertEqual((a["color"], a["text_color"]), ("#0039A6", "#fff"))

    def test_express_trains_are_diamonds_and_shuttles_read_s(self):
        express = trip.line_badge(self.ride("6", route_id="6X"))
        shuttle = trip.line_badge(self.ride("S", route_id="GS"))

        self.assertEqual((express["shape"], express["text"]), ("diamond", "6"))
        self.assertEqual(shuttle["text"], "S")

    def test_other_services_get_short_labels(self):
        airtrain = trip.line_badge(
            self.ride(
                "Jamaica AirTrain", vehicle="LIGHT_RAIL", route_id="", color="#d00"
            )
        )
        lirr = trip.line_badge(self.ride("LIRR", vehicle="HEAVY_RAIL", route_id=""))
        bus = trip.line_badge(self.ride("M15-SBS", vehicle="BUS", route_id=""))

        self.assertEqual(
            (airtrain["text"], airtrain["shape"], airtrain["color"]),
            ("AirTrain", "pill", "#d00"),
        )
        self.assertEqual(lirr["text"], "LIRR")
        self.assertEqual(bus["text"], "M15-SBS")


@override_settings(GOOGLE_MAPS_API_KEY="")
class MissedTripTests(TestCase):
    """When every on-time trip has already left, plan the next one you can catch."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username="Ana", password="x")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)

    def setUp(self):
        cache.clear()

    def test_replans_from_now_and_says_how_late(self):
        now = django_timezone.now()
        event = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Meeting",
            starts_at=now + timedelta(minutes=10),
            ends_at=now + timedelta(minutes=70),
            latitude=UNION_SQ[0],
            longitude=UNION_SQ[1],
        )
        gone = {"itineraries": [transitous_itinerary(now - timedelta(minutes=5))]}
        catchable = {"itineraries": [transitous_itinerary(now + timedelta(minutes=8))]}
        patches = mta_ok()
        with mock.patch.object(
            trip.requests,
            "get",
            side_effect=[fake_response(gone), fake_response(catchable)],
        ) as get, patches[0], patches[1], patches[2]:
            card = trip.build_trip(
                event,
                self.admin,
                origin=trip.Origin(*TIMES_SQ, "start", TripStart.Mode.HERE),
                now=now,
            )

        self.assertEqual(card["status"], "ok")
        self.assertGreaterEqual(card["leave_by"], now - trip.CATCH_SLACK)
        self.assertGreater(card["late_minutes"], 0)
        second = get.call_args_list[1].kwargs["params"]
        self.assertEqual(second["arriveBy"], "false")
