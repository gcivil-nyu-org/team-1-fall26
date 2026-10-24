import logging

from django.test import SimpleTestCase, override_settings


@override_settings(ALLOWED_HOSTS=["syncmily.example.com"])
class DisallowedHostLoggingTests(SimpleTestCase):
    def test_bad_host_is_a_400_without_error_records_on_the_root_logger(self):
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        response = self.client.get("/accounts/login/", HTTP_HOST="203.0.113.7")

        self.assertEqual(response.status_code, 400)
        self.assertEqual([r for r in records if r.levelno >= logging.ERROR], [])

    def test_disallowed_host_logger_does_not_propagate(self):
        logger = logging.getLogger("django.security.DisallowedHost")

        self.assertFalse(logger.propagate)
        self.assertTrue(logger.handlers)
        self.assertTrue(all(type(h) is logging.NullHandler for h in logger.handlers))

    def test_other_security_loggers_still_propagate(self):
        self.assertTrue(logging.getLogger("django.security.SuspiciousFileOperation").propagate)
