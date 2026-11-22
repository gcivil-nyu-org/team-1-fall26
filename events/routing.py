import contextlib
import contextvars
import hashlib
import math
import os
import time
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone as django_timezone

from . import upstream

OSRM_BASE_URL = "https://router.project-osrm.org"
VALHALLA_BASE_URL = "https://valhalla1.openstreetmap.de"
OTP_GRAPHQL_URL = os.environ.get(
    "OTP_GRAPHQL_URL",
    "http://localhost:8080/otp/routers/default/index/graphql",
)


GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_TRAVEL_MODES = {
    "driving": "DRIVE",
    "walking": "WALK",
    "cycling": "BICYCLE",
    "transit": "TRANSIT",
}
# Google accepts transit departure times from 7 days ago to 100 days ahead.
GOOGLE_TRANSIT_PAST_LIMIT = timedelta(days=7)
GOOGLE_TRANSIT_FUTURE_LIMIT = timedelta(days=100)
GOOGLE_CACHE_SECONDS = 60 * 60
# Cache keys round pins to three decimals (about 110 m), so nudging a pin by a few
# metres can't force a new paid lookup each time.
KEY_DECIMALS = 3
# One request (a save, a preview) spends at most this many seconds on routing calls.
REQUEST_BUDGET_SECONDS = 30
# A save or preview checks at most this many legs; the rest stay unverified.
MAX_LEGS_PER_REQUEST = 12
# The longest trip we accept from a provider (seven days).
MAX_TRIP_SECONDS = 7 * 24 * 3600
# Per call: (connect, read, total) seconds.
GOOGLE_DEADLINE = upstream.Deadline(3.05, 5, total=8, max_bytes=256 * 1024)
OPEN_DEADLINE = upstream.Deadline(3.05, 5, total=8, max_bytes=1024 * 1024)


class _Budget:
    def __init__(self, seconds):
        self.remaining = seconds
        self.legs = 0

    def charge(self, seconds):
        self.remaining -= seconds


_budget = contextvars.ContextVar("routing_budget", default=None)


@contextlib.contextmanager
def request_budget(seconds=REQUEST_BUDGET_SECONDS):
    """Cap what one request spends on routing calls (a worst case of `seconds`)."""
    token = _budget.set(_Budget(seconds))
    try:
        yield _budget.get()
    finally:
        _budget.reset(token)


def take_leg():
    """True while this request may still check another leg."""
    budget = _budget.get()
    if budget is None:
        return True
    if budget.legs >= MAX_LEGS_PER_REQUEST:
        return False
    budget.legs += 1
    return True


def _key_point(point):
    return f"{point.latitude:.{KEY_DECIMALS}f},{point.longitude:.{KEY_DECIMALS}f}"


def _call(provider, method, url, deadline, request_key, **kwargs):
    """One outbound routing call: refused while the provider is down or this request's
    time budget is spent, charged against the budget, and never redirected."""
    upstream.check(provider, request_key)
    budget = _budget.get()
    if budget is not None and budget.remaining < deadline.total:
        raise upstream.RecentlyUnavailable("this request's routing time is used up")
    started = time.monotonic()
    try:
        response = getattr(requests, method)(
            url, timeout=deadline, allow_redirects=False, **kwargs
        )
        response.raise_for_status()
    except requests.RequestException as error:
        if budget is not None:
            # A stalled call may have used its whole allowance, however fast it ended.
            budget.charge(
                deadline.total
                if upstream.looks_like_outage(error)
                else time.monotonic() - started
            )
        upstream.record_failure(provider, error, request_key)
        raise
    if budget is not None:
        budget.charge(time.monotonic() - started)
    return response


def _seconds_or_none(value):
    """A trip length in seconds, or None when the provider sent something odd."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= MAX_TRIP_SECONDS:
        return None
    return value


def _cached_seconds(key, compute):
    """Seconds from the cache, else from compute(). A route nobody finds is cached
    too, so the open fallbacks aren't asked again for it."""
    cached = cache.get(key)
    if cached is not None:
        return cached["seconds"]
    seconds = compute()
    cache.set(key, {"seconds": seconds}, GOOGLE_CACHE_SECONDS)
    return seconds


