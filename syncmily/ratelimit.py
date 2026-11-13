"""
Small cache-based rate limiter shared by every app.

Production runs one daphne process, so the default local-memory cache is
shared by every request and its increments are atomic within the process.
If the app ever runs more than one process, point CACHES at a shared
backend (Redis or the database cache) so the limits hold across workers.
"""

from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render


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


def _enabled():
    return getattr(settings, "RATELIMIT_ENABLED", True)


def hit(scope, ident, limit, window_seconds):
    """
    Count one hit for (scope, ident). Return True while the caller is
    within `limit` hits per `window_seconds`, False once over it.
    """
    if not _enabled():
        return True
    key = f"rl:{scope}:{ident}:{window_seconds}"
    if cache.add(key, 1, window_seconds):
        return limit >= 1
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.add(key, 1, window_seconds)
        count = 1
    return count <= limit


def identities(request, key):
    """The identities a limit applies to: the user, the IP, or both."""
    ip = client_ip(request)
    user = getattr(request, "user", None)
    user_id = user.pk if user is not None and user.is_authenticated else None
    if key == "ip":
        return [f"ip:{ip}"]
    if key == "user":
        return [f"user:{user_id}"] if user_id else [f"ip:{ip}"]
    # user_and_ip: both must stay under the limit, so one account can't
    # rotate IPs and one IP can't rotate accounts.
    ids = [f"ip:{ip}"]
    if user_id:
        ids.append(f"user:{user_id}")
    return ids


def allow(request, scope, limit, window_seconds, key="user_and_ip"):
    """Record one attempt and say whether it is allowed."""
    results = [
        hit(scope, ident, limit, window_seconds) for ident in identities(request, key)
    ]
    return all(results)


def too_many_requests(request, message=None):
    message = message or "You're doing that too often. Wait a minute and try again."
    wants_json = request.headers.get(
        "x-requested-with"
    ) == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")
    if wants_json:
        return JsonResponse({"ok": False, "error": message}, status=429)
    return render(
        request,
        "accounts/too_many_requests.html",
        {"message": message},
        status=429,
    )


def rate_limit(scope, limit, window_seconds, key="user_and_ip", methods=("POST",)):
    """View decorator: answer 429 once the caller is over the limit."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods and not allow(
                request, scope, limit, window_seconds, key
            ):
                return too_many_requests(request)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
