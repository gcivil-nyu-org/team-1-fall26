"""
Security lane "gap3": paid-API cost amplification through the trip card and the
travel checks, and the end-to-end LocMem eviction of rate-limit counters.

Every test_poc_GAP3_* asserts the SECURE behaviour, so it FAILS while the gap exists.
test_control_GAP3_* tests pin behaviour that already holds (they pass).

Run:
  cd /Users/simonsavine/syncmily-security-fixes && set -a && . <target.env> && set +a && \
  POSTGRES_DB=syncmily_sec_gap3 python manage.py test \
      syncmily.tests.security.test_gap3 --noinput -v 2

Nothing leaves the machine. requests.sessions.Session.request is replaced by a router
that answers for routes.googleapis.com, api.transitous.org, the MTA feeds, OSRM,
Valhalla and OpenTripPlanner, counts every call per host, and raises for any other
host. The Google key is a dummy string. Time is simulated by patching
django.utils.timezone.now (and the time module events.mta reads) one second per
request. No production system is contacted.

Price used for the estimates (Google Maps Platform price list, checked 2026-11-18):
Compute Routes Pro (the old "Advanced" tier) is $10.00 per 1,000 calls after 5,000
free per month; Compute Routes Enterprise is $15.00 per 1,000. The trip card asks for
transit steps and alternatives, so the real SKU is Pro or Enterprise. Estimates below
use $10 (low) and $15 (high) per 1,000 and ignore the free monthly allowance.
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from events.models import Event
from syncmily import ratelimit

User = get_user_model()
PW = "Gap3-Local-Only-Pw-2026!"
USD_LOW, USD_HIGH = 10.0 / 1000, 15.0 / 1000

# Real A-train station coordinates (events/data/mta_subway_stations.csv: A02, A05) so the
# MTA matching in build_trip runs the same code path as in production.
BOARD = (40.868072, -73.919899)
ALIGHT = (40.859022, -73.934180)
HOME = (40.8700, -73.9150)


def usd(calls):
    return f"${calls * USD_LOW:,.2f} to ${calls * USD_HIGH:,.2f}"


# ------------------------------------------------------------------ fake clock ---


class Clock:
    def __init__(self, start=None):
        self.now = start or datetime.now(dt_timezone.utc)

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def patches(self):
        now_patch = mock.patch(
            "django.utils.timezone.now", side_effect=lambda: self.now
        )
        mta_time = mock.patch(
            "events.mta.time", SimpleNamespace(time=lambda: self.now.timestamp())
        )
        return now_patch, mta_time


# ---------------------------------------------------------- fake upstream hosts ---


def _response(payload=None, content=None, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = content if content is not None else json.dumps(payload).encode()
    response.headers["Content-Type"] = "application/json"
    return response


def _iso(moment):
    return (
        moment.astimezone(dt_timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stop(name, point, when):
    return {
        "name": name,
        "location": {"latLng": {"latitude": point[0], "longitude": point[1]}},
    }, _iso(when)


def _transit_route(depart, arrive):
    board, board_time = _stop("Inwood-207 St", BOARD, depart)
    alight, alight_time = _stop("190 St", ALIGHT, arrive)
    seconds = int((arrive - depart).total_seconds())
    return {
        "duration": f"{seconds + 180}s",
        "legs": [
            {
                "steps": [
                    {"travelMode": "WALK", "staticDuration": "120s"},
                    {
                        "travelMode": "TRANSIT",
                        "staticDuration": f"{seconds}s",
                        "transitDetails": {
                            "stopDetails": {
                                "departureStop": board,
                                "arrivalStop": alight,
                                "departureTime": board_time,
                                "arrivalTime": alight_time,
                            },
                            "transitLine": {
                                "nameShort": "A",
                                "name": "8 Avenue Express",
                                "vehicle": {
                                    "type": "SUBWAY",
                                    "name": {"text": "Subway"},
                                },
                            },
                            "headsign": "Far Rockaway",
                            "stopCount": 4,
                        },
                    },
                    {"travelMode": "WALK", "staticDuration": "60s"},
                ]
            }
        ],
    }


class Upstream:
    """Router + counter for every outbound HTTP call the app can make."""

    def __init__(self):
        self.hosts = Counter()
        self.google = []  # payload per routes.googleapis.com call
        self.google_empty = False  # True: Google finds no route (forces fallbacks)
        self.mta_urls = Counter()

    def __call__(self, session, method, url, **kwargs):
        host = urlsplit(url).hostname
        self.hosts[host] += 1
        if host == "routes.googleapis.com":
            payload = kwargs.get("json") or {}
            self.google.append(payload)
            if self.google_empty:
                return _response({})
            if not payload.get("computeAlternativeRoutes"):
                return _response({"routes": [{"duration": "600s"}]})
            # Trip card. Run late: the on-time trip left 15 min before "now" when the
            # event is 30 min away, so every on-time trip has already left and the app
            # plans again "from now" (events/trip.py:439-458).
            if "arrivalTime" in payload:
                depart = _parse(payload["arrivalTime"]) - timedelta(minutes=45)
            else:
                depart = _parse(payload["departureTime"]) + timedelta(minutes=3)
            route = _transit_route(depart, depart + timedelta(minutes=25))
            return _response({"routes": [route]})
        if host == "api.transitous.org":
            return _response({"itineraries": [], "direct": []})
        if host == "api-endpoint.mta.info":
            self.mta_urls[url.rsplit("/", 1)[-1]] += 1
            if url.endswith(".json"):
                return _response({"entity": []})
            return _response(content=b"")
        if host == "router.project-osrm.org":
            return _response({"code": "Ok", "routes": [{"duration": 600}]})
        if host == "valhalla1.openstreetmap.de":
            return _response({"trip": {"summary": {"time": 600}, "legs": []}})
        if host == "localhost":  # OpenTripPlanner default URL
            return _response({"data": {"plan": {"itineraries": []}}})
        raise AssertionError(f"unexpected outbound call to {host}")

    def google_calls(self):
        return len(self.google)


# -------------------------------------------------------------------- fixtures ---


@override_settings(RATELIMIT_ENABLED=True, GOOGLE_MAPS_API_KEY="dummy-not-a-real-key")
class Gap3Base(TestCase):
    def setUp(self):
        cache.clear()
        with override_settings(DEBUG=True):
            call_command("seed_demo_family", password=PW, stdout=StringIO())
        self.ws = Workspace.objects.get(name="Demo family")
        self.parent = User.objects.get(username="demo-parent")
        self.parent2 = User.objects.get(username="demo-parent-2")
        self.kid = User.objects.get(username="demo-kid")
        self.ws.home_location = "1 Home Street"
        self.ws.home_latitude, self.ws.home_longitude = HOME
        self.ws.save()
        self.up = Upstream()
        self.clock = Clock()
        upstream = self.up

        def fake_request(session, method, url, **kwargs):
            return upstream(session, method, url, **kwargs)

        patcher = mock.patch.object(requests.sessions.Session, "request", fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The seeded household has no event with a pin, so add one that starts in 30 min,
        # has a location, and lists the kid as a participant.
        now = self.clock.now
        self.event = Event.objects.create(
            created_by=self.parent,
            workspace=self.ws,
            title="Late bus",
            starts_at=now + timedelta(minutes=30),
            ends_at=now + timedelta(minutes=90),
            location="Dyckman St",
            latitude=ALIGHT[0],
            longitude=ALIGHT[1],
            transport_mode=Event.TransportMode.TRANSIT,
        )
        self.event.participants.set([self.kid])

    def client_for(self, user, **extra):
        client = Client(**extra)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    def poll(self, client, count, url=None):
        """GET the trip card `count` times, one simulated second apart."""
        url = url or reverse("events:trip", args=[self.event.pk])
        statuses = Counter()
        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            for _ in range(count):
                statuses[client.get(url).status_code] += 1
                self.clock.advance(1)
        return statuses


# =========================================================================
# (1) GET /events/<pk>/trip/ as a kid, 120 times over 120 simulated seconds
# =========================================================================


class TripCardPolling(Gap3Base):
    def test_poc_GAP3_001_trip_card_poll_keeps_google_calls_constant(self):
        """GAP3-001. ASVS 5.0 V2.4.1 (anti-automation on costly actions), V2.3.2
        (business-logic limits), V2.1.3 (documented limits); V15.2.2. Pitfalls 1, 88.

        The "plan again from now" call keys the Google Routes cache on depart_at=now at
        one-second resolution (events/trip.py:298-310, 439-458) and GET
        /events/<pk>/trip/ has no throttle (events/views.py:772-822), and a kid can
        call it. A kid polling an event that is running late makes one billable call
        per second.

        Measured: see the printed line (-v 2). Secure design: key the cache by minute
        or throttle; calls stay at a small constant, so this asserts <= 6 in 120 s.
        """
        kid = self.client_for(self.kid)
        statuses = self.poll(kid, 120)
        calls = self.up.google_calls()
        per_minute = calls / 2
        per_day = calls / 120 * 86400
        print(
            f"\nGAP3-001 statuses={dict(statuses)} google_calls_120s={calls} "
            f"per_minute={per_minute:.1f} per_day_if_sustained={per_day:,.0f} "
            f"est_usd_per_day={usd(per_day)} est_usd_per_minute={usd(per_minute)} "
            f"hosts={dict(self.up.hosts)} mta_urls={dict(self.up.mta_urls)}"
        )
        self.assertEqual(
            set(statuses), {200}, "expected the card to render for the kid"
        )
        self.assertLessEqual(
            calls,
            6,
            f"{calls} billable Google Routes calls in 120 simulated seconds "
            f"({per_minute:.0f}/min); key the cache by minute or throttle the GET",
        )

    def test_poc_GAP3_001b_more_events_multiply_the_per_second_calls(self):
        """GAP3-001b. ASVS V2.4.1, V2.3.2. Pitfalls 1, 88.

        The cache key is (origin, destination, second), so each late event adds its
        own call every second. 5 late events, 60 s of polling, kid polls all 5.
        A secure design bounds calls per user, not per event.
        """
        extra = []
        for i in range(4):
            event = Event.objects.create(
                created_by=self.parent,
                workspace=self.ws,
                title=f"Late bus {i}",
                starts_at=self.event.starts_at + timedelta(minutes=i + 1),
                ends_at=self.event.ends_at,
                location="Dyckman St",
                latitude=ALIGHT[0] + 0.001 * (i + 1),
                longitude=ALIGHT[1],
                transport_mode=Event.TransportMode.TRANSIT,
            )
            event.participants.set([self.kid])
            extra.append(event)
        kid = self.client_for(self.kid)
        urls = [reverse("events:trip", args=[e.pk]) for e in [self.event, *extra]]
        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            for _ in range(60):
                for url in urls:
                    self.assertEqual(kid.get(url).status_code, 200)
                self.clock.advance(1)
        calls = self.up.google_calls()
        print(
            f"\nGAP3-001b events=5 google_calls_60s={calls} per_minute={calls} "
            f"per_day_if_sustained={calls * 1440:,} est_usd_per_day={usd(calls * 1440)}"
        )
        self.assertLessEqual(calls, 30, f"{calls} Google calls in 60 s for one kid")


# =========================================================================
# (2) Parent edit-form preview with 5 participants, 10 times
# =========================================================================


class TravelCheckFanOut(Gap3Base):
    def setUp(self):
        super().setUp()
        self.extra = [
            User.objects.create_user(f"gap3-member-{i}", password=PW) for i in range(2)
        ]
        for user, role in zip(
            self.extra, (Membership.Role.KID, Membership.Role.PARENT)
        ):
            Membership.objects.create(
                workspace=self.ws,
                user=user,
                role=role,
                invited_by=self.parent,
                accepted_at=timezone.now(),
            )
        self.people = [self.parent, self.parent2, self.kid, *self.extra]
        self.zone = timezone.get_current_timezone()
        self.day = timezone.localdate() + timedelta(days=3)

        def at(hour):
            return datetime(
                self.day.year, self.day.month, self.day.day, hour, 0, tzinfo=self.zone
            )

        self.edited = Event.objects.create(
            created_by=self.parent,
            workspace=self.ws,
            title="Recital",
            starts_at=at(10),
            ends_at=at(11),
            location="Hall",
            latitude=40.80,
            longitude=-73.95,
            transport_mode=Event.TransportMode.DRIVING,
        )
        self.edited.participants.set(self.people)
        # Each person has an event before and after, with a pin and a location, so the
        # travel check has two legs per person (10 legs for 5 people). Distinct pins per
        # person: the worst realistic family, everyone with their own activity.
        for i, person in enumerate(self.people):
            for title, start, end, lat in (
                ("Before", at(7), at(9), 40.70 + 0.01 * i),
                ("After", at(12), at(13), 40.75 + 0.01 * i),
            ):
                event = Event.objects.create(
                    created_by=self.parent,
                    workspace=self.ws,
                    title=f"{title} {i}",
                    starts_at=start,
                    ends_at=end,
                    location=f"{title} place {i}",
                    latitude=lat,
                    longitude=-73.90,
                    transport_mode=Event.TransportMode.DRIVING,
                )
                event.participants.set([person])

    def preview_data(self, latitude):
        return {
            "action": "preview",
            "title": "Recital",
            "date": self.day.isoformat(),
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": timezone.get_current_timezone_name(),
            "location": "Hall",
            "latitude": f"{latitude:.5f}",
            "longitude": "-73.95000",
            "transport_mode": "driving",
            "participants": [p.pk for p in self.people],
        }

    def run_previews(self, count, jitter):
        parent = self.client_for(self.parent)
        url = reverse("events:edit", args=[self.edited.pk])
        per_post = []
        for i in range(count):
            before_google = self.up.google_calls()
            before_osrm = self.up.hosts["router.project-osrm.org"]
            data = self.preview_data(40.80 + (i * 0.0001 if jitter else 0))
            response = parent.post(url, data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.context["travel_checks"],
                "form did not validate: " + str(response.context["form"].errors),
            )
            per_post.append(
                (
                    self.up.google_calls() - before_google,
                    self.up.hosts["router.project-osrm.org"] - before_osrm,
                )
            )
        return per_post

    def test_poc_GAP3_002_preview_with_changing_pin_has_no_bounded_fanout(self):
        """GAP3-002. ASVS 5.0 V2.4.1, V2.3.2, V2.1.3; V15.2.2. Pitfalls 1, 88.

        Every preview POST of the edit (and create) form runs find_travel_checks
        (events/views.py:166, :290; events/travel_conflicts.py), two Routes lookups per
        participant, with no rate limit. The Google cache key holds the edited event's
        coordinates to 5 decimals, and those come from the form, so changing the last
        digit of the latitude on each POST defeats the 1 h cache.

        Measured (10 POSTs, 5 participants, latitude +0.0001 each time): see printed
        per-POST list. Secure design: the whole account stays under the repo's own
        paid-Routes budget (TRIP_STARTS_PER_HOUR = 30, events/views.py:827), so this
        asserts <= 30 Google calls over the 10 POSTs.
        """
        per_post = self.run_previews(10, jitter=True)
        total = self.up.google_calls()
        print(
            f"\nGAP3-002 per_post_(google,osrm)={per_post} total_google={total} "
            f"est_usd_per_1000_such_posts={usd(total / 10 * 1000)}"
        )
        self.assertLessEqual(
            total,
            30,
            f"{total} paid Google Routes calls from 10 previews by one parent",
        )

    def test_control_GAP3_002b_identical_preview_hits_the_one_hour_cache(self):
        """Control: the same preview 10 times costs one fan-out, then 0 (Google cache,
        events/routing.py:98-121). Passes today."""
        per_post = self.run_previews(10, jitter=False)
        print(f"\nGAP3-002b per_post_(google,osrm)={per_post}")
        self.assertEqual(sum(g for g, _ in per_post[1:]), 0)
        self.assertGreater(per_post[0][0], 0)

    def test_poc_GAP3_003_no_route_result_is_not_cached_for_the_fallbacks(self):
        """GAP3-003. ASVS V2.4.1, V2.3.2, V15.2.2. Pitfalls 1, 88.

        When Google finds no route, estimate_google_seconds caches the miss
        (events/routing.py:123-125) but the open fallbacks are not cached at all
        (events/routing.py:129-139, :394-415): every identical preview POST asks OSRM
        again (driving) for each leg. The public OSRM demo server is a third party with
        a 1 request per second policy, so this is abuse of someone else's service
        rather than money.

        Measured: 10 identical POSTs x 5 participants: see printed per-POST list.
        Secure: <= 10 OSRM calls in total (one fan-out, then cached).
        """
        self.up.google_empty = True
        per_post = self.run_previews(10, jitter=False)
        osrm = self.up.hosts["router.project-osrm.org"]
        print(f"\nGAP3-003 per_post_(google,osrm)={per_post} total_osrm={osrm}")
        self.assertLessEqual(osrm, 10, f"{osrm} OSRM calls for 10 identical previews")


# =========================================================================
# (3) Eviction end to end
# =========================================================================


def present(key):
    """Non-touching LocMem lookup (cache.get would move the key to the safe end)."""
    return cache.make_key(key) in cache._cache


class Eviction(Gap3Base):
    VICTIM_IP = "203.0.113.50"

    def victim_state(self, client):
        for _ in range(4):
            client.post(
                reverse("accounts:login"),
                {"username": "demo-parent-2", "password": "wrong-password"},
                REMOTE_ADDR=self.VICTIM_IP,
            )
        for _ in range(29):
            client.post(
                reverse("accounts:password_reset"),
                {"email": "victim@family.test"},
                REMOTE_ADDR=self.VICTIM_IP,
            )
        # chat_hour: the websocket path counts by user pk (chat/consumers.py:71).
        for _ in range(299):
            ratelimit.hit("chat_hour", self.parent2.pk, 300, 3600)
        return {
            "login": f"rl:login:ip:{self.VICTIM_IP}:600",
            "reset": f"rl:password_reset:ip:{self.VICTIM_IP}:3600",
            "chat": f"rl:chat_hour:{self.parent2.pk}:3600",
        }

    def test_poc_GAP3_004_kid_trip_polling_evicts_login_reset_and_chat_counters(self):
        """GAP3-004. ASVS 5.0 V2.4.1, V6.3.1 (credential brute force), V2.1.3,
        V13.1.x (resource limits). Pitfalls 1, 88.

        settings.py has no CACHES, so the default 300-entry LocMemCache holds the
        limiter counters (syncmily/ratelimit.py:47-62) and the Google/MTA route
        caches (events/trip.py:310-360). Django culls the least recently used third
        when full. A victim IP records 4 failed logins and 29 password-reset
        requests, a user records 299 chat messages in the hour. A kid then polls the
        trip card with distinct seconds. The victim counters must survive.

        The test prints the poll number at which each counter first disappears.
        Real attacker cost: one GET per second (one new key per distinct second), no
        throttle; with 5 late events, 5 keys per second. The login lockout in django
        axes is database backed (AXES_HANDLER default) and is unaffected, see
        test_control_GAP3_004b.
        """
        anon = Client()
        keys = self.victim_state(anon)
        # Section 10.1: the login and reset counters live in the RateCounter table;
        # only the chat counter stays in the cache.
        self.assertEqual(ratelimit.get_count("login", f"ip:{self.VICTIM_IP}", 600), 4)
        self.assertEqual(
            ratelimit.get_count("password_reset", f"ip:{self.VICTIM_IP}", 3600), 29
        )
        self.assertTrue(present(keys["chat"]), "setup: chat counter missing")
        self.assertEqual(cache.get(keys["chat"]), 299)

        kid = self.client_for(self.kid)
        url = reverse("events:trip", args=[self.event.pk])
        first_lost = {}
        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            for i in range(1, 401):
                self.assertEqual(kid.get(url).status_code, 200)
                self.clock.advance(1)
                for name, key in keys.items():
                    if name != "chat":
                        continue  # these two live in the database now
                    if name not in first_lost and not present(key):
                        first_lost[name] = i
        print(
            f"\nGAP3-004 first_poll_that_evicted={first_lost} "
            f"cache_entries_after={len(cache._cache)} google_calls={self.up.google_calls()}"
        )

        # Functional bypass: the exhausted reset limiter should still stop request 31.
        statuses = [
            anon.post(
                reverse("accounts:password_reset"),
                {"email": "victim@family.test"},
                REMOTE_ADDR=self.VICTIM_IP,
            ).status_code
            for _ in range(2)
        ]
        print(f"GAP3-004 reset_posts_30_and_31_status={statuses}")
        self.assertEqual(
            first_lost, {}, "limiter counters evicted by trip-card polling"
        )
        self.assertEqual(statuses[1], 429, "31st reset request from one IP was allowed")

    def test_control_GAP3_004b_axes_lockout_is_database_backed(self):
        """Control: django-axes keeps failures in the database, so cache churn does not
        unlock a locked username (default AxesDatabaseHandler, no AXES_HANDLER set).
        Passes today."""
        from axes.models import AccessAttempt

        anon = Client()
        for _ in range(5):
            anon.post(
                reverse("accounts:login"),
                {"username": "demo-parent-2", "password": "wrong-password"},
                REMOTE_ADDR=self.VICTIM_IP,
            )
        self.assertTrue(AccessAttempt.objects.filter(username="demo-parent-2").exists())
        self.poll(self.client_for(self.kid), 400)
        anon.post(
            reverse("accounts:login"),
            {"username": "demo-parent-2", "password": PW},
            REMOTE_ADDR=self.VICTIM_IP,
        )
        self.assertNotIn("_auth_user_id", anon.session)

    def test_poc_GAP3_005_kid_resets_own_trip_start_budget_by_polling(self):
        """GAP3-005. ASVS V2.4.1, V2.3.2. Pitfalls 1, 88.

        The only throttle on paid Routes lookups, trip_start = 30 per hour per user
        (events/views.py:802, :827), is a LocMem counter. The kid uses all 30, polls
        the card 400 distinct seconds to churn the cache, and the 31st change of start
        place must still be 429.
        """
        kid = self.client_for(self.kid)
        url = reverse("events:trip", args=[self.event.pk])

        def start(i):
            return kid.post(
                url,
                {
                    "mode": "here",
                    "latitude": f"{40.80 + i * 0.001:.4f}",
                    "longitude": "-73.95",
                    "label": f"Spot {i}",
                },
            ).status_code

        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            first = [start(i) for i in range(30)]
            self.assertEqual(set(first), {200})
            self.assertEqual(start(30), 429, "control: the 31st start is blocked")
            for _ in range(400):
                kid.get(url)
                self.clock.advance(1)
            after = start(31)
        print(
            f"\nGAP3-005 31st_start_before_churn=429 after_400_polls={after} "
            f"google_calls={self.up.google_calls()}"
        )
        self.assertEqual(after, 429, "trip_start budget was reset by cache churn")


# =========================================================================
# (4) Several households / several signups multiply the per-user limit
# =========================================================================


class LimitMultiplication(Gap3Base):
    IP = "198.51.100.77"

    def sign_up(self, name):
        anon = Client()
        response = anon.post(
            reverse("accounts:signup"),
            {
                "username": name,
                "email": f"{name}@family.test",
                "password1": PW,
                "password2": PW,
            },
            REMOTE_ADDR=self.IP,
        )
        self.assertEqual(response.status_code, 302, "signup failed")
        return anon, User.objects.get(username=name)

    def own_household(self, user):
        ws = Workspace.objects.create(name=f"{user.username} home", primary_admin=user)
        ws.home_location = "1 Home Street"
        ws.home_latitude, ws.home_longitude = HOME
        ws.save()
        return Event.objects.create(
            created_by=user,
            workspace=ws,
            title="Late bus",
            starts_at=self.clock.now + timedelta(minutes=30),
            ends_at=self.clock.now + timedelta(minutes=90),
            location="Dyckman St",
            latitude=ALIGHT[0],
            longitude=ALIGHT[1],
            transport_mode=Event.TransportMode.TRANSIT,
        )

    def allowed_starts(self, client, event, tries):
        allowed = 0
        for i in range(tries):
            status = client.post(
                reverse("events:trip", args=[event.pk]),
                {
                    "mode": "here",
                    "latitude": f"{40.80 + i * 0.001:.4f}",
                    "longitude": "-73.95",
                    "label": f"Spot {i}",
                },
            ).status_code
            allowed += status == 200
        return allowed

    def test_poc_GAP3_006_signups_from_one_ip_multiply_the_trip_start_budget(self):
        """GAP3-006. ASVS 5.0 V2.4.1, V2.3.2, V6.3.3 (account creation abuse).
        Pitfalls 1, 88.

        trip_start is counted per account (key="user", events/views.py:802 comment:
        "not per IP"), while signup allows 50 accounts per IP per hour
        (syncmily/ratelimit.py:21) and each account can create a household and an
        event. 3 signups from one IP, each trying 31 start changes.

        Measured: starts allowed in total, printed. Secure: one IP stays within one
        budget (<= 30). Extrapolation: 50 signups/h x 30 = 1,500 paid start-change
        lookups per hour per IP, each at least one Routes call.
        """
        total = 0
        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            for name in ("gap3-sock-a", "gap3-sock-b", "gap3-sock-c"):
                client, user = self.sign_up(name)
                event = self.own_household(user)
                total += self.allowed_starts(client, event, 31)
        calls = self.up.google_calls()
        print(
            f"\nGAP3-006 accounts=3 allowed_starts={total} google_calls={calls} "
            f"extrapolated_50_accounts_per_hour={total // 3 * 50} starts, "
            f"{calls // 3 * 50} Google calls, est_usd_per_hour={usd(calls // 3 * 50)}"
        )
        self.assertLessEqual(total, 30, f"{total} start changes allowed from one IP")

    def test_control_GAP3_006b_one_account_in_two_households_shares_one_budget(self):
        """Control: the per-user key is not multiplied by households. A user can sit in
        only one workspace as a member (accounts_membership.user_id is unique) but can
        own several (5 per hour). One parent who owns two workspaces gets 30 start
        changes in total across both. Passes today."""
        other_ws = Workspace.objects.create(
            name="Other home", primary_admin=self.parent
        )
        other_ws.home_location = "2 Home Street"
        other_ws.home_latitude, other_ws.home_longitude = HOME
        other_ws.save()
        other_event = Event.objects.create(
            created_by=self.parent,
            workspace=other_ws,
            title="Other late bus",
            starts_at=self.clock.now + timedelta(minutes=30),
            ends_at=self.clock.now + timedelta(minutes=90),
            location="Dyckman St",
            latitude=ALIGHT[0],
            longitude=ALIGHT[1],
            transport_mode=Event.TransportMode.TRANSIT,
        )
        parent = self.client_for(self.parent)
        now_patch, mta_patch = self.clock.patches()
        with now_patch, mta_patch:
            a = self.allowed_starts(parent, self.event, 20)
            b = self.allowed_starts(parent, other_event, 20)
        print(f"\nGAP3-006b household_a={a} household_b={b}")
        self.assertEqual(a + b, 30)
