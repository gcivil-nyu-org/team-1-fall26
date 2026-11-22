"""
Security lane "supply": configuration, secrets and supply-chain PoCs.

Every test_poc_* asserts the SECURE behaviour, so it FAILS while the bug exists.
test_control_* tests record controls that held up (they pass).
Run:
  POSTGRES_DB=syncmily_sec_supply python manage.py test syncmily.tests.security.test_config
No network, no secrets printed: assertion messages carry counts and names only.
"""

import unittest
import os
import re
import subprocess
import sys
import tracemalloc
from pathlib import Path

import django
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase

BASE = Path(settings.BASE_DIR)
PY = sys.executable


def _read(rel):
    return (BASE / rel).read_text()


def _pins():
    """name -> version for every `name==version` line in the hashed lockfile."""
    pins = {}
    for line in _read("requirements.txt").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)", line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def _settings_in_subprocess(drop=(), add=None, expr="1"):
    """Import syncmily.settings in a fresh interpreter and print `expr` (booleans only)."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(add or {})
    env["DJANGO_SETTINGS_MODULE"] = "syncmily.settings"
    code = f"from django.conf import settings as s; print({expr})"
    out = subprocess.run(
        [PY, "-c", code], cwd=BASE, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, "settings import failed (stderr suppressed)"
    return out.stdout.strip()


# ---------------------------------------------------------------- dependencies


class DependencyPoCs(SimpleTestCase):
    def test_poc_SUPPLY_01_django_multipart_base64_memory_is_bounded(self):
        """SUPPLY-01 (CVE-2026-108147, Django < 5.2.19): a multipart FILE part sent
        with Content-Transfer-Encoding: base64 makes MultiPartParser read ahead until the
        stripped chunk length is a multiple of 4, and chunk boundaries rarely align, so
        it buffers the whole part in memory (measured 22 MiB for an 8 MiB part on 5.2.18,
        0.3 MiB on 5.2.19). Reachable pre-auth on any POST that CsrfViewMiddleware parses (the
        login URL here). Secure behaviour: peak memory stays near the chunk size."""
        payload = b"AAAA" * (2 * 1024 * 1024)  # 8 MiB of valid base64
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="f"; filename="a.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n" + payload + b"\r\n--B--\r\n"
        )
        client = Client(enforce_csrf_checks=True)
        client.get("/accounts/login/")  # anonymous visitor gets a csrftoken cookie
        tracemalloc.start()
        tracemalloc.reset_peak()
        base_now, _ = tracemalloc.get_traced_memory()
        try:
            client.post(
                "/accounts/login/",
                data=body,
                content_type="multipart/form-data; boundary=B",
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        used = peak - base_now
        # The test client copies the 8 MiB body once (FakePayload); a second full buffer
        # inside the parser pushes the peak well past 16 MiB.
        self.assertLess(
            used,
            16 * 1024 * 1024,
            f"parser peak {used // (1024 * 1024)} MiB for an 8 MiB body: part buffered in memory",
        )

    def test_poc_SUPPLY_02_django_is_on_latest_5_2_patch(self):
        """SUPPLY-02: production pins Django 5.2.18; 5.2.19 (2026-11-03) fixes 5 CVEs."""
        version = tuple(int(x) for x in _pins()["django"].split("."))
        self.assertGreaterEqual(
            version, (5, 2, 19), f"requirements.txt pins Django {_pins()['django']}"
        )
        self.assertGreaterEqual(django.VERSION[:3], (5, 2, 19))

    def test_poc_SUPPLY_03_dependabot_can_offer_django_5_2_patches(self):
        """SUPPLY-03: without an ignore rule for django semver-major, Dependabot only ever
        proposes Django 6.x (PRs #111, #166 closed) and never the 5.2.x patch."""
        cfg = _read(".github/dependabot.yml")
        self.assertTrue(
            re.search(r"ignore:[\s\S]*django[\s\S]*semver-major", cfg),
            "dependabot.yml has no ignore rule keeping Django on 5.2",
        )

    def test_poc_SUPPLY_04_no_prerelease_in_production_lockfile(self):
        """SUPPLY-04: requirements.txt pins twisted==26.10.0rc1 (final 26.10.0 exists)."""
        pre = {
            n: v
            for n, v in _pins().items()
            if re.search(r"(a|b|rc|dev)\d*$", v) and not re.fullmatch(r"[\d.]+", v)
        }
        self.assertEqual(sorted(pre), [], f"pre-release pins: {sorted(pre)}")

    def test_poc_SUPPLY_05_requirements_dev_is_hash_locked(self):
        """SUPPLY-05: CI installs requirements-dev.txt without hashes and without
        pinned transitive dependencies, so a hijacked transitive release runs in CI."""
        text = _read("requirements-dev.txt")
        self.assertIn("--hash=", text, "requirements-dev.txt carries no hashes")

    def test_control_cve_2026_113593_not_applicable(self):
        """Control: no cache_page / cache middleware, so the Vary-key CVE does not apply;
        django.contrib.gis is not installed, so the three GIS CVEs do not apply."""
        mw = " ".join(settings.MIDDLEWARE)
        self.assertNotIn("UpdateCacheMiddleware", mw)
        self.assertNotIn("FetchFromCacheMiddleware", mw)
        self.assertFalse([a for a in settings.INSTALLED_APPS if "gis" in a])
        for path in BASE.rglob("*.py"):
            if "tests" in path.parts or ".venv" in path.parts:
                continue
            self.assertNotIn("cache_page", path.read_text(errors="ignore"), str(path))


# ------------------------------------------------------------ data at rest/transit


class DataAtRestPoCs(TestCase):
    def test_poc_SUPPLY_06_totp_seed_and_backup_codes_not_plaintext(self):
        """SUPPLY-06: django-otp stores the TOTP seed (hex, varchar(80)) and the static
        backup codes (varchar(16)) as plaintext columns; a DB read (SQLi, backup, RDS
        snapshot, admin) yields working second factors. ASVS 6.5.2 / 13.3.1."""
        from accounts.two_step import _new_backup_codes
        from django_otp.plugins.otp_totp.models import TOTPDevice

        user = get_user_model().objects.create_user("seedcheck", password="x" * 14)
        device = TOTPDevice.objects.create(user=user, name="d", confirmed=True)
        codes = _new_backup_codes(user)
        with connection.cursor() as cur:
            cur.execute("select key from otp_totp_totpdevice where id=%s", [device.pk])
            raw_key = cur.fetchone()[0]
            cur.execute(
                "select token from otp_static_statictoken st "
                "join otp_static_staticdevice sd on sd.id=st.device_id where sd.user_id=%s",
                [user.pk],
            )
            raw_tokens = {r[0] for r in cur.fetchall()}
        seed_plain = raw_key == device.key and len(device.bin_key) >= 20
        leaked_codes = len(raw_tokens & set(codes))
        self.assertFalse(seed_plain, "TOTP seed is stored as plaintext hex")
        self.assertEqual(
            leaked_codes,
            0,
            f"{leaked_codes} of {len(codes)} backup codes stored plaintext",
        )

    def test_poc_SUPPLY_07_database_connection_verifies_tls(self):
        """SUPPLY-07: DATABASES has no OPTIONS, so libpq sslmode=prefer: it falls back to
        plaintext and never verifies the RDS certificate (ASVS 12.3.1/12.3.2)."""
        opts = settings.DATABASES["default"].get("OPTIONS", {})
        self.assertIn(
            opts.get("sslmode"), {"verify-full", "verify-ca"}, f"OPTIONS={sorted(opts)}"
        )
        self.assertTrue(opts.get("sslrootcert"), "no sslrootcert pinned")


class SettingsDefaultPoCs(SimpleTestCase):
    def test_poc_SUPPLY_08_https_flags_default_to_secure(self):
        """SUPPLY-08: fail-open default. If DJANGO_HTTPS is missing (a config-only EB
        redeploy, a new environment), SESSION/CSRF cookies lose Secure, HSTS and the
        SSL redirect vanish, and nothing at deploy time notices (CI runs check --deploy
        only with DJANGO_HTTPS=true set explicitly, ci.yml:89)."""
        got = _settings_in_subprocess(
            drop=("DJANGO_HTTPS",),
            add={"DJANGO_DEBUG": "false"},
            expr="[s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_SSL_REDIRECT,"
            " bool(s.SECURE_HSTS_SECONDS)]",
        )
        self.assertEqual(
            got, "[True, True, True, True]", f"flags with DJANGO_HTTPS unset: {got}"
        )

    def test_poc_SUPPLY_09_email_backend_fails_closed(self):
        """SUPPLY-09: DJANGO_EMAIL_BACKEND defaults to the console backend, which writes
        invite and password-reset links into the server log / CloudWatch. Only
        check --deploy flags it (accounts.E001) and nothing runs that on the server."""
        got = _settings_in_subprocess(
            drop=("DJANGO_EMAIL_BACKEND",),
            expr="s.EMAIL_BACKEND.rsplit('.', 2)[-2]",
        )
        self.assertNotIn(got, {"console", "dummy", "locmem", "filebased"}, got)

    def test_control_deploy_check_clean_with_https(self):
        """Control: manage.py check --deploy is clean when production variables are set."""
        env = {k: v for k, v in os.environ.items()}
        env.update(
            DJANGO_DEBUG="false",
            DJANGO_HTTPS="true",
            DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            DJANGO_DEFAULT_FROM_EMAIL="no-reply@syncmily.app",
        )
        out = subprocess.run(
            [PY, "manage.py", "check", "--deploy"],
            cwd=BASE,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn("no issues", out.stdout + out.stderr)

    def test_control_cookie_flags_under_https(self):
        """Control: with DJANGO_HTTPS=true the cookies are Secure/HttpOnly/Lax."""
        got = _settings_in_subprocess(
            add={"DJANGO_HTTPS": "true", "DJANGO_DEBUG": "false"},
            expr="[s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SESSION_COOKIE_HTTPONLY,"
            " s.SESSION_COOKIE_SAMESITE, s.CSRF_COOKIE_SAMESITE, s.SECURE_HSTS_SECONDS]",
        )
        self.assertEqual(got, "[True, True, True, 'Lax', 'Lax', 31536000]")

    def test_control_secrets_have_no_fallback(self):
        """Control: DJANGO_SECRET_KEY has no default; DEBUG defaults off; empty
        FIELD_ENCRYPTION_KEYS fails closed on write."""
        out = subprocess.run(
            [PY, "-c", "import syncmily.settings"],
            cwd=BASE,
            env={
                **{k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"},
                "DJANGO_SETTINGS_MODULE": "syncmily.settings",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("KeyError", out.stderr)
        self.assertEqual(
            _settings_in_subprocess(drop=("DJANGO_DEBUG",), expr="s.DEBUG"), "False"
        )


# ------------------------------------------------------------ deploy scripts / CI


class DeployPipelinePoCs(SimpleTestCase):
    def test_poc_SUPPLY_10_certbot_install_is_pinned_and_hashed(self):
        """SUPPLY-10: .platform/hooks/postdeploy/03_https.sh runs, as root on every new
        instance, `pip install --upgrade pip certbot` from PyPI with no version or hash.
        That root process can read /opt/elasticbeanstalk/deployment/env (all secrets).
        """
        script = _read(".platform/hooks/postdeploy/03_https.sh")
        line = next(ln for ln in script.splitlines() if "pip install" in ln)
        self.assertRegex(line, r"certbot==\d")
        self.assertIn("--require-hashes", line)

    def test_poc_SUPPLY_11_token_urls_stay_out_of_access_log(self):
        """SUPPLY-11: invite/<token>/ and reset/<uid>/<token>/ are bearer tokens in the
        URL path; the HTTPS server block logs every request with `access_log ... main`
        (EB streams it to CloudWatch, 30 days). Secure: those locations log nothing
        (or use a format without $request)."""
        script = _read(".platform/hooks/postdeploy/03_https.sh")
        block = re.search(
            r"location\s+~\s+\^/accounts/\(?[^{]*(invite|reset)[^{]*\{[^}]*access_log\s+off",
            script,
        )
        self.assertTrue(
            block, "no access_log off location for invite/reset token paths"
        )

    def test_poc_SUPPLY_12_checkout_does_not_persist_credentials(self):
        """SUPPLY-12 (zizmor artipacked x6): actions/checkout leaves the GITHUB_TOKEN in
        .git/config for every later step of the job (ci.yml:34,66,124; security.yml:26,38,52).
        """
        bad = 0
        for wf in ("ci.yml", "security.yml"):
            text = _read(f".github/workflows/{wf}")
            bad += text.count("actions/checkout@") - text.count(
                "persist-credentials: false"
            )
        self.assertEqual(bad, 0, f"{bad} checkout steps keep credentials")

    def test_poc_SUPPLY_13_deploy_job_waits_for_security_scans(self):
        """SUPPLY-13: deploy `needs: [lint, test]` only; security.yml (pip-audit, semgrep,
        gitleaks) is a separate workflow that does not gate a production deploy."""
        ci = _read(".github/workflows/ci.yml")
        needs = re.search(r"deploy:[\s\S]*?needs:\s*\[([^\]]*)\]", ci).group(1)
        self.assertRegex(needs, r"audit|security|scan", f"deploy needs: [{needs}]")

    # Open: Hriday changes the IAM trust policy first, then the deploy job gets environment:. Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_SUPPLY_14_oidc_deploy_role_not_trusted_from_develop_for_prod(self):
        """SUPPLY-14: the same role (AdministratorAccess-AWSElasticBeanstalk) is assumed
        from develop pushes, and the job picks the environment from workflow text a
        develop commit controls; secure: prod deploy only via a protected GitHub
        environment. Static check: the deploy job names an `environment:`."""
        ci = _read(".github/workflows/ci.yml")
        deploy = ci[ci.index("\n  deploy:") :]
        self.assertRegex(
            deploy, r"\n    environment:", "deploy job has no protected environment"
        )
