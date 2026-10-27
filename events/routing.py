
import math
from dataclasses import dataclass

import requests


OSRM_BASE_URL = "https://router.project-osrm.org"
VALHALLA_BASE_URL = "https://valhalla1.openstreetmap.de"
OTP_GRAPHQL_URL = "http://localhost:8080/otp/routers/default/index/graphql"


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

    itineraries = (
        result.get("data", {})
        .get("plan", {})
        .get("itineraries", [])
    )

    valid_durations = []

    for itinerary in itineraries:
        legs = itinerary.get("legs", [])

        has_transit = any(
            leg.get("mode") in {
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
    Estimate travel duration for:
      driving -> OSRM
      walking -> Valhalla pedestrian
      cycling -> Valhalla bicycle
      transit -> Valhalla multimodal, if transit data is available

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
        if transport_mode == "driving":
            seconds = estimate_driving_seconds(first_event, second_event)

        elif transport_mode == "transit":
            seconds = estimate_transit_seconds(
                first_event,
                second_event,
                departure_time=getattr(first_event, "ends_at", None),
            )

        else:
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
    ):
        return RouteEstimate(None, "unverified")
