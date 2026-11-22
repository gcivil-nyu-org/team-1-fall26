"""Request-level guards: body size, the default write ceiling, the CSP header and
no-store caching for signed-in pages (hardening spec 4.1, 4.4, 7.1 and OPS-HDR-001)."""

import re

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.cache import patch_vary_headers

from syncmily import limits, logs, ratelimit
from syncmily.errors import too_large_response

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SCAN_PATH = re.compile(r"^/events/workspaces/[0-9a-f-]{36}/scan-ocr/$")

# Paths the default write ceiling skips. Logout must always work, the health checks must
# answer while the app is busy, and /csp-report/ has its own limit.
WRITE_EXEMPT_PATHS = frozenset({"/healthz/", "/readyz/", "/csp-report/"})
WRITE_EXEMPT_URL_NAMES = frozenset({"accounts:logout"})


class RequestContextMiddleware:
    """Give each request an id for the log lines it writes, and send it back in the
    X-Request-ID header so a person can quote it. Runs first, so every later record
    (even an error answer) carries it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = logs.new_request_id(request.headers.get("x-request-id"))
        request.request_id = request_id
        id_token = logs.request_id_var.set(request_id)
        request_token = logs.request_var.set(request)
        try:
            response = self.get_response(request)
        finally:
            logs.request_var.reset(request_token)
            logs.request_id_var.reset(id_token)
        response["X-Request-ID"] = request_id
        return response


class RequestBodyLimitMiddleware:
    """Refuse an oversized or malformed body before anything reads it.

    The size comes from Content-Length, so a 40 MB upload is turned away without
    Django touching it. scan-ocr takes up to 11 MiB and answers 413 JSON. Every other
    path takes 128 KiB and answers 400. A write with an unreadable Content-Length, or
    a chunked body, answers 400.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        refusal = self.refuse(request)
        if refusal is not None:
            return refusal
        return self.get_response(request)

    def refuse(self, request):
        raw = request.META.get("CONTENT_LENGTH", "")
        is_write = request.method in WRITE_METHODS
        if raw in ("", None):
            if is_write and request.META.get("HTTP_TRANSFER_ENCODING"):
                return too_large_response(request)
            return None
        if not raw.isascii() or not raw.isdigit():
            return too_large_response(request) if is_write else None
        size = int(raw)
        if SCAN_PATH.match(request.path):
            if size > limits.SCAN_BODY_CAP_BYTES:
                return JsonResponse(
                    {
                        "ok": False,
                        "status": "error",
                        "error": "That upload is too large.",
                        "error_code": "too_large",
                    },
                    status=413,
                )
        elif size > limits.DEFAULT_BODY_CAP_BYTES:
            return too_large_response(request)
        return None


class WriteRateLimitMiddleware:
    """The default ceiling on every POST, PUT, PATCH and DELETE: 120 a minute and
    1,000 an hour for a signed-in user, 120 a minute per IP for everyone else. Named
    limits (@rate_limit) apply on top. Logout and the health checks are exempt."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._exempt_paths = None

    def exempt_paths(self):
        if self._exempt_paths is None:
            self._exempt_paths = WRITE_EXEMPT_PATHS | {
                reverse(name) for name in WRITE_EXEMPT_URL_NAMES
            }
        return self._exempt_paths

    def __call__(self, request):
        if request.method in WRITE_METHODS and request.path not in self.exempt_paths():
            scope = self.over_limit(request)
            if scope:
                return ratelimit.too_many_requests(request, scope=scope)
        return self.get_response(request)

    def over_limit(self, request):
        """The scope of the limit the caller is over, or None."""
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            for scope in ("write_minute", "write_hour"):
                rate = ratelimit.RATE_LIMITS[scope]
                if not ratelimit.hit(scope, f"user:{user.pk}", rate.limit, rate.window):
                    return scope
            return None
        rate = ratelimit.RATE_LIMITS["write_anonymous"]
        ip = ratelimit.ip_identity(ratelimit.client_ip(request))
        if not ratelimit.hit("write_anonymous", ip, rate.limit, rate.window):
            return "write_anonymous"
        return None


class ContentSecurityPolicyMiddleware:
    """Put settings.CSP_POLICY on every response that lacks one, so a response that
    skips nginx still carries it. The same policy stays in nginx; the guard test keeps
    the two copies equal."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CSP_POLICY", "")
        if policy:
            name = (
                "Content-Security-Policy-Report-Only"
                if getattr(settings, "CSP_REPORT_ONLY", False)
                else "Content-Security-Policy"
            )
            if name not in response:
                response[name] = policy
            if "Reporting-Endpoints" not in response:
                response["Reporting-Endpoints"] = 'csp="/csp-report/"'
        return response


class PrivatePageCacheMiddleware:
    """Signed-in HTML is one person's schedule: a shared cache or the back button must
    not keep it. Mark it private and no-store."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and response.headers.get("Content-Type", "").startswith("text/html")
        ):
            response["Cache-Control"] = "private, no-store"
            patch_vary_headers(response, ("Cookie",))
        return response
