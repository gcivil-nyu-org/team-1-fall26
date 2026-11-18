"""Live leave-by times for an event, checked against MTA delays and service alerts.

A planner (the free Transitous service, or Google's Routes API when a key is set) offers
trips that arrive by the event's start. The MTA's own feeds then show which of those
trips actually work right now: alerts rule out trips through suspended or skipped
stations, and live arrivals show when the train really leaves. Of the trips that still
arrive on time, the one that lets you leave latest wins.

Live checks start two hours before the leave-by time and the MTA data is re-read every
five minutes. Before that, the card shows the planned time and any planned work.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from . import mta, upstream
from .access import family_events, family_workspaces
from .models import Event, TripStart

logger = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_FIELDS = ",".join(
    [
        "routes.duration",
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.transitDetails",
    ]
)
# Free, keyless planner run by the Transitous community (MOTIS); it covers NYC transit.
# Their usage policy asks clients to identify themselves.
TRANSITOUS_URL = "https://api.transitous.org/api/v1/plan"
TRANSITOUS_USER_AGENT = (
    "Syncmily/1.0 (student project; https://github.com/gcivil-nyu-org/team-1-fall26)"
)
SUBWAY_ROUTE_PREFIX = "us-ny-MTA-NYCSubway_"
PLAN_CACHE_SECONDS = 5 * 60
TRIP_MAX_EARLY = timedelta(hours=3)
# Live checks start this long before the leave-by time (a 2 h trip goes live 4 h out).
LIVE_LEAD = timedelta(hours=2)
# MTA data older than this isn't shown as live (it refreshes every five minutes).
FRESH_FOR = timedelta(minutes=7)
# A train this close to its planned time still counts as the planned train.
CATCH_SLACK = timedelta(minutes=1)

VEHICLE_NAMES = {
    "SUBWAY": "Subway",
    "BUS": "Bus",
    "COACH": "Bus",
    "RAIL": "Train",
    "REGIONAL_RAIL": "Train",
    "SUBURBAN": "Train",
    "TRAM": "Tram",
    "FERRY": "Ferry",
}


# The MTA's official line colours (its bullets are just these circles with the letter).
MTA_LINE_COLORS = {
    **dict.fromkeys(["1", "2", "3"], "#EE352E"),
    **dict.fromkeys(["4", "5", "6", "6X"], "#00933C"),
    **dict.fromkeys(["7", "7X"], "#B933AD"),
    **dict.fromkeys(["A", "C", "E"], "#0039A6"),
    **dict.fromkeys(["B", "D", "F", "FX", "M"], "#FF6319"),
    "G": "#6CBE45",
    **dict.fromkeys(["J", "Z"], "#996633"),
    "L": "#A7A9AC",
    **dict.fromkeys(["N", "Q", "R", "W"], "#FCCC0A"),
    **dict.fromkeys(["GS", "FS", "H", "S"], "#808183"),
    **dict.fromkeys(["SI", "SIR"], "#0039A6"),
}
DARK_TEXT_LINES = {"N", "Q", "R", "W"}


def line_badge(segment):
    """How a ride's line is drawn: an MTA bullet for the subway (a diamond for express
    trains), a short rounded label for everything else."""
    line = segment["line"]
    if segment["vehicle"] == "SUBWAY":
        route = (segment.get("route_id") or line).upper()
        text = {"GS": "S", "FS": "S", "H": "S", "SI": "SIR"}.get(route, route)
        text = text.removesuffix("X") if route in ("6X", "7X", "FX") else text
        return {
            "text": text,
            "shape": "diamond" if route.endswith("X") and len(route) == 2 else "bullet",
            "color": MTA_LINE_COLORS.get(route, "#808183"),
            "text_color": "#000" if route in DARK_TEXT_LINES else "#fff",
        }
    name = f"{line} {segment.get('line_name', '')}".lower()
    if "airtrain" in name:
        text = "AirTrain"
    elif "lirr" in name or "long island" in name:
        text = "LIRR"
    elif "metro-north" in name or "metro north" in name:
        text = "Metro-North"
    elif "path" == line.lower():
        text = "PATH"
    else:
        text = line[:8]
    return {
        "text": text,
        "shape": "pill",
        "color": segment.get("color") or "#0039A6",
        "text_color": segment.get("text_color") or "#fff",
    }


@dataclass(frozen=True)
class Origin:
    latitude: float
    longitude: float
    label: str
    mode: str = TripStart.Mode.HERE
    event: object = None  # the previous event, when the trip starts there
    # Typed address, for when the pin sits somewhere no route reaches (mid-airfield).
    address: str = ""


@dataclass
class MtaStatus:
    """How fresh the MTA data behind a card is."""

    checked: bool = False
    unavailable: bool = False
    stale: bool = False
    updated: list = field(default_factory=list)

    def note(self, snapshot):
        self.checked = True
        self.updated.append(snapshot["fetched"])
        self.stale = self.stale or bool(snapshot.get("stale"))

    @property
    def updated_at(self):
        if not self.updated:
            return None
        return datetime.fromtimestamp(min(self.updated), datetime_timezone.utc)

    def is_fresh(self, now):
        return (
            self.checked
            and not self.unavailable
            and not self.stale
            and self.updated_at is not None
            and now - self.updated_at <= FRESH_FOR
        )


# --- small parsers --------------------------------------------------------------------


def _seconds(value):
    """Google durations look like "345s"."""
    try:
        return int(float(str(value).rstrip("s")))
    except (TypeError, ValueError):
        return 0


def _when(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _utc_z(moment):
    """2026-11-11T23:31:50Z. Fractional seconds confuse Transitous, so drop them."""
    return (
        moment.astimezone(datetime_timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _latlng(stop):
    location = (stop or {}).get("location", {}).get("latLng", {})
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return latitude, longitude
    return None


def _hex(color):
    return f"#{color}" if color and not color.startswith("#") else (color or "")


def _point(stop):
    latitude, longitude = (stop or {}).get("lat"), (stop or {}).get("lon")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return latitude, longitude
    return None


def _subway_stop(stop):
    """MTA stop id from a Transitous stop: us-ny-MTA-NYCSubway_R16S -> R16S."""
    stop_id = (stop or {}).get("stopId") or ""
    return (
        stop_id.removeprefix(SUBWAY_ROUTE_PREFIX)
        if SUBWAY_ROUTE_PREFIX in stop_id
        else ""
    )


# --- where the trip starts ------------------------------------------------------------


def event_zone(event):
    """The event's own time zone (the card shows times in it), else the server's."""
    try:
        return ZoneInfo(event.timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.get_current_timezone()


def previous_event(event, user):
    """The viewer's last located plan that ends before this one starts, same day
    (the day in the event's own time zone)."""
    day_start = timezone.localtime(event.starts_at, event_zone(event)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    candidates = (
        family_events(user)
        .filter(
            Q(participants=user) | Q(participants__isnull=True),
            starts_at__gte=day_start,
            ends_at__lte=event.starts_at,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .exclude(pk=event.pk)
        .distinct()
        .order_by("-ends_at", "-pk")
    )
    return next((plan for plan in candidates if plan.has_valid_coordinates), None)


def resolve_start(event, user):
    """(origin or None, chosen mode, previous event, family home workspace or None).

    The person's saved choice wins. "Previous event" falls back to Home when there's no
    earlier plan today, so the first trip of the day still gets a time.
    """
    choice = TripStart.objects.filter(user=user, event=event).first()
    mode = choice.mode if choice else TripStart.Mode.PREVIOUS
    previous = previous_event(event, user)
    home = event.workspace if event.workspace and event.workspace.has_home else None

    def from_home():
        return Origin(
            home.home_latitude,
            home.home_longitude,
            "Home",
            TripStart.Mode.HOME,
            address=home.home_location,
        )

    origin = None
    if mode == TripStart.Mode.HERE and choice.latitude is not None:
        origin = Origin(
            choice.latitude,
            choice.longitude,
            choice.label or "Somewhere else",
            TripStart.Mode.HERE,
            address="" if choice.label == "Your current location" else choice.label,
        )
    elif mode == TripStart.Mode.HOME and home:
        origin = from_home()
    elif mode == TripStart.Mode.PREVIOUS and previous:
        origin = Origin(
            previous.latitude,
            previous.longitude,
            previous.directions_location or previous.title,
            TripStart.Mode.PREVIOUS,
            event=previous,
            address=previous.directions_location,
        )
    elif mode == TripStart.Mode.PREVIOUS and home:
        origin = from_home()
    return origin, mode, previous, home


# --- trip options from the planners ---------------------------------------------------


def request_routes(origin, destination, arrive_by, addresses=("", ""), depart_at=None):
    """Routes API transit trips arriving by `arrive_by`, with alternatives, so the MTA
    check has more than one option to pick from.

    Big places (JFK, Central Park) have their pin in the middle, where no route ends.
    When the pins give nothing, the trip is asked for again by address, and Google
    routes to a reachable entrance.
    """
    when = (
        {"departureTime": _utc_z(depart_at)}
        if depart_at
        else {"arrivalTime": _utc_z(arrive_by)}
    )
    key = (
        "trip-google:"
        f"{origin[0]:.4f},{origin[1]:.4f}:{destination[0]:.4f},{destination[1]:.4f}:"
        f"{when}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    upstream.check("google-routes")

    def point(latitude, longitude):
        return {"location": {"latLng": {"latitude": latitude, "longitude": longitude}}}

    def post(start, end):
        response = requests.post(
            ROUTES_URL,
            json={
                "origin": start,
                "destination": end,
                "travelMode": "TRANSIT",
                **when,
                "computeAlternativeRoutes": True,
                "languageCode": "en-US",
                "units": "IMPERIAL",
            },
            headers={
                "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": ROUTES_FIELDS,
            },
            timeout=upstream.TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("routes") or []

    def ask(start, end):
        try:
            return post(start, end)
        except requests.RequestException as error:
            upstream.record_failure("google-routes", error)
            raise

    start, end = point(*origin), point(*destination)
    routes = ask(start, end)
    start_address, end_address = (address.strip() for address in addresses)
    if not routes and (start_address or end_address):
        routes = ask(
            {"address": start_address} if start_address else start,
            {"address": end_address} if end_address else end,
        )
    cache.set(key, routes, PLAN_CACHE_SECONDS)
    return routes


def _finish(segments, leave_by, arrive, transfers=0):
    for segment in segments:
        if segment["kind"] == "walk":
            segment["minutes"] = max(1, math.ceil(segment["seconds"] / 60))
    return {
        "segments": segments,
        "leave_by": leave_by,
        "arrive": arrive,
        "minutes": max(1, math.ceil((arrive - leave_by).total_seconds() / 60)),
        "walk_only": not any(s["kind"] == "ride" for s in segments),
        "transfers": transfers,
    }


def _add_walk(segments, seconds):
    if segments and segments[-1]["kind"] == "walk":
        segments[-1]["seconds"] += seconds
    else:
        segments.append({"kind": "walk", "seconds": seconds})


def summarize_route(route, arrive_by):
    """Turn a Routes API route into walk/ride segments, leave-by and arrival times."""
    steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
    segments = []
    for step in steps:
        if step.get("travelMode") == "TRANSIT" and step.get("transitDetails"):
            details = step["transitDetails"]
            stops = details.get("stopDetails", {})
            line = details.get("transitLine", {})
            vehicle = line.get("vehicle", {})
            from_point = _latlng(stops.get("departureStop"))
            to_point = _latlng(stops.get("arrivalStop"))
            segments.append(
                {
                    "kind": "ride",
                    "line": line.get("nameShort") or line.get("name") or "Transit",
                    "route_id": line.get("nameShort", ""),
                    "line_name": line.get("name", ""),
                    "color": line.get("color", ""),
                    "text_color": line.get("textColor", ""),
                    "vehicle": vehicle.get("type", ""),
                    "vehicle_name": vehicle.get("name", {}).get("text", ""),
                    "headsign": details.get("headsign", ""),
                    "stops": details.get("stopCount") or 0,
                    "from": stops.get("departureStop", {}).get("name", ""),
                    "to": stops.get("arrivalStop", {}).get("name", ""),
                    "from_point": from_point,
                    "to_point": to_point,
                    "board_ids": mta.station_ids(from_point),
                    "alight_ids": mta.station_ids(to_point),
                    "station_ids": mta.station_ids(from_point)
                    | mta.station_ids(to_point),
                    "depart": _when(stops.get("departureTime")),
                    "arrive": _when(stops.get("arrivalTime")),
                }
            )
        else:
            _add_walk(segments, _seconds(step.get("staticDuration")))

    rides = [segment for segment in segments if segment["kind"] == "ride"]
    if rides and rides[0]["depart"] and rides[-1]["arrive"]:
        first, last = segments.index(rides[0]), segments.index(rides[-1])
        walk_before = sum(s["seconds"] for s in segments[:first] if s["kind"] == "walk")
        walk_after = sum(
            s["seconds"] for s in segments[last + 1 :] if s["kind"] == "walk"
        )
        leave_by = rides[0]["depart"] - timedelta(seconds=walk_before)
        arrive = rides[-1]["arrive"] + timedelta(seconds=walk_after)
    else:
        arrive = arrive_by
        leave_by = arrive_by - timedelta(seconds=_seconds(route.get("duration")))
    return _finish(segments, leave_by, arrive)


def transitous_options(origin, destination, arrive_by, depart_at=None):
    """Every Transitous trip that arrives by `arrive_by` (but not hours early), or with
    `depart_at`, every trip leaving from then on."""
    moment = depart_at or arrive_by
    key = (
        "trip-transitous:"
        f"{origin[0]:.4f},{origin[1]:.4f}:{destination[0]:.4f},{destination[1]:.4f}:"
        f"{int(moment.timestamp())}:{'depart' if depart_at else 'arrive'}"
    )
    data = cache.get(key)
    if data is None:
        upstream.check("transitous")
        try:
            response = _ask_transitous(origin, destination, moment, depart_at)
        except requests.RequestException as error:
            upstream.record_failure("transitous", error)
            raise
        data = response.json()
        cache.set(key, data, PLAN_CACHE_SECONDS)
    return _transitous_plans(data, arrive_by, depart_at)


def _ask_transitous(origin, destination, moment, depart_at):
    response = requests.get(
        TRANSITOUS_URL,
        params={
            "fromPlace": f"{origin[0]},{origin[1]}",
            "toPlace": f"{destination[0]},{destination[1]}",
            "time": _utc_z(moment),
            "arriveBy": "false" if depart_at else "true",
        },
        headers={"User-Agent": TRANSITOUS_USER_AGENT},
        timeout=upstream.TIMEOUT,
    )
    response.raise_for_status()
    return response


def _transitous_plans(data, arrive_by, depart_at):
    plans = []
    for itinerary in (data.get("itineraries") or []) + (data.get("direct") or []):
        start, end = _when(itinerary.get("startTime")), _when(itinerary.get("endTime"))
        if not (start and end):
            continue
        if depart_at:
            if start < depart_at - CATCH_SLACK:
                continue
        # Arrive in time, but not hours early: that would be a trip for another day.
        elif not arrive_by - TRIP_MAX_EARLY <= end <= arrive_by:
            continue
        segments = []
        for leg in itinerary.get("legs", []):
            mode = leg.get("mode", "")
            if mode == "WALK" or not mode:
                _add_walk(segments, int(leg.get("duration") or 0))
                continue
            route_id = leg.get("routeId") or ""
            subway = route_id.startswith(SUBWAY_ROUTE_PREFIX)
            stops = [leg.get("from")] + (leg.get("intermediateStops") or [])
            stops.append(leg.get("to"))
            segments.append(
                {
                    "kind": "ride",
                    "line": leg.get("routeShortName")
                    or leg.get("displayName")
                    or leg.get("routeLongName")
                    or VEHICLE_NAMES.get(mode, "Transit"),
                    # The MTA's own line id (GS, 6X...), which its alerts and feeds use.
                    "route_id": (
                        route_id.removeprefix(SUBWAY_ROUTE_PREFIX) if subway else ""
                    ),
                    "line_name": leg.get("routeLongName", ""),
                    "color": _hex(leg.get("routeColor")),
                    "text_color": _hex(leg.get("routeTextColor")),
                    "vehicle": mode,
                    "vehicle_name": VEHICLE_NAMES.get(
                        mode, mode.replace("_", " ").title()
                    ),
                    "headsign": leg.get("headsign", ""),
                    "stops": len(leg.get("intermediateStops") or []) + 1,
                    "from": leg.get("from", {}).get("name", ""),
                    "to": leg.get("to", {}).get("name", ""),
                    "from_point": _point(leg.get("from")),
                    "to_point": _point(leg.get("to")),
                    "board_ids": mta.station_ids(
                        stop_ids=[_subway_stop(leg.get("from"))]
                    ),
                    "alight_ids": mta.station_ids(
                        stop_ids=[_subway_stop(leg.get("to"))]
                    ),
                    "station_ids": mta.station_ids(
                        stop_ids=[_subway_stop(stop) for stop in stops]
                    ),
                    "depart": _when(leg.get("startTime")),
                    "arrive": _when(leg.get("endTime")),
                }
            )
        plans.append(
            _finish(segments, start, end, transfers=itinerary.get("transfers") or 0)
        )
    return plans


def plan_with_transitous(origin, destination, arrive_by):
    """The Transitous trip that leaves latest and still arrives by `arrive_by`."""
    plans = transitous_options(origin, destination, arrive_by)
    return max(plans, key=_lateness_key) if plans else None


def _lateness_key(plan):
    return (plan["leave_by"], -plan["transfers"])


def trip_options(start, end, arrive_by, addresses=("", ""), depart_at=None):
    if settings.GOOGLE_MAPS_API_KEY:
        # choose() prefers trips that arrive on time and warns when none do.
        routes = request_routes(start, end, arrive_by, addresses, depart_at)
        return ("google", [summarize_route(route, arrive_by) for route in routes])
    return ("transitous", transitous_options(start, end, arrive_by, depart_at))


# --- checking options against the MTA -------------------------------------------------


def _subway_rides(plan):
    return [
        segment
        for segment in plan["segments"]
        if segment["kind"] == "ride" and segment["vehicle"] == "SUBWAY"
    ]


def check_alerts(plan, alerts):
    """Attach the alerts touching each subway ride; mark trips they make impossible."""
    plan["alerts"], plan["blocked_by"] = [], None
    for segment in _subway_rides(plan):
        segment["alerts"] = []
        if not segment["route_id"] or not segment["depart"] or not segment["arrive"]:
            continue
        for alert in alerts:
            if segment["route_id"] not in alert.routes or not alert.active_during(
                segment["depart"], segment["arrive"]
            ):
                continue
            if alert.base_kind == "Stops Skipped":
                # Only matters when the train skips where you get on or off.
                hit = bool(
                    alert.stations & (segment["board_ids"] | segment["alight_ids"])
                )
            else:
                hit = not alert.stations or bool(
                    alert.stations & segment["station_ids"]
                )
            if not hit:
                continue
            segment["alerts"].append(alert)
            if alert not in plan["alerts"]:
                plan["alerts"].append(alert)
            if alert.blocks and plan["blocked_by"] is None:
                plan["blocked_by"] = (segment["line"], alert)


def check_live(plan, now, status):
    """Live departures for each subway ride, and the delay to the first one.

    If the planned train is running late, you can leave that much later and arrive that
    much later; the rest of the trip is assumed to keep its gaps.
    """
    plan["shift"] = timedelta(0)
    for index, segment in enumerate(_subway_rides(plan)):
        segment["live"], segment["live_minutes"] = [], []
        if not (segment["route_id"] and segment["depart"] and segment["from_point"]):
            continue
        feed = mta.feed_for_line(segment["route_id"])
        if not feed:
            continue
        try:
            departures = mta.next_departures(
                segment["route_id"],
                segment["from_point"],
                segment["to_point"],
                now=now,
                limit=8,
                board_ids=segment["board_ids"],
                alight_ids=segment["alight_ids"],
            )
            status.note(mta.feed_snapshot(feed))
        except (requests.RequestException, ValueError, IndexError):
            logger.warning("MTA live feed unavailable for the %s", segment["line"])
            status.unavailable = True
            continue
        upcoming = [d for d in departures if d >= segment["depart"] - CATCH_SLACK]
        segment["live"] = departures[:3]
        segment["live_minutes"] = [
            max(0, math.floor((d - now).total_seconds() / 60)) for d in departures[:3]
        ]
        if index == 0 and upcoming:
            plan["shift"] = max(timedelta(0), upcoming[0] - segment["depart"])
            # The train you'll actually catch, shown instead of the timetable time.
            segment["live_depart"] = upcoming[0]
    plan["live_leave_by"] = plan["leave_by"] + plan["shift"]
    plan["live_arrive"] = plan["arrive"] + plan["shift"]


def choose(plans, arrive_by, now=None):
    """Leave as late as possible and still arrive on time; else arrive as early as
    possible. Trips that have already left, or that go through suspended or skipped
    stations, only win if nothing else is left."""
    for plan in plans:
        plan.setdefault("live_leave_by", plan["leave_by"])
        plan.setdefault("live_arrive", plan["arrive"])
    if now is not None:
        catchable = [p for p in plans if p["leave_by"] >= now - CATCH_SLACK]
        plans = catchable or plans
    open_plans = [plan for plan in plans if not plan.get("blocked_by")] or plans
    on_time = [plan for plan in open_plans if plan["live_arrive"] <= arrive_by]
    if on_time:
        return max(
            on_time, key=lambda plan: (plan["live_leave_by"], -plan["transfers"])
        )
    return min(open_plans, key=lambda plan: plan["live_arrive"])


def switch_reason(chosen, scheduled_best):
    """Why the card isn't showing the trip the timetable alone would pick."""
    if chosen is scheduled_best:
        return ""
    if scheduled_best.get("blocked_by"):
        line, alert = scheduled_best["blocked_by"]
        return f"{line} {alert.base_kind.lower()}"
    rides = _subway_rides(scheduled_best)
    line = rides[0]["line"] if rides else "that train"
    if scheduled_best.get("alerts"):
        return f"{line} {scheduled_best['alerts'][0].base_kind.lower()}"
    if scheduled_best.get("shift"):
        return f"{line} running late"
    return ""


def maps_url(origin, event):
    """Google Maps transit directions; with no origin, Maps starts from the device."""
    params = {
        "api": "1",
        "destination": f"{event.latitude},{event.longitude}",
        "travelmode": "transit",
    }
    if origin is not None:
        params["origin"] = f"{origin.latitude},{origin.longitude}"
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def build_trip(event, user, origin=None, now=None):
    """Everything the trip card and the timeline marker show.

    `status` picks the version of the card: ok, needs_location, no_route, error, or
    unavailable (no card at all: past events, events with no map pin).
    """
    now = now or timezone.now()
    chosen_mode = TripStart.Mode.HERE
    previous = home = None
    if origin is None:
        origin, chosen_mode, previous, home = resolve_start(event, user)
    context = {
        "event": event,
        "origin": origin,
        "mode": chosen_mode,
        "previous": previous,
        "home": home,
        "is_transit": event.transport_mode == Event.TransportMode.TRANSIT,
        "can_set_home": bool(event.workspace_id)
        and family_workspaces(user, caregivers_only=True)
        .filter(pk=event.workspace_id)
        .exists(),
        "maps_url": maps_url(origin, event),
    }
    if (
        not context["is_transit"]
        or not event.has_valid_coordinates
        or event.starts_at <= now
    ):
        return {**context, "status": "unavailable"}
    if origin is None:
        return {**context, "status": "needs_location"}

    start, end = (origin.latitude, origin.longitude), (event.latitude, event.longitude)
    try:
        provider, plans = trip_options(
            start, end, event.starts_at, (origin.address, event.directions_location)
        )
    except (requests.RequestException, ValueError):
        logger.warning("Trip request failed for event %s", event.pk, exc_info=True)
        return {**context, "status": "error"}
    context["provider"] = provider
    if not plans:
        return {**context, "status": "no_route"}

    status = MtaStatus()
    scheduled_best = max(plans, key=_lateness_key)
    live_from = scheduled_best["leave_by"] - LIVE_LEAD
    has_subway = any(_subway_rides(plan) for plan in plans)
    is_live = now >= live_from and has_subway

    try:
        snapshot = mta.alerts_snapshot()
        status.note(snapshot)
        alerts = snapshot["data"]
    except (requests.RequestException, ValueError):
        logger.warning("MTA alerts unavailable", exc_info=True)
        status.unavailable, alerts = True, []

    def check(options):
        for plan in options:
            check_alerts(plan, alerts)
            if is_live:
                check_live(plan, now, status)

    check(plans)
    # Every on-time trip has already left: plan from now instead, the next one you
    # can still catch, even if it gets there late.
    # Judged on the timetable: if the planned first train is gone, a later one breaks
    # the connections after it, so the whole trip is planned again.
    if not any(p["leave_by"] >= now - CATCH_SLACK for p in plans):
        try:
            _, from_now = trip_options(
                start,
                end,
                event.starts_at,
                (origin.address, event.directions_location),
                depart_at=now,
            )
        except (requests.RequestException, ValueError):
            from_now = []
        if from_now:
            check(from_now)
            plans = from_now
            scheduled_best = min(plans, key=lambda plan: plan["arrive"])

    chosen = choose(plans, event.starts_at, now)
    for segment in chosen["segments"]:
        if segment["kind"] == "ride":
            segment["badge"] = line_badge(segment)
    leave_by, arrive = chosen["live_leave_by"], chosen["live_arrive"]
    late = arrive - event.starts_at
    return {
        **context,
        "status": "ok",
        "plan": chosen,
        "leave_by": leave_by,
        "arrive": arrive,
        "leave_in_minutes": math.floor((leave_by - now).total_seconds() / 60),
        "late_minutes": max(0, math.ceil(late.total_seconds() / 60)),
        "switched_because": switch_reason(chosen, scheduled_best),
        "alerts": chosen["alerts"],
        "blocked_by": chosen.get("blocked_by"),
        "clashes_with_previous": origin.event is not None
        and leave_by < origin.event.ends_at,
        "is_live": is_live,
        "has_subway": has_subway,
        "live_from": live_from,
        # Only blink when the trip picked actually rides the subway.
        "live_dot": is_live and bool(_subway_rides(chosen)) and status.is_fresh(now),
        "mta": status,
        "now": now,
    }


def marker_data(trip):
    """The small JSON the timeline marker draws from."""
    if trip["status"] != "ok":
        return {"status": trip["status"]}
    rides = [s for s in trip["plan"]["segments"] if s["kind"] == "ride"]
    return {
        "status": "ok",
        "leave_by": trip["leave_by"].isoformat(),
        "arrive": trip["arrive"].isoformat(),
        "line": rides[0]["line"] if rides else "",
        "live": trip["live_dot"],
        "late_minutes": trip["late_minutes"],
        "alerts": len(trip["alerts"]),
        **_clock_labels(trip["leave_by"], event_zone(trip["event"])),
    }


def _clock_labels(moment, zone=None):
    """12h and 24h labels, in the event's zone like the card; strftime("%-I") doesn't
    work on Windows."""
    local = timezone.localtime(moment, zone)
    suffix = "AM" if local.hour < 12 else "PM"
    return {
        "label": f"{local.hour % 12 or 12}:{local.minute:02d} {suffix}",
        "clock": f"{local.hour:02d}:{local.minute:02d}",
    }
