import hashlib
import math
import os
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone as django_timezone

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
                f"{travel_mode}|{start.latitude:.5f},{start.longitude:.5f}|"
                f"{end.latitude:.5f},{end.longitude:.5f}|{(departure or '')[:13]}"
            ).encode()
        ).hexdigest()
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached["seconds"]

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

    response = requests.post(
        GOOGLE_ROUTES_URL,
        json=payload,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.duration",
        },
        timeout=5,
    )
    response.raise_for_status()

    routes = response.json().get("routes") or []
    seconds = _parse_google_duration(routes[0].get("duration")) if routes else None
    # Cache misses too, so a route Google can't find isn't asked again.
    cache.set(cache_key, {"seconds": seconds}, GOOGLE_CACHE_SECONDS)
    return seconds


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

    response = requests.get(
        url,
        params={"overview": "false"},
        timeout=6,
    )
    response.raise_for_status()

    data = response.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        return None

    return data["routes"][0].get("duration")


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

    response = requests.post(
        f"{VALHALLA_BASE_URL}/route",
        json=payload,
        timeout=15,
    )
    response.raise_for_status()

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

    return summary.get("time")


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

    response = requests.post(
        OTP_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        timeout=15,
    )
    response.raise_for_status()

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

    return min(valid_durations) if valid_durations else None


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
