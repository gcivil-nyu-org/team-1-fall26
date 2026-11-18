"""Location search through Google Places (New), called from our server so the key stays
private. The browser's location box asks for suggestions as you type, then for the exact
coordinates of the one you pick. Both calls share a session token, which Google bills as
one search.
"""

import re

import requests
from django.conf import settings

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
PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{10,300}$")
SESSION = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class PlacesUnavailable(Exception):
    """No key set, or Google couldn't be reached."""


def enabled():
    return bool(settings.GOOGLE_MAPS_API_KEY)


def valid_session(token):
    return bool(token and SESSION.match(token))


def valid_place_id(place_id):
    return bool(place_id and PLACE_ID.match(place_id))


def _headers(fields=None):
    headers = {"X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY}
    if fields:
        headers["X-Goog-FieldMask"] = fields
    return headers


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
            AUTOCOMPLETE_URL, json=body, headers=_headers(), timeout=8
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PlacesUnavailable from error

    results = []
    for suggestion in data.get("suggestions") or []:
        prediction = suggestion.get("placePrediction") or {}
        place_id = prediction.get("placeId", "")
        structured = prediction.get("structuredFormat") or {}
        main = (structured.get("mainText") or {}).get("text") or (
            prediction.get("text") or {}
        ).get("text", "")
        if valid_place_id(place_id) and main:
            results.append(
                {
                    "id": place_id,
                    "main": main,
                    "secondary": (structured.get("secondaryText") or {}).get(
                        "text", ""
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
            DETAILS_URL.format(place_id=place_id),
            params=params,
            headers=_headers(DETAILS_FIELDS),
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PlacesUnavailable from error

    location = data.get("location") or {}
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        return None
    name = (data.get("displayName") or {}).get("text", "")
    address = data.get("formattedAddress", "")
    # "Times Square" + "Manhattan, NY 10036, USA"; skip the name if the address has it.
    if name and address and not address.startswith(name):
        label = f"{name}, {address}"
    else:
        label = address or name
    return {"label": label[:255], "latitude": latitude, "longitude": longitude}
