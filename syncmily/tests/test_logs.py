"""The log cleaner and the security-event logger (hardening spec 10.2)."""

import logging

from django.test import SimpleTestCase

from syncmily import logs, seclog


class CleanLogTextTests(SimpleTestCase):
    def test_control_characters_are_escaped(self):
        text = logs.clean_log_text("a\r\nb\x1b[31m\x00")
        self.assertEqual(text, "a\\r\\nb\\x1b[31m\\x00")

    def test_invite_and_reset_tokens_are_hidden(self):
        invite = logs.clean_log_text("Bad Request: /accounts/invite/abc-123_x/")
        reset = logs.clean_log_text("Gone: /accounts/reset/MQ/set-password/")
        self.assertNotIn("abc-123_x", invite)
        self.assertNotIn("set-password", reset)

    def test_query_strings_and_secret_pairs_are_hidden(self):
        text = logs.clean_log_text("GET /x/?code=123456&next=/y password=hunter2")
        self.assertNotIn("123456", text)
        self.assertNotIn("hunter2", text)

    def test_request_ids_from_outside_are_checked(self):
        self.assertEqual(logs.new_request_id("abcd-1234-efgh"), "abcd-1234-efgh")
        self.assertNotEqual(logs.new_request_id("bad id\n"), "bad id\n")


class SecurityEventTests(SimpleTestCase):
    def test_event_line_has_fields_and_no_raw_newline(self):
        with self.assertLogs("syncmily.security", level="INFO") as captured:
            seclog.event("login_failed", outcome="failure", attempted="x\r\nfake")
        message = captured.records[0].getMessage()
        self.assertIn("event=login_failed", message)
        self.assertIn("outcome=failure", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)

    def test_records_are_safe_records(self):
        record = logging.getLogger("x").makeRecord("x", 20, "f", 1, "a\nb", (), None)
        self.assertNotIn("\n", record.getMessage())
