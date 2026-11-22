import json
import logging
import re

from django.http import HttpResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

from syncmily import ratelimit

logger = logging.getLogger("syncmily.csp")

CSP_REPORT_MAX_BYTES = 8 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _clean(value, length=100):
    """A value safe to log on one line: text only, no control characters, cut short."""
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value)[:length]


def _first_report(data):
    """The report body from either browser format: the older {"csp-report": {...}} or
    the Reporting API list [{"type": "csp-violation", "body": {...}}]."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    body = data.get("csp-report") or data.get("body") or {}
    return body if isinstance(body, dict) else {}


@csrf_exempt
def csp_report(request):
    """Browsers post a report here when the page breaks the CSP. Log only the directive
    and the blocked host, never the page URL, the sample or the full address."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    rate = ratelimit.RATE_LIMITS["csp_report"]
    ip = ratelimit.ip_identity(ratelimit.client_ip(request))
    if not ratelimit.hit("csp_report", ip, rate.limit, rate.window):
        return HttpResponse(status=429, headers={"Retry-After": str(rate.window)})
    try:
        size = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = CSP_REPORT_MAX_BYTES + 1
    if size > CSP_REPORT_MAX_BYTES:
        return HttpResponse(status=413)
    try:
        report = _first_report(json.loads(request.body or b"{}"))
    except (ValueError, RecursionError, UnicodeError):
        return HttpResponse(status=400)
    directive = report.get("effective-directive") or report.get("effectiveDirective")
    directive = directive or report.get("violated-directive")
    blocked = report.get("blocked-uri") or report.get("blockedURL") or ""
    host = re.sub(r"^[a-z][a-z0-9+.-]*:(//)?", "", _clean(blocked, 200).lower())
    host = re.split(r"[/?#]", host, maxsplit=1)[0]
    logger.info("csp violation directive=%s blocked=%s", _clean(directive), host[:100])
    return HttpResponse(status=204)
