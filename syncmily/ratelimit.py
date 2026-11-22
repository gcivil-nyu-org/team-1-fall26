"""
Rate limiter shared by every app (hardening spec 4.4 and 4.5, with 10.1).

Counters live in one of two places:

* "db": a Postgres row per window (syncmily.models.RateCounter), counted with one
  atomic INSERT ... ON CONFLICT upsert. The count is exact under concurrency, shared by
  every process, and survives a restart and any cache churn. Every HTTP limit uses it.
* "memory": the default local-memory cache. Chat frames and socket connects use it,
  because the consumer must not touch the database for a frame it refuses. Production
  runs one Daphne process, so one cache serves every socket. A move to several
  processes needs a shared counter here.

The numbers live in syncmily/limits.py (RATES). RATE_LIMITS below adds the store.
"""

import hashlib
import ipaddress
import logging
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from syncmily import limits, seclog

# Scopes whose counters live in process memory (see the module note).
MEMORY_SCOPES = frozenset(
    {"ws_connect", "chat_burst", "chat_hour", "chat_day", "chat_refused"}
)


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window: int
    key: str
    store: str = "db"


RATE_LIMITS = {
    scope: RateLimit(
        rate.limit,
        rate.window,
        rate.key,
        "memory" if scope in MEMORY_SCOPES else "db",
    )
    for scope, rate in limits.RATES.items()
}

# Limits counted per client IP, as (limit, window). Kept for the views that unpack them.
IP_LIMITS = {
    scope: (RATE_LIMITS[scope].limit, RATE_LIMITS[scope].window)
    for scope in ("signup", "login", "password_reset", "two_step_verify")
}


def client_ip(request):
    """
    Return the caller's IP the same way django-axes does in production:
    the right-most X-Forwarded-For entry (added by our own nginx), else
    REMOTE_ADDR. The left-most entries are client-controlled.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def ip_identity(ip):
    """The key part for an address. One IPv6 customer holds a whole /64, so a /64 is
    one identity; an IPv4-mapped address counts as its IPv4 address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return f"ip:{ip}"
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return f"ip:{address.ipv4_mapped}"
        network = ipaddress.ip_network(f"{address}/64", strict=False)
        return f"ip:{network.network_address}/64"
    return f"ip:{address}"


def _enabled():
    return getattr(settings, "RATELIMIT_ENABLED", True)


def _store_for(scope):
    entry = RATE_LIMITS.get(scope)
    return entry.store if entry else "db"


def _memory_key(scope, ident, window_seconds):
    return f"rl:{scope}:{ident}:{window_seconds}"


def _db_key(scope, ident, window_seconds):
    key = f"{scope}:{ident}:{window_seconds}"
    if len(key) > 255:
        key = f"{scope}:{hashlib.sha256(key.encode()).hexdigest()}:{window_seconds}"
    return key


_UPSERT = """
INSERT INTO syncmily_ratecounter (key, count, expires_at)
VALUES (%(key)s, %(start)s, %(expires)s)
ON CONFLICT (key) DO UPDATE SET
    count = CASE WHEN syncmily_ratecounter.expires_at <= %(now)s
                 THEN %(start)s ELSE syncmily_ratecounter.count + 1 END,
    expires_at = CASE WHEN syncmily_ratecounter.expires_at <= %(now)s
                      THEN EXCLUDED.expires_at
                      ELSE syncmily_ratecounter.expires_at END
RETURNING count
"""


def _db_hit(key, window_seconds):
    now = timezone.now()
    params = {
        "key": key,
        "start": 1,
        "now": now,
        "expires": now + timedelta(seconds=window_seconds),
    }
    with connection.cursor() as cursor:
        cursor.execute(_UPSERT, params)
        return cursor.fetchone()[0]


def _memory_hit(key, window_seconds):
    if cache.add(key, 1, window_seconds):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.add(key, 1, window_seconds)
        return 1


