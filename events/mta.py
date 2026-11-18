"""Live subway arrivals from the MTA's GTFS-realtime feeds (free, no API key).

The feeds are protobuf. Rather than add a dependency for the generated GTFS-realtime
classes, this reads the handful of fields we need straight from the wire format:
FeedMessage.entity -> TripUpdate -> trip.route_id and stop_time_update (stop_id, times).
Field numbers come from https://gtfs.org/realtime/reference/.
"""

import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from functools import lru_cache
from pathlib import Path

import requests
from django.core.cache import cache

from . import upstream

FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F{feed}"
ALERTS_URL = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
)
# MTA data is re-read every five minutes: close enough to live for leave-by times, and
# gentle on the MTA. The last good copy is kept for an hour so an outage can say when
# the information is from instead of showing nothing.
FEED_CACHE_SECONDS = 5 * 60
STALE_KEEP_SECONDS = 60 * 60
STATIONS_CSV = Path(__file__).parent / "data" / "mta_subway_stations.csv"
# How far Google's stop can be from the MTA's coordinates for the same station.
STATION_MATCH_METERS = 300

# Which feed carries each line. Express variants (6X, 7X) ride with their local line.
LINE_FEEDS = {
    **dict.fromkeys(["A", "C", "E", "H", "FS"], "gtfs-ace"),
    **dict.fromkeys(["B", "D", "F", "FX", "M"], "gtfs-bdfm"),
    **dict.fromkeys(["G"], "gtfs-g"),
    **dict.fromkeys(["J", "Z"], "gtfs-jz"),
    **dict.fromkeys(["N", "Q", "R", "W"], "gtfs-nqrw"),
    **dict.fromkeys(["L"], "gtfs-l"),
    **dict.fromkeys(["1", "2", "3", "4", "5", "6", "6X", "7", "7X", "GS"], "gtfs"),
    **dict.fromkeys(["SI", "SIR"], "gtfs-si"),
}


@dataclass(frozen=True)
class Station:
    stop_id: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class StopTime:
    stop_id: str
    time: int  # Unix seconds


@dataclass(frozen=True)
class TripUpdate:
    route_id: str
    stops: tuple  # StopTime, in travel order


# --- protobuf wire format -------------------------------------------------------------


