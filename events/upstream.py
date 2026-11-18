"""
Fail fast on a trip planner or MTA feed that just failed.

The trip card asks Google Routes or Transitous, and the MTA, inside the page request,
and the timeline polls it too. When one of them is down or slow, every page load would
wait for the full timeout again and tie up a worker.

A failure that looks like an outage (no connection, a timeout, a 5xx or a 429) is
remembered for COOL_OFF_SECONDS:

- That exact request isn't sent again, so one odd trip can't keep hitting a service.
- The whole service is skipped once OUTAGE_THRESHOLD different requests have failed
  within the window (a 429 counts at once: Google's quota is shared by everyone).
  One person's pin can't switch the trip card off for every family this way.

Feeds with fixed addresses (the MTA) pass threshold=1: a failure there is the
service, not the request. A 4xx for one request never counts.
"""

import hashlib

import requests
from django.core.cache import cache

COOL_OFF_SECONDS = 60
OUTAGE_THRESHOLD = 3

# (connect, read) seconds. Connecting to a healthy service takes well under a second;
# the read allowance covers a slow answer without holding a worker for long.
TIMEOUT = (3.05, 8)


class RecentlyUnavailable(requests.ConnectionError):
    """Raised instead of calling a service (or repeating a request) that just failed."""


def _down_key(name):
    return f"upstream-down:{name}"


def _request_key(name, request_key):
    digest = hashlib.sha256(str(request_key).encode()).hexdigest()[:32]
    return f"upstream-failed:{name}:{digest}"


def check(name, request_key=None):
    """Raise RecentlyUnavailable while `name` (or this request to it) is cooling off."""
    if cache.get(_down_key(name)):
        raise RecentlyUnavailable(f"{name} failed recently; not calling it yet")
    if request_key is not None and cache.get(_request_key(name, request_key)):
        raise RecentlyUnavailable(f"this {name} request failed recently")


def is_cooling_off(name):
    return bool(cache.get(_down_key(name)))


def _status(error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def looks_like_outage(error):
    if isinstance(error, RecentlyUnavailable):
        return False
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    status = _status(error)
    return status is not None and (status >= 500 or status == 429)


def _count_failure(name):
    key = f"upstream-fails:{name}"
    if cache.add(key, 1, COOL_OFF_SECONDS):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.add(key, 1, COOL_OFF_SECONDS)
        return 1


def record_failure(name, error, request_key=None, threshold=OUTAGE_THRESHOLD):
    """Remember an outage-like failure of `name` (see the module docstring)."""
    if not looks_like_outage(error):
        return
    if request_key is not None:
        cache.set(_request_key(name, request_key), True, COOL_OFF_SECONDS)
    if _status(error) == 429 or _count_failure(name) >= threshold:
        cache.set(_down_key(name), True, COOL_OFF_SECONDS)
