"""
Fail fast on a trip planner or MTA feed that just failed.

The trip card asks Google Routes or Transitous, and the MTA, inside the page request,
and the timeline polls it too. When one of them is down or slow, every page load would
wait for the full timeout again and tie up a worker. After a failure that looks like an
outage (no connection, a timeout, a 5xx or a 429), calls to that service are skipped
for COOL_OFF_SECONDS: the card shows its "unavailable" state at once, and the MTA
keeps serving its last good copy. A 4xx for one odd request doesn't count, so a single
bad address can't switch the service off for everyone.
"""

import requests
from django.core.cache import cache

COOL_OFF_SECONDS = 60

# (connect, read) seconds. Connecting to a healthy service takes well under a second;
# the read allowance covers a slow answer without holding a worker for long.
TIMEOUT = (3.05, 8)


class RecentlyUnavailable(requests.ConnectionError):
    """Raised instead of calling a service that failed in the last COOL_OFF_SECONDS."""


def _key(name):
    return f"upstream-down:{name}"


def check(name):
    """Raise RecentlyUnavailable while `name` is cooling off after an outage."""
    if cache.get(_key(name)):
        raise RecentlyUnavailable(f"{name} failed recently; not calling it yet")


def is_cooling_off(name):
    return bool(cache.get(_key(name)))


def looks_like_outage(error):
    if isinstance(error, RecentlyUnavailable):
        return False
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


def record_failure(name, error):
    """Start the cool-off for `name` if `error` looks like an outage."""
    if looks_like_outage(error):
        cache.set(_key(name), True, COOL_OFF_SECONDS)
