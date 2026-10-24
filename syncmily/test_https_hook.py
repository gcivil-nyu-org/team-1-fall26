import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

HOOK = Path(settings.BASE_DIR) / ".platform/hooks/postdeploy/03_https.sh"


class HttpsHookHostFilterTests(SimpleTestCase):
    """Runs the real hook against a temp dir (no certbot, no nginx) and checks the text it writes.

    This cannot prove nginx accepts the config: `nginx -t` is not available in CI.
    """

    def run_hook(self, domains):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "conf.d/elasticbeanstalk").mkdir(parents=True)
        primary = domains.split(",")[0]
        (root / "live" / primary).mkdir(parents=True)
        (root / "live" / primary / "fullchain.pem").touch()
        env = {
            "PATH": os.environ["PATH"],
            "SITE_DOMAINS": domains,
            "NGINX_DIR": str(root),
            "LIVE_DIR": str(root / "live"),
            "WEBROOT": str(root / "web"),
            "SKIP_CERTBOT": "1",
        }
        subprocess.run(["bash", str(HOOK)], env=env, check=True, capture_output=True)
        return (
            (root / "conf.d/syncmily_https.conf").read_text(),
            (root / "conf.d/elasticbeanstalk/03_https_redirect.conf").read_text(),
        )

    def test_https_server_drops_unknown_hosts_with_444(self):
        https, _ = self.run_hook("syncmily.app,www.syncmily.app")

        self.assertIn(r'if ($host !~* "^(syncmily\.app|www\.syncmily\.app)$") {' + "\n        return 444;", https)
        self.assertLess(https.index("return 444"), https.index("return 301"))

    def test_http_server_drops_unknown_hosts_but_not_health_or_certificate_checks(self):
        _, redirect = self.run_hook("syncmily.app,www.syncmily.app")

        self.assertIn(r'if ($request_uri ~ "^/(\.well-known/acme-challenge/|healthz/)") {', redirect)
        self.assertIn('set $reject_host "0$redirect_to_https";', redirect)
        self.assertIn('set $reject_host "1$redirect_to_https";', redirect)
        # The 444 only fires when the host is unknown AND the path is not exempt (flag still 1).
        self.assertIn('if ($reject_host = "11") {\n    return 444;', redirect)
        self.assertLess(redirect.index("return 444"), redirect.index("return 301"))
        # The exemption is computed before the 444 check reads the flag.
        self.assertLess(redirect.index("set $redirect_to_https 0;"), redirect.index("$reject_host"))

    def test_single_domain(self):
        https, _ = self.run_hook("syncmily.app")

        self.assertIn(r'"^(syncmily\.app)$"', https)
