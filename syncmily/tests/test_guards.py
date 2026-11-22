"""Guard tests: fail the build when someone reintroduces a gap (hardening spec 8.3
and 8.4.1 to 8.4.5), plus tests for the rate store, the error contract, the
middleware, the database checks and the retention jobs."""

import ast
import importlib
import inspect
import json
import random
import re
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, models, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.test import override_settings
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse
from django.utils import timezone

from syncmily import limits, ratelimit
from syncmily.errors import json_endpoint
from syncmily.middleware import WRITE_EXEMPT_PATHS, WRITE_EXEMPT_URL_NAMES
from syncmily.models import RateCounter

BASE_DIR = Path(__file__).resolve().parents[2]
User = get_user_model()
NGINX_SCRIPT = (BASE_DIR / ".platform/hooks/postdeploy/03_https.sh").read_text()


def all_url_names(patterns=None, prefix=""):
    patterns = get_resolver().url_patterns if patterns is None else patterns
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            namespace = f"{pattern.namespace}:" if pattern.namespace else ""
            yield from all_url_names(pattern.url_patterns, prefix + namespace)
        elif isinstance(pattern, URLPattern) and pattern.name:
            yield prefix + pattern.name


class RateLimitCoverageGuard(SimpleTestCase):
    """8.4.3: every write is covered by the default ceiling or a named limit."""

    def middleware_index(self, name):
        return settings.MIDDLEWARE.index(name)

    def test_default_ceiling_runs_after_authentication(self):
        self.assertGreater(
            self.middleware_index("syncmily.middleware.WriteRateLimitMiddleware"),
            self.middleware_index(
                "django.contrib.auth.middleware.AuthenticationMiddleware"
            ),
        )

    def test_body_limit_runs_right_after_security_middleware(self):
        self.assertEqual(
            self.middleware_index("syncmily.middleware.RequestBodyLimitMiddleware"),
            self.middleware_index("django.middleware.security.SecurityMiddleware") + 1,
        )

    def test_exemptions_are_explicit_and_real(self):
        self.assertEqual(WRITE_EXEMPT_PATHS, {"/healthz/", "/readyz/", "/csp-report/"})
        self.assertEqual(WRITE_EXEMPT_URL_NAMES, {"accounts:logout"})
        names = set(all_url_names())
        self.assertTrue(WRITE_EXEMPT_URL_NAMES <= names)
        self.assertEqual(resolve("/csp-report/").func.__name__, "csp_report")

    def test_every_limit_in_limits_py_is_in_the_registry(self):
        self.assertEqual(set(ratelimit.RATE_LIMITS), set(limits.RATES))
        for scope, entry in ratelimit.RATE_LIMITS.items():
            self.assertIn(entry.store, {"db", "memory"}, scope)
            self.assertGreater(entry.limit, 0, scope)

    def test_chat_counters_stay_in_memory_and_http_counters_in_the_database(self):
        for scope in ("chat_burst", "chat_hour", "chat_day", "chat_refused"):
            self.assertEqual(ratelimit.RATE_LIMITS[scope].store, "memory")
        for scope in ("login", "signup", "password_reset", "write_minute"):
            self.assertEqual(ratelimit.RATE_LIMITS[scope].store, "db")

    def test_decorator_scopes_exist_in_the_registry(self):
        pattern = re.compile(r'@rate_limit\(\s*"([a-z_]+)"')
        found = set()
        for path in BASE_DIR.glob("*/*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            found.update(pattern.findall(path.read_text()))
        self.assertTrue(found)
        self.assertLessEqual(found, set(ratelimit.RATE_LIMITS))


class SettingsSyncGuard(SimpleTestCase):
    """8.4.4: the two copies of each shared rule agree."""

    def nginx_csp(self):
        raw = re.search(
            r'add_header Content-Security-Policy "([^"]+)"', NGINX_SCRIPT
        ).group(1)
        return raw.replace("$PRIMARY", settings.CSP_PRIMARY_HOST)

    def test_csp_policy_equals_the_nginx_string(self):
        self.assertEqual(settings.CSP_POLICY, self.nginx_csp())

    def test_csp_policy_reports_violations(self):
        self.assertIn("report-uri /csp-report/", settings.CSP_POLICY)
        self.assertIn("report-to csp", settings.CSP_POLICY)
        self.assertIn("Reporting-Endpoints", NGINX_SCRIPT)

    def test_nginx_body_caps_equal_limits_py(self):
        self.assertIn(
            f"client_max_body_size  {limits.NGINX_BODY_CAP_SCAN};", NGINX_SCRIPT
        )
        self.assertIn(
            f"client_max_body_size {limits.NGINX_BODY_CAP_DEFAULT};", NGINX_SCRIPT
        )
        self.assertIn(
            f"client_max_body_size {limits.NGINX_BODY_CAP_WEBSOCKET};", NGINX_SCRIPT
        )

    def test_middleware_caps_match_the_nginx_caps(self):
        self.assertEqual(limits.DEFAULT_BODY_CAP_BYTES, 128 * 1024)
        self.assertLess(limits.SCAN_BODY_CAP_BYTES, 12 * 1024 * 1024)

    def test_django_upload_settings_come_from_limits_py(self):
        for name in (
            "DATA_UPLOAD_MAX_MEMORY_SIZE",
            "DATA_UPLOAD_MAX_NUMBER_FIELDS",
            "DATA_UPLOAD_MAX_NUMBER_FILES",
            "FILE_UPLOAD_MAX_MEMORY_SIZE",
        ):
            self.assertEqual(getattr(settings, name), getattr(limits, name), name)


@override_settings(RATELIMIT_ENABLED=True)
class RateStoreTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_counter_is_a_database_row_and_counts_exactly(self):
        for _ in range(3):
            ratelimit.hit("login", "ip:1.2.3.4", 150, 600)
        self.assertEqual(ratelimit.get_count("login", "ip:1.2.3.4", 600), 3)
        self.assertEqual(RateCounter.objects.get().count, 3)

    def test_window_restarts_after_it_expires(self):
        ratelimit.set_count("login", "ip:1.2.3.4", 600, 5)
        RateCounter.objects.update(expires_at=timezone.now() - timezone.timedelta(1))
        self.assertEqual(ratelimit.get_count("login", "ip:1.2.3.4", 600), 0)
        self.assertTrue(ratelimit.hit("login", "ip:1.2.3.4", 1, 600))
        self.assertEqual(ratelimit.get_count("login", "ip:1.2.3.4", 600), 1)
        self.assertFalse(ratelimit.hit("login", "ip:1.2.3.4", 1, 600))

    def test_purge_removes_only_expired_rows(self):
        ratelimit.set_count("login", "ip:a", 600, 1)
        ratelimit.set_count("login", "ip:b", 600, 1)
        RateCounter.objects.filter(key__contains="ip:a").update(
            expires_at=timezone.now() - timezone.timedelta(1)
        )
        self.assertEqual(ratelimit.purge_expired_counters(), 1)
        self.assertEqual(RateCounter.objects.count(), 1)

    def test_chat_scopes_use_memory_and_no_sql(self):
        with self.assertNumQueries(0):
            for _ in range(40):
                ratelimit.hit("chat_burst", 7, 20, 10)
        self.assertEqual(cache.get("rl:chat_burst:7:10"), 40)
        self.assertEqual(RateCounter.objects.count(), 0)

    def test_long_keys_are_hashed(self):
        ratelimit.hit("login", "ip:" + "x" * 400, 5, 600)
        self.assertLessEqual(len(RateCounter.objects.get().key), 255)

    def test_ipv6_addresses_share_one_counter_per_64(self):
        rf = RequestFactory()
        first = ratelimit.identities(rf.post("/", REMOTE_ADDR="2001:db8:1:2::1"), "ip")
        second = ratelimit.identities(
            rf.post("/", REMOTE_ADDR="2001:db8:1:2:f::9"), "ip"
        )
        other = ratelimit.identities(rf.post("/", REMOTE_ADDR="2001:db8:1:3::1"), "ip")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_rate_limit_decorator_reads_the_registry_and_sets_retry_after(self):
        @ratelimit.rate_limit("signup")
        def view(request):
            return HttpResponse("ok")

        rf = RequestFactory()
        for _ in range(50):
            self.assertEqual(view(rf.post("/")).status_code, 200)
        response = view(rf.post("/"))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "3600")

    @override_settings(RATELIMIT_ENABLED=False)
    def test_disabled_limiter_counts_nothing(self):
        ratelimit.hit("login", "ip:1.2.3.4", 1, 600)
        self.assertEqual(RateCounter.objects.count(), 0)


