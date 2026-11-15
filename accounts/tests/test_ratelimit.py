from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from syncmily import ratelimit


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()

    def test_client_ip_uses_right_most_forwarded_entry(self):
        request = self.rf.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 9.9.9.9")
        self.assertEqual(ratelimit.client_ip(request), "9.9.9.9")
        self.assertEqual(ratelimit.client_ip(self.rf.get("/")), "127.0.0.1")

    def test_spoofed_left_entries_share_one_bucket(self):
        for n in range(3):
            request = self.rf.post("/", HTTP_X_FORWARDED_FOR=f"6.6.6.{n}, 9.9.9.9")
            allowed = ratelimit.allow(request, "t", 2, 60, key="ip")
        self.assertFalse(allowed)

    def test_limit_then_block(self):
        request = self.rf.post("/")
        results = [ratelimit.allow(request, "t", 3, 60, key="ip") for _ in range(4)]
        self.assertEqual(results, [True, True, True, False])

    def test_decorator_returns_429_html_and_json(self):
        @ratelimit.rate_limit("d", 1, 60, key="ip")
        def view(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        self.assertEqual(view(self.rf.post("/")).status_code, 200)
        self.assertEqual(view(self.rf.post("/")).status_code, 429)
        json_response = view(self.rf.post("/", HTTP_ACCEPT="application/json"))
        self.assertEqual(json_response.status_code, 429)
        self.assertEqual(json_response["Content-Type"], "application/json")
        self.assertEqual(view(self.rf.get("/")).status_code, 200)

    @override_settings(RATELIMIT_ENABLED=False)
    def test_disabled_allows_everything(self):
        request = self.rf.post("/")
        self.assertTrue(all(ratelimit.allow(request, "t", 1, 60) for _ in range(5)))