def _varint(data, pos):
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(data):
    """Yield (number, wire_type, value) for one message; nested messages stay bytes."""
    pos, end = 0, len(data)
    while pos < end:
        key, pos = _varint(data, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _varint(data, pos)
        elif wire_type == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire_type == 2:
            length, pos = _varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        elif wire_type == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def _stop_time(data):
    stop_id, time = "", 0
    for number, wire_type, value in _fields(data):
        if number == 4 and wire_type == 2:  # stop_id
            stop_id = value.decode()
        elif number in (2, 3) and wire_type == 2:  # arrival, departure: StopTimeEvent
            for event_number, event_type, event_value in _fields(value):
                if event_number == 2 and event_type == 0:  # time
                    # Prefer departure (3) over arrival (2) when both are there.
                    if number == 3 or not time:
                        time = event_value
    return StopTime(stop_id, time) if stop_id and time else None


def parse_feed(data):
    """The trip updates in one GTFS-realtime FeedMessage."""
    trips = []
    for number, wire_type, entity in _fields(data):
        if number != 2 or wire_type != 2:  # FeedMessage.entity
            continue
        for entity_number, entity_type, update in _fields(entity):
            if entity_number != 3 or entity_type != 2:  # FeedEntity.trip_update
                continue
            route_id, stops = "", []
            for field_number, field_type, value in _fields(update):
                if field_number == 1 and field_type == 2:  # trip: TripDescriptor
                    for trip_number, trip_type, trip_value in _fields(value):
                        if trip_number == 5 and trip_type == 2:  # route_id
                            route_id = trip_value.decode()
                elif field_number == 2 and field_type == 2:  # stop_time_update
                    stop = _stop_time(value)
                    if stop:
                        stops.append(stop)
            if route_id and stops:
                trips.append(TripUpdate(route_id, tuple(stops)))
    return trips


# --- stations and feeds ---------------------------------------------------------------


@lru_cache
def stations():
    with STATIONS_CSV.open(newline="", encoding="utf-8") as handle:
        return tuple(
            Station(row["stop_id"], row["name"], float(row["lat"]), float(row["lon"]))
            for row in csv.DictReader(handle)
        )


def distance_meters(lat1, lon1, lat2, lon2):
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def stations_near(latitude, longitude, meters=STATION_MATCH_METERS):
    return [
        station
        for station in stations()
        if distance_meters(latitude, longitude, station.latitude, station.longitude)
        <= meters
    ]


def feed_for_line(line):
    return LINE_FEEDS.get((line or "").upper())


def _snapshot(key, url, parse, timeout=upstream.TIMEOUT):
    """{"data": ..., "fetched": unix time}, refreshed every FEED_CACHE_SECONDS.

    When the MTA can't be reached, the last good copy is returned with "stale": True;
    with no copy at all the request error is raised. After an outage the feed isn't
    asked again for upstream.COOL_OFF_SECONDS, so each page load doesn't wait for it.
    """
    entry = cache.get(key)
    if entry and time.time() - entry["fetched"] < FEED_CACHE_SECONDS:
        return entry
    try:
        upstream.check(key)
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        entry = {"data": parse(response.content), "fetched": time.time()}
    except (requests.RequestException, ValueError, IndexError) as error:
        upstream.record_failure(key, error)
        if not entry:
            raise
        return {**entry, "stale": True}
    cache.set(key, entry, STALE_KEEP_SECONDS)
    return entry


def feed_snapshot(feed):
    return _snapshot(f"mta-feed:{feed}", FEED_URL.format(feed=feed), parse_feed)


def fetch_trips(feed):
    """Trip updates for one feed (see FEED_CACHE_SECONDS)."""
    return feed_snapshot(feed)["data"]


def _same_line(route_id, line):
    route_id, line = route_id.upper(), line.upper()
    return route_id == line or route_id.rstrip("X") == line.rstrip("X")


def station_ids(point=None, stop_ids=()):
    """Parent station ids: exact from MTA stop ids (R16S -> R16), else nearby ones."""
    exact = {stop_id.rstrip("NS") for stop_id in stop_ids if stop_id}
    if exact:
        return exact
    return {station.stop_id for station in stations_near(*point)} if point else set()


def next_departures(
    line, board, alight, now=None, limit=3, board_ids=None, alight_ids=None
):
    """The next live departures on `line` from near `board` that go on to near `alight`.

    `board` and `alight` are (latitude, longitude) pairs; `board_ids`/`alight_ids` are
    exact parent station ids when the planner gave them. Matching trips that call at the
    boarding station before the alighting one picks the right platform group and
    direction without guessing from headsigns. Returns aware datetimes, or an empty list
    when the line isn't covered or nothing matches.
    """
    feed = feed_for_line(line)
    if not feed:
        return []
    board_ids = board_ids or station_ids(board)
    alight_ids = alight_ids or station_ids(alight)
    if not board_ids or not alight_ids:
        return []

    now = int((now or datetime.now(datetime_timezone.utc)).timestamp())
    times = []
    for trip in fetch_trips(feed):
        if not _same_line(trip.route_id, line):
            continue
        boarding = None
        for stop in trip.stops:
            parent = stop.stop_id.rstrip("NS")  # platform ids end in N or S
            if boarding is None and parent in board_ids:
                boarding = stop
            elif boarding is not None and parent in alight_ids:
                if boarding.time >= now:
                    times.append(boarding.time)
                break
    # Two trains in the same minute read as one departure on screen.
    minutes = sorted({time // 60 * 60 for time in times})
    return [
        datetime.fromtimestamp(time, datetime_timezone.utc) for time in minutes[:limit]
    ]


# --- service alerts -------------------------------------------------------------------

# Alert types that mean a train won't stop where the trip needs it to.
BLOCKING_TYPES = {"Suspended", "Part Suspended", "Stops Skipped", "No Trains"}
# Alert types that mean slower trips but trains still run.
DELAY_TYPES = {
    "Delays",
    "Some Delays",
    "Severe Delays",
    "Expect Delays",
    "Reduced Service",
    "Slow Speeds",
    "Express to Local",
    "Reroute",
}


@dataclass(frozen=True)
class Alert:
    id: str
    kind: str  # "Delays", "Planned - Part Suspended", ...
    header: str
    routes: frozenset
    stations: frozenset  # parent station ids it names; empty means the whole line
    periods: tuple  # (start, end) unix times; end 0 means open-ended

    @property
    def base_kind(self):
        return self.kind.removeprefix("Planned - ")

    @property
    def is_planned(self):
        return self.kind.startswith("Planned")

    @property
    def blocks(self):
        return self.base_kind in BLOCKING_TYPES

    @property
    def slows(self):
        return self.base_kind in DELAY_TYPES

    def active_during(self, start, end):
        start, end = start.timestamp(), end.timestamp()
        return any(
            begin <= end and (not finish or finish >= start)
            for begin, finish in self.periods
        )


def parse_alerts(content):
    data = json.loads(content)
    alerts = []
    for entity in data.get("entity", []):
        alert = entity.get("alert") or {}
        informed = alert.get("informed_entity") or []
        routes = frozenset(i["route_id"] for i in informed if i.get("route_id"))
        if not routes:
            continue
        translations = (alert.get("header_text") or {}).get("translation") or []
        header = next(
            (t.get("text", "") for t in translations if t.get("language") == "en"),
            translations[0].get("text", "") if translations else "",
        )
        alerts.append(
            Alert(
                id=str(entity.get("id", "")),
                kind=(alert.get("transit_realtime.mercury_alert") or {}).get(
                    "alert_type", "Alert"
                ),
                header=" ".join(header.split()),
                routes=routes,
                stations=frozenset(
                    i["stop_id"].rstrip("NS") for i in informed if i.get("stop_id")
                ),
                periods=tuple(
                    (int(p.get("start", 0)), int(p.get("end", 0)))
                    for p in alert.get("active_period") or [{}]
                ),
            )
        )
    return alerts


def alerts_snapshot():
    return _snapshot("mta-alerts", ALERTS_URL, parse_alerts, timeout=(3.05, 10))