@override_settings(RATELIMIT_ENABLED=True)
class WriteCeilingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_anonymous_posts_stop_at_120_a_minute_per_ip(self):
        client = Client()
        statuses = [
            client.post(
                "/csp-report-not-a-route/", REMOTE_ADDR="198.51.100.4"
            ).status_code
            for _ in range(121)
        ]
        self.assertEqual(statuses.count(429), 1)
        self.assertEqual(statuses[-1], 429)

    def test_logout_and_health_checks_are_exempt(self):
        client = Client()
        for _ in range(130):
            client.post("/csp-report-not-a-route/", REMOTE_ADDR="198.51.100.5")
        client.get("/healthz/")
        response = client.post(reverse("accounts:logout"), REMOTE_ADDR="198.51.100.5")
        self.assertNotEqual(response.status_code, 429)


class ErrorContractTests(TestCase):
    json_headers = {
        "HTTP_ACCEPT": "application/json",
        "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
    }

    def test_404_answers_json_for_a_script_and_a_page_for_a_person(self):
        response = self.client.get("/no/such/page/", **self.json_headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not_found")
        page = self.client.get("/no/such/page/")
        self.assertEqual(page.status_code, 404)
        self.assertIn("text/html", page["Content-Type"])

    def test_csrf_failure_json_and_page(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/accounts/login/", {}, **self.json_headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "session_expired")

    def test_error_bodies_carry_no_internals(self):
        response = self.client.get("/no/such/page/", **self.json_headers)
        self.assertEqual(set(response.json()), {"ok", "error", "message"})

    def test_json_endpoint_decorator(self):
        @json_endpoint(["POST"])
        def view(request):
            return HttpResponse("ok")

        rf = RequestFactory()
        request = rf.post("/", **self.json_headers)
        request.user = type("U", (), {"is_authenticated": False})()
        self.assertEqual(view(request).status_code, 401)
        request.user = type("U", (), {"is_authenticated": True})()
        self.assertEqual(view(request).status_code, 200)
        get = rf.get("/", **self.json_headers)
        get.user = request.user
        response = view(get)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(json.loads(response.content)["error"], "method_not_allowed")


class RequestBodyLimitTests(TestCase):
    def test_body_over_128_kib_is_400_for_other_paths(self):
        response = self.client.post(
            "/accounts/login/",
            b"a=" + b"A" * (129 * 1024),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("more than Syncmily accepts", response.content.decode())

    def test_scan_ocr_over_11_mib_is_413_json(self):
        path = f"/events/workspaces/{'0' * 8}-0000-0000-0000-{'0' * 12}/scan-ocr/"
        response = self.client.post(
            path, b"x", content_type="image/png", CONTENT_LENGTH=str(12 * 1024 * 1024)
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error_code"], "too_large")

    def test_invalid_content_length_is_400(self):
        response = self.client.post(
            "/accounts/login/", b"a=1", content_type="text/plain", CONTENT_LENGTH="abc"
        )
        self.assertEqual(response.status_code, 400)

    def test_chunked_body_is_400(self):
        response = self.client.post(
            "/accounts/login/",
            b"a=1",
            content_type="text/plain",
            CONTENT_LENGTH="",
            HTTP_TRANSFER_ENCODING="chunked",
        )
        self.assertEqual(response.status_code, 400)


class HealthAndCspTests(TestCase):
    def test_readyz_is_200_with_a_database(self):
        self.assertEqual(self.client.get("/readyz/").status_code, 200)

    def test_every_response_carries_the_csp(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response["Content-Security-Policy"], settings.CSP_POLICY)
        self.assertEqual(response["Reporting-Endpoints"], 'csp="/csp-report/"')

    @override_settings(RATELIMIT_ENABLED=True)
    def test_csp_report_logs_directive_and_host_only(self):
        body = json.dumps(
            {
                "csp-report": {
                    "effective-directive": "img-src\nFORGED",
                    "blocked-uri": "https://evil.example/path?secret=1",
                    "document-uri": "https://syncmily.app/accounts/login/?token=abc",
                }
            }
        )
        with self.assertLogs("syncmily.csp", level="INFO") as logs:
            response = self.client.post(
                "/csp-report/", body, content_type="application/csp-report"
            )
        self.assertEqual(response.status_code, 204)
        line = logs.output[0]
        self.assertIn("directive=img-src FORGED", line)
        self.assertIn("blocked=evil.example", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("token", line)
        self.assertNotIn("\nFORGED", line)

    def test_csp_report_is_post_only_and_capped_at_8_kib(self):
        self.assertEqual(self.client.get("/csp-report/").status_code, 405)
        big = json.dumps({"csp-report": {"blocked-uri": "x" * 9000}})
        response = self.client.post(
            "/csp-report/", big, content_type="application/json"
        )
        self.assertEqual(response.status_code, 413)

    def test_csp_report_needs_no_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/csp-report/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 204)


class UnsafeOutputLint(SimpleTestCase):
    """Hardening spec 8.4.2: Django's autoescape is the XSS control, so every way
    around it must be on this list. A new use fails the build until someone reviews it
    and adds it here."""

    TEMPLATE_PATTERN = re.compile(r"\|\s*(?:safe|urlize|linebreaks)\b|autoescape\s+off")
    PYTHON_PATTERN = re.compile(r"\bmark_safe\b|\bSafeString\b|\bSafeText\b")
    JS_PATTERN = re.compile(
        r"\.innerHTML\s*=|innerHTML\s*:|insertAdjacentHTML|outerHTML\s*=|"
        r"document\.write"
    )
    # (path relative to the repository, why the use is safe)
    TEMPLATES = {
        "accounts/templates/accounts/two_step_setup.html": "the QR code SVG the "
        "server draws from the authenticator URI",
        "accounts/templates/accounts/emails/invite.txt": "plain-text mail",
        "accounts/templates/accounts/emails/password_reset.txt": "plain-text mail",
        "accounts/templates/accounts/emails/signup_notice.txt": "plain-text mail",
        "accounts/templates/accounts/emails/verify_email.txt": "plain-text mail",
    }
    PYTHON = {}
    JS = {}

    def _scan(self, roots, suffixes, pattern, allowed):
        found = {}
        for root in roots:
            for path in (BASE_DIR / root).rglob("*"):
                parts = set(path.parts)
                if (
                    path.suffix not in suffixes
                    or parts & {".venv", "node_modules", "staticfiles", "tests"}
                    or path.name.startswith("test_")
                    or path.name == "tests.py"
                ):
                    continue
                text = path.read_text(errors="ignore")
                if pattern.search(text):
                    found[str(path.relative_to(BASE_DIR))] = text
        unexpected = sorted(set(found) - set(allowed))
        self.assertEqual(unexpected, [], "unreviewed way around autoescape")
        return found

    APPS = ("accounts", "calendars", "chat", "checklists", "events", "syncmily")

    def test_templates_never_turn_autoescape_off_outside_the_allow_list(self):
        self._scan(self.APPS, {".html", ".txt"}, self.TEMPLATE_PATTERN, self.TEMPLATES)

    def test_python_never_marks_text_safe(self):
        # format_html escapes every value and is the safe way to build markup.
        self._scan(self.APPS, {".py"}, self.PYTHON_PATTERN, self.PYTHON)

    def test_javascript_never_assigns_markup_strings(self):
        self._scan(self.APPS, {".js"}, self.JS_PATTERN, self.JS)

    def test_the_allow_list_has_no_stale_entries(self):
        found = self._scan(
            self.APPS, {".html", ".txt"}, self.TEMPLATE_PATTERN, self.TEMPLATES
        )
        self.assertEqual(sorted(set(self.TEMPLATES) - set(found)), [])


# ---------------------------------------------------------------------------------
# Raw request reads (spec 2.4, 8.4.1)
# ---------------------------------------------------------------------------------


class RawReadGuard(SimpleTestCase):
    """Views read request.GET and request.POST only through a Form or Params, so
    every value is typed and checked before use. A raw read fails the build."""

    FILES = ("accounts/two_step.py", "calendars/views.py")
    # Passing the whole query string on to another page reads no value.
    PASS_THROUGH = {"urlencode"}

    @staticmethod
    def _callee(call):
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            # Form.from_query(request.GET, ...) builds a form too.
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id.endswith("Form"):
                return owner.id
            return func.attr
        return ""

    def _allowed(self, node, parents):
        child, parent = node, parents.get(node)
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr in self.PASS_THROUGH
            and isinstance(parents.get(parent), ast.Call)
        ):
            return True
        # Climb through `x or None`, `x if cond else None` and `GET | POST`.
        while isinstance(parent, (ast.BoolOp, ast.IfExp, ast.BinOp)):
            if isinstance(parent, ast.IfExp) and child is parent.test:
                return False
            child, parent = parent, parents.get(parent)
        if isinstance(parent, ast.keyword):
            child, parent = parent, parents.get(parent)
        if not isinstance(parent, ast.Call) or child is parent.func:
            return False
        name = self._callee(parent)
        return name.endswith("Form") or name == "Params"

    def raw_reads(self):
        paths = sorted(
            {*BASE_DIR.glob("*/views.py"), *map(BASE_DIR.joinpath, self.FILES)}
        )
        found = []
        for path in paths:
            tree = ast.parse(path.read_text())
            parents = {
                child: node
                for node in ast.walk(tree)
                for child in ast.iter_child_nodes(node)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in ("GET", "POST")
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "request"
                    and not self._allowed(node, parents)
                ):
                    found.append(f"{path.relative_to(BASE_DIR)}:{node.lineno}")
        return found

    def test_views_read_requests_only_through_forms_or_params(self):
        self.assertEqual(self.raw_reads(), [])

    def test_guard_catches_a_raw_read(self):
        tree = ast.parse(
            "def view(request):\n"
            "    a = request.GET.get('x')\n"
            "    b = SomeForm(request.POST or None)\n"
            "    c = Params(request.GET).int('page', 1, 9)\n"
            "    d = 'x' in request.GET\n"
        )
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        lines = sorted(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in ("GET", "POST")
            and not self._allowed(node, parents)
        )
        self.assertEqual(lines, [2, 5])


# ---------------------------------------------------------------------------------
# Form text policies (spec 8.3)
# ---------------------------------------------------------------------------------


class FormPolicyGuard(SimpleTestCase):
    """Every text box in every app form names its policy (spec 2.3, 3.1). Boxes that
    take no stored free text are listed here with the reason."""

    APPS = ("accounts", "calendars", "chat", "checklists", "events")
    NOT_FREE_TEXT = {
        ("accounts.forms.TwoStepCodeForm", "code"): "a one-time or backup code",
        ("accounts.forms.TwoStepEnrolForm", "code"): "a one-time code",
        ("accounts.forms.TwoStepEnrolForm", "password"): "the account password, "
        "checked and never stored",
        ("events.forms.PlaceDetailsForm", "session"): "a token checked by a pattern",
        ("events.forms.PlaceQueryForm", "session"): "a token checked by a pattern",
    }

    def unexplained_fields(self):
        from django import forms as django_forms
        from django.contrib.auth.forms import UsernameField

        from syncmily.safety import SafeCharField

        missing = []
        for app in self.APPS:
            try:
                module = importlib.import_module(f"{app}.forms")
            except ModuleNotFoundError:
                continue
            for _, form in inspect.getmembers(module, inspect.isclass):
                if (
                    not issubclass(form, django_forms.BaseForm)
                    or form.__module__ != module.__name__
                ):
                    continue
                meta = getattr(form, "Meta", None)
                declared = getattr(meta, "text_policies", None) or {}
                for name, field in form.base_fields.items():
                    if not isinstance(field, django_forms.CharField):
                        continue
                    if (
                        isinstance(field, SafeCharField)
                        or name in declared
                        # Checked by their own rules: addresses, usernames
                        # (spec 3.2), patterns, and secrets nobody sees again.
                        or isinstance(
                            field,
                            (
                                django_forms.EmailField,
                                django_forms.RegexField,
                                UsernameField,
                            ),
                        )
                        or isinstance(
                            field.widget,
                            (django_forms.PasswordInput, django_forms.HiddenInput),
                        )
                        or (f"{module.__name__}.{form.__name__}", name)
                        in self.NOT_FREE_TEXT
                    ):
                        continue
                    missing.append(f"{module.__name__}.{form.__name__}.{name}")
        return missing

    def test_every_text_box_names_its_policy(self):
        self.assertEqual(self.unexplained_fields(), [])

    def test_declared_policies_exist(self):
        from syncmily.safety import POLICIES

        for app in self.APPS:
            try:
                module = importlib.import_module(f"{app}.forms")
            except ModuleNotFoundError:
                continue
            for _, form in inspect.getmembers(module, inspect.isclass):
                meta = getattr(form, "Meta", None)
                for policy in (getattr(meta, "text_policies", None) or {}).values():
                    self.assertIn(policy, POLICIES, form)


# ---------------------------------------------------------------------------------
# Database constraints (spec 8.4.5, 8.5) and migration safety (OPS-MIG-001)
# ---------------------------------------------------------------------------------

CONSTRAINT_APPS = {"accounts", "calendars", "chat", "checklists", "events"}


def _app_models():
    from django.apps import apps

    return [m for m in apps.get_models() if m._meta.app_label in CONSTRAINT_APPS]


def _checks(model):
    return [c for c in model._meta.constraints if isinstance(c, models.CheckConstraint)]


class ConstraintPresenceGuard(TestCase):
    """New models and columns keep the database checks."""

    def test_every_user_text_column_has_a_text_check(self):
        from syncmily.constraints import MULTILINE_TEXT_PATTERN, TEXT_PATTERN
        from syncmily.safety import CleanCharField, CleanTextField

        missing = []
        for model in _app_models():
            for field in model._meta.concrete_fields:
                if not isinstance(field, (CleanCharField, CleanTextField)):
                    continue
                pattern = (
                    MULTILINE_TEXT_PATTERN if field.policy.multiline else TEXT_PATTERN
                )
                wanted = models.Q((f"{field.name}__regex", pattern), _negated=True)
                if not any(c.condition == wanted for c in _checks(model)):
                    missing.append(f"{model.__name__}.{field.name}")
        self.assertEqual(missing, [])

    def test_every_text_field_has_a_length_check(self):
        from syncmily.constraints import CharLength

        missing = []
        for model in _app_models():
            for field in model._meta.concrete_fields:
                if not isinstance(field, models.TextField):
                    continue
                if not any(
                    f"{CharLength.__name__}(F({field.name}))" in str(c.condition)
                    for c in _checks(model)
                ):
                    missing.append(f"{model.__name__}.{field.name}")
        self.assertEqual(missing, [])

    def test_every_declared_check_exists_and_is_validated(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT t.relname, c.conname, c.convalidated FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE c.contype = 'c'"
            )
            in_db = {(table, name): valid for table, name, valid in cursor.fetchall()}
        problems = []
        for model in _app_models():
            for check in _checks(model):
                key = (model._meta.db_table, check.name)
                if key not in in_db:
                    problems.append(f"{check.name} missing")
                elif not in_db[key]:
                    problems.append(f"{check.name} not validated")
        self.assertEqual(problems, [])

    def test_normalized_text_always_passes_the_text_check(self):
        """Whatever the normalizer stores, Postgres accepts: a refused write would
        be a 500 page, not a form error."""
        from syncmily.constraints import MULTILINE_TEXT_PATTERN, TEXT_PATTERN
        from syncmily.text import normalize_text

        alphabet = [
            *map(chr, range(0, 0x250)),
            *map(chr, range(0x2000, 0x2070)),
            *map(chr, range(0x0300, 0x0370)),
            *map(chr, range(0x1AB0, 0x1B00)),
            *map(chr, range(0x1DC0, 0x1E00)),
            *map(chr, range(0x20D0, 0x2100)),
            *map(chr, range(0xFE00, 0xFE30)),
            *map(chr, range(0xE0000, 0xE0080)),
            "؜",
            "﻿",
            "ا",
            "क",
            "्",
            "\U0001f468",
            "\U0001f3f4",
        ]
        rng = random.Random(8045)
        samples = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
            for _ in range(400)
        ]
        values = []
        for raw in samples:
            values.append((normalize_text(raw), TEXT_PATTERN))
            values.append((normalize_text(raw, multiline=True), MULTILINE_TEXT_PATTERN))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM unnest(%s::text[], %s::text[]) AS v(value, p) "
                "WHERE value ~ p",
                [[v for v, _ in values], [p for _, p in values]],
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_check_refuses_a_raw_write(self):
        from accounts.models import Workspace

        owner = User.objects.create_user("guard-owner", password="x" * 12)
        family = Workspace.objects.create(name="Guard family", primary_admin=owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts_workspace SET name = %s WHERE id = %s",
                    ["a‮b", family.pk],
                )


class NormalizingQuerySetTests(TestCase):
    """update() and bulk_update() skip pre_save, so the QuerySet cleans the text
    itself instead of hitting the database check (spec 10.2)."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Workspace

        cls.owner = User.objects.create_user("qs-owner", password="x" * 12)
        cls.family = Workspace.objects.create(name="Plain", primary_admin=cls.owner)

    def test_update_normalizes_clean_fields(self):
        from accounts.models import Workspace

        Workspace.objects.filter(pk=self.family.pk).update(
            name="Bad‮\r\nname", home_location="​Main St"
        )
        self.family.refresh_from_db()
        self.assertEqual(self.family.name, "Bad name")
        self.assertEqual(self.family.home_location, "Main St")

    def test_bulk_update_normalizes_clean_fields(self):
        from accounts.models import Workspace

        self.family.name = "Tab\there"
        Workspace.objects.bulk_update([self.family], ["name"])
        self.family.refresh_from_db()
        self.assertEqual(self.family.name, "Tab here")

    def test_related_managers_normalize_too(self):
        from chat.models import ChatMessage

        message = ChatMessage.objects.create(
            workspace=self.family, sender=self.owner, content="hi"
        )
        self.family.chat_messages.filter(pk=message.pk).update(content="a\x07b")
        message.refresh_from_db()
        self.assertEqual(message.content, "a b")


class MigrationDefaultGuard(SimpleTestCase):
    """OPS-MIG-001 for every migration: a NOT NULL column gets a database default,
    so the release still serving during migrate can keep inserting rows."""

    # Shipped long before any release that could run without them.
    LEGACY = {
        ("events", "0004_event_timezone_name", "timezone_name"),
        ("events", "0008_event_transport_mode", "transport_mode"),
    }

    def test_not_null_columns_have_a_database_default(self):
        from django.db.migrations.loader import MigrationLoader
        from django.db.models import NOT_PROVIDED

        loader = MigrationLoader(None, ignore_no_migrations=True)
        bad = []
        for (app, name), migration in sorted(loader.disk_migrations.items()):
            if app not in CONSTRAINT_APPS | {"syncmily"}:
                continue
            for operation in migration.operations:
                if operation.__class__.__name__ != "AddField":
                    continue
                field = operation.field
                if field.null or field.many_to_many or field.primary_key:
                    continue
                if field.db_default is NOT_PROVIDED and (
                    (app, name, operation.name) not in self.LEGACY
                ):
                    bad.append(f"{app}.{name}.{operation.name}")
        self.assertEqual(bad, [])


# ---------------------------------------------------------------------------------
# Retention (spec 4.5) and the pre-flight audit (spec 8.5)
# ---------------------------------------------------------------------------------


class RetentionTests(TestCase):
    def test_purge_timer_runs_every_retention_job(self):
        script = (BASE_DIR / ".platform/hooks/postdeploy/04_purge_timer.sh").read_text()
        for needed in (
            "clearsessions",
            "axes_reset_logs --age 30",
            "purge_expired",
            "User=webapp",
            "TimeoutStartSec",
            "EnvironmentFile",
        ):
            self.assertIn(needed, script)

    def test_purge_expired_removes_old_rows_and_keeps_current_ones(self):
        from datetime import timedelta

        from accounts.models import EmailAddress
        from chat.models import ChatMessage

        now = timezone.now()
        RateCounter.objects.create(
            key="old", count=1, expires_at=now - timedelta(minutes=1)
        )
        RateCounter.objects.create(
            key="live", count=1, expires_at=now + timedelta(minutes=1)
        )
        sender = User.objects.create_user("purge-sender", password="x" * 12)
        from accounts.models import Workspace

        family = Workspace.objects.create(name="Purge", primary_admin=sender)
        old = ChatMessage.objects.create(workspace=family, sender=sender, content="old")
        ChatMessage.objects.filter(pk=old.pk).update(
            created_at=now - timedelta(days=400)
        )
        fresh = ChatMessage.objects.create(
            workspace=family, sender=sender, content="new"
        )
        pending = User.objects.create_user("purge-pending", password="x" * 12)
        stale_link = EmailAddress.objects.create(
            user=pending, email="p@example.com", sent_at=now - timedelta(days=2)
        )
        used = EmailAddress.objects.create(
            user=sender, email="s@example.com", verified_at=now
        )

        out = StringIO()
        call_command("purge_expired", stdout=out)

        self.assertEqual(
            list(RateCounter.objects.values_list("key", flat=True)), ["live"]
        )
        self.assertEqual(
            list(ChatMessage.objects.values_list("pk", flat=True)), [fresh.pk]
        )
        stale_link.refresh_from_db()
        self.assertIsNone(stale_link.sent_at)
        self.assertIsNone(stale_link.verified_at)
        self.assertFalse(EmailAddress.objects.filter(pk=used.pk).exists())
        self.assertIn("Removed 1 rate counter(s), 1 chat message(s)", out.getvalue())

    def test_audit_constraints_counts_rows_that_would_fail(self):
        from accounts.models import MemberColor, Workspace

        owner = User.objects.create_user("audit-owner", password="x" * 12)
        family = Workspace.objects.create(name="Audit", primary_admin=owner)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "ALTER TABLE accounts_membercolor DROP CONSTRAINT membercolor_color_hex"
            )
        MemberColor.objects.create(workspace=family, user=owner, color="red")
        out = StringIO()
        call_command("audit_constraints", stdout=out)
        self.assertIn("accounts.MemberColor membercolor_color_hex: 1", out.getvalue())
        self.assertIn("Rows that would fail: 1", out.getvalue())
