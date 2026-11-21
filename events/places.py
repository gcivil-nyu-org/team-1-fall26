"""Location search through Google Places (New), called from our server so the key stays
private. The browser's location box asks for suggestions as you type, then for the exact
coordinates of the one you pick. Both calls share a session token, which Google bills as
one search.
"""

import math
import re
from urllib.parse import quote

import requests
from django.conf import settings

from syncmily.text import clean_visible_text, truncate_graphemes

from . import upstream

AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
DETAILS_FIELDS = "id,displayName,formattedAddress,location"
# Prefer places around New York City without ruling out trips further away.
NYC_BIAS = {
    "circle": {
        "center": {"latitude": 40.7128, "longitude": -74.0060},
        "radius": 50000.0,
    }
}
PLACE_ID = re.compile(r"[A-Za-z0-9_-]{10,300}")
SESSION = re.compile(r"[A-Za-z0-9-]{1,64}")
# Both calls come from a person typing; a quick answer or none.
DEADLINE = upstream.Deadline(3.05, 5, total=8, max_bytes=256 * 1024)


class PlacesUnavailable(Exception):
    """No key set, or Google couldn't be reached."""


def enabled():
    return bool(settings.GOOGLE_MAPS_API_KEY)


def valid_session(token):
    return isinstance(token, str) and bool(SESSION.fullmatch(token))


def valid_place_id(place_id):
    return isinstance(place_id, str) and bool(PLACE_ID.fullmatch(place_id))


def _headers(fields=None):
    headers = {"X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY}
    if fields:
        headers["X-Goog-FieldMask"] = fields
    return headers


def _dict(value):
    return value if isinstance(value, dict) else {}


def _clean(value, limit=255):
    if not isinstance(value, str):
        return ""
    return truncate_graphemes(clean_visible_text(value), limit)


def _coordinate(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or abs(value) > limit:
        return None
    return float(value)


def suggest(query, session):
    """Up to five places matching what's typed: [{"id", "main", "secondary"}]."""
    if not enabled():
        raise PlacesUnavailable
    body = {
        "input": query,
        "locationBias": NYC_BIAS,
        "includedRegionCodes": ["us"],
        "languageCode": "en",
    }
    if valid_session(session):
        body["sessionToken"] = session
    try:
        response = requests.post(
            AUTOCOMPLETE_URL,
            json=body,
            headers=_headers(),
            timeout=DEADLINE,
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected answer from Places.")
    except (requests.RequestException, ValueError) as error:
        raise PlacesUnavailable from error

    results = []
    for suggestion in (data.get("suggestions") or [])[:20]:
        if not isinstance(suggestion, dict):
            continue
        prediction = _dict(suggestion.get("placePrediction"))
        place_id = prediction.get("placeId", "")
        structured = _dict(prediction.get("structuredFormat"))
        main = _dict(structured.get("mainText")).get("text") or _dict(
            prediction.get("text")
        ).get("text", "")
        # Provider text reaches the page, so it gets the same cleaning as typed text.
        main = _clean(main)
        if valid_place_id(place_id) and main:
            results.append(
                {
                    "id": place_id,
                    "main": main,
                    "secondary": _clean(
                        _dict(structured.get("secondaryText")).get("text", "")
                    ),
                }
            )
    return results[:5]


def details(place_id, session):
    """{"label", "latitude", "longitude"} for a picked place, or None if it has none."""
    if not enabled():
        raise PlacesUnavailable
    params = {"sessionToken": session} if valid_session(session) else {}
    try:
        response = requests.get(
            DETAILS_URL.format(place_id=quote(place_id, safe="")),
            params=params,
            headers=_headers(DETAILS_FIELDS),
            timeout=DEADLINE,
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected answer from Places.")
    except (requests.RequestException, ValueError) as error:
        raise PlacesUnavailable from error

    location = _dict(data.get("location"))
    latitude = _coordinate(location.get("latitude"), 90)
    longitude = _coordinate(location.get("longitude"), 180)
    if latitude is None or longitude is None:
        return None
    name = _clean(_dict(data.get("displayName")).get("text", ""))
    address = _clean(data.get("formattedAddress", ""))
    # "Times Square" + "Manhattan, NY 10036, USA"; skip the name if the address has it.
    if name and address and not address.startswith(name):
        label = f"{name}, {address}"
    else:
        label = address or name
    return {"label": label[:255], "latitude": latitude, "longitude": longitude}
