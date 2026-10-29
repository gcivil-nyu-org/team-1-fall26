from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from .. import google


@override_settings(GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="test-secret")
class GoogleOAuthTests(SimpleTestCase):
    def test_authorization_url_asks_for_calendar_scope_and_offline_access(self):
        url, state, verifier = google.authorization_url("https://example.test/cb")
        self.assertIn("accounts.google.com", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("calendar.readonly", url)
        self.assertTrue(state)
        self.assertTrue(verifier)

    def test_login_url_skips_calendar_scope(self):
        url, state, verifier = google.login_authorization_url("https://example.test/cb")
        self.assertNotIn("calendar", url)
        self.assertIn("userinfo.profile", url)
        self.assertTrue(state and verifier)

    @mock.patch(
        "calendars.google.id_token.verify_oauth2_token",
        return_value={"email": "a@example.test"},
    )
    @mock.patch("calendars.google._flow")
    def test_exchange_code_returns_credentials_and_email(self, flow_factory, verify):
        flow = flow_factory.return_value
        credentials, email = google.exchange_code(
            "https://example.test/cb", "code", "verifier"
        )
        flow.fetch_token.assert_called_once_with(code="code")
        self.assertIs(credentials, flow.credentials)
        self.assertEqual(email, "a@example.test")

    @mock.patch("calendars.google.id_token.verify_oauth2_token", return_value={})
    @mock.patch("calendars.google._flow")
    def test_exchange_code_defaults_to_empty_email(self, flow_factory, verify):
        self.assertEqual(
            google.exchange_code("https://example.test/cb", "c", "v")[1], ""
        )

    @mock.patch(
        "calendars.google.id_token.verify_oauth2_token",
        return_value={"email": "b@example.test"},
    )
    @mock.patch("calendars.google._flow")
    def test_verify_login_returns_claims(self, flow_factory, verify):
        claims = google.verify_login("https://example.test/cb", "code", "verifier")
        self.assertEqual(claims["email"], "b@example.test")


class RevokeTests(SimpleTestCase):
    @mock.patch("calendars.google.requests.post")
    def test_revoke_true_when_google_confirms(self, post):
        post.return_value.ok = True
        self.assertTrue(google.revoke("token"))

    @mock.patch("calendars.google.requests.post")
    def test_revoke_false_when_google_refuses(self, post):
        post.return_value.ok = False
        self.assertFalse(google.revoke("token"))

    @mock.patch("calendars.google.requests.post", side_effect=requests.ConnectionError)
    def test_revoke_false_on_network_error(self, post):
        self.assertFalse(google.revoke("token"))
