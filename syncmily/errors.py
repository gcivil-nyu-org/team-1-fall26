"""The error contract (hardening spec section 5).

Every error answers in the shape the caller can read: JSON for a script that asked for
JSON, a branded page for a person. A body never carries a stack trace, SQL, a setting,
a path or anything the caller typed. The pages here extend nothing that needs the
request, so they cannot raise while an error is already in flight.
"""

import logging
import sys
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import (
    DisallowedHost,
    RequestDataTooBig,
    TooManyFieldsSent,
    TooManyFilesSent,
)
from django.db.utils import InterfaceError, OperationalError
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import requires_csrf_token

from syncmily import seclog

logger = logging.getLogger(__name__)

_TOO_LARGE = (RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent)

# Seconds a caller should wait before retrying when the database is busy.
RETRY_AFTER_SECONDS = 5

# The last resort, if even a static template fails to render.
_PLAIN_PAGE = (
    "<!doctype html><html lang='en'><meta charset='utf-8'>"
    "<title>Syncmily</title><h1>Something went wrong</h1>"
    "<p><a href='/accounts/login/'>Back to Syncmily</a></p></html>"
)


def _refusal(
    request, event, outcome, status, exception=None, level=logging.INFO, **ids
):
    """Log one line for a refusal or an error. The reason is the exception's class,
    never its message, because that can hold what the caller typed. Logging must not
    break the error answer, so any failure here is dropped."""
    try:
        if exception is None:
            exc_type = sys.exc_info()[0]
        else:
            exc_type = type(exception)
        seclog.event(
            event,
            request=request,
            outcome=outcome,
            level=level,
            status=status,
            reason=exc_type.__name__ if exc_type else None,
            **ids,
        )
    except Exception:  # pragma: no cover - last resort
        pass


def wants_json(request):
    """True when the caller is a script that expects JSON back."""
    return (
        "application/json" in request.headers.get("accept", "")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    )


def json_error(status, error, message="", **extra):
    """A JSON error: {"ok": false, "error": <code>, "message": <words>}."""
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    body.update(extra)
    return JsonResponse(body, status=status)


def page(template, status, context=None):
    """Render a standalone error page without the request, so it cannot raise."""
    try:
        html = render_to_string(template, context or {})
    except Exception:  # pragma: no cover - last resort
        logger.exception("Error page %s failed to render", template)
        html = _PLAIN_PAGE
    return HttpResponse(html, status=status)


def _page_with_request(request, template, status):
    """Render a page that uses the site layout. Fall back to the plain page if the
    layout fails, because an error handler must never raise."""
    try:
        html = render_to_string(template, {}, request=request)
    except Exception:
        return page("500.html", status)
    return HttpResponse(html, status=status)


def too_large_response(request):
    """A body over the Django or middleware limit (400, not 413, outside scan-ocr)."""
    message = "That was more than Syncmily accepts."
    if wants_json(request):
        return json_error(400, "too_large", message)
    return page("400.html", 400, {"too_large": True})


def handler400(request, exception=None):
    if isinstance(exception, DisallowedHost):
        _refusal(
            request,
            "bad_host",
            "refused",
            400,
            exception,
            level=logging.WARNING,
            host=request.META.get("HTTP_HOST", "")[:100],
        )
    else:
        _refusal(request, "bad_request", "rejected", 400, exception)
    if isinstance(exception, _TOO_LARGE):
        return too_large_response(request)
    if wants_json(request):
        return json_error(
            400,
            "invalid_request",
            "Something in that request didn't look right. Go back and try again.",
        )
    return page("400.html", 400)


def handler403(request, exception=None):
    _refusal(request, "access_denied", "forbidden", 403, exception)
    if wants_json(request):
        return json_error(403, "forbidden", "You can't do that.")
    return _page_with_request(request, "403.html", 403)


def handler404(request, exception=None):
    _refusal(request, "not_found", "not_found", 404, exception)
    if wants_json(request):
        return json_error(404, "not_found", "That wasn't found.")
    return _page_with_request(request, "404.html", 404)


def _database_unavailable():
    """True when the error in flight is the database refusing or timing out a
    connection (Postgres out of connections, or the pool wait expired)."""
    return isinstance(sys.exc_info()[1], (OperationalError, InterfaceError))


def service_unavailable_response(request):
    """A 503 with Retry-After: the database is busy, so the caller should retry."""
    if wants_json(request):
        response = json_error(
            503, "service_busy", "Syncmily is busy right now. Try again in a moment."
        )
    else:
        response = page("503.html", 503)
    response["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response


def handler500(request):
    # Django calls this inside its except block, so sys.exc_info() holds the error,
    # including errors raised in middleware (the session lookup, for example).
    if _database_unavailable():
        _refusal(request, "db_unavailable", "error", 503, level=logging.WARNING)
        return service_unavailable_response(request)
    _refusal(request, "server_error", "error", 500, level=logging.ERROR)
    if wants_json(request):
        return json_error(
            500, "server_error", "Something went wrong on our side. Try again."
        )
    return page("500.html", 500)


@requires_csrf_token
def csrf_failure(request, reason=""):
    """The CSRF failure page. It never shows the reason: it only helps the person
    sign in again."""
    _refusal(request, "csrf_failed", "refused", 403, level=logging.WARNING)
    if wants_json(request):
        return json_error(
            403, "session_expired", "Your session expired. Sign in again."
        )
    return page("csrf_failure.html", 403)


def method_not_allowed(request, methods):
    _refusal(request, "method_not_allowed", "refused", 405, method=request.method)
    response = (
        json_error(405, "method_not_allowed", "That method isn't allowed here.")
        if wants_json(request)
        else page("405.html", 405)
    )
    response["Allow"] = ", ".join(methods)
    return response


def json_endpoint(methods=("GET",)):
    """Decorator for routes that scripts call with fetch().

    A wrong method answers 405 and a signed-out caller answers 401, both as JSON for a
    script that asked for JSON. A person who opens the address in a browser still gets
    the sign-in redirect. The view runs only for a signed-in caller.
    """
    allowed = tuple(m.upper() for m in methods)

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if wants_json(request):
                    return json_error(401, "login_required", "Sign in to continue.")
                return redirect_to_login(request.get_full_path())
            if request.method not in allowed:
                return method_not_allowed(request, allowed)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