@dataclass
class RouteEstimate:
    duration_minutes: int | None
    status: str


def valid_coordinates(event):
    latitude = getattr(event, "latitude", None)
    longitude = getattr(event, "longitude", None)

    if not isinstance(latitude, (int, float)):
        return False

    if not isinstance(longitude, (int, float)):
        return False

    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _google_departure(mode, departure_time):
    """Return an RFC 3339 departure time Google will accept, or None."""
    if mode != "TRANSIT" or departure_time is None:
        return None
    now = django_timezone.now()
    if not (
        now - GOOGLE_TRANSIT_PAST_LIMIT
        < departure_time
        < now + GOOGLE_TRANSIT_FUTURE_LIMIT
    ):
        return None
    return (
        departure_time.astimezone(dt_timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_google_duration(value):
    """Google returns durations like "754s"."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    return float(value[:-1])


def estimate_google_seconds(start, end, mode, departure_time=None):
    """
    Estimate a route with the Google Routes API.

    Returns None when no key is configured or Google finds no route,
    so callers can fall back to the open providers.
    """
    api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    travel_mode = GOOGLE_TRAVEL_MODES.get(mode)
    if not api_key or travel_mode is None:
        return None

    departure = _google_departure(travel_mode, departure_time)
    cache_key = (
        "google-route:"
        + hashlib.sha256(
            (
                f"{travel_mode}|{_key_point(start)}|{_key_point(end)}|"
                f"{(departure or '')[:13]}"
            ).encode()
        ).hexdigest()
    )

    def compute():
        payload = {
            "origin": {
                "location": {
                    "latLng": {"latitude": start.latitude, "longitude": start.longitude}
                }
            },
            "destination": {
                "location": {
                    "latLng": {"latitude": end.latitude, "longitude": end.longitude}
                }
            },
            "travelMode": travel_mode,
        }
        if departure:
            payload["departureTime"] = departure
        response = _call(
            "google-routes",
            "post",
            GOOGLE_ROUTES_URL,
            GOOGLE_DEADLINE,
            cache_key,
            json=payload,
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "routes.duration",
            },
        )
        routes = response.json().get("routes") or []
        if not routes or not isinstance(routes[0], dict):
            return None
        return _seconds_or_none(_parse_google_duration(routes[0].get("duration")))

    return _cached_seconds(cache_key, compute)


def _estimate_with_google(first_event, second_event, transport_mode):
    try:
        return estimate_google_seconds(
            first_event,
            second_event,
            transport_mode,
            departure_time=getattr(first_event, "ends_at", None),
        )
    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return None


def estimate_driving_seconds(start, end):
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/"
        f"{start.longitude},{start.latitude};"
        f"{end.longitude},{end.latitude}"
    )
    key = f"osrm-route:{_key_point(start)}:{_key_point(end)}"

    def compute():
        response = _call(
            "osrm", "get", url, OPEN_DEADLINE, key, params={"overview": "false"}
        )
        data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        return _seconds_or_none(data["routes"][0].get("duration"))

    return _cached_seconds(key, compute)


def estimate_valhalla_seconds(start, end, mode):
    costing_profiles = {
        "walking": "pedestrian",
        "cycling": "bicycle",
        "transit": "multimodal",
    }

    costing = costing_profiles[mode]

    payload = {
        "locations": [
            {"lat": start.latitude, "lon": start.longitude},
            {"lat": end.latitude, "lon": end.longitude},
        ],
        "costing": costing,
        "units": "kilometers",
    }

    key = f"valhalla-route:{costing}:{_key_point(start)}:{_key_point(end)}"
    return _cached_seconds(key, lambda: _valhalla_seconds(payload, mode, key))


def _valhalla_seconds(payload, mode, key):
    response = _call(
        "valhalla",
        "post",
        f"{VALHALLA_BASE_URL}/route",
        OPEN_DEADLINE,
        key,
        json=payload,
    )
    data = response.json()

    trip = data.get("trip", {})
    summary = trip.get("summary", {})

    if mode == "transit":
        # A multimodal route is only acceptable if it actually
        # includes a public-transit leg.
        legs = trip.get("legs", [])
        transit_types = {
            "tram",
            "metro",
            "rail",
            "bus",
            "ferry",
            "cable_car",
            "gondola",
            "funicular",
        }

        has_transit = False

        for leg in legs:
            for maneuver in leg.get("maneuvers", []):
                transit_info = maneuver.get("transit_info")
                if transit_info:
                    has_transit = True
                    break

                travel_type = maneuver.get("travel_type")
                if travel_type in transit_types:
                    has_transit = True
                    break

            if has_transit:
                break

        if not has_transit:
            return None

    return _seconds_or_none(summary.get("time"))


def estimate_transit_seconds(start, end, departure_time=None):
    """
    Estimate a public-transit journey using the local OpenTripPlanner server.

    Returns the duration in seconds, or None when no valid
    transit itinerary is available.
    """
    if departure_time is None:
        from django.utils import timezone

        departure_time = timezone.now()

    local_departure = departure_time.astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )

    query = """
    query TransitRoute(
        $from: InputCoordinates!,
        $to: InputCoordinates!,
        $date: String!,
        $time: String!
    ) {
        plan(
            from: $from,
            to: $to,
            date: $date,
            time: $time,
            transportModes: [{mode: WALK}, {mode: TRANSIT}]
        ) {
            itineraries {
                duration
                legs {
                    mode
                }
            }
        }
    }
    """

    variables = {
        "from": {"lat": start.latitude, "lon": start.longitude},
        "to": {"lat": end.latitude, "lon": end.longitude},
        "date": local_departure.strftime("%Y-%m-%d"),
        "time": local_departure.strftime("%H:%M"),
    }

    hour = local_departure.strftime("%Y-%m-%dT%H")
    key = f"otp-route:{_key_point(start)}:{_key_point(end)}:{hour}"
    return _cached_seconds(key, lambda: _otp_seconds(query, variables, key))


def _otp_seconds(query, variables, key):
    response = _call(
        "otp",
        "post",
        OTP_GRAPHQL_URL,
        OPEN_DEADLINE,
        key,
        json={"query": query, "variables": variables},
    )
    result = response.json()

    if result.get("errors"):
        return None

    itineraries = ((result.get("data") or {}).get("plan") or {}).get(
        "itineraries"
    ) or []

    valid_durations = []

    for itinerary in itineraries:
        legs = itinerary.get("legs", [])

        has_transit = any(
            leg.get("mode")
            in {
                "SUBWAY",
                "BUS",
                "RAIL",
                "TRAM",
                "FERRY",
            }
            for leg in legs
        )

        duration = itinerary.get("duration")

        if has_transit and isinstance(duration, (int, float)) and duration >= 0:
            valid_durations.append(duration)

    return _seconds_or_none(min(valid_durations)) if valid_durations else None


def estimate_travel_time(first_event, second_event, transport_mode):
    """
    Estimate travel duration with Google Routes when GOOGLE_MAPS_API_KEY
    is set, falling back to the open providers:
      driving -> OSRM
      walking -> Valhalla pedestrian
      cycling -> Valhalla bicycle
      transit -> OpenTripPlanner, if a server is configured

    Unavailable or invalid routes remain unverified.
    """
    supported_modes = {
        "driving",
        "walking",
        "cycling",
        "transit",
    }

    if transport_mode not in supported_modes:
        return RouteEstimate(None, "unverified")

    if not valid_coordinates(first_event):
        return RouteEstimate(None, "unverified")

    if not valid_coordinates(second_event):
        return RouteEstimate(None, "unverified")

    try:
        seconds = _estimate_with_google(first_event, second_event, transport_mode)

        if seconds is None and transport_mode == "driving":
            seconds = estimate_driving_seconds(first_event, second_event)

        elif seconds is None and transport_mode == "transit":
            seconds = estimate_transit_seconds(
                first_event,
                second_event,
                departure_time=getattr(first_event, "ends_at", None),
            )

        elif seconds is None:
            seconds = estimate_valhalla_seconds(
                first_event, second_event, transport_mode
            )

        if (
            not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            return RouteEstimate(None, "unverified")

        return RouteEstimate(
            duration_minutes=math.ceil(seconds / 60),
            status="estimated",
        )

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
    ):
        return RouteEstimate(None, "unverified")