def hit(scope, ident, limit, window_seconds):
    """
    Count one hit for (scope, ident). Return True while the caller is
    within `limit` hits per `window_seconds`, False once over it.
    """
    if not _enabled():
        return True
    if _store_for(scope) == "memory":
        count = _memory_hit(_memory_key(scope, ident, window_seconds), window_seconds)
    else:
        count = _db_hit(_db_key(scope, ident, window_seconds), window_seconds)
    return count <= limit


def record(scope, ident, window_seconds):
    """Count one event and return the window's count, even when RATELIMIT_ENABLED is
    off. For limits that are product rules, not abuse throttles: invite caps and the
    account-wide sign-in pause hold in every environment, like the axes lockout."""
    if _store_for(scope) == "memory":
        return _memory_hit(_memory_key(scope, ident, window_seconds), window_seconds)
    return _db_hit(_db_key(scope, ident, window_seconds), window_seconds)


def get_count(scope, ident, window_seconds):
    """The hits counted so far in the current window (0 when there are none)."""
    if _store_for(scope) == "memory":
        return cache.get(_memory_key(scope, ident, window_seconds), 0)
    from syncmily.models import RateCounter

    row = RateCounter.objects.filter(
        key=_db_key(scope, ident, window_seconds), expires_at__gt=timezone.now()
    ).first()
    return row.count if row else 0


def set_count(scope, ident, window_seconds, count):
    """Set the count for the current window (a new window starts now). For tests and
    admin repairs."""
    if _store_for(scope) == "memory":
        cache.set(_memory_key(scope, ident, window_seconds), count, window_seconds)
        return
    from syncmily.models import RateCounter

    RateCounter.objects.update_or_create(
        key=_db_key(scope, ident, window_seconds),
        defaults={
            "count": count,
            "expires_at": timezone.now() + timedelta(seconds=window_seconds),
        },
    )


def purge_expired_counters():
    """Delete counter rows whose window has passed. Return how many went."""
    from syncmily.models import RateCounter

    deleted, _ = RateCounter.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted


def identities(request, key):
    """The identities a limit applies to: the user, the IP, or both."""
    ip = ip_identity(client_ip(request))
    user = getattr(request, "user", None)
    user_id = user.pk if user is not None and user.is_authenticated else None
    if key == "ip":
        return [ip]
    if key == "user":
        return [f"user:{user_id}"] if user_id else [ip]
    # user_and_ip: both must stay under the limit, so one account can't
    # rotate IPs and one IP can't rotate accounts.
    ids = [ip]
    if user_id:
        ids.append(f"user:{user_id}")
    return ids


def allow(request, scope, limit, window_seconds, key="user_and_ip"):
    """Record one attempt and say whether it is allowed."""
    results = [
        hit(scope, ident, limit, window_seconds) for ident in identities(request, key)
    ]
    return all(results)


def too_many_requests(request, message=None, scope=None):
    """The 429 answer: JSON for scripts, the slow-down page for people, both with a
    Retry-After header. `scope` names the limit that fired, for the log."""
    message = message or "You're doing that too often. Wait a minute and try again."
    seclog.event(
        "rate_limited",
        request=request,
        outcome="too_many_requests",
        level=logging.WARNING,
        status=429,
        scope=scope or "unknown",
    )
    entry = RATE_LIMITS.get(scope)
    retry_after = str(entry.window if entry else 60)
    wants_json = request.headers.get(
        "x-requested-with"
    ) == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")
    if wants_json:
        response = JsonResponse({"ok": False, "error": message}, status=429)
    else:
        response = render(
            request,
            "accounts/too_many_requests.html",
            {"message": message},
            status=429,
        )
    response["Retry-After"] = retry_after
    return response


def rate_limit(scope, limit=None, window_seconds=None, key=None, methods=("POST",)):
    """View decorator: answer 429 once the caller is over the limit.

    `@rate_limit("login")` reads the numbers from RATE_LIMITS. Passing a limit,
    window and key keeps the older call style working.
    """
    entry = RATE_LIMITS.get(scope)
    if limit is None:
        limit, window_seconds = entry.limit, entry.window
    if key is None:
        key = entry.key if entry else "user_and_ip"

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods and not allow(
                request, scope, limit, window_seconds, key
            ):
                return too_many_requests(request, scope=scope)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
