from django.test import SimpleTestCase, override_settings


class HealthCheckTests(SimpleTestCase):
    @override_settings(ALLOWED_HOSTS=["syncmily.example.com"])
    def test_health_check_answers_any_host(self):
        response = self.client.get("/healthz/", HTTP_HOST="172.31.5.10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    @override_settings(ALLOWED_HOSTS=["syncmily.example.com"])
    def test_other_paths_still_check_the_host(self):
        response = self.client.get("/accounts/login/", HTTP_HOST="172.31.5.10")

        self.assertEqual(response.status_code, 400)
